#!/usr/bin/env python3
"""
compare_sync_async.py - Compare synchronous vs asynchronous MCP server performance
"""

import json
import subprocess
import threading
from time import time
from typing import List, Dict, Any, Tuple


def send_request(server_script: str, request: dict) -> tuple[dict, float]:
    """Send a JSON-RPC request and measure response time."""
    request_json = json.dumps(request)
    
    start_time = time()
    try:
        result = subprocess.run(
            ["python", server_script],
            input=request_json,
            text=True,
            capture_output=True,
            timeout=30
        )
        end_time = time()
        
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip()), end_time - start_time
        else:
            return {"error": f"Server error: {result.stderr}"}, end_time - start_time
    except Exception as e:
        end_time = time()
        return {"error": f"Request failed: {e}"}, end_time - start_time


def run_concurrent_requests(server_script: str, requests: List[dict]) -> tuple[List[dict], float]:
    """Run multiple requests concurrently and measure total time."""
    results = [None] * len(requests)
    threads = []
    
    start_time = time()
    
    def worker(index: int, request: dict):
        response, _ = send_request(server_script, request)
        results[index] = response
    
    # Start all threads
    for i, request in enumerate(requests):
        thread = threading.Thread(target=worker, args=(i, request))
        threads.append(thread)
        thread.start()
    
    # Wait for all to complete
    for thread in threads:
        thread.join()
    
    end_time = time()
    return results, end_time - start_time


def run_sequential_requests(server_script: str, requests: List[dict]) -> tuple[List[dict], float]:
    """Run multiple requests sequentially and measure total time."""
    results = []
    start_time = time()
    
    for request in requests:
        response, _ = send_request(server_script, request)
        results.append(response)
    
    end_time = time()
    return results, end_time - start_time


def compare_calculator_performance():
    """Compare sync vs async calculator performance."""
    print("🧮 Calculator Performance Comparison")
    print("-" * 50)
    
    # Requests that involve operations with different complexities
    requests = [
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "add", "arguments": {"a": 10, "b": 5}}, "id": 1},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "multiply_async", "arguments": {"a": 8, "b": 7}}, "id": 2},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "power_async", "arguments": {"base": 2, "exponent": 10}}, "id": 3},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "factorial_async", "arguments": {"n": 15}}, "id": 4},
    ]
    
    print(f"Running {len(requests)} operations...")
    print()
    
    # Test sync version (sequential only - no async server)
    print("📊 Synchronous Calculator (calculator_server.py):")
    sync_requests = [
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "add", "arguments": {"a": 10, "b": 5}}, "id": 1},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "multiply", "arguments": {"a": 8, "b": 7}}, "id": 2},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "add", "arguments": {"a": 1024, "b": 1}}, "id": 3},  # Substitute for power
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "add", "arguments": {"a": 1307674368000, "b": 0}}, "id": 4},  # Substitute for factorial
    ]
    
    sync_results, sync_time = run_sequential_requests("calculator_server.py", sync_requests)
    print(f"   Sequential execution time: {sync_time:.3f}s")
    
    successful_sync = sum(1 for r in sync_results if "result" in r)
    print(f"   Successful operations: {successful_sync}/{len(sync_requests)}")
    print()
    
    # Test async version (can handle both concurrent and sequential)
    print("🚀 Asynchronous Calculator (async_calculator_server.py):")
    
    # Sequential execution
    async_results_seq, async_time_seq = run_sequential_requests("async_calculator_server.py", requests)
    print(f"   Sequential execution time: {async_time_seq:.3f}s")
    
    # Concurrent execution simulation
    async_results_conc, async_time_conc = run_concurrent_requests("async_calculator_server.py", requests)
    print(f"   Concurrent execution time: {async_time_conc:.3f}s")
    
    successful_async = sum(1 for r in async_results_conc if "result" in r)
    print(f"   Successful operations: {successful_async}/{len(requests)}")
    
    # Performance improvement
    if async_time_conc > 0:
        improvement = ((async_time_seq - async_time_conc) / async_time_seq) * 100
        print(f"   Performance improvement: {improvement:.1f}% faster with concurrency")
    
    print()
    print("💡 Note: Async benefits are more apparent with I/O-bound operations")
    print()


