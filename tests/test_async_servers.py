#!/usr/bin/env python3
"""
test_async_servers.py - Test script for async MCP servers
Demonstrates the performance benefits of async operations
"""

import asyncio
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from time import time


def send_request(server_script: str, request: dict) -> dict:
    """Send a JSON-RPC request to a server and return the response."""
    request_json = json.dumps(request)
    
    try:
        result = subprocess.run(
            ["python", server_script],
            input=request_json,
            text=True,
            capture_output=True,
            timeout=30
        )
        
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
        else:
            return {"error": f"Server error: {result.stderr}"}
    except subprocess.TimeoutExpired:
        return {"error": "Request timed out"}
    except json.JSONDecodeError as e:
        return {"error": f"JSON decode error: {e}"}
    except Exception as e:
        return {"error": f"Request failed: {e}"}


async def test_async_performance():
    """Test async server performance with concurrent operations."""
    print("🚀 Testing Async MCP Server Performance\n")
    
    # Test 1: Tools discovery
    print("1️⃣ Testing tools discovery...")
    tools_request = {
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1
    }
    
    response = send_request("async_calculator_server.py", tools_request)
    if "result" in response:
        tools = response["result"]["tools"]
        print(f"   ✅ Discovered {len(tools)} tools")
        for tool in tools:
            async_flag = " (ASYNC)" if tool.get("async", False) else " (SYNC)"
            print(f"      - {tool['name']}{async_flag}: {tool['description']}")
    else:
        print(f"   ❌ Error: {response}")
    
    print()
    
    # Test 2: Mixed sync/async operations
    print("2️⃣ Testing mixed sync/async calculator operations...")
    
    calc_requests = [
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "add", "arguments": {"a": 10, "b": 5}}, "id": 2},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "multiply_async", "arguments": {"a": 8, "b": 7}}, "id": 3},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "power_async", "arguments": {"base": 2, "exponent": 10}}, "id": 4},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "factorial_async", "arguments": {"n": 10}}, "id": 5}
    ]
    
    start_time = time()
    
    # Test concurrent execution (simulate what async server can do)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(send_request, "async_calculator_server.py", req)
            for req in calc_requests
        ]
        
        responses = [future.result() for future in futures]
    
    end_time = time()
    
    print(f"   ⏱️  Total time for 4 operations: {end_time - start_time:.2f}s")
    
    for i, response in enumerate(responses):
        if "result" in response:
            content = json.loads(response["result"]["content"][0]["text"])
            operation = content.get("operation", "unknown")
            result = content.get("result", "N/A")
            is_async = content.get("async", False)
            async_flag = " (ASYNC)" if is_async else " (SYNC)"
            print(f"      {operation}{async_flag}: {result}")
        else:
            print(f"      Error in request {i+1}: {response}")
    
    print()
    
    # Test 3: Movie server async operations
    print("3️⃣ Testing async movie server operations...")
    
    # Test movie search (concurrent API calls)
    search_request = {
        "jsonrpc": "2.0", 
        "method": "tools/call", 
        "params": {"name": "search_movies_async", "arguments": {"query": "Matrix"}}, 
        "id": 6
    }
    
    print("   🔍 Searching for movies (with concurrent API calls)...")
    start_time = time()
    response = send_request("async_movie_server.py", search_request)
    end_time = time()
    
    if "result" in response:
        content = json.loads(response["result"]["content"][0]["text"])
        if "error" not in content:
            print(f"   ⏱️  Search completed in {end_time - start_time:.2f}s")
            print(f"      Found {content['total_results']} total results")
            print(f"      Local matches: {len(content['local_matches'])}")
            print(f"      External matches: {len(content['external_matches'])}")
            print(f"      Recommendations: {len(content['recommendations'])}")
        else:
            print(f"   ❌ Search error: {content['error']}")
    else:
        print(f"   ❌ Request error: {response}")
    
    print()
    
    # Test 4: Booking workflow (multiple async operations)
    print("4️⃣ Testing async booking workflow...")
    
    booking_request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "book_ticket_async",
            "arguments": {
                "movie_id": 1,
                "show_time": "20:30",
                "num_tickets": 2,
                "customer_email": "test@example.com"
            }
        },
        "id": 7
    }
    
    print("   🎫 Booking tickets (seat check + payment + confirmation)...")
    start_time = time()
    response = send_request("async_movie_server.py", booking_request)
    end_time = time()
    
    if "result" in response:
        content = json.loads(response["result"]["content"][0]["text"])
        if "error" not in content:
            print(f"   ⏱️  Booking completed in {end_time - start_time:.2f}s")
            print(f"      Booking ID: {content['booking_id']}")
            print(f"      Movie: {content['movie_title']}")
            print(f"      Total: ${content['total_price']}")
            
            # Test booking retrieval
            booking_id = content['booking_id']
            retrieval_request = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "get_booking_async", "arguments": {"booking_id": booking_id}},
                "id": 8
            }
            
            print("   📋 Retrieving booking details...")
            response = send_request("async_movie_server.py", retrieval_request)
            if "result" in response:
                content = json.loads(response["result"]["content"][0]["text"])
                if "error" not in content:
                    print(f"      ✅ Booking retrieved successfully")
                    print(f"      Status: {content['status']}")
                else:
                    print(f"      ❌ Retrieval error: {content['error']}")
            else:
                print(f"      ❌ Request error: {response}")
        else:
            print(f"   ❌ Booking error: {content['error']}")
    else:
        print(f"   ❌ Request error: {response}")
    
    print()
    print("🎉 Async MCP Server testing completed!")


def test_introspection():
    """Test the introspection capabilities of async servers."""
    print("🔍 Testing Async Server Introspection\n")
    
    servers = [
        ("async_calculator_server.py", "Calculator"),
        ("async_movie_server.py", "Movie")
    ]
    
    for server_file, server_name in servers:
        print(f"📊 {server_name} Server Tools:")
        
        tools_request = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1
        }
        
        response = send_request(server_file, tools_request)
        if "result" in response:
            tools = response["result"]["tools"]
            
            sync_tools = [t for t in tools if not t.get("async", False)]
            async_tools = [t for t in tools if t.get("async", False)]
            
            print(f"   Total tools: {len(tools)}")
            print(f"   Synchronous: {len(sync_tools)}")
            print(f"   Asynchronous: {len(async_tools)}")
            print()
            
            for tool in tools:
                async_flag = "🔄 ASYNC" if tool.get("async", False) else "⚡ SYNC"
                print(f"   {async_flag} {tool['name']}")
                print(f"      {tool['description']}")
                if 'inputSchema' in tool:
                    required = tool['inputSchema'].get('required', [])
                    properties = tool['inputSchema'].get('properties', {})
                    params = [f"{name}({props.get('type', 'any')})" for name, props in properties.items()]
                    required_params = [p for p in params if any(p.startswith(r) for r in required)]
                    optional_params = [p for p in params if not any(p.startswith(r) for r in required)]
                    
                    if required_params:
                        print(f"      Required: {', '.join(required_params)}")
                    if optional_params:
                        print(f"      Optional: {', '.join(optional_params)}")
                print()
        else:
            print(f"   ❌ Error getting tools: {response}")
        
        print()


if __name__ == "__main__":
    print("🧪 Async MCP Server Test Suite")
    print("=" * 50)
    print()
    
    # Test introspection first
    test_introspection()
    
    print("=" * 50)
    print()
    
    # Test performance
    asyncio.run(test_async_performance())
