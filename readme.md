# 🐚 MicroMCP

This is a lightweight, zero-overhead implementation of [Model Context Protocol (MCP)](https://modelcontextprotocol.io) in pure Python inspired by the original `bash` implementation by Muthukumaran Navaneethakrishnan.

**Why?** I found the idea of using the simplest possible implementation of MCP in a shell script fascinating, but I wanted to see how it would look in Python with true introspection capabilities.

---

## 📋 Features

* ✅ Full JSON-RPC 2.0 protocol over stdio
* ✅ Complete MCP protocol implementation
* ✅ Dynamic tool discovery via function naming convention
* ✅ Complete introspection of function signatures
* ✅ Easy to extend with custom tools
* ✅ Prompt templates for reusable, structured interactions

---

## 🔧 Requirements

* Python 3

---

## 🚀 Quick Start

1. **Clone the repo**

```bash
git clone https://github.com/rcarmo/umcp
```

2. **Try it out**

```bash
echo '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_movies"}, "id": 1}' | python ./movie_server.py
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

## 🔌 Examples

This implementation includes two simple example servers that demonstrate how to use the MCP protocol, one for a movie booking system and another for a calculator.

Both are supplied in synchronous and asynchronous versions, showcasing how to implement tools and introspection.

---

## 📝 Prompt Templates (prompts/list & prompts/get)

In addition to tool discovery, the servers expose reusable prompt templates using a simple naming convention:

* Any method named `prompt_<name>` is treated as a prompt definition.
* The method docstring becomes the prompt description (first line as summary, rest as detail).
* Its signature is introspected and converted into a JSON Schema input definition (same mechanism as tools).
* Optional categories can be embedded in the docstring to help clients filter prompts.

### Categories Syntax

You can declare categories in the docstring using any of the following forms (case–insensitive):

```text
Category: review
Categories: code, quality
[category: explanation]
[categories: summarization, docs]
```

All collected category tokens are lower‑cased, de‑duplicated, and returned as a list.

### Return Value Semantics
When the host calls `prompts/get` with arguments, the underlying `prompt_<name>` method is invoked. Its return value is normalized into a `messages` array as follows:

| Return Type | Interpretation |
|-------------|---------------|
| `str` | Wrapped as a single user message (`{"role": "user", "content": {"type": "text", "text": ...}}`) |
| `list` of message dicts | Used as-is if each item has `role` and `content` |
| `dict` containing a `messages` list | That list is used directly |
| Any other object | JSON-serialized into a single user message |

If the host omits `arguments`, only the prompt description (and categories) are returned—no invocation occurs. This keeps lightweight discovery cheap.

### Example: Defining Prompts

```python
class MyServer(MCPServer):
        def prompt_code_review(self, filename: str, issues: int = 0) -> str:
                """Generate a focused code review instruction.\nCategories: code, review"""
                return (
                        f"Please review '{filename}'. Assume ~{issues} pre-identified issues. "
                        "List key problems and actionable improvements concisely." 
                )

        def prompt_summary(self, topic: str, bullets: int = 5):
                """Return a structured summarization dialogue.\n[categories: summary, documentation]"""
                return [
                        {"role": "system", "content": {"type": "text", "text": "You are a precise technical summarizer."}},
                        {"role": "user", "content": {"type": "text", "text": f"Summarize '{topic}' in {bullets} bullet points."}},
                ]
```

### Listing Prompts

```json
{"jsonrpc": "2.0", "id": 1, "method": "prompts/list"}
```

Example (truncated) response:

```json
{
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "prompts": [
            {
                "name": "code_review",
                "description": "Generate a focused code review instruction.",
                "inputSchema": {"type": "object", "properties": {"filename": {"type": "string"}, "issues": {"type": "integer"}}, "required": ["filename"]},
                "categories": ["code", "review"]
            }
        ]
    }
}
```

### Getting (Invoking) a Prompt

```json
{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "prompts/get",
    "params": {"name": "code_review", "arguments": {"filename": "main.py", "issues": 3}}
}
```

Example response payload (messages shortened):

```json
{
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
        "description": "Generate a focused code review instruction.",
        "categories": ["code", "review"],
        "messages": [
            {"role": "user", "content": {"type": "text", "text": "Please review 'main.py'..."}}
        ]
    }
}
```

### Async Prompt Support

In the async server (`AsyncMCPServer`), prompt methods may themselves be `async def`. The dispatcher awaits them transparently; mixed sync/async prompt definitions are supported.

### Testing & Examples

See:
* `tests/test_prompts.py` – sync prompt behaviors
* `tests/test_async_prompts.py` – async + mixed return forms

These illustrate categories, description-only retrieval, and multi-message templates.

### Design Rationale
Prompts mirror tools for discoverability but return message scaffolds instead of “tool results”. This enables clients (e.g., Copilot Chat) to:
* Offer pre-built structured prompt options
* Dynamically parameterize prompt templates via introspected schemas
* Reduce boilerplate in higher-level orchestrators

---

---

## �️ Using with VS Code & GitHub Copilot

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

---

## 🚫 Limitations

* No concurrency/parallel processing in synchronous version
* No streaming responses
* Not designed for high throughput

For AI assistants and local tool execution, these aren't blocking issues.

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
