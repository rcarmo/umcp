# Architecture

`umcp` is a small, dependency-free implementation of the [Model Context
Protocol][mcp] in Python. The whole library is two files -- `umcp.py`
for synchronous servers, `aioumcp.py` for asynchronous ones -- and the
shape of both is the same: a base class that introspects subclass
methods, generates JSON Schema from their type hints, and serves a
JSON-RPC 2.0 protocol over one of three transports.

This document covers what's in there and why. For chaining patterns and
recommendations for how MCP servers should *behave* once they're built,
see [`CHAINING.md`](CHAINING.md). For prompt templates specifically,
see [`PROMPTS.md`](PROMPTS.md).

[mcp]: https://modelcontextprotocol.io

## Two implementations, one shape

`umcp.py` and `aioumcp.py` are kept in deliberate parity. They expose
the same base-class API (`MCPServer` / `AsyncMCPServer`), the same
discovery rules, the same schema generation, the same annotation
inference, the same strictness model, the same transports. The only
difference is whether `tool_*` and `prompt_*` methods are sync or
`async def`. Tests in `tests/` exercise both paths the same way; if a
behaviour changes in one, it changes in the other.

The reason both exist is straightforward: they're for different I/O
shapes, not different feature levels.

`umcp.py` is for tools that talk to the local filesystem, run local
processes, or do CPU work. Office document editing, image processing,
shelling out to a system tool, parsing a file -- all of this is
synchronous from the kernel's perspective and gains nothing (often
loses clarity) when forced through `async`/`await`. The MCP protocol is
inherently single-request-at-a-time over stdio anyway, so a synchronous
server is the natural fit for the bulk of real-world tools.

`aioumcp.py` is for tools that talk to the network. HTTP calls,
streaming responses from upstream services, multiple in-flight requests
to a remote API, websockets, anything where blocking the event loop
would mean blocking the whole server. Network I/O is where `asyncio`
actually pays for itself, and where the SSE transport (which is itself
HTTP and benefits from non-blocking sockets) belongs.

The fork-and-keep-in-sync cost is real but contained: the two files are
deliberately structured to make diffs easy to read, and the test suite
catches drift. The alternative -- a single async base with sync wrappers
-- adds an event loop to every tool that doesn't need one and makes
local-disk tools harder to reason about. Two files, kept tidy, beat one
file with a coloured-function problem.

A rough rule for which to subclass: if your tools call `open()`,
`subprocess.run()`, or anything in `pathlib`/`os`, use `MCPServer`. If
they call `httpx.AsyncClient`, `aiohttp`, or hit any remote service that
might be slow, use `AsyncMCPServer`. Mixed servers are fine, but pick
the base that matches the dominant I/O shape.

## Transports

Both implementations support three transports, selectable on the
command line. The protocol payload is identical across them; only the
framing differs.

**stdio** (default). One JSON-RPC message per line on stdin, one
response per line on stdout. The MCP host launches the server as a
subprocess and pipes JSON in and out. This is what Claude Desktop, VS
Code's MCP support, and most other clients use by default. No port, no
network, no auth -- the host owns the process. On Windows, `umcp`
explicitly switches stdin/stdout/stderr to binary mode and disables
buffering before any I/O happens, because the default text-mode CRLF
translation on Windows will corrupt JSON-RPC framing in subtle ways.

**SSE** (`--port N`). HTTP server bound to `127.0.0.1:N` implementing
the MCP Server-Sent Events transport: `GET /sse` opens the event
stream and emits an `endpoint` event with the per-session POST URL;
`POST /message?session_id=...` accepts JSON-RPC requests for that
session. Useful when the client lives in a different process from the
server, when you want multiple clients sharing one server, or when the
client is a web app that can't spawn subprocesses. Bound to localhost
by default because there's no auth model -- if you need to expose it,
front it with a reverse proxy that handles authentication.

**TCP** (`--port N --tcp`). Raw TCP socket with newline-delimited
JSON-RPC. Effectively stdio over a socket. Kept for compatibility with
older clients that predate SSE; new deployments should prefer SSE.

The transport layer is small and lives at the bottom of each base
class. The protocol handler doesn't know which transport is in use --
it gets a JSON-RPC request and produces a JSON-RPC response, and the
transport is responsible for delivering it.

## Tool discovery via introspection

The discovery rule is one line: any method on the subclass whose name
starts with `tool_` is a tool. The MCP-visible name is everything after
the prefix. So `tool_get_movies` exposes a tool called `get_movies`.

For each tool, `discover_tools()` does the following:

1. Reads the docstring -- the first line becomes the tool description.
2. Reads the type hints via `inspect.signature` and `get_type_hints`.
3. Builds a JSON Schema `inputSchema` from the parameters: required
   parameters become required schema keys; defaulted parameters
   become optional with their default value carried through.
