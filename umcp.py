#!/usr/bin/env python3
"""
umcp.py - Core MCP (Model Context Protocol) server implementation
Handles JSON-RPC 2.0 messaging and MCP protocol infrastructure
"""

from inspect import getdoc, getmembers, ismethod, signature, Parameter
from logging import FileHandler, basicConfig, getLogger, INFO
from sys import argv, exit, stdin
from json import JSONDecodeError, dumps, loads
from pathlib import Path
from typing import Any, Dict, Optional, Union, get_args, get_origin, get_type_hints


class MCPServer:
    """Core MCP server implementation using JSON-RPC 2.0 protocol."""
    
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
            },
            "instructions": self.get_instructions()
        }
    
    def get_instructions(self) -> str:
        """Get server instructions. Override in subclasses for custom instructions."""
        return "This server provides tool functionality via the Model Context Protocol."
    
    def discover_tools(self) -> Dict[str, Any]:
        """Discover available tools through introspection."""
        tools = []
        
        # Find all methods that start with 'tool_'
        for name, method in getmembers(self, predicate=ismethod):
            if name.startswith('tool_'):
                tool_name = name[5:]  # Remove 'tool_' prefix
                
                # Get method signature and docstring
                sig = signature(method)
                doc = getdoc(method) or f"Execute {tool_name} tool"
                
                # Extract parameters info from signature
                parameters = self._extract_parameters_from_signature(sig, method)
                
                tool_def = {
                    "name": tool_name,
                    "description": doc,
                    "parameters": parameters
                }
                
                tools.append(tool_def)
        
        return {"tools": tools}
    
    def _extract_parameters_from_signature(self, sig: signature, method) -> Dict[str, Any]:
        """Extract parameter schema from method signature and type hints."""
        # Get type hints for the method
        try:
            type_hints = get_type_hints(method)
        except (NameError, AttributeError):
            type_hints = {}
        
        # Skip 'self' parameter only
        params = [param for name, param in sig.parameters.items() 
                 if name != 'self']
        
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
        
        # Handle generic types like List[str], Dict[str, Any]
        if origin is list:
            args = get_args(param_type)
            schema = {"type": "array"}
            if args:
                schema["items"] = self._type_to_json_schema(args[0])
            return schema
        elif origin is dict:
            return {"type": "object"}
        
        # Default to string for unknown types
        return {"type": "string"}

    # ==== MCP Protocol Core Implementation ====
    
    def handle_initialize(self, request_id: Union[str, int, None], params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle MCP initialize method."""
        # Parse client info from params (capabilities and protocol version are logged but not used)
        client_info = params.get('clientInfo', {})
        
        self.logger.info("Initialize request from client: %s", client_info)
        
        # Generate configuration dynamically instead of reading from file
        result = self.get_config()
        
        return self.create_response(request_id, result, None)
    
    def handle_tools_list(self, request_id: Union[str, int, None]) -> Dict[str, Any]:
        """List available tools."""
        # Discover tools dynamically through introspection
        result = self.discover_tools()
        
        return self.create_response(request_id, result, None)
    
    def handle_tools_call(self, request_id: Union[str, int, None], params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tool calls - delegates to tool implementations."""
        tool_name = params.get('name', '')
        arguments = params.get('arguments', {})
        
        self.logger.info("Tool call: %s with arguments: %s", tool_name, dumps(arguments))
        
        # Call the function from the main script if it exists
        tool_method_name = f"tool_{tool_name}"
        
        if hasattr(self, tool_method_name):
            try:
                method = getattr(self, tool_method_name)
                sig = signature(method)
                
                # Map from arguments dict to individual parameters
                params_list = [p for name, p in sig.parameters.items() if name != 'self']
                
                if len(params_list) == 0:
                    # No parameters
                    content = method()
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
                    
                    content = method(**kwargs)
                
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
        # Convert content to string if it's not already
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
        
        return self.create_response(request_id, result, None)
    
    # ==== JSON-RPC 2.0 Handler ====
    
    def create_response(self, request_id: Union[str, int, None], result: Any, error: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Create a JSON-RPC 2.0 response."""
        if error is not None:
            response = {
                "jsonrpc": "2.0",
                "error": error,
                "id": request_id
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "result": result,
                "id": request_id
            }
        
        # Log the response
        self.logger.info("RESPONSE: %s", dumps(response))
        
        return response
    
    def create_error(self, code: int, message: str) -> Dict[str, Any]:
        """Create a JSON-RPC 2.0 error."""
        return {
            "code": code,
            "message": message
        }
    
    def process_request(self, input_data: str) -> Dict[str, Any]:
        """Process a JSON-RPC 2.0 request."""
        try:
            request = loads(input_data)
        except JSONDecodeError as e:
            self.logger.error("Invalid JSON: %s", e)
            error = self.create_error(-32700, "Parse error")
            return self.create_response(None, None, error)
        
        # Parse the JSON-RPC 2.0 request
        jsonrpc = request.get('jsonrpc')
        request_id = request.get('id')
        method = request.get('method')
        params = request.get('params', {})
        
        # Log the method being called
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
            return self.handle_tools_call(request_id, params)
        elif method == "notifications/initialized":
            # Don't invoke any response, just log it
            self.logger.info("Host confirmed toolContract reception with 'notifications/initialized'")
            return None
        else:
            error = self.create_error(-32601, f"Method not found: {method}")
            return self.create_response(request_id, None, error)
    
    # ==== Main execution ====
    
    def run(self, args: list = None):
        """Run the MCP server."""
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
                response = self.process_request(input_data)
                
                if response is not None:
                    # Output the response
                    print(dumps(response))
                    
            except (OSError, IOError) as e:
                self.logger.error("Error reading file %s: %s", args[0], e)
                exit(1)
        else:
            # Continuously read from stdin line by line
            self.logger.info("MCP Server started. Waiting for JSON-RPC 2.0 messages...")
            
            try:
                for line in stdin:
                    line = line.strip()
                    
                    # Skip empty lines
                    if not line:
                        continue
                    
                    # Log the input
                    self.logger.info("REQUEST: %s", line)
                    
                    # Process the JSON-RPC 2.0 request
                    response = self.process_request(line)
                    
                    if response is not None:
                        # Output the response
                        print(dumps(response), flush=True)
                        
            except KeyboardInterrupt:
                self.logger.info("MCP Server stopped.")
                exit(0)
            except EOFError:
                self.logger.info("MCP Server finished processing input.")
                exit(0)


if __name__ == "__main__":
    # This will be overridden by subclasses
    server = MCPServer()
    server.run()
