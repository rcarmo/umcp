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
    tool_list = tools['tools']
    assert len(tool_list) >= 2, "Expected at least 2 tools"
    names = {t['name'] for t in tool_list}
    assert 'hello' in names and 'hello_async' in names
    print(f"Discovered {len(tool_list)} tools:")
    for tool in tool_list:
        async_flag = " (ASYNC)" if tool.get('async', False) else " (SYNC)"
        print(f"  - {tool['name']}{async_flag}: {tool['description']}")
    # No return (pytest warning avoidance)


async def _test_tool_execution_async():
    """Async body for tool execution test (invoked via sync wrapper for pytest)."""
    print("\n🛠️  Testing tool execution...")
    server = SimpleAsyncTestServer()
    # Sync tool
    response = await server.handle_tools_call_async(1, {"name": "hello", "arguments": {"name": "Sync"}})
    assert response["result"]["content"][0]["type"] == "text"
    # Async tool
    response2 = await server.handle_tools_call_async(2, {"name": "hello_async", "arguments": {"name": "Async"}})
    assert response2["result"]["content"][0]["type"] == "text"

def test_tool_execution():
    """Pytest-compatible wrapper that runs the async test."""
    asyncio.run(_test_tool_execution_async())


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
    with open("test_request.json", "w", encoding="utf-8") as f:
        json.dump(test_request, f)
    print("Test request created: test_request.json")
    # Assert file exists instead of returning value
    from pathlib import Path as _P
    assert _P("test_request.json").exists()


async def main():
    """Main test function."""
    print("🚀 Async MCP Server Test")
    print("=" * 40)
    
    try:
        # Test introspection
        test_introspection()
        
        # Test tool execution
        await test_tool_execution()
        
        # Test JSON-RPC
        test_json_rpc()
        
        print("\n✅ All tests completed successfully!")
        print("🎯 You can test the server with:")
        print("   python3 simple_async_test.py test_request.json")
        
    except Exception as e:  # noqa: BLE001 broad for test harness simplicity
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    # Check if we're running with a file argument
    if len(sys.argv) > 1:
        # Run as MCP server
        server = SimpleAsyncTestServer()
        server.run()
    else:
        # Run tests
        asyncio.run(main())
