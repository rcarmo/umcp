# 🐚 MicroMCP

A lightweight, zero-overhead implementation of [Model Context Protocol (MCP)](https://modelcontextprotocol.io) in pure Python inspired by the original `bash` implementation by Muthukumaran Navaneethakrishnan.

**Why?** I found the idea of using the simplest possible implementation of MCP in a shell script fascinating, but I wanted to see how it would look in Python with true introspection capabilities.

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

## 📚 Further reading

* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) -- design, transports, sync vs. async, schema generation, what's deliberately *not* included.
* [`docs/CHAINING.md`](docs/CHAINING.md) -- how language models actually chain MCP tool calls in practice, with `python-office-mcp-server` as the worked example.
* [`PROMPTS.md`](PROMPTS.md) -- prompt template reference and examples.

---

## 🔧 Requirements

- Python 3.10+ (both servers use PEP 604 unions and `types.UnionType`)

---

## 📦 Installation

1. **Clone the repository**

```bash
git clone https://github.com/rcarmo/umcp
cd umcp
```

2. **Verify installation**

```bash
python movie_server.py --help
```

No additional packages required - MicroMCP uses only the Python standard library!

---

## 🚀 Quick Start

### 1. Try the Example Server

```bash
echo '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_movies"}, "id": 1}' | python ./movie_server.py
```

### 2. List Available Tools

```bash
echo '{"jsonrpc": "2.0", "method": "tools/list", "id": 1}' | python ./movie_server.py
```

### 3. Try the Calculator

```bash
echo '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "add", "arguments": {"a": 5, "b": 3}}, "id": 1}' | python ./calculator_server.py
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

---

## 🎯 Getting Started Tutorial

### Creating Your First MCP Server

Create a file `my_server.py`:

```python
#!/usr/bin/env python3
from umcp import MCPServer
from typing import Dict, Any, Optional

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

### Testing Your Server

```bash
# Make it executable
chmod +x my_server.py

# Test the greet tool
echo '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "greet", "arguments": {"name": "Alice"}}, "id": 1}' | ./my_server.py

# Test the add tool
echo '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "add_numbers", "arguments": {"a": 10, "b": 5}}, "id": 2}' | ./my_server.py
```

### Async Version

Create `async_server.py`:

```python
#!/usr/bin/env python3
import asyncio
from aioumcp import AsyncMCPServer

class AsyncMyServer(AsyncMCPServer):
    """An async example MCP server."""

    async def tool_fetch_data(self, url: str) -> Dict[str, Any]:
        """Simulate fetching data from a URL.

        Args:
            url: The URL to fetch from

        Returns:
            Mock data response
        """
        await asyncio.sleep(0.1)  # Simulate network delay
        return {"url": url, "status": "success", "data": "mock response"}

if __name__ == "__main__":
    server = AsyncMyServer()
    server.run()
```

---

## 🔌 Examples

This implementation includes two example servers that demonstrate how to use the MCP protocol:

### Movie Booking Server (`movie_server.py`)

- Demonstrates CRUD operations
- Shows parameter validation
- Includes prompt templates for movie-related tasks

### Calculator Server (`calculator_server.py`)

- Simple mathematical operations
- Error handling for edge cases
- Type-safe parameter handling

Both are supplied in synchronous and asynchronous versions, showcasing how to implement tools and introspection.

### Running the Examples

```bash
# Synchronous versions
python movie_server.py
python calculator_server.py

# Asynchronous versions
python async_movie_server.py
python async_calculator_server.py
```

---

## 📝 Prompt Templates

MicroMCP supports reusable prompt templates using a simple naming convention. See [PROMPTS.md](PROMPTS.md) for detailed documentation.

### Quick Overview

- Any method named `prompt_<name>` is treated as a prompt definition
- The method docstring becomes the prompt description
- Function signature is introspected for JSON Schema input definition
- Optional categories can be embedded in the docstring

### Simple Example

```python
class MyServer(MCPServer):
    def prompt_code_review(self, filename: str, issues: int = 0) -> str:
        """Generate a focused code review instruction.\nCategories: code, review"""
        return f"Please review '{filename}'. Assume ~{issues} pre-identified issues."
```

### Testing Prompts

```bash
# List available prompts
echo '{"jsonrpc": "2.0", "method": "prompts/list", "id": 1}' | python ./movie_server.py

# Get a specific prompt
echo '{"jsonrpc": "2.0", "method": "prompts/get", "params": {"name": "code_review", "arguments": {"filename": "main.py"}}, "id": 2}' | python ./movie_server.py
```

---

## 📚 API Reference

### Core Classes

#### `MCPServer`

Base class for synchronous MCP servers.

**Key Methods:**

- `discover_tools()` - Automatically finds all `tool_*` methods
- `discover_prompts()` - Automatically finds all `prompt_*` methods
- `handle_tools_call()` - Dispatches tool execution
- `handle_prompt_get()` - Handles prompt template retrieval

