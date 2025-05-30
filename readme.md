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

---

## 🔧 Requirements

- Python 3

---

## 🚀 Quick Start

1. **Clone the repo**

```bash
git clone https://github.com/rcarmo/micro-mcp
```

2. **Try it out**

```bash
echo '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_movies"}, "id": 1}' | python ./introspected_movie_server.py
```

---

## 🏗️ Architecture

```
┌─────────────┐         ┌───────────────┐
│ MCP Host    │         │ MCP Server    │
│ (AI System) │◄──────► │ (myserver.py) │
└─────────────┘ stdio   └───────────────┘
                                │
                      ┌─────────┴──────────┐
                      ▼                    ▼
              ┌────────────────┐  ┌────────────────┐
              │ Protocol Layer │  │ Business Logic │
              │ (umcp.py)      │  │(tool_* methods)│
              └────────────────┘  └────────────────┘
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

## 🖥️ Using with VS Code & GitHub Copilot

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