4. Sets `additionalProperties: false` on the generated schema so
   unknown arguments are rejected at validation time, not silently
   ignored.
5. Infers MCP annotations from the tool's name (see below).
6. Returns the lot as the tool definition the client will see in
   `tools/list`.

The naming-convention discovery has two practical consequences. First,
adding a tool is `def tool_whatever(...)` and nothing else -- no
registration boilerplate, no decorator, no separate manifest. Second,
the schema you ship to clients is *derived* from the function
signature, so it can't drift from the implementation. Renaming a
parameter renames it in the schema; changing a type changes the schema
type. There is no second source of truth.

## Type-hint to JSON Schema

The schema generator handles the standard cases:

* Primitives: `str`, `int`, `float`, `bool`, `None`.
* Containers: `list[T]`, `dict[str, T]`, `tuple[T, ...]`.
* Optionals: `T | None` and `Optional[T]` both work; the resulting
  schema marks the parameter as not required and carries the inner
  type. PEP 604 `T | None` syntax is preferred (and is the reason for
  the Python 3.10 minimum -- `types.UnionType` only landed in 3.10).
* Unions: `str | int` becomes a JSON Schema `type: ["string",
  "integer"]`. Useful for tools that genuinely accept multiple shapes;
  best avoided when a single type would do.
* `Literal[...]`: becomes a JSON Schema `enum`. This is the trick that
  makes `mode="dry_run" | "best_effort" | "safe" | "strict"` show up
  to the model as a closed set rather than a free-text string.
* `TypedDict`: becomes a typed object schema with the right
  `properties`, `required`, and -- in keeping with the rest of the
  library -- `additionalProperties: false`. `total=False` TypedDicts
  produce schemas with no `required` keys. This lets you declare
  structured argument shapes (the office server uses this for
  `PatchChange`) and have them schema-checked end to end.
* Default values are carried into the schema, which gives clients
  something useful to display and gives the model a hint about the
  expected shape.

Anything the generator can't classify falls back to an empty
`{}` schema, which clients treat as "any". That's a deliberate
soft-fail -- the alternative would be a hard error at server startup
for tool authors who use exotic types, and the hard error tends to be
more annoying than the loose schema.

## Tool annotations from naming conventions

The MCP spec defines optional `annotations` on tools that hint at
behaviour: `readOnlyHint`, `destructiveHint`, `openWorldHint`. `umcp`
infers these from the tool name so most tool authors get them for free.

