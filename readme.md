# 🐚 MicroMCP

This is a lightweight, zero-overhead implementation of [Model Context Protocol (MCP)](https://modelcontextprotocol.io) in pure Python inspired by the original `bash`` implementation by Muthukumaran Navaneethakrishnan.

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

- **mcpserver_core.sh**: Handles JSON-RPC and MCP protocol
- **moviemcpserver.sh**: Contains business logic functions
- **assets/**: JSON configuration files

---

## 🔌 Creating Your Own MCP Server

1. **Create your business logic file (e.g., `weatherserver.sh`)**

```bash
#!/bin/bash
# Weather API implementation

# Source the core MCP server
source "$(dirname "${BASH_SOURCE[0]}")/mcpserver_core.sh"

# Access environment variables
API_KEY="${MCP_API_KEY:-default_key}"

# Weather tool implementation
tool_get_weather() {
  local args="$1"
  local location=$(echo "$args" | jq -r '.location')
  
  # Call external API
  local weather=$(curl -s "https://api.example.com/weather?location=$location&apikey=$API_KEY")
  echo "$weather"
  return 0
}

# Forecast tool implementation
tool_get_forecast() {
  local args="$1"
  local location=$(echo "$args" | jq -r '.location')
  local days=$(echo "$args" | jq -r '.days')
  
  local forecast=$(curl -s "https://api.example.com/forecast?location=$location&days=$days&apikey=$API_KEY")
  echo "$forecast"
  return 0
}

# Start the MCP server
run_mcp_server "$@"
```

2. **Create `tools_list.json` in the assets directory**

```json
{
  "tools": [
    {
      "name": "get_weather",
      "description": "Get current weather for a location",
      "parameters": {
        "type": "object",
        "properties": {
          "location": {
            "type": "string",
            "description": "City name or coordinates"
          }
        },
        "required": ["location"]
      }
    },
    {
      "name": "get_forecast",
      "description": "Get weather forecast for multiple days",
      "parameters": {
        "type": "object",
        "properties": {
          "location": {
            "type": "string",
            "description": "City name or coordinates"
          },
          "days": {
            "type": "integer",
            "description": "Number of days to forecast"
          }
        },
        "required": ["location", "days"]
      }
    }
  ]
}
```

3. **Update `mcpserverconfig.json`**

```json
{
  "protocolVersion": "0.1.0",
  "serverInfo": {
    "name": "WeatherServer",
    "version": "1.0.0"
  },
  "capabilities": {
    "tools": {
      "listChanged": true
    }
  },
  "instructions": "This server provides weather information and forecasts."
}
```

4. **Make your file executable**

```bash
chmod +x weatherserver.sh
```

---

## 🖥️ Using with VS Code & GitHub Copilot

1. **Update VS Code settings.json**

```jsonc
"mcp": {
    "servers": {
        "my-weather-server": {
            "type": "stdio",
            "command": "/path/to/your/weatherserver.sh",
            "args": [],
            "env": {
                "MCP_API_KEY": "your-api-key"
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

* No concurrency/parallel processing
* Limited memory management
* No streaming responses
* Not designed for high throughput

For AI assistants and local tool execution, these aren't blocking issues.

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

**The complete code is available at: https://github.com/muthuishere/mcp-server-bash-sdk**
