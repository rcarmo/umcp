#!/usr/bin/env python3
"""
async_micromcp.py - Async MCP (Model Context Protocol) server implementation
Handles JSON-RPC 2.0 messaging and MCP protocol infrastructure with asyncio support
"""

from sys import argv, exit, stdin
from asyncio import CancelledError, StreamReader, StreamReaderProtocol, get_event_loop, run
from inspect import Parameter, Signature, getdoc, getmembers, ismethod, iscoroutinefunction, signature
from json import JSONDecodeError, dumps, loads
from logging import FileHandler, INFO, basicConfig, getLogger
from pathlib import Path
from typing import Any, Dict, Optional, Union, get_args, get_origin, get_type_hints


class AsyncMCPServer:
    """Async MCP server implementation using JSON-RPC 2.0 protocol with asyncio."""
    
    def __init__(self):
        # Get the directory where the script is located
        self.script_dir = Path(__file__).parent.absolute()
        self.log_file = self.script_dir / "mcpserver.log"
        
        # Set up logging
        self._setup_logging()
        
    def _setup_logging(self):
        """Set up logging configuration."""
        # Create logs directory if it doesn't exist
        self.log_file.parent.mkdir(exist_ok=True)
        
        # Configure logging
        basicConfig(
            level=INFO,
            format='[%(asctime)s] [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            handlers=[
                FileHandler(self.log_file),
            ]
        )
        self.logger = getLogger(__name__)
    
    def get_config(self) -> Dict[str, Any]:
        """Generate server configuration dynamically."""
        return {
            "protocolVersion": "0.1.0",
            "serverInfo": {
                "name": self.__class__.__name__,
                "version": "0.1.0"
            },
            "capabilities": {
                "tools": {
                    "listChanged": True
                }
            }
        }
    
    def get_instructions(self) -> str:
        """Get server-specific instructions. Override in subclasses."""
        return "Base MCP server with dynamic tool discovery."
    
    def discover_tools(self) -> Dict[str, Any]:
        """Discover tools by introspecting methods that start with 'tool_'."""
        tools = []
        
        for name, method in getmembers(self, predicate=ismethod):
            if name.startswith('tool_'):
                tool_name = name[5:]  # Remove 'tool_' prefix
                
                # Get method signature and docstring
                sig = signature(method)
                doc = getdoc(method) or f"Execute {tool_name}"
                
                # Extract description from docstring (first line)
                description = doc.split('\n')[0].strip()
                
                # Generate schema from method signature
                schema = self._generate_schema_from_signature(sig)
                
                # Check if method is async
                is_async = iscoroutinefunction(method)
                
                tool_def = {
                    "name": tool_name,
                    "description": description,
                    "async": is_async  # Add async flag for introspection
                }
                
                if schema:
                    tool_def["inputSchema"] = schema
                
                tools.append(tool_def)
        
        return {"tools": tools}
    
    def _generate_schema_from_signature(self, sig: Signature) -> Dict[str, Any]:
        """Generate JSON schema from method signature."""
        # Get type hints
        try:
            type_hints = get_type_hints(sig.return_annotation.__func__ if hasattr(sig.return_annotation, '__func__') else lambda: None)
        except (TypeError, AttributeError, NameError):
            type_hints = {}
        
        # Get parameters (excluding 'self')
        params = [p for name, p in sig.parameters.items() if name != 'self']
        
        if not params:
            return {}
        
        # Create schema from individual parameters
        properties = {}
        required = []
        
        for param in params:
            param_type = type_hints.get(param.name)
            
            prop_def = self._type_to_json_schema(param_type)
            properties[param.name] = prop_def
            
            if param.default == Parameter.empty:
                required.append(param.name)
        
        if properties:
            schema = {
                "type": "object",
                "properties": properties
            }
            if required:
                schema["required"] = required
            return schema
        
        return {}
    
    def _type_to_json_schema(self, param_type) -> Dict[str, Any]:
        """Convert Python type annotation to JSON schema property."""
        if param_type is None or param_type == type(None):
            return {"type": "null"}
        elif param_type is str:
            return {"type": "string"}
        elif param_type is int:
            return {"type": "integer"}
        elif param_type is float:
            return {"type": "number"}
        elif param_type is bool:
            return {"type": "boolean"}
        elif param_type is list:
            return {"type": "array"}
        elif param_type is dict:
            return {"type": "object"}
        
        # Handle Union types (e.g., Optional[str])
        origin = get_origin(param_type)
        if origin is Union:
            args = get_args(param_type)
            # Handle Optional[T] which is Union[T, None]
            if len(args) == 2 and type(None) in args:
                non_none_type = args[0] if args[1] is type(None) else args[1]
                return self._type_to_json_schema(non_none_type)
        
        # Default to string for unknown types
        return {"type": "string"}
    
    # ==== Protocol handlers ====
    
    def handle_initialize(self, request_id: int, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle initialize request."""
        _ = params  # Acknowledge params parameter
        config = self.get_config()
        return self.create_response(request_id, config)
    
    def handle_tools_list(self, request_id: int) -> Dict[str, Any]:
        """Handle tools/list request."""
        tools_info = self.discover_tools()
        return self.create_response(request_id, tools_info)
    
    async def handle_tools_call_async(self, request_id: int, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/call request asynchronously."""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        
        if not tool_name:
            error = self.create_error(-32602, "Missing 'name' parameter")
            return self.create_response(request_id, None, error)
        
        # Find the tool method
        method_name = f"tool_{tool_name}"
        method = getattr(self, method_name, None)
        
        if method and callable(method):
            try:
                # Get method signature for parameter mapping
                sig = signature(method)
                
                # Map from arguments dict to individual parameters
                params_list = [p for name, p in sig.parameters.items() if name != 'self']
                
                if len(params_list) == 0:
                    # No parameters - call method based on whether it's async
                    if iscoroutinefunction(method):
                        content = await method()
                    else:
                        # Run sync method in thread pool to avoid blocking
                        content = await get_event_loop().run_in_executor(None, method)
                else:
                    # Individual parameters - map from arguments dict
                    kwargs = {}
                    for param_name, param in sig.parameters.items():
                        if param_name == 'self':
                            continue
                        
                        if param_name in arguments:
                            kwargs[param_name] = arguments[param_name]
                        elif param.default != Parameter.empty:
                            # Use default value if available
                            kwargs[param_name] = param.default
                        else:
                            # Required parameter is missing
                            raise ValueError(f"Required parameter '{param_name}' is missing")
                    
                    # Call method based on whether it's async
                    if iscoroutinefunction(method):
                        content = await method(**kwargs)
                    else:
                        # Run sync method in thread pool to avoid blocking
                        content = await get_event_loop().run_in_executor(
                            None, lambda: method(**kwargs)
                        )
                
                if content is None:
                    error = self.create_error(-32603, f"Tool execution error for {tool_name}")
                    return self.create_response(request_id, None, error)
                
            except (ValueError, TypeError, KeyError) as e:
                self.logger.error("Tool execution error for %s: %s", tool_name, e)
                error = self.create_error(-32603, f"Tool execution error for {tool_name}: {str(e)}")
                return self.create_response(request_id, None, error)
        else:
            error = self.create_error(-32601, f"Tool not found: {tool_name}")
            return self.create_response(request_id, None, error)
        
        # Ensure proper JSON encoding for the content
        if isinstance(content, (dict, list)):
            stringified_content = dumps(content)
        else:
            stringified_content = str(content)
        
        # Build the response structure with the stringified content
        result = {
            "content": [{
                "type": "text",
                "text": stringified_content
            }]
        }
        
        return self.create_response(request_id, result)
    
    # ==== JSON-RPC utilities ====
    
    def create_response(self, request_id: int, result: Optional[Dict[str, Any]] = None, 
                       error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Create a JSON-RPC 2.0 response."""
        response = {
            "jsonrpc": "2.0",
            "id": request_id
        }
        
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result
        
        return response
    
    def create_error(self, code: int, message: str, data: Optional[Any] = None) -> Dict[str, Any]:
        """Create a JSON-RPC 2.0 error object."""
        error = {
            "code": code,
            "message": message
        }
        if data is not None:
            error["data"] = data
        return error
    
    async def process_request_async(self, request_data: str) -> Optional[Dict[str, Any]]:
        """Process a JSON-RPC 2.0 request asynchronously."""
        try:
            request = loads(request_data)
        except JSONDecodeError as e:
            self.logger.error("JSON decode error: %s", e)
            error = self.create_error(-32700, "Parse error")
            return self.create_response(None, None, error)
        
        # Extract request components
        jsonrpc = request.get("jsonrpc")
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")
        
        self.logger.info("Processing method: %s (id: %s)", method, request_id)
        
        # Validate JSON-RPC 2.0 version
        if jsonrpc != "2.0":
            error = self.create_error(-32600, "Invalid Request: Not a JSON-RPC 2.0 request")
            return self.create_response(request_id, None, error)
        
        # Process the method
        if method == "initialize":
            return self.handle_initialize(request_id, params)
        elif method == "tools/list":
            return self.handle_tools_list(request_id)
        elif method == "tools/call":
            return await self.handle_tools_call_async(request_id, params)
        elif method == "notifications/initialized":
            # Don't invoke any response, just log it
            self.logger.info("Host confirmed toolContract reception with 'notifications/initialized'")
            return None
        else:
            error = self.create_error(-32601, f"Method not found: {method}")
            return self.create_response(request_id, None, error)
    
    # ==== Main execution ====
    
    async def run_mcp_server_async(self, args: list = None):
        """Run the MCP server asynchronously."""
        if args is None:
            args = argv[1:]
        
        # Check if reading from a file or continuous stdin
        if args:
            # Read from file if provided as argument
            try:
                with open(args[0], 'r', encoding='utf-8') as f:
                    input_data = f.read()
                
                # Log the input
                self.logger.info("REQUEST: %s", input_data)
                
                # Process the JSON-RPC 2.0 request
                response = await self.process_request_async(input_data)
                
                if response is not None:
                    # Output the response
                    print(dumps(response))
                    
            except (OSError, IOError) as e:
                self.logger.error("Error reading file %s: %s", args[0], e)
                sys.exit(1)
        else:
            # Continuously read from stdin line by line using asyncio
            self.logger.info("Async MCP Server started. Waiting for JSON-RPC 2.0 messages...")
            
            try:
                # Create async stdin reader
                loop = get_event_loop()
                reader = StreamReader()
                protocol = StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: protocol, stdin)
                
                while True:
                    try:
                        line = await reader.readline()
                        if not line:  # EOF
                            break
                            
                        line = line.decode().strip()
                        
                        # Skip empty lines
                        if not line:
                            continue
                        
                        # Log the input
                        self.logger.info("REQUEST: %s", line)
                        
                        # Process the JSON-RPC 2.0 request
                        response = await self.process_request_async(line)
                        
                        if response is not None:
                            # Output the response
                            print(dumps(response), flush=True)
                            
                    except CancelledError:
                        break
                        
            except KeyboardInterrupt:
                self.logger.info("Async MCP Server stopped.")
                exit(0)
            except Exception as e:
                self.logger.error("Async MCP Server error: %s", e)
                exit(1)
    
    def run_mcp_server(self, args: list = None):
        """Synchronous wrapper to run the async MCP server."""
        try:
            run(self.run_mcp_server_async(args))
        except KeyboardInterrupt:
            self.logger.info("Async MCP Server stopped by user.")
            sys.exit(0)


if __name__ == "__main__":
    # This will be overridden by subclasses
    server = AsyncMCPServer()
    server.run_mcp_server()
