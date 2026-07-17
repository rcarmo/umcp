# 🐚 umcp

A lightweight, zero-dependency implementation of the [Model Context
Protocol (MCP)](https://modelcontextprotocol.io) in pure Python --
inspired by the original `bash` implementation by Muthukumaran
Navaneethakrishnan.

**Why?** I found the idea of an MCP server written as a shell script
fascinating, and wanted to see what the same idea looked like in Python
with proper introspection -- type hints to JSON Schema, naming
conventions to MCP annotations, no decorator boilerplate, and the whole
thing readable in an afternoon.

The runtime is three files: `umcp.py`, `aioumcp.py`, and the shared
`umcp_shared.py`. It has no third-party runtime dependencies, supports
stdio, stateless Streamable HTTP, legacy HTTP+SSE, and raw TCP, and ships
with a handful of runnable examples in [`examples/`](examples/).

---

## 📑 Table of contents

* [Features](#-features)
* [Requirements](#-requirements)
* [Installation](#-installation)
* [Quick start](#-quick-start)
* [Transports](#-transports)
* [Architecture](#%EF%B8%8F-architecture)
* [Getting started tutorial](#-getting-started-tutorial)
* [Examples](#-examples)
* [Prompt templates](#-prompt-templates)
* [Resources](#-resources)
* [API reference](#-api-reference)
* [Testing](#-testing)
* [Development](#%EF%B8%8F-development)
* [Integration (VS Code, Claude Desktop)](#-integration)
* [Limitations](#-limitations)
* [Deployment notes](#-deployment-notes)
* [Troubleshooting](#-troubleshooting)
* [Further reading](#-further-reading)
* [License](#-license)

---

## 📋 Features

- ✅ Full JSON-RPC 2.0 protocol over stdio, SSE, streamable HTTP, or TCP
- ✅ Complete MCP protocol implementation (tools, prompts, **resources**, annotations)
- ✅ Dynamic discovery via function naming convention (`tool_*`, `prompt_*`, `resource_*`, `resource_template_*`)
- ✅ Runtime tool/prompt/resource registration with list-changed notifications
- ✅ Stable sorted discovery with optional cursor pagination for list endpoints
- ✅ Complete introspection of function signatures, including `Literal`, `Union`, and `TypedDict`
- ✅ MCP `inputSchema` generated automatically from type hints
- ✅ Automatic `readOnlyHint` / `destructiveHint` / `openWorldHint` annotations from naming conventions
- ✅ Strict argument validation (`additionalProperties: false`, unknown-arg rejection, type coercion for stringy clients)
- ✅ Prompt templates for reusable, structured interactions
- ✅ Both synchronous and asynchronous implementations -- pick by I/O shape (local disk vs. network)
- ✅ Zero third-party dependencies

---

## 🔧 Requirements

- Python 3.10+ (both bases use PEP 604 unions and `types.UnionType`)

---

## 📦 Installation

```bash
git clone https://github.com/rcarmo/umcp
cd umcp
python -m py_compile umcp.py aioumcp.py umcp_shared.py
```

No additional packages required -- `umcp` uses only the Python
standard library.

---

## 🚀 Quick start

### Try the example server

```bash
echo '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_movies"}, "id": 1}' \
  | python ./examples/movie_server.py
```

### List available tools

```bash
echo '{"jsonrpc": "2.0", "method": "tools/list", "id": 1}' \
  | python ./examples/movie_server.py
```

### Try the calculator

```bash
echo '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "add", "arguments": {"a": 5, "b": 3}}, "id": 1}' \
  | python ./examples/calculator_server.py
```

---

## 🚚 Transports

stdio remains the default and is the right choice when the MCP host launches
the server locally. Plain `--port N` deliberately retains the old SSE
behaviour for compatibility; new network deployments should select
Streamable HTTP explicitly.

| Transport | Invocation | Intended use |
|---|---|---|
| stdio | `python server.py` | Local child-process integrations |
| Streamable HTTP | `python server.py --port 9000 --http` | New network services; stateless `POST /mcp` |
| legacy SSE | `python server.py --port 9000 --sse` | Existing HTTP+SSE clients; deprecated, with no removal date |
| raw TCP | `python server.py --port 9000 --tcp` | Legacy compatibility only |

The equivalent explicit form is `--transport stdio|streamable-http|sse|tcp`.
Network transports also accept `--host`, `--endpoint`,
`--max-request-bytes`, and repeatable `--allowed-origin` options. Conflicting
aliases and network transports without `--port` are rejected.

For compatibility with existing lightweight clients and command-line use,
the dispatcher does not keep a connection-level "initialized" flag; it will
answer methods such as `tools/list` before `initialize`. Standards-compliant
MCP clients should still perform the normal initialize handshake.

Streamable HTTP accepts one JSON-RPC object per `POST`. Requests return
`200 application/json`; notifications and client responses return `202`.
The initial `initialize` negotiates either `2025-03-26` or `2024-11-05`.
Later requests must send a supported value in `MCP-Protocol-Version`.
Request targets are matched on the URL path, so `/mcp?trace=1` is accepted
and treated the same as `/mcp`. HTTP/1.1 requires exactly one `Host` header;
HTTP/1.0 may omit it. Duplicate `Host`, `Authorization`, `Origin`, `Accept`,
`Content-Type`, `MCP-Protocol-Version`, `Content-Length`, or
`Transfer-Encoding` headers are rejected with `400`, and any
`Transfer-Encoding` is rejected. Stateless `GET` and `DELETE` return `405`.
Browser preflight is only enabled for an allowed `Origin`, and only on the
configured endpoint path.

---

## 🏗️ Architecture

```diagram
┌─────────────┐         ┌───────────────┐
│ MCP Host    │         │ MCP Server    │
│ (AI System) │◄──────► │ (myserver.py) │
└─────────────┘ stdio   └───────────────┘
                                │
                      ┌─────────┴──────────┐────────────────────┐
                      ▼                    ▼                    ▼
              ┌────────────────┐  ┌────────────────┐  ┌────────────────────┐
              │ Protocol Layer │  │ Business Logic │  │ Prompt Templates   │
              │ (umcp.py)      │  │(tool_* methods)│  │ (prompt_* methods) │
              └────────────────┘  └────────────────┘  └────────────────────┘
                      │                    │
                      ▼                    ▼
              ┌───────────────┐    ┌───────────────┐
              │ Introspection │    │ External      │
              └───────────────┘    │ Services/APIs │
                                   └───────────────┘
```

For the design details -- transports, sync vs. async rationale, schema
generation, annotation inference, what's deliberately *not* included --
see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 🎯 Getting started tutorial

### Creating your first MCP server

Create a file `my_server.py`:

```python
#!/usr/bin/env python3
from umcp import MCPServer

class MyServer(MCPServer):
    """A simple example MCP server."""

    def tool_greet(self, name: str = "World") -> str:
        """Greet someone by name.

        Args:
            name: The name to greet

        Returns:
            A friendly greeting message
        """
        return f"Hello, {name}!"

    def tool_add_numbers(self, a: float, b: float) -> float:
        """Add two numbers together.

        Args:
            a: First number
            b: Second number

        Returns:
            The sum of the two numbers
        """
        return a + b

if __name__ == "__main__":
    server = MyServer()
    server.run()
```

### Testing your server

```bash
chmod +x my_server.py

echo '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "greet", "arguments": {"name": "Alice"}}, "id": 1}' | ./my_server.py
echo '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "add_numbers", "arguments": {"a": 10, "b": 5}}, "id": 2}' | ./my_server.py
```

### Async version

Use `AsyncMCPServer` from `aioumcp` instead, and make tool methods
`async def`. Pick async when your tools talk to the network; pick sync
when they talk to the local filesystem or run subprocesses. The
[architecture doc](docs/ARCHITECTURE.md#two-implementations-one-shape)
explains why both exist.

```python
#!/usr/bin/env python3
import asyncio
from aioumcp import AsyncMCPServer

class AsyncMyServer(AsyncMCPServer):
    """An async example MCP server."""

    async def tool_fetch_data(self, url: str) -> dict:
        """Simulate fetching data from a URL."""
        await asyncio.sleep(0.1)
        return {"url": url, "status": "success", "data": "mock response"}

if __name__ == "__main__":
    server = AsyncMyServer()
    server.run()
```

---

## 🔌 Examples

The runnable examples live under [`examples/`](examples/):

* [`examples/movie_server.py`](examples/movie_server.py) -- CRUD over an
  in-memory store, parameter validation, prompt templates.
* [`examples/calculator_server.py`](examples/calculator_server.py) --
  pure compute, error handling, type-safe parameters.
* [`examples/resource_server.py`](examples/resource_server.py) --
  static and templated MCP resources, plus tools that mutate them and
  emit `notifications/resources/updated`.
* [`examples/async_movie_server.py`](examples/async_movie_server.py) --
  async version of the movie server.
* [`examples/async_calculator_server.py`](examples/async_calculator_server.py)
  -- async version of the calculator.
* [`examples/async_resource_server.py`](examples/async_resource_server.py)
  -- async version of the resource server.

```bash
# Synchronous
python examples/movie_server.py
python examples/calculator_server.py

# Asynchronous
python examples/async_movie_server.py
python examples/async_calculator_server.py
```

### Production-grade example

For a real, sizeable MCP server built on `umcp`, see
[`rcarmo/python-office-mcp-server`][office] -- a Word/Excel/PowerPoint
server with 100+ tools, structured workflow discovery, mutation
diagnostics, and the chaining patterns documented in
[`docs/CHAINING.md`](docs/CHAINING.md). It's the canonical worked
example for what a production deployment of `umcp` looks like.

[office]: https://github.com/rcarmo/python-office-mcp-server

---

## 📝 Prompt templates

`umcp` supports reusable prompt templates using the same naming
convention as tools: methods named `prompt_<name>` are discovered and
exposed via the MCP `prompts/list` and `prompts/get` methods. You can
also register prompts at runtime with `register_prompt()` /
`unregister_prompt()`, or use `register_prompt_and_notify()` /
`unregister_prompt_and_notify()` as convenience wrappers. The plain
register/unregister APIs only mutate local state; if you use them
directly, you must call `notify_prompt_list_changed()` yourself. See
[`docs/PROMPTS.md`](docs/PROMPTS.md) for the full reference.

Quick example:

```python
class MyServer(MCPServer):
    def prompt_code_review(self, filename: str, issues: int = 0) -> str:
        """Generate a focused code review instruction.
        Categories: code, review"""
        return f"Please review '{filename}'. Assume ~{issues} pre-identified issues."
```

```bash
echo '{"jsonrpc": "2.0", "method": "prompts/list", "id": 1}' | python ./examples/movie_server.py
echo '{"jsonrpc": "2.0", "method": "prompts/get", "params": {"name": "code_review", "arguments": {"filename": "main.py"}}, "id": 2}' | python ./examples/movie_server.py
```

### Completions and runtime logging

`umcp` also exposes MCP `completion/complete` and `logging/setLevel`.
`initialize` always advertises `logging: {}` and only adds
`completions: {}` when the server actually has something completable
(prompt arguments, resource-template arguments, or registered completion
providers).

Completion values can come from:

* `Literal[...]` annotations
* `Enum` annotations
* prompt `inputSchema` enums supplied to `register_prompt()`
* registered completion providers via `register_completion_provider()`

Providers receive `prefix`, `arguments`, `ref`, and `argument`, and may
return either a plain list or `{ "values": [...], "total": N,
"hasMore": bool }`. Results are prefix-filtered, deduplicated, and
capped at 100 values.

For runtime logs, call `notify_log_message()` / `log_message()` with one
of the standard MCP levels (`debug`, `info`, `notice`, `warning`,
`error`, `critical`, `alert`, `emergency`). Payloads are redacted
recursively by default for common secret-looking keys and bearer/token
strings; pass `sanitize=False` to opt out explicitly.

Progress and cancellation are request-local. If a client supplies
`params._meta.progressToken`, server code can read it with
`get_progress_token()` and emit `notifications/progress` via
`notify_progress(...)` or the server instance method of the same name.
No token means no progress notification is sent. Cancellation arrives as
`notifications/cancelled`; async handlers are cancelled actively when
possible, while sync handlers are cooperative only and should call
`is_request_cancelled()` / `raise_if_cancelled()` inside long-running
work.

---

## 📁 Resources

`umcp` implements the [MCP resources spec][mcp-resources]: methods named
`resource_<name>` are exposed as static resources, and methods named
`resource_template_<name>` become parameterised resource templates whose
signature parameters fill in the URI placeholders.

The MCP capability set is declared automatically on `initialize`:

* `tools: {"listChanged": true}`
* `prompts: {"get": true, "listChanged": true}`
* `resources: {"subscribe": true, "listChanged": true}`
* `logging: {}`
* `completions: {}` when prompt/resource-template completion is available

The following methods are wired through:

* `tools/list`, `prompts/list`, `resources/list`, and
  `resources/templates/list` return stable sorted results. When called
  without pagination params they preserve the previous one-shot behaviour.
  When called with `pageSize` (or a returned `cursor`) they return a page
  plus `nextCursor`.
* `resources/list` -- returns every `resource_*` method, plus anything
  registered via `register_resource()`.
* `resources/templates/list` -- every `resource_template_*` method, plus
  `register_resource_template()`.
* `resources/read` -- looks up by URI; static resources match by exact
  URI, templates by `{placeholder}` regex.  Returns `-32002` with the URI
  in `data` for unknown resources, per spec.
* `resources/subscribe` / `resources/unsubscribe` -- track URIs of
  interest.

Return types are normalised into MCP `contents` entries:

* `str` → text content (`text` + `mimeType`, default `text/plain`).
* `bytes` → binary content (base64-encoded `blob`, default `application/octet-stream`).
* `dict` → a single content entry, with `uri` filled in if missing.
* `list[dict]` → multiple content entries.

Attach `_mcp_resource = {...}` (or `_mcp_resource_template = {...}`) to a
resource method to override the auto-generated `uri` / `uri_template`,
set a `title`, `description`, `mime_type`, `size`, or annotations
(`audience`, `priority`, `lastModified`).

Mutating tools should call `notify_resource_updated(uri)` (only fires
for URIs the client has subscribed to) and/or
`notify_resource_list_changed()` after they change resource state.  In
the async base these helpers are coroutines (`await
self.notify_resource_updated(...)`).

Quick example:

```python
class MyServer(MCPServer):
    def resource_motd(self) -> str:
        """Message of the day."""
        return "Hello!"
    resource_motd._mcp_resource = {"mime_type": "text/plain", "title": "MOTD"}

    def resource_template_user(self, user_id: str) -> dict:
        """Synthesised user profile."""
        return {"mimeType": "application/json",
                "text": f'{{"id": "{user_id}"}}'}
```

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"resources/list"}' | python examples/resource_server.py
echo '{"jsonrpc":"2.0","id":2,"method":"resources/read","params":{"uri":"umcp://ResourceServer/motd"}}' | python examples/resource_server.py
echo '{"jsonrpc":"2.0","id":3,"method":"resources/read","params":{"uri":"file:///hello.txt"}}' | python examples/resource_server.py
```

[mcp-resources]: https://modelcontextprotocol.io/specification/2025-11-25/server/resources


---

## 📚 API reference

### Core classes

#### `MCPServer` (`umcp.py`)

Base class for synchronous MCP servers.

* `discover_tools()` -- finds all `tool_*` methods on the subclass.
* `discover_prompts()` -- finds all `prompt_*` methods on the subclass.
* `discover_resources()` / `discover_resource_templates()` -- finds all
  `resource_*` and `resource_template_*` methods on the subclass.
* `register_tool(name, callable, ...)` / `unregister_tool(name)` --
  runtime registration of tools. These are mutation-only; call
  `notify_tool_list_changed()` explicitly afterwards, or use
  `register_tool_and_notify(...)` / `unregister_tool_and_notify(...)`.
  `register_tool()` also accepts `output_schema=...`, and tool methods
  may expose `_mcp_output_schema` metadata for `tools/list`.
* `register_prompt(name, callable, ...)` / `unregister_prompt(name)` --
  runtime registration of prompts. These are mutation-only; call
  `notify_prompt_list_changed()` explicitly afterwards, or use
  `register_prompt_and_notify(...)` /
  `unregister_prompt_and_notify(...)`.
* `register_resource(uri, callable, ...)` /
  `register_resource_template(uri_template, callable, ...)` -- runtime
  registration of resources/templates.
* `notify_tool_list_changed()` / `notify_prompt_list_changed()` /
  `notify_resource_updated(uri)` / `notify_resource_list_changed()` --
  emit MCP notifications when the advertised catalogue changes. In
  `AsyncMCPServer`, these notification helpers are coroutines.
* `handle_tools_call()` -- dispatches a tool call. Mapping / structured
  tool results preserve the legacy text `content` block and also expose
  `structuredContent` when possible; advertised `outputSchema` values are
  validated on the way out.
* `handle_prompt_get()` -- dispatches a prompt fetch.
* `get_config()` -- override to declare server name, version, capabilities.
* `get_instructions()` -- override to give the model session-level guidance.
* `run()` -- start the server on the configured transport (stdio by default; plain `--port N` retains legacy SSE, `--http` selects Streamable HTTP, and `--tcp` selects raw TCP).
* `authenticate_request()` / `authorize_request()` -- optional HTTP identity and policy hooks. The default principal is anonymous for compatibility.
* Module-level `umcp.get_request_context()` -- return immutable request-local transport, protocol, principal, peer, and header metadata.

#### `AsyncMCPServer` (`aioumcp.py`)

The same MCP feature surface, with coroutine entry points named
`process_request_async()`, `run_socket_async()`, `run_sse_async()`, and
`run_streamable_http_async()`; `tool_*` and `prompt_*` methods may also be
`async def`. Use this when your tools are network-bound; use `MCPServer` when
they're local-disk or compute-bound.

### Request identity and context

Remote services can validate HTTP credentials without turning identity into
a model-controlled tool argument:

```python
from typing import Mapping
from umcp import MCPServer, MCPPrincipal, get_request_context

class PrivateServer(MCPServer):
    def authenticate_request(
        self, *, method: str, path: str,
        headers: Mapping[str, str], peer: str | None,
    ) -> MCPPrincipal | None:
        if headers.get("authorization") != "Bearer expected-token":
            return None
        return MCPPrincipal(name="alice", roles=("reader",))

    def authorize_request(
        self, principal: MCPPrincipal | None, *,
        rpc_method: str | None, tool_name: str | None,
    ) -> bool:
        return principal is not None and (
            tool_name != "write" or "writer" in principal.roles
        )

    def tool_whoami(self) -> str:
        return get_request_context().principal or "local"
```

Authentication failure is an HTTP `401`; authorization failure is `403`.
The context is reset in `finally` after every dispatch and is isolated across
threads, asyncio tasks, and executor workers. `MCPPrincipal.metadata` and
`MCPRequestContext.headers` are defensive immutable copies.

### Tool method signature

```python
def tool_<name>(self, param1: type1, param2: type2 = default) -> return_type:
    """Tool description (first line becomes the summary).

    Args:
        param1: Description of parameter 1
        param2: Description of parameter 2

    Returns:
        Description of return value
    """
```

### Prompt method signature

```python
def prompt_<name>(self, param1: type1, param2: type2 = default) -> return_type:
    """Prompt description.
    Categories: category1, category2"""
```

For schema generation rules (`Literal`, `Union`, `Optional`, `TypedDict`)
and annotation inference, see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 🧪 Testing

The project uses `pytest` and ships a `Makefile` plus a GitHub Actions
workflow that runs the suite on Python 3.10, 3.11, and 3.12.

```bash
# Make targets (preferred)
make test            # full suite
make test-fast       # -x -q
make coverage        # coverage report on stdout
make coverage-html   # writes htmlcov/index.html
make clean           # remove caches and log files

# Or pytest directly
python -m pytest tests/

# Or with uv (hermetic, no global pytest required)
uv run --with pytest --with pytest-asyncio --python 3.12 \
  python -m pytest tests/

# Specific files / verbose
python -m pytest tests/test_resources.py -v
```

### What the suite covers

| File | Area |
|---|---|
| `test_introspection.py` | end-to-end tool discovery via subprocess |
| `test_movieserver.py` | worked-example end-to-end test |
| `test_protocol_errors.py` | JSON-RPC error paths -- parse/version/method/args/raise |
| `test_schema_generation.py` | type hint -> JSON Schema mapping (primitives, `Optional`, `Union`, `Literal`, `TypedDict`, `list`/`dict`) |
| `test_schema_fallbacks.py` | exotic-type schema fallback behaviour |
| `test_annotations.py` | `readOnlyHint` / `destructiveHint` / `openWorldHint` inference + overrides |
| `test_coercion.py` | stringy-client argument coercion (str -> int / float / bool) |
| `test_prompts.py` / `test_async_prompts.py` | prompt discovery and dispatch |
| `test_resources.py` | resources/list, /templates/list, /read, subscribe/unsubscribe, dynamic registration, capability declaration |
| `test_notifications.py` | `notifications/resources/list_changed` and `notifications/resources/updated` emission and subscription gating |
| `test_async_servers.py` | async stdio subprocess round-trips |
| `test_transports.py` | sync stdio, TCP, legacy SSE, and Streamable HTTP subprocess tests |
| `test_shared_negotiation_context.py` | protocol negotiation and immutable request/principal context |
| `test_streamable_http_sync_async.py` | matching sync/async Streamable HTTP dispatch |
| `test_streamable_http_regressions.py` | auth, Origin, CORS, limits, context isolation, CLI, and remote-safe errors |
| `simple_async_test.py` | smoke tests for the async base |

The suite covers both bases and runs on Python 3.10, 3.11, and 3.12.

---

## 🛠️ Development

### Setup

```bash
git clone https://github.com/rcarmo/umcp
cd umcp
python -m pytest tests/
```

### Code style

* Explicit imports only.
* Functional style; short, single-responsibility functions.
* Type hints on all parameters and returns.
* Double quotes for strings; triple-double-quote docstrings.
* `snake_case` method naming.
* f-strings only when needed.
* Logging over print statements.

### Contributing

1. Fork the repository.
2. Create a feature branch: `git checkout -b feature-name`.
3. Make your changes following the code style.
4. Add tests for new functionality.
5. Ensure all tests pass: `python -m pytest tests/`.
6. Submit a pull request.

### Project structure

```
umcp/
├── umcp.py                -- sync MCPServer base class
├── aioumcp.py             -- async AsyncMCPServer base class
├── umcp_shared.py         -- versions, principals, and request context
├── scripts/               -- optional compatibility smoke tests
├── examples/              -- runnable example servers
│   ├── movie_server.py
│   ├── async_movie_server.py
│   ├── calculator_server.py
│   ├── async_calculator_server.py
│   ├── resource_server.py
│   └── async_resource_server.py
├── tests/                 -- pytest suite
├── docs/
│   ├── ARCHITECTURE.md    -- design, transports, schema generation
│   ├── CHAINING.md        -- chaining patterns for MCP server authors
│   └── PROMPTS.md         -- prompt template reference
├── readme.md              -- this file
└── LICENSE
```

---

## 🔧 Integration

### VS Code & GitHub Copilot

```jsonc
"mcp": {
    "servers": {
        "my-weather-server": {
            "type": "stdio",
            "command": "/path/to/your/server.py",
            "args": [],
            "env": {
                "MCP_API_KEY": "anything_you_need"
            }
        }
    }
}
```

Then `/mcp my-weather-server get weather for New York` from Copilot Chat.

### Claude Desktop

```json
{
  "mcpServers": {
    "my-server": {
      "command": "python",
      "args": ["/path/to/your/server.py"],
      "env": {}
    }
  }
}
```

---

## 🚫 Limitations

* No streaming responses -- partial results aren't supported.
* No built-in authentication backend or policy engine. Streamable HTTP
  exposes `authenticate_request()` and `authorize_request()` hooks, and
  reverse proxies can supply trusted identity, but the application owns
  credentials, roles, and policy.
* No concurrency in the synchronous version -- one request at a time.
  Use `AsyncMCPServer` if you need overlapping I/O.

For most AI-assistant / local-tool use cases, none of these are
blocking. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#what-umcp-deliberately-doesnt-do)
for the rationale.

---

## 🚢 Deployment notes

For local hosts (Claude Desktop, VS Code, Copilot, Piclaw, etc.), prefer
**stdio**. `umcp` now tags stdio/file requests as local request contexts,
keeps request metadata immutable, and avoids exposing internal exception
strings over remote transports.

If you need network access, prefer **streamable HTTP** over legacy SSE/TCP:

* bind to loopback unless you have a real reason not to;
* implement `authenticate_request()` and `authorize_request()` for remote use;
  hook exceptions are logged server-side and returned as generic `500`s;
* require a supported `MCP-Protocol-Version` on non-`initialize` requests;
* set `--allowed-origin` explicitly for browser clients, or rely on loopback-only
  Origin handling for local web UIs;
* expect `OPTIONS` preflight only when the browser sends a valid `Origin`.

A typical reverse-proxy setup should preserve `Host`, `Authorization`,
`Origin`, `Accept`, `Content-Type`, and `MCP-Protocol-Version` headers unchanged,
and should not inject duplicates or `Transfer-Encoding`.
If the proxy terminates TLS or auth, keep the upstream `umcp` listener on
localhost.

Minimal `systemd` service sketch:

```ini
[Unit]
Description=umcp HTTP server
After=network.target

[Service]
ExecStart=/usr/bin/python /srv/umcp/server.py --port 9000 --http --host 127.0.0.1 --allowed-origin https://ui.example
WorkingDirectory=/srv/umcp
Restart=on-failure
User=umcp
Group=umcp

[Install]
WantedBy=multi-user.target
```

Container deployments should follow the same pattern: keep the container
port private, expose it through a reverse proxy, and do not publish raw
SSE/TCP unless you have a separate auth story. Session IDs, if stateful
HTTP support is added later, are routing identifiers rather than credentials.

### Piclaw compatibility smoke test

Piclaw's `pi-mcp-adapter` uses the official MCP SDK's
`StreamableHTTPClientTransport` before considering its legacy SSE fallback.
The repository includes a repeatable smoke test for that exact transport:

```bash
# Terminal 1
python examples/calculator_server.py --port 9000 --http

# Terminal 2 -- point this at Piclaw's bundled SDK directory
MCP_URL=http://127.0.0.1:9000/mcp \
MCP_SDK_ROOT=/path/to/piclaw/node_modules/@modelcontextprotocol/sdk \
  bun scripts/piclaw_streamable_http_smoke.ts
```

A successful run connects without SSE, discovers the calculator tools, and
calls `add(2, 3)`. This is an optional compatibility check; Bun and the MCP
SDK are not runtime dependencies of `umcp`.

---

## ❓ Troubleshooting

**Server doesn't respond to JSON-RPC requests.** Check the JSON is
valid and the server is running. Try a `tools/list` request first --
it has no arguments and exercises the protocol path.

**Tools not showing up in `tools/list`.** Ensure the methods are
named `tool_*` and have proper type hints. Check `mcpserver.log` next
to the script for introspection errors.

**Async server seems slow.** The async examples use `asyncio.sleep()`
to simulate I/O. Remove these in real applications.

**Permission denied on the script.** `chmod +x your_server.py`.

**Debug logging.**

```python
if __name__ == "__main__":
    server = MyServer()
    server.logger.setLevel("DEBUG")
    server.run()
```

---

## 📚 Further reading

* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) -- design notes:
  transports, sync vs. async rationale, schema generation, annotation
  inference, what's deliberately not included.
* [`docs/CHAINING.md`](docs/CHAINING.md) -- how language models
  actually chain MCP tool calls in practice, with
  [`python-office-mcp-server`][office] as the worked example. Read
  this before building anything non-trivial; it's where the real
  reliability work lives.
* [`docs/PROMPTS.md`](docs/PROMPTS.md) -- prompt template reference.
* [`python-office-mcp-server`][office] -- production-grade MCP server
  built on `umcp`, with 100+ tools for Word/Excel/PowerPoint editing.

---

## 📄 License

MIT -- see [`LICENSE`](LICENSE).

---

## 🙏 Acknowledgments

* Inspired by the original `bash` MCP implementation by Muthukumaran Navaneethakrishnan.
* Built against the [Model Context Protocol](https://modelcontextprotocol.io) specification.