def compare_movie_performance():
    """Compare movie server operations that benefit more from async."""
    print("🎬 Movie Server Performance Comparison")
    print("-" * 50)
    
    # I/O-intensive operations
    requests = [
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_movie_details_async", "arguments": {"movie_id": 1}}, "id": 1},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "search_movies_async", "arguments": {"query": "Matrix"}}, "id": 2},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "book_ticket_async", "arguments": {"movie_id": 2, "show_time": "18:30", "num_tickets": 2, "customer_email": "test@example.com"}}, "id": 3},
    ]
    
    print(f"Running {len(requests)} I/O-intensive operations...")
    print()
    
    # Test synchronous movie server
    print("📊 Synchronous Movie Server (movie_server.py):")
    sync_requests = [
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_movies"}, "id": 1},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "book_ticket", "arguments": {"movie_id": 2, "show_time": "18:30", "num_tickets": 2}}, "id": 2},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_movies"}, "id": 3},
    ]
    
    sync_results, sync_time = run_sequential_requests("movie_server.py", sync_requests)
    print(f"   Sequential execution time: {sync_time:.3f}s")
    
    successful_sync = sum(1 for r in sync_results if "result" in r)
    print(f"   Successful operations: {successful_sync}/{len(sync_requests)}")
    print()
    
    # Test async movie server
    print("🚀 Asynchronous Movie Server (async_movie_server.py):")
    
    # Sequential execution
    async_results_seq, async_time_seq = run_sequential_requests("async_movie_server.py", requests)
    print(f"   Sequential execution time: {async_time_seq:.3f}s")
    
    # Concurrent execution
    async_results_conc, async_time_conc = run_concurrent_requests("async_movie_server.py", requests)
    print(f"   Concurrent execution time: {async_time_conc:.3f}s")
    
    successful_async = sum(1 for r in async_results_conc if "result" in r)
    print(f"   Successful operations: {successful_async}/{len(requests)}")
    
    # Performance improvement
    if async_time_conc > 0:
        improvement = ((async_time_seq - async_time_conc) / async_time_seq) * 100
        print(f"   Performance improvement: {improvement:.1f}% faster with concurrency")
    
    print()
    print("🎯 Async operations show significant benefits for I/O-bound tasks!")
    print()


def feature_comparison():
    """Compare features between sync and async implementations."""
    print("🔍 Feature Comparison")
    print("-" * 50)
    
    features = [
        ("JSON-RPC 2.0 Protocol", "✅", "✅"),
        ("MCP Protocol Support", "✅", "✅"),
        ("Dynamic Tool Discovery", "✅", "✅"),
        ("Type Introspection", "✅", "✅"),
        ("Concurrent Tool Execution", "❌", "✅"),
        ("Non-blocking I/O", "❌", "✅"),
        ("Async/Await Support", "❌", "✅"),
        ("Mixed Sync/Async Tools", "❌", "✅"),
        ("Thread Pool Fallback", "❌", "✅"),
        ("Resource Efficiency", "⚡", "🚀"),
        ("Complexity", "Low", "Medium"),
        ("Learning Curve", "Easy", "Moderate"),
    ]
    
    print(f"{'Feature':<25} {'Sync':<10} {'Async':<10}")
    print("-" * 45)
    
    for feature, sync_support, async_support in features:
        print(f"{feature:<25} {sync_support:<10} {async_support:<10}")
    
    print()
    print("🏆 Async implementation provides better performance and scalability")
    print("🎯 Use async for I/O-bound operations, API calls, and concurrent workloads")
    print()


def main():
    """Run all comparisons."""
    print("⚖️  MCP Server: Synchronous vs Asynchronous Comparison")
    print("=" * 60)
    print()
    
    feature_comparison()
    print("=" * 60)
    print()
    
    compare_calculator_performance()
    print("=" * 60)
    print()
    
    compare_movie_performance()
    print("=" * 60)
    print()
    
    print("📋 Summary:")
    print("• Async servers support both sync and async tools")
    print("• Performance benefits are most apparent with I/O-bound operations")
    print("• Concurrent execution allows better resource utilization")
    print("• Backward compatibility maintained for existing sync tools")
    print("• Gradual migration path from sync to async implementations")


if __name__ == "__main__":
    main()