Method names starting with `get_`, `list_`, `read_`, `inspect_`,
`describe_`, `search_`, `find_` are treated as read-only.
`delete_`, `clear_`, `cleanup_`, `restart_` are treated as
destructive. `web_*` and `azure_*` are treated as open-world (they
talk to a network the client can't reason about). The full lists are
near the top of `_infer_tool_annotations` in both files; extending
them is a matter of adding a prefix.

If a tool needs different annotations from what the prefix implies,
attach a `_mcp_annotations` dict to the method directly and the
inferrer will use it instead. Most don't.

The reason this lives in the library rather than in tool authors'
hands is that planners use these annotations as *scheduling* hints --
read-only calls can run in parallel, destructive ones serialise behind
confirmations, open-world ones get isolated. Lying in annotations is
the most common cause of agents misbehaving in unobvious ways. Naming
discipline turns into safety metadata, and the library does the work.

## Strict argument validation and type coercion

When a `tools/call` request arrives, the server validates the supplied
arguments against the generated schema before dispatching. Three
behaviours are worth calling out.

*Unknown arguments are rejected.* `additionalProperties: false` on
every schema means a model that invents a `force=True` parameter gets
a clear error back rather than having the parameter silently dropped.
This sounds unfriendly until you've watched a model write a confident
summary of an action that didn't happen because the server ignored
the argument that was supposed to control it.

*Missing required arguments are rejected.* Required is determined by
"no default value in the function signature", which is the obvious
reading.

*Type coercion happens for stringy clients.* Some MCP clients send all
arguments as strings (especially when the user typed them into a chat
input). `_coerce_value` will turn `"42"` into `42` for an `int`
parameter, `"true"` into `True` for a `bool`, and so on -- but only
for unambiguous cases. If coercion fails, the original value is
preserved so the validator can produce a clean error rather than a
confusing one. The goal is to make the common stringy-client case
work without making the strict-validation guarantee meaningless.

The combination -- strict on the schema, lenient on obvious type
fixes -- is the right balance for a library where the consumer is a
language model rather than a careful caller.

## Prompt templates

Prompts are the same idea as tools, with a different prefix. Methods
named `prompt_<name>` are discovered and registered as MCP prompts.
The signature becomes the prompt's `inputSchema`; the docstring
becomes the description; an optional `Categories: a, b, c` line in
the docstring is parsed out and surfaced as the prompt's `categories`
field.

`prompts/list` and `prompts/get` are the two relevant MCP methods,
both implemented. The return value of a prompt method can be a string
(returned as a single user message), a list (returned as a sequence of
messages), or a dict (returned verbatim). See [`PROMPTS.md`](PROMPTS.md)
for the full prompt API and examples.

## Resources

Resources are the third discovery surface, alongside tools and prompts.
The naming convention is `resource_<name>` for static resources and
`resource_template_<name>` for parameterised ones; the parameter list of
the templated method becomes the URI placeholders. The library handles
all five `resources/*` JSON-RPC methods (`list`, `templates/list`,
`read`, `subscribe`, `unsubscribe`) plus the two notifications
(`notifications/resources/list_changed` and
`notifications/resources/updated`), and declares the `resources`
capability with `subscribe` and `listChanged` set to `true` on
`initialize`.

The URI is auto-generated as `umcp://<ServerClassName>/<name>` (or
`umcp://<ServerClassName>/<name>/{p1}/{p2}` for templates), but a
resource method can override that -- and the rest of its metadata --
by attaching a `_mcp_resource` (or `_mcp_resource_template`) dict
attribute. Supported keys are `uri` / `uri_template`, `name`, `title`,
`description`, `mime_type`, `size`, and `annotations` (which carries
`audience`, `priority`, `lastModified` per the spec).

Return-type normalisation matters because the MCP `contents` schema is
strict. The library accepts:

* `str` -- becomes a single text content entry; `mimeType` defaults to
  `text/plain`.
* `bytes` (or `bytearray` / `memoryview`) -- becomes a binary content
  entry with `blob` base64-encoded; `mimeType` defaults to
  `application/octet-stream`.
* `dict` -- treated as a single content entry; the resource URI is
  filled in automatically if missing.
* `list[dict]` -- multiple content entries (the MCP spec permits a
  single resource to expand to several).

URI template matching is deliberately simple: each `{name}` placeholder
compiles to a named regex group that matches one path segment (no `/`).
This covers the common case without dragging in a full RFC 6570
implementation, and the captured groups are passed to the resource
method as keyword arguments.

For runtime registration, `register_resource(uri, callable, ...)` and
`register_resource_template(uri_template, callable, ...)` add resources
that aren't methods on the subclass. Useful when the resource set is
data-driven (config files, items in a database) rather than known at
import time.

Notifications are best-effort and transport-aware:
`notify_resource_list_changed()` fires unconditionally;
`notify_resource_updated(uri)` only fires when at least one client has
subscribed to that URI (the spec's intent -- there's no point in
broadcasting changes nobody asked for). The async base exposes both as
coroutines (`await self.notify_resource_updated(...)`); the sync base
as plain methods. Both write SSE messages to every connected session
when SSE is the active transport, and fall back to a single
newline-delimited message on stdout for stdio / TCP / file mode.

Unknown URIs return JSON-RPC error `-32002` with the URI in the `data`
field, matching the spec's error-handling guidance.

## Server-level configuration and instructions

Two methods are intended for subclasses to override.

`get_config()` returns a dict that's merged into the
`initialize` response. Useful for declaring server name, version,
capabilities, or anything else the client should know up front.

`get_instructions()` returns a string that the MCP host surfaces to
the model as system-level guidance about how to use this server. This
is where the *core-first* guidance, recovery hints, and chaining
advice live -- see `CHAINING.md` for what to put in there. The
string is sent once per session, so it's worth making it count.

Logging is set up automatically: every server writes to
`mcpserver.log` next to the script, at INFO by default. Set
`self.log_level = "DEBUG"` before `run()` to get more detail. Logging
goes to a file rather than stderr because stderr is sometimes
multiplexed with the protocol stream by clients that don't separate
them carefully.

## What `umcp` deliberately doesn't do

A few things you might expect that aren't here, with reasons.

*No streaming responses.* The MCP spec defines `notifications/progress`
and partial results, but the bulk of real-world tools either return
quickly or don't have meaningful intermediate state. Adding streaming
would complicate every tool author's contract for the benefit of a
small minority. A future addition, if the use case becomes common.

*No authentication.* The stdio transport is owned by the host process,
which is the security boundary. The SSE and TCP transports bind to
localhost. If you need authenticated remote access, put a reverse
proxy in front of SSE; the library doesn't try to be one.

*No concurrency in the synchronous version.* `MCPServer` handles one
request at a time. Given that MCP over stdio is inherently
single-request-per-message and the typical sync use case is local
file/process work where concurrency would just add lock complexity,
this is the right default. Use `AsyncMCPServer` if you genuinely need
to overlap I/O.

*No persistent state between calls.* The library doesn't help you
manage state -- if your tools need shared state, that's instance
attributes on your subclass, and you own the lifetime. The library
gives you a class; what you put in it is your problem.

*No third-party dependencies.* The library uses only the Python
standard library. This keeps install painless (`git clone` and run),
makes the dependency surface auditable in a single afternoon, and
means tools built on `umcp` inherit nothing they didn't ask for. The
HTTP/SSE transport is implemented by hand on top of `asyncio.start_server`
rather than pulling in `aiohttp` or `starlette` for the same reason.

## Repository layout

```
umcp/
  umcp.py                    -- sync MCPServer base class
  aioumcp.py                 -- async AsyncMCPServer base class

  examples/                  -- runnable example servers
    movie_server.py            -- CRUD over an in-memory store
    async_movie_server.py      -- async version of the same
    calculator_server.py       -- pure compute, type-safe params
    async_calculator_server.py -- async version of the same
    resource_server.py         -- static + templated MCP resources
    async_resource_server.py   -- async version of the resource server

  tests/                     -- pytest suite covering both bases
    test_introspection.py    -- discovery, schema generation, annotations
    test_prompts.py          -- prompt discovery and dispatch (sync)
    test_async_prompts.py    -- prompt discovery and dispatch (async)
    test_async_servers.py    -- end-to-end async transport tests
    test_movieserver.py      -- worked-example end-to-end test
    test_resources.py        -- resources/* coverage for both bases
    test_schema_fallbacks.py -- exotic-type schema fallback behaviour

  docs/
    ARCHITECTURE.md          -- this file
    CHAINING.md              -- patterns for chaining MCP tool calls
    PROMPTS.md               -- prompt template documentation

  readme.md                  -- quick-start and API reference
  LICENSE                    -- MIT
```

The directory structure is flat on purpose. The whole library is two
files; the rest is examples, tests, and documentation.

## Testing notes

The test suite is split into focused modules so failures point at one
layer of the library at a time:

* **Discovery / introspection** -- `test_introspection.py`,
  `test_movieserver.py`, `test_schema_generation.py`, `test_schema_fallbacks.py`,
  `test_annotations.py`. These exercise the `tool_*` discovery rules,
  the type-hint to JSON Schema mapping, and the annotation inferrer.
* **Wire protocol** -- `test_protocol_errors.py`, `test_coercion.py`.
  These cover the JSON-RPC error paths (parse / version / method /
  args / tool-raise) and the lenient stringy-client coercion.
* **Prompts** -- `test_prompts.py`, `test_async_prompts.py`.
* **Resources** -- `test_resources.py` (list/templates/read/subscribe/
  unsubscribe / capability declaration / dynamic registration) and
  `test_notifications.py` (which monkey-patches the binary stdout
  buffer to assert that `notifications/resources/list_changed` and
  `notifications/resources/updated` actually go out, and that
  subscription gating works).
* **Transports** -- `test_async_servers.py` (async stdio over
  subprocess), `test_transports.py` (sync stdio, sync TCP, sync SSE
  end-to-end).

End-to-end suites spawn the example servers as subprocesses and
round-trip JSON-RPC requests; this catches transport and framing bugs
the unit tests can't see (Windows CRLF translation, blocking-stdout
buffering, SSE event framing).

