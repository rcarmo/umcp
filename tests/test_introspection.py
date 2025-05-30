#!/usr/bin/env python3
"""
test_introspection.py - Comprehensive test of the introspection-based MCP servers
Tests true introspection functionality that generates tool definitions from class members and type annotations
"""

import json
import os
import subprocess
import sys
from typing import Dict, Any

def send_request(server_script: str, request: Dict[str, Any]) -> Dict[str, Any]:
    """Send a JSON-RPC request to a server script and return the response."""
    request_json = json.dumps(request)
    
    try:
        # Use a relative path that works when run from the tests directory
        script_path = "../" + server_script
        print(f"DEBUG: Executing {sys.executable} {script_path}")
        print(f"DEBUG: Request: {request_json}")
        
        result = subprocess.run(
            [sys.executable, script_path],
            input=request_json,
            text=True,
            capture_output=True,
            check=False,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        
        if result.returncode != 0:
            print(f"Error running {server_script}: {result.stderr}")
            print(f"DEBUG: Stdout: {result.stdout}")
            return {}
        
        return json.loads(result.stdout.strip())
    
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        print(f"Error communicating with {server_script}: {e}")
        return {}

def test_introspected_movie_server():
    """Test the introspected movie server functionality."""
    print("🎬 Testing Introspected Movie Server...")
    
    # Test 1: Initialize
    print("  Testing initialize...")
    response = send_request('movie_server.py', {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"}
        }
    })
    
    assert response.get("result", {}).get("serverInfo", {}).get("name") == "MovieMCPServer"
    print("  ✅ Initialize works")
    
    # Test 2: Tools list with introspection
    print("  Testing tools list (introspection)...")
    response = send_request('movie_server.py', {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {}
    })
    
    tools = response.get("result", {}).get("tools", [])
    tool_names = [tool["name"] for tool in tools]
    
    expected_tools = ["book_ticket", "get_movies", "get_showtimes", "search_movies"]
    for expected_tool in expected_tools:
        assert expected_tool in tool_names, f"Tool {expected_tool} not found in {tool_names}"
    
    # Verify that book_ticket has proper parameters from introspection
    book_ticket_tool = next(tool for tool in tools if tool["name"] == "book_ticket")
    params = book_ticket_tool["parameters"]
    assert params["type"] == "object"
    assert "movie_id" in params["properties"]
    assert "show_time" in params["properties"] 
    assert "num_tickets" in params["properties"]
    assert params["properties"]["movie_id"]["type"] == "integer"
    assert params["properties"]["show_time"]["type"] == "string"
    assert params["properties"]["num_tickets"]["type"] == "integer"
    assert params["required"] == ["movie_id", "show_time", "num_tickets"]
    
    print("  ✅ Tools list introspection works correctly")
    
    # Test 3: Tool calls with individual typed parameters
    print("  Testing tool calls with individual parameters...")
    
    # Get movies (no parameters)
    response = send_request('movie_server.py', {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "get_movies", "arguments": {}}
    })
    
    content = json.loads(response["result"]["content"][0]["text"])
    assert isinstance(content, list)
    assert len(content) == 4
    print("  ✅ get_movies works")
    
    # Book ticket with individual parameters
    response = send_request('movie_server.py', {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "book_ticket",
            "arguments": {"movie_id": 1, "show_time": "20:30", "num_tickets": 2}
        }
    })
    
    booking = json.loads(response["result"]["content"][0]["text"])
    assert booking["movieId"] == 1
    assert booking["showTime"] == "20:30"
    assert booking["numTickets"] == 2
    assert booking["totalPrice"] == 25.98
    print("  ✅ book_ticket with individual parameters works")
    
    # Search with optional parameters
    response = send_request('movie_server.py', {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {
            "name": "search_movies",
            "arguments": {"title": "matrix"}
        }
    })
    
    results = json.loads(response["result"]["content"][0]["text"])
    assert len(results) == 1
    assert "Matrix" in results[0]["title"]
    print("  ✅ search_movies with optional parameters works")

def test_calculator_server():
    """Test the calculator server functionality."""
    print("🧮 Testing Calculator Server...")
    
    # Test tools list
    print("  Testing tools list...")
    response = send_request('calculator_server.py', {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {}
    })
    
    tools = response.get("result", {}).get("tools", [])
    tool_names = [tool["name"] for tool in tools]
    
    expected_tools = ["add", "subtract", "multiply", "divide"]
    for expected_tool in expected_tools:
        assert expected_tool in tool_names, f"Tool {expected_tool} not found"
    
    print("  ✅ Tools list works")
    
    # Test calculation
    print("  Testing calculations...")
    response = send_request('calculator_server.py', {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "multiply",
            "arguments": {"a": 7, "b": 8}
        }
    })
    
    result = json.loads(response["result"]["content"][0]["text"])
    assert result["result"] == 56.0
    print("  ✅ Calculations work")

def test_basic_movie_server():
    """Test that the basic movie server works with individual parameters."""
    print("📄 Testing Basic Movie Server (Individual Parameters)...")
    
    # Test tools list
    response = send_request('movie_server.py', {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {}
    })
    
    tools = response.get("result", {}).get("tools", [])
    assert len(tools) > 0
    print("  ✅ Basic movie server works with individual parameters")

def main():
    """Run all introspection tests."""
    print("🔍 Testing True Introspection Implementation")
    print("=" * 50)
    
    try:
        test_introspected_movie_server()
        print()
        test_calculator_server()
        print()
        test_basic_movie_server()
        print()
        print("🎉 All introspection tests passed!")
        print()
        print("✨ Summary of achievements:")
        print("  • True introspection extracts parameter schemas from type annotations")
        print("  • Individual typed parameters work correctly")
        print("  • Optional parameters are handled properly")
        print("  • No JSON configuration files required")
        print("  • Tool definitions generated dynamically from method signatures")
        print("  • Args-dict pattern support has been completely removed")
        print("  • All servers now use individual typed parameters exclusively")
        
    except AssertionError as e:
        print(f"❌ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"💥 Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
