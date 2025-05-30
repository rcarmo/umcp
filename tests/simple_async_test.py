#!/usr/bin/env python3
"""
simple_async_test.py - Simple test of the async MCP server
"""

import asyncio
import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from aioumcp import AsyncMCPServer


class SimpleAsyncTestServer(AsyncMCPServer):
    """Simple test server for async functionality."""
    
    def get_instructions(self) -> str:
        return "Simple async test server"
    
    def tool_hello(self, name: str = "World") -> dict:
        """Say hello (synchronous)."""
        return {"message": f"Hello, {name}!", "async": False}
    
    async def tool_hello_async(self, name: str = "World") -> dict:
        """Say hello asynchronously."""
        await asyncio.sleep(0.1)  # Simulate async work
        return {"message": f"Hello async, {name}!", "async": True}


def test_introspection():
    """Test tool discovery."""
    print("🔍 Testing tool introspection...")
    server = SimpleAsyncTestServer()
    tools = server.discover_tools()
    
    print(f"Discovered {len(tools['tools'])} tools:")
    for tool in tools['tools']:
        async_flag = " (ASYNC)" if tool.get('async', False) else " (SYNC)"
        print(f"  - {tool['name']}{async_flag}: {tool['description']}")
    
    return tools


async def test_tool_execution():
    """Test actual tool execution."""
    print("\n🛠️  Testing tool execution...")
    server = SimpleAsyncTestServer()
    
    # Test sync tool
    print("Testing sync tool...")
    try:
        response = await server.handle_tools_call_async(1, {
            "name": "hello", 
            "arguments": {"name": "Sync"}
        })
        print(f"Sync response: {response}")
    except Exception as e:
        print(f"Sync tool error: {e}")
    
    # Test async tool
    print("Testing async tool...")
    try:
        response = await server.handle_tools_call_async(2, {
            "name": "hello_async", 
            "arguments": {"name": "Async"}
        })
        print(f"Async response: {response}")
    except Exception as e:
        print(f"Async tool error: {e}")


def test_json_rpc():
    """Test JSON-RPC request processing."""
    print("\n📡 Testing JSON-RPC processing...")
    
    # Create a simple request file for testing
    test_request = {
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1
    }
    
    # Write to temp file
    with open("test_request.json", "w") as f:
        json.dump(test_request, f)
    
    print("Test request created: test_request.json")
    return "test_request.json"


async def main():
    """Main test function."""
    print("🚀 Async MCP Server Test")
    print("=" * 40)
    
    try:
        # Test introspection
        tools = test_introspection()
        
        # Test tool execution
        await test_tool_execution()
        
        # Test JSON-RPC
        test_file = test_json_rpc()
        
        print(f"\n✅ All tests completed successfully!")
        print(f"🎯 You can test the server with:")
        print(f"   python3 simple_async_test.py {test_file}")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    # Check if we're running with a file argument
    if len(sys.argv) > 1:
        # Run as MCP server
        server = SimpleAsyncTestServer()
        server.run_mcp_server()
    else:
        # Run tests
        asyncio.run(main())
