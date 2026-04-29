# 🐚 MicroMCP

A lightweight, zero-dependency implementation of the [Model Context
Protocol (MCP)](https://modelcontextprotocol.io) in pure Python --
inspired by the original `bash` implementation by Muthukumaran
Navaneethakrishnan.

**Why?** I found the idea of an MCP server written as a shell script
fascinating, and wanted to see what the same idea looked like in Python
with proper introspection -- type hints to JSON Schema, naming
conventions to MCP annotations, no decorator boilerplate, and the whole
thing readable in an afternoon.

The library is two files (`umcp.py` and `aioumcp.py`), no third-party
dependencies, supports stdio / SSE / TCP transports, and ships with a
handful of runnable examples in [`examples/`](examples/).

---

## 📑 Table of contents

* [Features](#-features)
* [Requirements](#-requirements)
* [Installation](#-installation)
* [Quick start](#-quick-start)
* [Architecture](#%EF%B8%8F-architecture)
* [Getting started tutorial](#-getting-started-tutorial)
* [Examples](#-examples)
* [Prompt templates](#-prompt-templates)
* [API reference](#-api-reference)
* [Testing](#-testing)
* [Development](#%EF%B8%8F-development)
* [Integration (VS Code, Claude Desktop)](#-integration)
* [Limitations](#-limitations)
* [Troubleshooting](#-troubleshooting)
* [Further reading](#-further-reading)
* [License](#-license)

---

## 📋 Features

- ✅ Full JSON-RPC 2.0 protocol over stdio, SSE, or TCP
- ✅ Complete MCP protocol implementation (tools, prompts, annotations)
- ✅ Dynamic tool discovery via function naming convention (`tool_*`, `prompt_*`)
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
python examples/movie_server.py --help
```

No additional packages required -- MicroMCP uses only the Python
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
* [`examples/async_movie_server.py`](examples/async_movie_server.py) --
  async version of the movie server.
* [`examples/async_calculator_server.py`](examples/async_calculator_server.py)
  -- async version of the calculator.

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

MicroMCP supports reusable prompt templates using the same naming
convention as tools: methods named `prompt_<name>` are discovered and
exposed via the MCP `prompts/list` and `prompts/get` methods. See
[`PROMPTS.md`](PROMPTS.md) for the full reference.

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

---

## 📚 API reference

### Core classes

#### `MCPServer` (`umcp.py`)

Base class for synchronous MCP servers.

* `discover_tools()` -- finds all `tool_*` methods on the subclass.
* `discover_prompts()` -- finds all `prompt_*` methods on the subclass.
* `handle_tools_call()` -- dispatches a tool call.
* `handle_prompt_get()` -- dispatches a prompt fetch.
* `get_config()` -- override to declare server name, version, capabilities.
* `get_instructions()` -- override to give the model session-level guidance.
* `run()` -- start the server on the configured transport (stdio by default; pass `--port N` for SSE, add `--tcp` for raw TCP).

#### `AsyncMCPServer` (`aioumcp.py`)

Same surface, but `tool_*` and `prompt_*` methods may be `async def`.
Use this when your tools are network-bound; use `MCPServer` when
they're local-disk or compute-bound.

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

```bash
# All tests
python -m pytest tests/

# Or with uv (recommended for a clean environment)
uv run --with pytest --with pytest-asyncio --python 3.12 \
  python -m pytest tests/

# Specific files
python -m pytest tests/test_introspection.py
python -m pytest tests/test_prompts.py
python -m pytest tests/test_async_prompts.py

# Verbose
python -m pytest tests/ -v
```

The suite covers tool/prompt discovery, JSON-RPC protocol compliance,
sync and async dispatch, schema fallback behaviour, and round-trips the
example servers as subprocesses to catch transport bugs.

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
├── examples/              -- runnable example servers
│   ├── movie_server.py
│   ├── async_movie_server.py
│   ├── calculator_server.py
│   └── async_calculator_server.py
├── tests/                 -- pytest suite
├── docs/
│   ├── ARCHITECTURE.md    -- design, transports, schema generation
│   └── CHAINING.md        -- chaining patterns for MCP server authors
├── PROMPTS.md             -- prompt template reference
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
* No built-in authentication -- the stdio transport is owned by the
  host process; SSE/TCP bind to localhost. Front with a reverse proxy
  if you need authenticated remote access.
* No concurrency in the synchronous version -- one request at a time.
  Use `AsyncMCPServer` if you need overlapping I/O.

For most AI-assistant / local-tool use cases, none of these are
blocking. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#what-umcp-deliberately-doesnt-do)
for the rationale.

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
    server.log_level = "DEBUG"
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
* [`PROMPTS.md`](PROMPTS.md) -- prompt template reference.
* [`python-office-mcp-server`][office] -- production-grade MCP server
  built on `umcp`, with 100+ tools for Word/Excel/PowerPoint editing.

---

## 📄 License

MIT -- see [`LICENSE`](LICENSE).

---

## 🙏 Acknowledgments

* Inspired by the original `bash` MCP implementation by Muthukumaran Navaneethakrishnan.
* Built against the [Model Context Protocol](https://modelcontextprotocol.io) specification.
