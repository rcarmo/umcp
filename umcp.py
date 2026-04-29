#!/usr/bin/env python3
"""
umcp.py - Core MCP (Model Context Protocol) server implementation
Handles JSON-RPC 2.0 messaging and MCP protocol infrastructure.

Transports:
  stdio  (default)        - newline-delimited JSON-RPC over stdin/stdout.
  sse    (--port N)       - HTTP server on 127.0.0.1:N implementing the MCP
                            SSE transport (GET /sse for the event stream,
                            POST /message for JSON-RPC requests).
  socket (--port N --tcp) - raw TCP with newline-delimited JSON-RPC. Legacy
                            mode; opt in with --tcp.

This is the synchronous sibling of ``aioumcp.py`` and intentionally mirrors
its public surface (schema generation, tool annotations, type coercion,
strict argument validation, transports).
"""

# ruff: noqa: E402

import os as _os
import sys as _sys

# Force unbuffered stdio before any other I/O occurs.  Setting the env var
# only helps child processes; it does NOT fix already-opened streams.
_os.environ.setdefault("PYTHONUNBUFFERED", "1")

# On Windows the default stdio streams are buffered text-mode with CRLF
# translation.  Switch stdout (and stderr) to binary / unbuffered so
# JSON-RPC newline-delimited output is flushed immediately and is not
# corrupted by \r\n translation.  stdin is switched to binary so that
# readline() returns raw bytes without encoding surprises.
if _os.name == "nt":
    import msvcrt as _msvcrt  # Windows-only
    _msvcrt.setmode(_sys.stdin.fileno(), _os.O_BINARY)
    _msvcrt.setmode(_sys.stdout.fileno(), _os.O_BINARY)
    _msvcrt.setmode(_sys.stderr.fileno(), _os.O_BINARY)

# Regardless of platform, grab the raw binary buffers for stdio.  Using
# ``.buffer`` bypasses Python's text-mode buffering layer entirely.
_stdin_bin  = _sys.stdin.buffer
_stdout_bin = _sys.stdout.buffer

import re
import socketserver
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from inspect import (
    Parameter,
    Signature,
    getdoc,
    getmembers,
    ismethod,
    signature,
)
from json import JSONDecodeError, dumps, loads
from logging import INFO, FileHandler, basicConfig, getLogger
from pathlib import Path
from queue import Empty, Queue
from sys import argv, exit
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints, is_typeddict
from urllib.parse import parse_qs, urlparse
from uuid import uuid4