A `Makefile` wraps the common operations:

```
make test            # full suite
make test-fast       # -x -q for the inner loop
make coverage        # coverage report on stdout
make coverage-html   # writes htmlcov/index.html
```

The recommended hermetic runner:

```
uv run --with pytest --with pytest-asyncio --python 3.12 \
  python -m pytest tests/
```

A GitHub Actions workflow (`.github/workflows/tests.yml`) runs the same
suite on Python 3.10, 3.11, and 3.12 for every push and pull request,
and emits a coverage report on 3.12.

The library targets Python 3.10 as a minimum (for `types.UnionType`)
but the suite passes on 3.10 / 3.11 / 3.12 in CI. There's nothing
version-specific beyond the union-syntax requirement.

## Extending the library

The bar for adding to the base class is high. The library is
deliberately small and the appeal is that you can read it in an
afternoon. Most things tool authors want belong in the *subclass* (or
in a mixin layered on top of it), not in `MCPServer` itself.

Two things that have earned their place over time, and the kind of
thing the bar lets through:

* The annotation inferrer. It's small, depends only on the tool name,
  and produces metadata that planners genuinely use. The alternative
  -- making every tool author add `@readonly` decorators -- adds work
  to every consumer of the library for the same outcome.
* The TypedDict schema generator. Without it, structured argument
  shapes had to be flattened into individual parameters, which made
  the wire schema ugly and the docstrings confusing. With it,
  `PatchChange(TypedDict)` shows up to the model as a typed nested
  object.

Things that have *not* earned their place include automatic retry,
caching of tool results, and pluggable validators. All of those are
tool-author concerns dressed up as library concerns. If the use case
is generic enough to belong in the library, it'll keep coming up;
otherwise it lives in the subclass where it belongs.
