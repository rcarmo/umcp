#!/usr/bin/env python3
"""
async_demo.py - Live demonstration of async MCP server capabilities
"""

import json
import subprocess
from time import time
from typing import Tuple


def send_request(server_script: str, request: dict) -> Tuple[dict, float]:
    """Send a JSON-RPC request and measure response time."""
    request_json = json.dumps(request)
    
    start_time = time()
    try:
        result = subprocess.run(
            ["python3", server_script],
            input=request_json,
            text=True,
            capture_output=True,
            timeout=10
        )
        end_time = time()
        
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip()), end_time - start_time
        else:
            return {"error": f"Server error: {result.stderr}"}, end_time - start_time
    except Exception as e:
        end_time = time()
        return {"error": f"Request failed: {e}"}, end_time - start_time


def demo_introspection():
    """Demonstrate tool discovery and introspection."""
    print("🔍 ASYNC MCP SERVER DEMONSTRATION")
    print("=" * 50)
    print()
    
    print("1️⃣ TOOL DISCOVERY & INTROSPECTION")
    print("-" * 30)
    
    # Test async calculator
    request = {"jsonrpc": "2.0", "method": "tools/list", "id": 1}
    response, timing = send_request("async_calculator_server.py", request)
    
    if "result" in response:
        tools = response["result"]["tools"]
        sync_count = sum(1 for t in tools if not t.get("async", False))
        async_count = sum(1 for t in tools if t.get("async", False))
        
        print(f"📊 Calculator Server: {len(tools)} tools discovered")
        print(f"   ⚡ Synchronous: {sync_count}")
        print(f"   🔄 Asynchronous: {async_count}")
        print()
        
        for tool in tools[:3]:  # Show first 3 tools
            async_flag = "🔄 ASYNC" if tool.get("async", False) else "⚡ SYNC"
            print(f"   {async_flag} {tool['name']}: {tool['description'][:50]}...")
    else:
        print(f"❌ Error: {response}")
    
    print()


def demo_performance():
    """Demonstrate performance differences."""
    print("2️⃣ PERFORMANCE DEMONSTRATION")
    print("-" * 30)
    
    # Test sync operation
    print("⚡ Testing SYNC operation (addition)...")
    sync_request = {
        "jsonrpc": "2.0", 
        "method": "tools/call", 
        "params": {"name": "add", "arguments": {"a": 123, "b": 456}}, 
        "id": 1
    }
    
    response, timing = send_request("async_calculator_server.py", sync_request)
    if "result" in response:
        content = json.loads(response["result"]["content"][0]["text"])
        print(f"   Result: {content['result']} (⏱️ {timing:.3f}s)")
    
    # Test async operation
    print("🔄 Testing ASYNC operation (power calculation)...")
    async_request = {
        "jsonrpc": "2.0", 
        "method": "tools/call", 
        "params": {"name": "power_async", "arguments": {"base": 2, "exponent": 16}}, 
        "id": 2
    }
    
    response, timing = send_request("async_calculator_server.py", async_request)
    if "result" in response:
        content = json.loads(response["result"]["content"][0]["text"])
        print(f"   Result: {content['result']} (⏱️ {timing:.3f}s - includes async delay)")
    
    print()


def demo_concurrent_operations():
    """Demonstrate concurrent execution capabilities."""
    print("3️⃣ CONCURRENT OPERATIONS DEMO")
    print("-" * 30)
    
    print("🎬 Testing movie search with concurrent API calls...")
    
    search_request = {
        "jsonrpc": "2.0", 
        "method": "tools/call", 
        "params": {"name": "search_movies_async", "arguments": {"query": "Avengers"}}, 
        "id": 3
    }
    
    start_time = time()
    response, timing = send_request("async_movie_server.py", search_request)
    
    if "result" in response:
        content = json.loads(response["result"]["content"][0]["text"])
        if "error" not in content:
            print(f"   ✅ Search completed in ⏱️ {timing:.3f}s")
            print(f"   📊 Results:")
            print(f"      • Local matches: {len(content['local_matches'])}")
            print(f"      • External API results: {len(content['external_matches'])}")
            print(f"      • Recommendations: {len(content['recommendations'])}")
            print(f"      • Total results: {content['total_results']}")
            print()
            print("   💡 This demonstrates concurrent execution of:")
            print("      - Local database search")
            print("      - External API calls")
            print("      - Recommendation engine")
            print("      All running simultaneously!")
        else:
            print(f"   ❌ Search error: {content['error']}")
    else:
        print(f"   ❌ Request error: {response}")
    
    print()