class MCPServer:
    """Core MCP server implementation using JSON-RPC 2.0 protocol."""

    def __init__(self):
        # Get the directory where the script is located
        self.script_dir = Path(__file__).parent.absolute()
        self.log_file = self.script_dir / "mcpserver.log"

        # Set up logging
        self._setup_logging()

        # SSE session registry: session_id -> Queue[bytes].  Populated lazily
        # the first time the SSE transport is started.
        self._sse_sessions: dict[str, Queue] = {}
        self._sse_lock = threading.Lock()

    def _setup_logging(self) -> None:
        """Set up logging configuration."""
        self.log_file.parent.mkdir(exist_ok=True)
        basicConfig(
            level=INFO,
            format='[%(asctime)s] [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            handlers=[
                FileHandler(self.log_file),
            ]
        )
        self.logger = getLogger(__name__)

    def get_config(self) -> dict[str, Any]:
        """Generate server configuration dynamically."""
        return {
            "protocolVersion": "2025-03-26",
            "serverInfo": {
                "name": self.__class__.__name__,
                "version": "0.1.0"
            },
            "capabilities": {
                "tools": {
                    "listChanged": True
                },
                "prompts": {
                    "listChanged": True,
                    "get": True,
                }
            },
            "instructions": self.get_instructions()
        }

    def get_instructions(self) -> str:
        """Get server instructions. Override in subclasses for custom instructions."""
        return "This server provides tool functionality via the Model Context Protocol."

    # ==== Tool discovery ====

    def discover_tools(self) -> dict[str, Any]:
        """Discover tools by introspecting methods that start with ``tool_``.

        Returns a strict MCP-compliant tools/list response.  Every tool
        definition includes ``name``, ``description``, and ``inputSchema``
        (the spec requires this even for parameter-less tools).
        """
        tools = []
        for name, method in getmembers(self, predicate=ismethod):
            if not name.startswith('tool_'):
                continue
            tool_name = name[5:]
            sig = signature(method)
            doc = getdoc(method) or f"Execute {tool_name} tool"
            parameters = self._extract_parameters_from_signature(sig, method)
            tool_def = {
                "name": tool_name,
                "description": doc,
                # MCP spec requires inputSchema; default to empty object schema
                "inputSchema": parameters if parameters else {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
            annotations = self._infer_tool_annotations(tool_name, method)
            if annotations:
                tool_def["annotations"] = annotations
            tools.append(tool_def)
        return {"tools": tools}

    @staticmethod
    def _infer_tool_annotations(tool_name: str, method: Any) -> dict[str, bool]:
        """Infer MCP tool annotations from naming conventions and metadata.

        If the method has a ``_mcp_annotations`` dict attribute it is returned
        directly, allowing individual tools to override the heuristics.

        Otherwise, the tool name is matched against known patterns:
        - Read-only tools: list, get, read, inspect, extract, query, search,
          check, audit, fetch, calculate, recommend
        - Destructive tools: delete, clear, cleanup, restart
        - Open-world (external I/O): web_*, azure_*, fetch
        """
        explicit = getattr(method, "_mcp_annotations", None)
        if explicit is not None:
            return dict(explicit)

        READ_PREFIXES = (
            "list_", "get_", "read_", "inspect_", "extract_",
            "query_", "search_", "check_", "audit_", "fetch_",
            "calculate_", "recommend_",
        )
        READ_EXACT = {
            "list_supported_formats", "office_read", "office_inspect",
            "office_audit",
        }
        DESTRUCTIVE_PREFIXES = ("delete_", "clear_", "cleanup_", "restart_")
        OPEN_WORLD_PREFIXES = ("web_", "azure_")

        is_read_only = (
            any(tool_name.startswith(p) or f"_{p[:-1]}" in tool_name for p in READ_PREFIXES)
            or tool_name in READ_EXACT
        )
        is_destructive = any(
            tool_name.startswith(p) or f"_{p[:-1]}" in tool_name for p in DESTRUCTIVE_PREFIXES
        )
        is_open_world = any(tool_name.startswith(p) for p in OPEN_WORLD_PREFIXES)

        annotations: dict[str, bool] = {}
        if is_read_only:
            annotations["readOnlyHint"] = True
            annotations["destructiveHint"] = False
        elif is_destructive:
            annotations["readOnlyHint"] = False
            annotations["destructiveHint"] = True
        else:
            annotations["readOnlyHint"] = False
            annotations["destructiveHint"] = False
        if is_open_world:
            annotations["openWorldHint"] = True
        return annotations

    # ==== Prompt discovery & handling ====

    def discover_prompts(self) -> dict[str, Any]:
        """Discover available prompts through introspection.

        A prompt is any method whose name starts with ``prompt_``. Its
        docstring becomes the description and its signature is converted to
        a JSON schema (similar to tool parameter extraction).
        """
        prompts = []
        for name, method in getmembers(self, predicate=ismethod):
            if not name.startswith('prompt_'):
                continue
            prompt_name = name[7:]
            sig = signature(method)
            doc = getdoc(method) or f"Prompt template {prompt_name}"
            parameters = self._extract_parameters_from_signature(sig, method)
            categories = self._extract_prompt_categories(doc)
            prompts.append({
                "name": prompt_name,
                "description": doc,
                "inputSchema": parameters or {},
                "categories": categories
            })
        return {"prompts": prompts}

    def _extract_prompt_categories(self, doc: str) -> list[str]:
        """Extract categories from a docstring.

        Supports patterns:
          Category: foo
          Categories: foo, bar
          [categories: foo, bar]
          [category: foo]
        Returns a list of lowercase trimmed category tokens.
        """
        if not doc:
            return []
        lines = doc.splitlines()
        cats = []
        pattern_line = re.compile(r'^\s*Categor(?:y|ies):\s*(.+)$', re.IGNORECASE)
        bracket_pattern = re.compile(r'\[(?:categor(?:y|ies)):\s*([^\]]+)\]', re.IGNORECASE)
        for ln in lines:
            m = pattern_line.match(ln)
            if m:
                cats.extend([c.strip().lower() for c in m.group(1).split(',') if c.strip()])
            for b in bracket_pattern.findall(ln):
                cats.extend([c.strip().lower() for c in b.split(',') if c.strip()])
        seen = set()
        out = []
        for c in cats:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def handle_prompt_get(self, request_id: str | int | None, params: dict[str, Any]) -> dict[str, Any]:
        """Handle prompts/get: return a prompt description and message list."""
        prompt_name = params.get('name')
        if not prompt_name:
            error = self.create_error(-32602, "Missing required parameter 'name'")
            return self.create_response(request_id, None, error)
        method_name = f"prompt_{prompt_name}"
        if not hasattr(self, method_name):
            error = self.create_error(-32601, f"Prompt not found: {prompt_name}")
            return self.create_response(request_id, None, error)
        method = getattr(self, method_name)
        sig = signature(method)
        doc = getdoc(method) or f"Prompt template {prompt_name}"
        arguments = params.get('arguments', {}) or {}
        categories = self._extract_prompt_categories(doc)
        result_body: dict[str, Any] = {'description': doc}
        if categories:
            result_body['categories'] = categories
        if arguments:
            try:
                kwargs = {}
                for p_name, p in sig.parameters.items():
                    if p_name == 'self':
                        continue
                    if p_name in arguments:
                        kwargs[p_name] = arguments[p_name]
                    elif p.default != Parameter.empty:
                        kwargs[p_name] = p.default
                    else:
                        raise ValueError(
                            f"Missing required argument '{p_name}' for prompt {prompt_name}"
                        )
                ret = method(**kwargs)
                messages: list[dict[str, Any]] | None = None
                if isinstance(ret, str):
                    messages = [{'role': 'user', 'content': {'type': 'text', 'text': ret}}]
                elif isinstance(ret, list) and all(
                    isinstance(m, dict) and 'role' in m and 'content' in m for m in ret
                ):
                    messages = ret
                elif isinstance(ret, dict) and 'messages' in ret and isinstance(ret['messages'], list):
                    messages = ret['messages']
                else:
                    messages = [{
                        'role': 'user',
                        'content': {'type': 'text', 'text': dumps(ret, ensure_ascii=False)}
                    }]
                result_body['messages'] = messages
            except (ValueError, TypeError) as e:
                error = self.create_error(-32603, f"Prompt execution error: {e}")
                return self.create_response(request_id, None, error)
        return self.create_response(request_id, result_body, None)

    # ==== Schema generation ====

    @staticmethod
    def _parse_args_descriptions(doc: str | None) -> dict[str, str]:
        """Parse the Args: section of a docstring into per-param descriptions."""
        if not doc:
            return {}
        lines = doc.splitlines()
        in_args = False
        descs: dict[str, str] = {}
        current_name: str | None = None
        param_indent: int | None = None
        for line in lines:
            stripped = line.strip()
            if stripped in ("Args:", "Arguments:"):
                in_args = True
                continue
            if not in_args:
                continue
            if not stripped:
                continue
            indent = len(line) - len(line.lstrip())
            if indent == 0 and stripped.endswith(":"):
                break
            if param_indent is not None and indent > param_indent:
                if current_name and not stripped.startswith("-"):
                    descs[current_name] += " " + stripped
                continue
            if ":" in stripped and not stripped.startswith("-"):
                colon_pos = stripped.index(":")
                candidate = stripped[:colon_pos].strip()
                if candidate.isidentifier():
                    if param_indent is None:
                        param_indent = indent
                    desc = stripped[colon_pos + 1:].strip()
                    if desc.endswith(":"):
                        desc = desc[:-1].strip()
                    descs[candidate] = desc
                    current_name = candidate
                    continue
            if param_indent is not None and indent <= param_indent:
                break
        return descs

    def _extract_parameters_from_signature(self, sig: Signature, method) -> dict[str, Any]:
        """Extract parameter schema from method signature and type hints."""
        try:
            type_hints = get_type_hints(method)
        except (NameError, AttributeError, TypeError):
            type_hints = {}
        params = [param for name, param in sig.parameters.items() if name != 'self']
        if not params:
            return {}
        doc = getdoc(method)
        arg_descs = self._parse_args_descriptions(doc)
        properties = {}
        required = []
        for param in params:
            # Fall back to the raw annotation when get_type_hints() failed.
            param_type = type_hints.get(param.name, param.annotation)
            prop_schema = self._type_to_json_schema(param_type)
            desc = arg_descs.get(param.name)
            if desc:
                prop_schema["description"] = desc
            properties[param.name] = prop_schema

            if param.default == Parameter.empty and param.name not in required:
                required.append(param.name)

            # Required by documentation hint (e.g. "(REQUIRED)")
            if desc and re.search(r"\brequired\b", desc, flags=re.IGNORECASE):
                if param.name not in required:
                    required.append(param.name)

        schema = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required

        # Markdown input helper: require at least one of inline markdown or markdown_file.
        if "markdown" in properties and "markdown_file" in properties:
            schema["oneOf"] = [
                {"required": ["markdown"]},
                {"required": ["markdown_file"]},
            ]

        return schema

    def _type_to_json_schema(self, param_type: Any) -> dict[str, Any]:
        """Convert Python type annotation to JSON schema property."""
        # Untyped parameter (no annotation) -> default to string for MCP clients.
        if param_type is Parameter.empty:
            return {"type": "string"}
        if param_type is None or param_type is type(None):
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

        origin = get_origin(param_type)
        args = get_args(param_type)

        # Handle Literal types -> JSON Schema enum
        if origin is Literal:
            values = list(args)
            if all(isinstance(v, str) for v in values):
                return {"type": "string", "enum": values}
            elif all(isinstance(v, int) for v in values):
                return {"type": "integer", "enum": values}
            return {"enum": values}

        # Handle Union types (Optional[T], T | None, X | Y, ...)
        is_union = isinstance(param_type, UnionType) or origin is Union
        if is_union:
            non_none_args = [a for a in args if a is not type(None)]
            if len(non_none_args) == 1:
                return self._type_to_json_schema(non_none_args[0])
            elif len(non_none_args) > 1:
                return {"oneOf": [self._type_to_json_schema(a) for a in non_none_args]}

        if origin is list:
            schema: dict[str, Any] = {"type": "array"}
            if args:
                schema["items"] = self._type_to_json_schema(args[0])
            return schema
        elif origin is dict:
            return {"type": "object"}

        # Handle TypedDict classes -> JSON Schema with typed properties
        try:
            if is_typeddict(param_type):
                td_hints = get_type_hints(param_type)
                td_props = {}
                for field_name, field_type in td_hints.items():
                    td_props[field_name] = self._type_to_json_schema(field_type)
                td_schema: dict[str, Any] = {"type": "object", "properties": td_props}
                td_required = list(getattr(param_type, "__required_keys__", td_hints.keys()))
                if td_required:
                    td_schema["required"] = td_required
                return td_schema
        except TypeError:
            pass

        return {"type": "string"}

    def _coerce_value(self, value: Any, param_type: Any) -> Any:
        """Coerce a value to the expected type if needed.

        MCP clients may send numeric values as strings.  This method converts
        them to the expected Python types based on type hints.
        """
        if value is None:
            return None

        actual_type = param_type
        origin = get_origin(param_type)
        is_union = isinstance(param_type, UnionType) or origin is Union

        if is_union:
            args = get_args(param_type)
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                actual_type = non_none[0]
            elif len(non_none) > 1:
                for candidate in non_none:
                    if isinstance(value, candidate):
                        return value
                if isinstance(value, str):
                    for candidate in non_none:
                        if candidate is int:
                            try:
                                return int(value)
                            except ValueError:
                                continue
                        elif candidate is float:
                            try:
                                return float(value)
                            except ValueError:
                                continue
                return value

        if isinstance(value, str):
            if actual_type is float:
                try:
                    return float(value)
                except ValueError:
                    return value
            elif actual_type is int:
                try:
                    return int(value)
                except ValueError:
                    return value
            elif actual_type is bool:
                return value.lower() in ('true', '1', 'yes')

        return value

    # ==== MCP Protocol Core Implementation ====

    def handle_initialize(self, request_id: str | int | None, params: dict[str, Any]) -> dict[str, Any]:
        """Handle MCP initialize method."""
        client_info = params.get('clientInfo', {})
        self.logger.info("Initialize request from client: %s", client_info)
        result = self.get_config()
        return self.create_response(request_id, result, None)

    def handle_tools_list(self, request_id: str | int | None) -> dict[str, Any]:
        """List available tools."""
        result = self.discover_tools()
        return self.create_response(request_id, result, None)

    def handle_tools_call(self, request_id: str | int | None, params: dict[str, Any]) -> dict[str, Any]:
        """Handle tool calls - delegates to tool implementations."""
        tool_name = params.get('name', '')
        arguments = params.get('arguments', {})

        self.logger.info("TOOL CALL: %s with args: %s", tool_name, list(arguments.keys()) if arguments else [])

        if not tool_name:
            error = self.create_error(-32602, "Missing 'name' parameter")
            return self.create_response(request_id, None, error)

        tool_method_name = f"tool_{tool_name}"

        if not hasattr(self, tool_method_name):
            error = self.create_error(-32601, f"Tool not found: {tool_name}")
            return self.create_response(request_id, None, error)

        try:
            method = getattr(self, tool_method_name)
            sig = signature(method)
            self.logger.info("TOOL DISPATCH: %s signature: %s", tool_name, sig)

            params_list = [p for name, p in sig.parameters.items() if name != 'self']

            # Reject unknown parameters to keep tool contracts strict (applies
            # even when the tool takes no parameters at all).
            allowed_params = {
                param_name for param_name in sig.parameters
                if param_name != 'self'
            }
            unknown_params = sorted(
                key for key in arguments
                if key not in allowed_params
            )
            if unknown_params:
                raise ValueError(
                    "Unrecognized parameter(s): " + ", ".join(unknown_params)
                )

            if len(params_list) == 0:
                self.logger.info("TOOL EXEC: %s (no params)", tool_name)
                content = method()
            else:
                try:
                    type_hints = get_type_hints(method)
                except (NameError, AttributeError, TypeError):
                    type_hints = {}

                kwargs = {}
                for param_name, param in sig.parameters.items():
                    if param_name == 'self':
                        continue
                    if param_name in arguments:
                        value = arguments[param_name]
                        if param_name in type_hints:
                            value = self._coerce_value(value, type_hints[param_name])
                        kwargs[param_name] = value
                    elif param.default != Parameter.empty:
                        kwargs[param_name] = param.default
                    else:
                        raise ValueError(f"Required parameter '{param_name}' is missing")

                self.logger.info("TOOL EXEC: %s with kwargs: %s", tool_name, list(kwargs.keys()))
                content = method(**kwargs)

            self.logger.info("TOOL SUCCESS: %s returned type: %s", tool_name, type(content).__name__)

            if content is None:
                error = self.create_error(-32603, f"Tool execution error for {tool_name}")
                return self.create_response(request_id, None, error)

        except Exception as e:
            tb = traceback.format_exc()
            self.logger.error("TOOL ERROR: %s failed with %s: %s\n%s", tool_name, type(e).__name__, e, tb)
            error = self.create_error(
                -32603, f"Tool execution error for {tool_name}: {type(e).__name__}: {str(e)}"
            )
            return self.create_response(request_id, None, error)

        stringified_content = dumps(content) if isinstance(content, (dict, list)) else str(content)
        result = {
            "content": [{
                "type": "text",
                "text": stringified_content
            }]
        }
        return self.create_response(request_id, result, None)

    # ==== JSON-RPC 2.0 Handler ====

    def create_response(self, request_id: str | int | None, result: Any,
                        error: dict[str, Any] | None) -> dict[str, Any]:
        """Create a JSON-RPC 2.0 response."""
        if error is not None:
            response = {"jsonrpc": "2.0", "error": error, "id": request_id}
        else:
            response = {"jsonrpc": "2.0", "result": result, "id": request_id}
        self.logger.info("RESPONSE: %s", dumps(response))
        return response

    def create_error(self, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
        """Create a JSON-RPC 2.0 error."""
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return error

    def process_request(self, input_data: str) -> dict[str, Any] | None:
        """Process a JSON-RPC 2.0 request."""
        try:
            request = loads(input_data)
        except JSONDecodeError as e:
            self.logger.error("Invalid JSON: %s", e)
            error = self.create_error(-32700, "Parse error")
            return self.create_response(None, None, error)

        jsonrpc = request.get('jsonrpc')
        request_id = request.get('id')
        method = request.get('method')
        params = request.get('params', {})

        self.logger.info("Processing method: %s (id: %s)", method, request_id)

        if jsonrpc != "2.0":
            error = self.create_error(-32600, "Invalid Request: Not a JSON-RPC 2.0 request")
            return self.create_response(request_id, None, error)

        if method == "initialize":
            return self.handle_initialize(request_id, params)
        elif method == "tools/list":
            return self.handle_tools_list(request_id)
        elif method == "tools/call":
            return self.handle_tools_call(request_id, params)
        elif method == "prompts/list":
            result = self.discover_prompts()
            return self.create_response(request_id, result, None)
        elif method == "prompts/get":
            return self.handle_prompt_get(request_id, params)
        elif method == "notifications/initialized":
            self.logger.info("Host confirmed toolContract reception with 'notifications/initialized'")
            return None
        else:
            error = self.create_error(-32601, f"Method not found: {method}")
            return self.create_response(request_id, None, error)

    # ==== TCP transport ====

    def run_socket(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Run the MCP server over TCP sockets.

        Listens on *host*:*port* for newline-delimited JSON-RPC connections.
        Each accepted connection is handled in its own thread.  When *port*
        is 0 the OS assigns an ephemeral port, which is printed to stdout so
        the caller can discover it.
        """
        server_self = self

        class _Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                peer = self.client_address
                server_self.logger.info("Socket client connected: %s", peer)
                try:
                    while True:
                        line = self.rfile.readline()
                        if not line:
                            break
                        decoded = line.decode("utf-8", errors="replace").strip()
                        if not decoded:
                            continue
                        server_self.logger.info("SOCKET REQUEST (%s): %s", peer, decoded)
                        response = server_self.process_request(decoded)
                        if response is not None:
                            payload = (dumps(response) + "\n").encode("utf-8")
                            self.wfile.write(payload)
                            self.wfile.flush()
                except Exception as e:  # noqa: BLE001
                    server_self.logger.error("Socket client %s error: %s", peer, e)
                finally:
                    server_self.logger.info("Socket client disconnected: %s", peer)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        with _Server((host, port), _Handler) as srv:
            actual_host, actual_port = srv.server_address[:2]
            self.logger.info("MCP Socket Server listening on %s:%s", actual_host, actual_port)
            print(f"Listening on {actual_host}:{actual_port}", flush=True)
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                self.logger.info("MCP Socket Server stopped.")

    # ==== SSE (Server-Sent Events) HTTP transport ====

    def run_sse(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Run the MCP server over HTTP with the SSE transport.

        Implements the MCP SSE transport protocol:
          GET  /sse       -> event stream (Content-Type: text/event-stream)
          POST /message   -> JSON-RPC request, response pushed to the SSE stream

        VS Code mcp.json example::

            { "type": "sse", "url": "http://127.0.0.1:<port>/sse" }
        """
        server_self = self

        class _Handler(BaseHTTPRequestHandler):
            # Quieter access logs; we already log requests through our logger.
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                server_self.logger.debug("HTTP %s - " + format, self.address_string(), *args)

            def _write_cors_preflight(self) -> None:
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def do_OPTIONS(self) -> None:  # noqa: N802
                self._write_cors_preflight()

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/sse":
                    self.send_error(404, "Not Found")
                    return

                session_id = str(uuid4())
                queue: Queue = Queue()
                with server_self._sse_lock:
                    server_self._sse_sessions[session_id] = queue
                server_self.logger.info("SSE: new session %s from %s", session_id, self.client_address)

                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()

                    endpoint_url = f"/message?sessionId={session_id}"
                    self.wfile.write(f"event: endpoint\ndata: {endpoint_url}\n\n".encode())
                    self.wfile.flush()

                    while True:
                        try:
                            payload = queue.get(timeout=15)
                        except Empty:
                            try:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                break
                            continue
                        if payload is None:
                            break
                        try:
                            self.wfile.write(payload)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            break
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with server_self._sse_lock:
                        server_self._sse_sessions.pop(session_id, None)
                    server_self.logger.info("SSE: session %s closed", session_id)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != "/message":
                    self.send_error(404, "Not Found")
                    return

                qs = parse_qs(parsed.query)
                session_id = (qs.get("sessionId") or [None])[0]
                with server_self._sse_lock:
                    queue = server_self._sse_sessions.get(session_id) if session_id else None

                if queue is None:
                    server_self.logger.warning(
                        "SSE: unknown session %s from %s", session_id, self.client_address
                    )
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b""
                request_data = body.decode("utf-8", errors="replace")
                server_self.logger.info("SSE REQUEST (session %s): %s", session_id, request_data[:200])

                response = server_self.process_request(request_data)
                if response is not None:
                    response_json = dumps(response)
                    queue.put(f"event: message\ndata: {response_json}\n\n".encode())

                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

        httpd = ThreadingHTTPServer((host, port), _Handler)
        httpd.daemon_threads = True
        actual_host, actual_port = httpd.server_address[:2]
        self.logger.info("MCP SSE Server listening on http://%s:%s/sse", actual_host, actual_port)
        print(f"MCP SSE Server listening on http://{actual_host}:{actual_port}/sse", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            self.logger.info("MCP SSE Server stopped.")
        finally:
            httpd.server_close()

    # ==== Main execution ====

    def run(self, args: list[str] | None = None) -> None:
        """Run the MCP server."""
        if args is None:
            args = argv[1:]

        # ---- Parse --port / --host / --tcp flags ----
        port: int | None = None
        host: str = "127.0.0.1"
        use_tcp: bool = False
        remaining: list[str] = []
        i = 0
        while i < len(args):
            if args[i] in ("--port", "-p") and i + 1 < len(args):
                port = int(args[i + 1])
                i += 2
            elif args[i] == "--host" and i + 1 < len(args):
                host = args[i + 1]
                i += 2
            elif args[i] == "--tcp":
                use_tcp = True
                i += 1
            else:
                remaining.append(args[i])
                i += 1

        if port is not None:
            if use_tcp:
                self.run_socket(host=host, port=port)
            else:
                self.run_sse(host=host, port=port)
            return

        # ---- Original stdio / file transport ----
        args = remaining

        if args:
            try:
                with open(args[0], encoding='utf-8') as f:
                    input_data = f.read()
                self.logger.info("REQUEST: %s", input_data)
                response = self.process_request(input_data)
                if response is not None:
                    payload = (dumps(response) + "\n").encode("utf-8")
                    _stdout_bin.write(payload)
                    _stdout_bin.flush()
            except OSError as e:
                self.logger.error("Error reading file %s: %s", args[0], e)
                exit(1)
        else:
            # Continuously read from stdin line by line via the binary buffer.
            self.logger.info("MCP Server started. Waiting for JSON-RPC 2.0 messages...")
            try:
                while True:
                    raw = _stdin_bin.readline()
                    if not raw:  # EOF
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    self.logger.info("REQUEST: %s", line)
                    response = self.process_request(line)
                    if response is not None:
                        payload = (dumps(response) + "\n").encode("utf-8")
                        _stdout_bin.write(payload)
                        _stdout_bin.flush()
            except KeyboardInterrupt:
                self.logger.info("MCP Server stopped.")
                exit(0)
            except EOFError:
                self.logger.info("MCP Server finished processing input.")
                exit(0)


if __name__ == "__main__":
    server = MCPServer()
    server.run()
