#!/usr/bin/env python3
"""
test_movieserver.py - Test script for the Movie MCP Server
"""

import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from movie_server import MovieMCPServer

def test_basic_functionality():
    """Test basic server functionality."""
    server = MovieMCPServer()
    
    # Test tool_get_movies
    print("Testing get_movies...")
    movies = server.tool_get_movies()
    print(f"Movies returned: {len(movies)}")
    print(f"First movie: {movies[0]['title']}")
    
    # Test tool_book_ticket with valid data
    print("\nTesting book_ticket with valid data...")
    booking = server.tool_book_ticket(1, "17:00", 2)
    print(f"Booking result: {booking}")
    
    # Test tool_book_ticket with invalid data
    print("\nTesting book_ticket with invalid data...")
    try:
        invalid_booking = server.tool_book_ticket("invalid", "", 0)
        print(f"Invalid booking result: {invalid_booking}")
    except (TypeError, ValueError) as e:
        print(f"Expected error for invalid data: {e}")
    
    # Test JSON-RPC initialize
    print("\nTesting JSON-RPC initialize...")
    init_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "test-client", "version": "1.0"},
            "protocolVersion": "0.1.0"
        }
    }
    
    response = server.process_request(json.dumps(init_request))
    print(f"Initialize response: {response['result']['serverInfo']['name']}")
    
    # Test tools/list
    print("\nTesting tools/list...")
    tools_request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {}
    }
    
    response = server.process_request(json.dumps(tools_request))
    tools = response['result']['tools']
    print(f"Available tools: {[tool['name'] for tool in tools]}")
    
    # Test tools/call for get_movies
    print("\nTesting tools/call for get_movies...")
    call_request = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "get_movies",
            "arguments": {}
        }
    }
    
    response = server.process_request(json.dumps(call_request))
    content = response['result']['content'][0]['text']
    movies_data = json.loads(content)
    print(f"Movies via tools/call: {len(movies_data)} movies")
    
    print("\nAll tests completed successfully!")

if __name__ == "__main__":
    test_basic_functionality()
