#!/usr/bin/env python3
"""
aioumcp.py - Async MCP (Model Context Protocol) server implementation
Handles JSON-RPC 2.0 messaging and MCP protocol infrastructure with asyncio support.

Transports:
  stdio  (default)       – reads newline-delimited JSON-RPC from stdin, writes to stdout.
  sse   (--port N)       – HTTP server on 127.0.0.1:N implementing the MCP SSE
                            transport (GET /sse for the event stream, POST /message
                            for JSON-RPC requests).  Compatible with VS Code and
                            other MCP clients that use the "sse" transport type.
  socket (--port N --tcp) – raw TCP with newline-delimited JSON-RPC.
                            Legacy mode; use --tcp flag to opt in.
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

# Regardless of platform, grab the raw binary buffers for stdio.
# Using .buffer bypasses Python's text-mode buffering layer entirely.
_stdin_bin  = _sys.stdin.buffer
_stdout_bin = _sys.stdout.buffer

from asyncio import (
    CancelledError,
    Event,
    StreamReader,
    StreamWriter,
    get_event_loop,
    run,
    sleep,
    start_server,
)
from base64 import b64encode
import re
from inspect import (
    Parameter,
    Signature,
    getdoc,
    getmembers,
    iscoroutinefunction,
    ismethod,
    signature,
)
from json import JSONDecodeError, dumps, loads
from logging import INFO, FileHandler, basicConfig, getLogger
from pathlib import Path
from sys import argv, exit
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints, is_typeddict
from urllib.parse import parse_qs, urlparse
from uuid import uuid4


class AsyncMCPServer:
    """Async MCP server implementation using JSON-RPC 2.0 protocol with asyncio."""

    def __init__(self):
        # Get the directory where the script is located
        self.script_dir = Path(__file__).parent.absolute()
        self.log_file = self.script_dir / "mcpserver.log"

        # Set up logging
        self._setup_logging()

        # Resource state.
        self._resource_subscriptions: set[str] = set()
        self._dynamic_resources: dict[str, tuple[dict[str, Any], Any]] = {}
        self._dynamic_resource_templates: list[tuple[dict[str, Any], Any]] = []
        # SSE session map -- populated by run_sse_async; declared here so
        # notification helpers can introspect it from any context.
        self._sse_sessions: dict[str, tuple[StreamWriter, Event]] = {}

    def _setup_logging(self) -> None:
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

    def get_config(self) -> dict[str, Any]:
        """Generate server configuration dynamically.

        Ports newer functionality from the synchronous server, including:
          * Updated protocolVersion
          * Prompt capabilities
          * Dynamic instructions string
        """
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
                },
                "resources": {
                    "subscribe": True,
                    "listChanged": True,
                }
            },
            "instructions": self.get_instructions()
        }

    def get_instructions(self) -> str:
        """Get server-specific instructions. Override in subclasses."""
        return "Base MCP server with dynamic tool discovery."

    def discover_tools(self) -> dict[str, Any]:
        """Discover tools by introspecting methods that start with 'tool_'.

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
        # Explicit override on the method itself
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
        is_destructive = any(tool_name.startswith(p) or f"_{p[:-1]}" in tool_name for p in DESTRUCTIVE_PREFIXES)
        is_open_world = any(tool_name.startswith(p) for p in OPEN_WORLD_PREFIXES)

        annotations: dict[str, bool] = {}
        if is_read_only:
            annotations["readOnlyHint"] = True
            annotations["destructiveHint"] = False
        elif is_destructive:
            annotations["readOnlyHint"] = False
            annotations["destructiveHint"] = True
        else:
            # Mutating but not destructive (patch, add, create, set, etc.)
            annotations["readOnlyHint"] = False
            annotations["destructiveHint"] = False
        if is_open_world:
            annotations["openWorldHint"] = True
        return annotations

    # --- Prompt discovery & handling (ported from synchronous server) ---
    def discover_prompts(self) -> dict[str, Any]:
        """Discover available prompts through introspection.

        A prompt is any method whose name starts with 'prompt_'. Its docstring
        becomes the description and its signature is converted to a JSON schema.
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
        import re
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

    async def handle_prompt_get_async(self, request_id: str | int | None, params: dict[str, Any]) -> dict[str, Any]:
        """Async handler for ``prompts/get`` supporting sync or async methods."""
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

        allowed_params = {p_name for p_name in sig.parameters if p_name != 'self'}
        unknown_params = sorted(key for key in arguments if key not in allowed_params)
        if unknown_params:
            error = self.create_error(
                -32602, "Unrecognized parameter(s): " + ", ".join(unknown_params)
            )
            return self.create_response(request_id, None, error)

        try:
            type_hints = get_type_hints(method)
        except (NameError, AttributeError, TypeError):
            type_hints = {}

        try:
            kwargs = {}
            for p_name, p in sig.parameters.items():
                if p_name == 'self':
                    continue
                if p_name in arguments:
                    value = arguments[p_name]
                    if p_name in type_hints:
                        value = self._coerce_value(value, type_hints[p_name])
                    kwargs[p_name] = value
                elif p.default != Parameter.empty:
                    kwargs[p_name] = p.default
                else:
                    raise ValueError(f"Missing required argument '{p_name}' for prompt {prompt_name}")
        except ValueError as e:
            error = self.create_error(-32602, str(e))
            return self.create_response(request_id, None, error)

        try:
            if iscoroutinefunction(method):
                ret = await method(**kwargs)
            else:
                ret = await get_event_loop().run_in_executor(None, lambda: method(**kwargs))
        except Exception as e:  # noqa: BLE001
            error = self.create_error(-32603, f"Prompt execution error: {e}")
            return self.create_response(request_id, None, error)

        if isinstance(ret, str):
            result_body['messages'] = [{'role': 'user', 'content': {'type': 'text', 'text': ret}}]
        elif isinstance(ret, list) and all(isinstance(m, dict) and 'role' in m and 'content' in m for m in ret):
            result_body['messages'] = ret
        elif isinstance(ret, dict):
            if 'messages' in ret and isinstance(ret['messages'], list):
                merged = dict(ret)
                merged.setdefault('description', doc)
                if categories and 'categories' not in merged:
                    merged['categories'] = categories
                result_body = merged
            else:
                result_body['messages'] = [{
                    'role': 'user',
                    'content': {'type': 'text', 'text': dumps(ret, ensure_ascii=False)}
                }]
        else:
            result_body['messages'] = [{
                'role': 'user',
                'content': {'type': 'text', 'text': dumps(ret, ensure_ascii=False)}
            }]
        return self.create_response(request_id, result_body, None)

    @staticmethod
    def _parse_args_descriptions(doc: str | None) -> dict[str, str]:
        """Parse the Args: section of a docstring to extract parameter descriptions.

        Returns a dict mapping parameter name to its one-line description.
        Multi-line continuations (indented deeper) are folded into the
        preceding parameter's description.
        """
        if not doc:
            return {}
        lines = doc.splitlines()
        in_args = False
        descs: dict[str, str] = {}
        current_name: str | None = None
        # Detect the indentation of the first param line to distinguish
        # param lines from continuation/sub-list lines.
        param_indent: int | None = None
        for line in lines:
            stripped = line.strip()
            if stripped in ("Args:", "Arguments:"):
                in_args = True
                continue
            if not in_args:
                continue
            # Blank line inside Args is OK (may separate groups)
            if not stripped:
                continue
            indent = len(line) - len(line.lstrip())
            # Section headers like "Returns:" are at indent 0
            if indent == 0 and stripped.endswith(":"):
                break
            # Once we know the param indent, lines indented deeper are continuations
            if param_indent is not None and indent > param_indent:
                # Sub-list / continuation — fold into current param description
                if current_name and not stripped.startswith("-"):
                    descs[current_name] += " " + stripped
                continue
            # Try to parse as "param_name: description"
            if ":" in stripped and not stripped.startswith("-"):
                colon_pos = stripped.index(":")
                candidate = stripped[:colon_pos].strip()
                if candidate.isidentifier():
                    if param_indent is None:
                        param_indent = indent
                    desc = stripped[colon_pos + 1:].strip()
                    # Remove trailing colon that introduces a sub-list
                    if desc.endswith(":"):
                        desc = desc[:-1].strip()
                    descs[candidate] = desc
                    current_name = candidate
                    continue
            # Unknown line at param indent level — stop parsing
            if param_indent is not None and indent <= param_indent:
                break
        return descs

    def _extract_parameters_from_signature(self, sig: Signature, method) -> dict[str, Any]:
        """Extract parameter schema from method signature and type hints (parity with sync server)."""
        try:
            type_hints = get_type_hints(method)
        except (NameError, AttributeError, TypeError):
            type_hints = {}
        params = [param for name, param in sig.parameters.items() if name != 'self']
        if not params:
            return {}
        # Parse Args: descriptions from docstring
        doc = getdoc(method)
        arg_descs = self._parse_args_descriptions(doc)
        properties = {}
        required = []
        for param in params:
            # Fall back to the raw annotation when get_type_hints() failed (e.g.
            # forward refs that don't resolve at import time).
            param_type = type_hints.get(param.name, param.annotation)
            prop_schema = self._type_to_json_schema(param_type)
            # Add description from docstring if available
            desc = arg_descs.get(param.name)
            if desc:
                prop_schema["description"] = desc
            properties[param.name] = prop_schema

            # Required by signature
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

        # Handle Literal types → JSON Schema enum
        if origin is Literal:
            values = list(args)
            # Infer type from the literal values
            if all(isinstance(v, str) for v in values):
                return {"type": "string", "enum": values}
            elif all(isinstance(v, int) for v in values):
                return {"type": "integer", "enum": values}
            return {"enum": values}

        # Handle Union types (e.g., Optional[str], str | None, str | int | None)
        is_union = isinstance(param_type, UnionType) or origin is Union
        if is_union:
            # Strip None from the union to find the real types
            non_none_args = [a for a in args if a is not type(None)]
            if len(non_none_args) == 1:
                # Optional[T] — just return T's schema
                return self._type_to_json_schema(non_none_args[0])
            elif len(non_none_args) > 1:
                # Multi-type union (e.g., str | int) — use oneOf
                return {"oneOf": [self._type_to_json_schema(a) for a in non_none_args]}

        # Handle generic types like List[str], Dict[str, Any]
        if origin is list:
            schema: dict[str, Any] = {"type": "array"}
            if args:
                schema["items"] = self._type_to_json_schema(args[0])
            return schema
        elif origin is dict:
            return {"type": "object"}

        # Handle TypedDict classes → JSON Schema with typed properties
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

        # Default to string for unknown types
        return {"type": "string"}

    def _coerce_value(self, value: Any, param_type: Any) -> Any:
        """Coerce a value to the expected type if needed.

        MCP clients may send numeric values as strings. This method converts
        them to the expected Python types based on type hints.
        """
        if value is None:
            return None

        # Get the actual type for Optional/Union types
        actual_type = param_type
        origin = get_origin(param_type)
        is_union = isinstance(param_type, UnionType) or origin is Union

        if is_union:
            args = get_args(param_type)
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                actual_type = non_none[0]
            elif len(non_none) > 1:
                # Multi-type union (e.g., str | int) — try each non-None type
                for candidate in non_none:
                    if isinstance(value, candidate):
                        return value
                # If value is a string, try coercing to the first numeric type
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

        # Coerce string to numeric types
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

    # ==== Resource discovery & handling ====
    #
    # Mirrors the synchronous implementation in umcp.py.  Resource methods
    # may be ``def`` or ``async def``; the read handler awaits coroutine
    # results.  See umcp.py for the full doc on naming, return-type
    # normalisation, URI templates, and the optional ``_mcp_resource``
    # override attribute.

    _DEFAULT_TEXT_MIME = "text/plain"
    _DEFAULT_BINARY_MIME = "application/octet-stream"

    @staticmethod
    def _resource_uri_template_to_regex(template: str) -> tuple[Any, list[str]]:
        names: list[str] = []
        pattern_parts: list[str] = []
        cursor = 0
        for m in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template):
            pattern_parts.append(re.escape(template[cursor:m.start()]))
            names.append(m.group(1))
            pattern_parts.append(f"(?P<{m.group(1)}>[^/]+)")
            cursor = m.end()
        pattern_parts.append(re.escape(template[cursor:]))
        return re.compile("^" + "".join(pattern_parts) + "$"), names

    def _default_resource_uri(self, name: str) -> str:
        return f"umcp://{self.__class__.__name__}/{name}"

    def _default_resource_template_uri(self, name: str, params: list[str]) -> str:
        slots = "/".join("{" + p + "}" for p in params)
        base = f"umcp://{self.__class__.__name__}/{name}"
        return f"{base}/{slots}" if slots else base

    def _resource_metadata(self, name: str, method: Any, params: list[str]) -> dict[str, Any]:
        override = (
            getattr(method, "_mcp_resource", None)
            or getattr(method, "_mcp_resource_template", None)
            or {}
        )
        doc = (getdoc(method) or "").strip()
        description = override.get("description") or (doc.splitlines()[0] if doc else None)
        meta: dict[str, Any] = {"name": override.get("name", name)}
        if params:
            meta["uriTemplate"] = override.get("uri_template") or self._default_resource_template_uri(name, params)
        else:
            meta["uri"] = override.get("uri") or self._default_resource_uri(name)
        if override.get("title"):
            meta["title"] = override["title"]
        if description:
            meta["description"] = description
        if override.get("mime_type"):
            meta["mimeType"] = override["mime_type"]
        if override.get("size") is not None:
            meta["size"] = override["size"]
        if override.get("annotations"):
            meta["annotations"] = dict(override["annotations"])
        return meta

    def _iter_resource_methods(self) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        for member_name, method in getmembers(self, predicate=ismethod):
            if member_name.startswith("resource_template_"):
                continue
            if not member_name.startswith("resource_"):
                continue
            out.append((member_name[len("resource_"):], method))
        return out

    def _iter_resource_template_methods(self) -> list[tuple[str, Any, list[str]]]:
        out: list[tuple[str, Any, list[str]]] = []
        for member_name, method in getmembers(self, predicate=ismethod):
            if not member_name.startswith("resource_template_"):
                continue
            sig = signature(method)
            params = [
                p.name for p in sig.parameters.values()
                if p.kind in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY)
            ]
            out.append((member_name[len("resource_template_"):], method, params))
        return out

    def discover_resources(self) -> dict[str, Any]:
        """Return the ``resources/list`` payload for static resources."""
        resources: list[dict[str, Any]] = []
        for name, method in self._iter_resource_methods():
            resources.append(self._resource_metadata(name, method, params=[]))
        for meta, _callable in self._dynamic_resources.values():
            resources.append(dict(meta))
        return {"resources": resources}

    def discover_resource_templates(self) -> dict[str, Any]:
        """Return the ``resources/templates/list`` payload."""
        templates: list[dict[str, Any]] = []
        for name, method, params in self._iter_resource_template_methods():
            templates.append(self._resource_metadata(name, method, params=params))
        for meta, _callable in self._dynamic_resource_templates:
            templates.append(dict(meta))
        return {"resourceTemplates": templates}

    def register_resource(
        self,
        uri: str,
        callable_: Any,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        mime_type: str | None = None,
        size: int | None = None,
        annotations: dict[str, Any] | None = None,
    ) -> None:
        meta: dict[str, Any] = {"uri": uri, "name": name or uri}
        if title:
            meta["title"] = title
        if description:
            meta["description"] = description
        if mime_type:
            meta["mimeType"] = mime_type
        if size is not None:
            meta["size"] = size
        if annotations:
            meta["annotations"] = dict(annotations)
        self._dynamic_resources[uri] = (meta, callable_)

    def register_resource_template(
        self,
        uri_template: str,
        callable_: Any,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        mime_type: str | None = None,
        annotations: dict[str, Any] | None = None,
    ) -> None:
        meta: dict[str, Any] = {"uriTemplate": uri_template, "name": name or uri_template}
        if title:
            meta["title"] = title
        if description:
            meta["description"] = description
        if mime_type:
            meta["mimeType"] = mime_type
        if annotations:
            meta["annotations"] = dict(annotations)
        self._dynamic_resource_templates.append((meta, callable_))

    def _normalise_resource_content(
        self, uri: str, default_mime: str | None, value: Any
    ) -> list[dict[str, Any]]:
        if isinstance(value, (bytes, bytearray, memoryview)):
            data = bytes(value)
            return [{
                "uri": uri,
                "mimeType": default_mime or self._DEFAULT_BINARY_MIME,
                "blob": b64encode(data).decode("ascii"),
            }]
        if isinstance(value, str):
            return [{
                "uri": uri,
                "mimeType": default_mime or self._DEFAULT_TEXT_MIME,
                "text": value,
            }]
        if isinstance(value, dict):
            entry = dict(value)
            entry.setdefault("uri", uri)
            if default_mime and "mimeType" not in entry:
                entry["mimeType"] = default_mime
            return [entry]
        if isinstance(value, list):
            out = []
            for item in value:
                if isinstance(item, dict):
                    entry = dict(item)
                    entry.setdefault("uri", uri)
                    if default_mime and "mimeType" not in entry:
                        entry["mimeType"] = default_mime
                    out.append(entry)
                else:
                    out.extend(self._normalise_resource_content(uri, default_mime, item))
            return out
        return [{
            "uri": uri,
            "mimeType": default_mime or self._DEFAULT_TEXT_MIME,
            "text": str(value),
        }]

    def handle_resources_list(
        self, request_id: int | str | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        return self.create_response(request_id, self.discover_resources())

    def handle_resources_templates_list(
        self, request_id: int | str | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        return self.create_response(request_id, self.discover_resource_templates())

    async def _maybe_await(self, value: Any) -> Any:
        if iscoroutinefunction(getattr(value, "__call__", None)):  # safety
            value = value()
        if hasattr(value, "__await__"):
            return await value
        return value

    async def handle_resources_read_async(
        self, request_id: int | str | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle ``resources/read`` -- supports both sync and async resource methods."""
        uri = params.get("uri")
        if not uri:
            error = self.create_error(-32602, "Missing 'uri' parameter")
            return self.create_response(request_id, None, error)

        async def call(method: Any, **kwargs: Any) -> Any:
            result = method(**kwargs)
            if hasattr(result, "__await__"):
                result = await result
            return result

        for name, method in self._iter_resource_methods():
            meta = self._resource_metadata(name, method, params=[])
            if meta["uri"] == uri:
                try:
                    value = await call(method)
                except Exception as exc:  # noqa: BLE001
                    self.logger.exception("Resource %s raised", uri)
                    return self.create_response(
                        request_id, None,
                        self.create_error(-32603, f"Resource read failed: {exc}"),
                    )
                contents = self._normalise_resource_content(uri, meta.get("mimeType"), value)
                return self.create_response(request_id, {"contents": contents})

        if uri in self._dynamic_resources:
            meta, fn = self._dynamic_resources[uri]
            try:
                value = await call(fn)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Resource %s raised", uri)
                return self.create_response(
                    request_id, None,
                    self.create_error(-32603, f"Resource read failed: {exc}"),
                )
            contents = self._normalise_resource_content(uri, meta.get("mimeType"), value)
            return self.create_response(request_id, {"contents": contents})

        for name, method, params_list in self._iter_resource_template_methods():
            meta = self._resource_metadata(name, method, params=params_list)
            regex, _ = self._resource_uri_template_to_regex(meta["uriTemplate"])
            m = regex.match(uri)
            if m:
                try:
                    value = await call(method, **m.groupdict())
                except Exception as exc:  # noqa: BLE001
                    self.logger.exception("Resource template %s raised", uri)
                    return self.create_response(
                        request_id, None,
                        self.create_error(-32603, f"Resource read failed: {exc}"),
                    )
                contents = self._normalise_resource_content(uri, meta.get("mimeType"), value)
                return self.create_response(request_id, {"contents": contents})

        for meta, fn in self._dynamic_resource_templates:
            regex, _ = self._resource_uri_template_to_regex(meta["uriTemplate"])
            m = regex.match(uri)
            if m:
                try:
                    value = await call(fn, **m.groupdict())
                except Exception as exc:  # noqa: BLE001
                    self.logger.exception("Resource template %s raised", uri)
                    return self.create_response(
                        request_id, None,
                        self.create_error(-32603, f"Resource read failed: {exc}"),
                    )
                contents = self._normalise_resource_content(uri, meta.get("mimeType"), value)
                return self.create_response(request_id, {"contents": contents})

        return self.create_response(
            request_id, None,
            self.create_error(-32002, "Resource not found", data={"uri": uri}),
        )

    def handle_resources_subscribe(
        self, request_id: int | str | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        uri = params.get("uri")
        if not uri:
            return self.create_response(
                request_id, None,
                self.create_error(-32602, "Missing 'uri' parameter"),
            )
        self._resource_subscriptions.add(uri)
        return self.create_response(request_id, {})

    def handle_resources_unsubscribe(
        self, request_id: int | str | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        uri = params.get("uri")
        if not uri:
            return self.create_response(
                request_id, None,
                self.create_error(-32602, "Missing 'uri' parameter"),
            )
        self._resource_subscriptions.discard(uri)
        return self.create_response(request_id, {})

    async def _send_notification_async(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        """Emit a JSON-RPC notification on the active transport.

        SSE: write to every connected session's StreamWriter.
        Stdio / TCP / file mode: write one newline-delimited message to stdout.
        """
        notification: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            notification["params"] = params
        payload_text = dumps(notification)

        if self._sse_sessions:
            sse_blob = f"event: message\ndata: {payload_text}\n\n".encode()
            for sid, (writer, _evt) in list(self._sse_sessions.items()):
                try:
                    writer.write(sse_blob)
                    await writer.drain()
                except Exception:  # noqa: BLE001
                    self.logger.debug("Dropping notification for stale SSE session %s", sid)
            return

        try:
            _stdout_bin.write((payload_text + "\n").encode("utf-8"))
            _stdout_bin.flush()
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed to emit notification %s", method)

    async def notify_resource_list_changed(self) -> None:
        """Tell connected clients the resource list has changed."""
        await self._send_notification_async("notifications/resources/list_changed")

    async def notify_resource_updated(self, uri: str) -> None:
        """Tell subscribed clients that *uri* has changed."""
        if uri in self._resource_subscriptions:
            await self._send_notification_async(
                "notifications/resources/updated", {"uri": uri},
            )

    # ==== Protocol handlers ====

    def handle_initialize(self, request_id: int, params: dict[str, Any]) -> dict[str, Any]:
        """Handle initialize request."""
        _ = params  # Acknowledge params parameter
        config = self.get_config()
        return self.create_response(request_id, config)

    def handle_tools_list(self, request_id: int) -> dict[str, Any]:
        """Handle tools/list request."""
        tools_info = self.discover_tools()
        return self.create_response(request_id, tools_info)

    async def handle_tools_call_async(self, request_id: int, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``tools/call`` asynchronously."""
        import traceback

        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        self.logger.info("TOOL CALL: %s with args: %s", tool_name, list(arguments.keys()) if arguments else [])

        if not tool_name:
            error = self.create_error(-32602, "Missing 'name' parameter")
            return self.create_response(request_id, None, error)

        method_name = f"tool_{tool_name}"
        method = getattr(self, method_name, None)
        if not method or not callable(method):
            error = self.create_error(-32601, f"Tool not found: {tool_name}")
            return self.create_response(request_id, None, error)

        sig = signature(method)
        self.logger.info("TOOL DISPATCH: %s signature: %s", tool_name, sig)

        params_list = [p for name, p in sig.parameters.items() if name != 'self']
        allowed_params = {
            param_name for param_name in sig.parameters
            if param_name != 'self'
        }
        unknown_params = sorted(
            key for key in arguments
            if key not in allowed_params
        )
        if unknown_params:
            error = self.create_error(
                -32602, "Unrecognized parameter(s): " + ", ".join(unknown_params)
            )
            return self.create_response(request_id, None, error)

        try:
            type_hints = get_type_hints(method)
        except (NameError, AttributeError, TypeError):
            type_hints = {}

        try:
            if len(params_list) == 0:
                self.logger.info("TOOL EXEC: %s (no params, async=%s)", tool_name, iscoroutinefunction(method))
                if iscoroutinefunction(method):
                    content = await method()
                else:
                    content = await get_event_loop().run_in_executor(None, method)
            else:
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

                self.logger.info("TOOL EXEC: %s with kwargs: %s (async=%s)", tool_name, list(kwargs.keys()), iscoroutinefunction(method))
                if iscoroutinefunction(method):
                    content = await method(**kwargs)
                else:
                    content = await get_event_loop().run_in_executor(
                        None, lambda: method(**kwargs)
                    )
        except ValueError as e:
            return self.create_response(request_id, None, self.create_error(-32602, str(e)))
        except Exception as e:
            tb = traceback.format_exc()
            self.logger.error("TOOL ERROR: %s failed with %s: %s\n%s", tool_name, type(e).__name__, e, tb)
            error = self.create_error(-32603, f"Tool execution error for {tool_name}: {type(e).__name__}: {str(e)}")
            return self.create_response(request_id, None, error)

        self.logger.info("TOOL SUCCESS: %s returned type: %s", tool_name, type(content).__name__)

        if isinstance(content, str):
            stringified_content = content
        else:
            try:
                stringified_content = dumps(content, ensure_ascii=False)
            except TypeError:
                stringified_content = str(content)

        result = {
            "content": [{
                "type": "text",
                "text": stringified_content
            }]
        }

        return self.create_response(request_id, result)

    # ==== JSON-RPC utilities ====

    def create_response(self, request_id: int | str | None, result: dict[str, Any] | None = None,
                        error: dict[str, Any] | None = None) -> dict[str, Any]:
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

    def create_error(self, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
        """Create a JSON-RPC 2.0 error object."""
        error = {
            "code": code,
            "message": message
        }
        if data is not None:
            error["data"] = data
        return error

    async def process_request_async(self, request_data: str) -> dict[str, Any] | None:
        """Process a JSON-RPC 2.0 request asynchronously."""
        try:
            request = loads(request_data)
        except JSONDecodeError as e:
            self.logger.error("JSON decode error: %s", e)
            error = self.create_error(-32700, "Parse error")
            return self.create_response(None, None, error)

        if not isinstance(request, dict):
            error = self.create_error(-32600, "Invalid Request: top-level JSON value must be an object")
            return self.create_response(None, None, error)

        # Extract request components
        jsonrpc = request.get("jsonrpc")
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")

        if params is None:
            params = {}
        elif not isinstance(params, dict):
            return self.create_response(
                request_id, None, self.create_error(-32602, "Invalid params: expected an object")
            )

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
        elif method == "prompts/list":
            result = self.discover_prompts()
            return self.create_response(request_id, result)
        elif method == "prompts/get":
            return await self.handle_prompt_get_async(request_id, params)
        elif method == "resources/list":
            return self.handle_resources_list(request_id, params)
        elif method == "resources/templates/list":
            return self.handle_resources_templates_list(request_id, params)
        elif method == "resources/read":
            return await self.handle_resources_read_async(request_id, params)
        elif method == "resources/subscribe":
            return self.handle_resources_subscribe(request_id, params)
        elif method == "resources/unsubscribe":
            return self.handle_resources_unsubscribe(request_id, params)
        elif method == "notifications/initialized":
            self.logger.info("Host confirmed toolContract reception with 'notifications/initialized'")
            return None
        else:
            error = self.create_error(-32601, f"Method not found: {method}")
            return self.create_response(request_id, None, error)

    # ==== Main execution ====

    async def _handle_socket_client(self, reader: StreamReader, writer: StreamWriter) -> None:
        """Handle a single TCP client connection with newline-delimited JSON-RPC."""
        peer = writer.get_extra_info("peername")
        self.logger.info("Socket client connected: %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:  # EOF / disconnect
                    break

                line = line.decode().strip()
                if not line:
                    continue

                self.logger.info("SOCKET REQUEST (%s): %s", peer, line)
                response = await self.process_request_async(line)

                if response is not None:
                    payload = dumps(response) + "\n"
                    writer.write(payload.encode())
                    await writer.drain()
        except CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 broad for runtime resilience
            self.logger.error("Socket client %s error: %s", peer, e)
        finally:
            self.logger.info("Socket client disconnected: %s", peer)
            writer.close()

    async def run_socket_async(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Run the MCP server over TCP sockets.

        Listens on *host*:*port* for newline-delimited JSON-RPC connections.
        Each accepted connection is handled concurrently.  When *port* is 0
        the OS assigns an ephemeral port, which is printed to stdout so the
        caller can discover it.
        """
        server = await start_server(self._handle_socket_client, host, port)
        addrs = [s.getsockname() for s in server.sockets]
        for addr in addrs:
            self.logger.info("MCP Socket Server listening on %s:%s", addr[0], addr[1])
            # Print the address to stdout so the parent process can discover it.
            print(f"Listening on {addr[0]}:{addr[1]}", flush=True)

        async with server:
            await server.serve_forever()

    # ---- SSE (Server-Sent Events) HTTP transport ----

    async def _sse_read_http_request(self, reader: StreamReader) -> tuple[str, str, dict[str, str], bytes]:
        """Read and parse an HTTP/1.1 request from *reader*.

        Returns (method, path, headers_dict, body_bytes).
        """
        request_line = await reader.readline()
        if not request_line:
            raise ConnectionError("Client disconnected")
        parts = request_line.decode("utf-8", errors="replace").strip().split(" ", 2)
        if len(parts) < 2:
            raise ValueError(f"Malformed request line: {request_line!r}")
        method, path = parts[0], parts[1]

        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            decoded = line.decode("utf-8", errors="replace")
            key, _, value = decoded.partition(":")
            headers[key.strip().lower()] = value.strip()

        body = b""
        content_length = int(headers.get("content-length", 0))
        if content_length > 0:
            body = await reader.readexactly(content_length)

        return method, path, headers, body

    async def _sse_handle_client(self, reader: StreamReader, writer: StreamWriter) -> None:
        """Handle a single HTTP connection for the SSE transport.

        Routes:
          GET  /sse       - SSE stream; sends ``endpoint`` event with POST URL.
          POST /message   - JSON-RPC request; response pushed via the SSE stream.
          OPTIONS *       - CORS preflight (permissive).
          *               - 404.
        """
        peer = writer.get_extra_info("peername")
        try:
            method, path, headers, body = await self._sse_read_http_request(reader)
        except (ConnectionError, ValueError) as exc:
            self.logger.warning("SSE: bad request from %s: %s", peer, exc)
            writer.close()
            return

        self.logger.info("SSE HTTP %s %s from %s", method, path, peer)

        # --- CORS preflight ---
        if method == "OPTIONS":
            writer.write(
                b"HTTP/1.1 204 No Content\r\n"
                b"Access-Control-Allow-Origin: *\r\n"
                b"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                b"Access-Control-Allow-Headers: Content-Type\r\n"
                b"\r\n"
            )
            await writer.drain()
            writer.close()
            return

        # --- GET /sse - open event stream ---
        if method == "GET" and path == "/sse":
            session_id = str(uuid4())
            self.logger.info("SSE: new session %s from %s", session_id, peer)

            # Register session before writing headers so a fast POST finds it.
            disconnect_event = Event()
            self._sse_sessions[session_id] = (writer, disconnect_event)

            # Send HTTP response headers for the SSE stream.
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\n"
                b"Access-Control-Allow-Origin: *\r\n"
                b"\r\n"
            )
            # Send the endpoint event so the client knows where to POST.
            endpoint_url = f"/message?sessionId={session_id}"
            writer.write(f"event: endpoint\ndata: {endpoint_url}\n\n".encode())
            await writer.drain()

            # Keep the stream alive with periodic comments until the client
            # disconnects or the disconnect event is set.
            try:
                while not disconnect_event.is_set():
                    await sleep(15)
                    if writer.is_closing():
                        break
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
            except (CancelledError, ConnectionError, OSError):
                pass
            finally:
                self._sse_sessions.pop(session_id, None)
                self.logger.info("SSE: session %s closed", session_id)
                writer.close()
            return

        # --- POST /message - JSON-RPC request ---
        if method == "POST" and path.startswith("/message"):
            parsed = urlparse(path)
            qs = parse_qs(parsed.query)
            session_id = (qs.get("sessionId") or [None])[0]

            if not session_id or session_id not in self._sse_sessions:
                self.logger.warning("SSE: unknown session %s from %s", session_id, peer)
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return

            sse_writer, _ = self._sse_sessions[session_id]
            request_data = body.decode("utf-8", errors="replace")
            self.logger.info("SSE REQUEST (session %s): %s", session_id, request_data[:200])

            response = await self.process_request_async(request_data)

            if response is not None:
                response_json = dumps(response)
                sse_writer.write(f"event: message\ndata: {response_json}\n\n".encode())
                await sse_writer.drain()

            # Acknowledge the POST.
            writer.write(
                b"HTTP/1.1 202 Accepted\r\n"
                b"Content-Length: 0\r\n"
                b"Access-Control-Allow-Origin: *\r\n"
                b"\r\n"
            )
            await writer.drain()
            writer.close()
            return

        # --- Fallback: 404 ---
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async def run_sse_async(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Run the MCP server over HTTP with the SSE transport.

        Implements the MCP SSE transport protocol:
          GET  /sse       -> event stream (Content-Type: text/event-stream)
          POST /message   -> JSON-RPC request, response pushed to the SSE stream

        VS Code mcp.json example::

            { "type": "sse", "url": "http://127.0.0.1:<port>/sse" }
        """
        # Session registry: session_id -> (StreamWriter, disconnect_event)
        # NB: ``_sse_sessions`` is also initialised in __init__ so that
        # notification helpers can introspect it from any context, but we
        # reset it here to ensure each ``run_sse_async`` invocation starts
        # from a clean session map.
        self._sse_sessions: dict[str, tuple[StreamWriter, Event]] = {}

        server = await start_server(self._sse_handle_client, host, port)
        addrs = [s.getsockname() for s in server.sockets]
        for addr in addrs:
            self.logger.info("MCP SSE Server listening on http://%s:%s/sse", addr[0], addr[1])
            print(f"MCP SSE Server listening on http://{addr[0]}:{addr[1]}/sse", flush=True)

        async with server:
            await server.serve_forever()

    async def run_async(self, args: list[str] | None = None) -> None:
        """Run the MCP server asynchronously."""
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
            elif args[i] in ("--host",) and i + 1 < len(args):
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
                await self.run_socket_async(host=host, port=port)
            else:
                await self.run_sse_async(host=host, port=port)
            return

        # ---- Original stdio / file transport ----
        args = remaining

        # Check if reading from a file or continuous stdin
        if args:
            # Read from file if provided as argument
            try:
                with open(args[0], encoding='utf-8') as f:
                    input_data = f.read()

                # Log the input
                self.logger.info("REQUEST: %s", input_data)

                # Process the JSON-RPC 2.0 request
                response = await self.process_request_async(input_data)

                if response is not None:
                    # Output the response via binary buffer
                    payload = (dumps(response) + "\n").encode("utf-8")
                    _stdout_bin.write(payload)
                    _stdout_bin.flush()

            except OSError as e:
                self.logger.error("Error reading file %s: %s", args[0], e)
                exit(1)
        else:
            # Continuously read from stdin line by line using asyncio.
            # We read/write via the raw binary buffers (_stdin_bin / _stdout_bin)
            # to avoid text-mode buffering and CRLF translation on Windows.
            self.logger.info("Async MCP Server started. Waiting for JSON-RPC 2.0 messages...")

            try:
                while True:
                    try:
                        raw = await get_event_loop().run_in_executor(None, _stdin_bin.readline)
                        if not raw:  # EOF
                            break

                        line = raw.decode("utf-8", errors="replace").strip()

                        # Skip empty lines
                        if not line:
                            continue

                        # Log the input
                        self.logger.info("REQUEST: %s", line)

                        # Process the JSON-RPC 2.0 request
                        response = await self.process_request_async(line)

                        if response is not None:
                            # Output the response via binary buffer
                            payload = (dumps(response) + "\n").encode("utf-8")
                            _stdout_bin.write(payload)
                            _stdout_bin.flush()

                    except CancelledError:
                        break

            except KeyboardInterrupt:
                self.logger.info("Async MCP Server stopped.")
                exit(0)
            except Exception as e:  # noqa: BLE001 broad for runtime resilience
                self.logger.error("Async MCP Server error: %s", e)
                exit(1)

    def run(self, args: list[str] | None = None) -> None:
        """Synchronous wrapper to run the async MCP server."""
        try:
            run(self.run_async(args))
        except KeyboardInterrupt:
            self.logger.info("Async MCP Server stopped by user.")
            exit(0)


if __name__ == "__main__":
    # This will be overridden by subclasses
    server = AsyncMCPServer()
    server.run()