def demo_complex_workflow():
    """Demonstrate complex async workflow."""
    print("4️⃣ COMPLEX ASYNC WORKFLOW")
    print("-" * 30)
    
    print("🎫 Testing ticket booking workflow...")
    print("   (Simulates: seat check → payment → confirmation → email)")
    
    booking_request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "book_ticket_async",
            "arguments": {
                "movie_id": 1,
                "show_time": "20:30",
                "num_tickets": 2,
                "customer_email": "demo@example.com"
            }
        },
        "id": 4
    }
    
    start_time = time()
    response, timing = send_request("async_movie_server.py", booking_request)
    
    if "result" in response:
        content = json.loads(response["result"]["content"][0]["text"])
        if "error" not in content:
            print(f"   ✅ Booking completed in ⏱️ {timing:.3f}s")
            print(f"   📋 Booking Details:")
            print(f"      • Booking ID: {content['booking_id']}")
            print(f"      • Movie: {content['movie_title']}")
            print(f"      • Show time: {content['show_time']}")
            print(f"      • Tickets: {content['num_tickets']}")
            print(f"      • Total: ${content['total_price']}")
            print()
            print("   🔄 This workflow included async operations for:")
            print("      - Seat availability check (0.4s)")
            print("      - Payment processing (0.6s)")
            print("      - Database save (0.2s)")
            print("      - Email confirmation (0.3s)")
        else:
            print(f"   ❌ Booking error: {content['error']}")
    else:
        print(f"   ❌ Request error: {response}")
    
    print()


def demo_comparison():
    """Show the key advantages."""
    print("5️⃣ KEY ADVANTAGES SUMMARY")
    print("-" * 30)
    
    advantages = [
        "🚀 Non-blocking I/O operations",
        "🔄 Concurrent tool execution",
        "⚡ Better resource utilization", 
        "🌐 Efficient external API calls",
        "📈 Higher throughput potential",
        "🔧 Backward compatibility with sync tools",
        "🔍 Automatic async/sync detection",
        "🧵 Thread pool fallback for sync operations"
    ]
    
    for advantage in advantages:
        print(f"   {advantage}")
    
    print()
    print("💡 WHEN TO USE ASYNC:")
    print("   • External API calls")
    print("   • Database operations") 
    print("   • File I/O")
    print("   • Network requests")
    print("   • Long-running calculations")
    print()
    print("⚡ WHEN TO STICK WITH SYNC:")
    print("   • Simple math operations")
    print("   • String manipulation")
    print("   • Pure CPU-bound tasks")
    print("   • Legacy tool compatibility")


def main():
    """Run the complete demonstration."""
    print()
    demo_introspection()
    demo_performance()
    demo_concurrent_operations()
    demo_complex_workflow()
    demo_comparison()
    
    print()
    print("🎉 DEMONSTRATION COMPLETE!")
    print("=" * 50)
    print()
    print("📚 Learn More:")
    print("   • README_ASYNC.md - Complete documentation")
    print("   • test_async_servers.py - Comprehensive tests")
    print("   • compare_sync_async.py - Performance analysis")
    print()
    print("🚀 Start Building:")
    print("   • Copy async_micromcp.py as your base")
    print("   • Create tool methods with async/sync as needed")
    print("   • Let the framework handle the complexity!")
    print()


if __name__ == "__main__":
    main()