#### `AsyncMCPServer`

Base class for asynchronous MCP servers.

**Key Methods:**

- `discover_tools()` - Automatically finds all `tool_*` methods
- `discover_prompts()` - Automatically finds all `prompt_*` methods
- `handle_tools_call()` - Dispatches tool execution (async)
- `handle_prompt_get()` - Handles prompt template retrieval (async)

### Tool Method Signature

```python
def tool_<name>(self, param1: type1, param2: type2 = default) -> return_type:
    """Tool description (first line becomes summary).

    Args:
        param1: Description of parameter 1
        param2: Description of parameter 2

    Returns:
        Description of return value
    """
    # Implementation
```

### Prompt Method Signature

```python
def prompt_<name>(self, param1: type1, param2: type2 = default) -> return_type:
    """Prompt description.\nCategories: category1, category2"""
    # Implementation returning str, list, or dict
```

---

## 🧪 Testing

### Running Tests

```bash
# Run all tests
python -m pytest tests/

# Run specific test files
python -m pytest tests/test_introspection.py
python -m pytest tests/test_prompts.py
python -m pytest tests/test_async_prompts.py

# Run with verbose output
python -m pytest tests/ -v
```

### Test Coverage

The test suite covers:

- **Introspection**: Tool and prompt discovery
- **Protocol Compliance**: JSON-RPC 2.0 implementation
- **Synchronous Operations**: Tool execution and prompt handling
- **Asynchronous Operations**: Async tool execution and prompt handling
- **Error Handling**: Proper error responses and logging
- **Performance**: Async vs sync comparisons

### Writing Your Own Tests

```python
import subprocess
import json

def test_my_server():
    # Test tool discovery
    result = subprocess.run([
        'echo', '{"jsonrpc": "2.0", "method": "tools/list", "id": 1}'
    ], capture_output=True, text=True)

    response = json.loads(result.stdout)
    assert 'result' in response
    assert 'tools' in response['result']
```

---

## 🛠️ Development

### Setting Up Development Environment

```bash
# Clone the repository
git clone https://github.com/rcarmo/umcp
cd umcp

# Create a virtual environment (optional but recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Run tests to verify everything works
python -m pytest tests/
```

### Code Style

This project follows these conventions:

- Explicit imports only
- Functional programming style
- Short, single-responsibility functions
- Type hints for all parameters/returns
- Double quotes for strings
- Triple-double quote docstrings
- `snake_case` method naming
- f-strings only when needed
- Logging over print statements

### Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature-name`
3. Make your changes following the code style
4. Add tests for new functionality
5. Ensure all tests pass: `python -m pytest tests/`
6. Submit a pull request

### Project Structure

```
umcp/
├── README.md              # This file
├── LICENSE                # MIT License
├── .github/
│   └── copilot-instructions.md  # AI assistant guidelines
├── umcp.py                # Synchronous MCP server base class
├── aioumcp.py             # Asynchronous MCP server base class
├── movie_server.py        # Example movie booking server
├── async_movie_server.py  # Async version of movie server
├── calculator_server.py   # Example calculator server
├── async_calculator_server.py  # Async version of calculator
├── tests/                 # Test suite
│   ├── test_introspection.py
│   ├── test_prompts.py
│   ├── test_async_prompts.py
│   └── ...
└── PROMPTS.md             # Detailed prompt documentation
```

---

## 🔧 Integration

### VS Code & GitHub Copilot

1. **Update VS Code settings.json**

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

2. **Use with GitHub Copilot Chat**

```
/mcp my-weather-server get weather for New York
```

### Claude Desktop

Add to your Claude Desktop configuration:

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

- No concurrency/parallel processing in synchronous version
- No streaming responses
- Not designed for high throughput

For AI assistants and local tool execution, these aren't blocking issues.

---

## ❓ Troubleshooting

### Common Issues

**Q: Server doesn't respond to JSON-RPC requests**
A: Check that your JSON is valid and that the server is running properly. Try testing with a simple tools/list request first.

**Q: Tools not showing up in tools/list**
A: Ensure your tool methods are named `tool_*` and have proper type hints. Check the server logs for any introspection errors.

**Q: Async server seems slow**
A: The async examples use `asyncio.sleep()` to simulate I/O operations. In real applications, remove these delays.

**Q: Permission denied on server script**
A: Make the script executable: `chmod +x your_server.py`

### Debug Mode

Enable debug logging by setting the log level:

```python
if __name__ == "__main__":
    server = MyServer()
    server.log_level = "DEBUG"
    server.run()
```

### Getting Help

- Check the [test files](tests/) for working examples
- Review the [prompt documentation](PROMPTS.md) for template guidance
- Open an issue on GitHub for bugs or feature requests

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- Inspired by the original `bash` MCP implementation by Muthukumaran Navaneethakrishnan
- Built on the [Model Context Protocol](https://modelcontextprotocol.io) specification
- Thanks to all contributors and the MCP community
