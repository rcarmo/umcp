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

from base64 import b64encode, urlsafe_b64decode, urlsafe_b64encode
from dataclasses import MISSING, asdict, fields, is_dataclass
from enum import Enum
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
    isawaitable,
    ismethod,
    signature,
)
import math
from json import JSONDecodeError, dumps, loads as _json_loads
from logging import INFO, FileHandler, basicConfig, getLogger
from pathlib import Path
from umcp_shared import (
    MCPCancellationState,
    MCPHTTPResponse,
    MCPPrincipal,
    MCPRequestCancelled,
    MCPRequestContext,
    MCPRequestRuntime,
    SUPPORTED_PROTOCOL_VERSIONS,
    content_type_is_json,
    exact_or_fallback,
    get_progress_token as _get_progress_token,
    get_request_context,
    get_request_runtime,
    is_jsonrpc_object,
    is_request_cancelled as _is_request_cancelled,
    is_valid_jsonrpc_id,
    is_valid_jsonrpc_response,
    has_ambiguous_singleton_values,
    has_singleton_header_violations,
    media_accepts_event_stream,
    media_accepts_json,
    origin_is_allowed,
    raise_if_cancelled as _raise_if_cancelled,
    request_target_path,
    reset_request_context,
    reset_request_runtime,
    set_request_context,
    set_request_runtime,
    validate_http_response,
)
from queue import Empty, Queue
from sys import argv, exit
from types import UnionType
from typing import Any, Literal, Mapping, Union, get_args, get_origin, get_type_hints, is_typeddict


_STRUCTURED_UNSET = object()
from urllib.parse import parse_qs
from uuid import uuid4


def _reject_json_constant(value: str) -> None:
    raise JSONDecodeError(f"Invalid JSON constant: {value}", value, 0)


def loads(value: str | bytes) -> Any:
    return _json_loads(value, parse_constant=_reject_json_constant)


def get_progress_token() -> str | int | None:
    return _get_progress_token()


def is_request_cancelled() -> bool:
    return _is_request_cancelled()


def raise_if_cancelled() -> None:
    _raise_if_cancelled()


def notify_progress(progress: float | int, total: float | int | None = None, message: str | None = None) -> None:
    runtime = get_request_runtime()
    if runtime is None or runtime.progress_callback is None:
        return
    runtime.progress_callback(progress, total, message)


class MCPServer:
    """Core MCP server implementation using JSON-RPC 2.0 protocol."""

    def __init__(self):
        # Get the directory where the script is located
        self.script_dir = Path(__file__).parent.absolute()
        self.log_file = self.script_dir / "mcpserver.log"

        # Set up logging
        self._setup_logging()

        # SSE session registry: session_id -> (Queue[bytes], principal name).
        self._sse_sessions: dict[str, tuple[Queue, str]] = {}
        self._sse_lock = threading.Lock()

        # Resource subscriptions. For stdio / TCP / file mode we keep a
        # transport-global set of URIs. For SSE we also track per-session
        # subscriptions so notifications can be targeted to the subscribing
        # session instead of being broadcast to every connected client.
        self._resource_subscriptions: set[str] = set()
        self._resource_session_subscriptions: dict[str, set[str]] = {}
        # Dynamically registered resources / templates (in addition to those
        # discovered via the ``resource_*`` / ``resource_template_*`` naming
        # conventions).  Each entry is ``(metadata, callable)``.
        self._dynamic_tools: dict[str, tuple[dict[str, Any], Any]] = {}
        self._dynamic_prompts: dict[str, tuple[dict[str, Any], Any]] = {}
        self._dynamic_resources: dict[str, tuple[dict[str, Any], Any]] = {}
        self._dynamic_resource_templates: list[tuple[dict[str, Any], Any]] = []
        self._completion_providers: dict[tuple[str, str, str], Any] = {}
        self.default_list_page_size: int | None = None
        self.max_completion_values: int = 100
        self.logging_level: str = "info"
        self._request_registry_lock = threading.RLock()
        self._active_requests_by_id: dict[str | int, MCPCancellationState] = {}
        self._active_requests_by_progress_token: dict[str | int, set[str | int]] = {}

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
        capabilities: dict[str, Any] = {
            "tools": {"listChanged": True},
            "prompts": {
                "get": True,
                "listChanged": True,
            },
            "resources": {
                "subscribe": True,
                "listChanged": True,
            },
            "logging": {},
        }
        if self._has_completion_support():
            capabilities["completions"] = {}
        return {
            "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
            "serverInfo": {
                "name": self.__class__.__name__,
                "version": "0.1.0"
            },
            "capabilities": capabilities,
            "instructions": self.get_instructions()
        }

    def get_instructions(self) -> str:
        """Get server instructions. Override in subclasses for custom instructions."""
        return "This server provides tool functionality via the Model Context Protocol."

    def _empty_object_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    def _sort_discovery_items(self, items: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: tuple(str(item.get(key, "")) for key in keys))

    def _cursor_fingerprint(self, label: str, items: list[dict[str, Any]], identity_keys: tuple[str, ...]) -> str:
        principal = get_request_context().principal or ""
        basis = [principal, label]
        for item in items:
            basis.append("\x1f".join(str(item.get(key, "")) for key in identity_keys))
        return urlsafe_b64encode("\n".join(basis).encode("utf-8")).decode("ascii").rstrip("=")

    def _encode_cursor(self, label: str, offset: int, fingerprint: str) -> str:
        payload = dumps({"v": 1, "l": label, "o": offset, "f": fingerprint}, separators=(",", ":"))
        return urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")

    def _decode_cursor(self, cursor: str, *, label: str, fingerprint: str) -> int:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = _json_loads(urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("Invalid cursor") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("l") != label:
            raise ValueError("Invalid cursor")
        offset = payload.get("o")
        if not isinstance(offset, int) or offset < 0 or payload.get("f") != fingerprint:
            raise ValueError("Invalid cursor")
        return offset

    def _page_list(
        self,
        *,
        label: str,
        items: list[dict[str, Any]],
        result_key: str,
        identity_keys: tuple[str, ...],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        cursor = params.get("cursor")
        page_size = params.get("pageSize")
        if page_size is not None and (not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0):
            raise ValueError("Invalid params: 'pageSize' must be a positive integer")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("Invalid params: 'cursor' must be a string")
        if cursor is None and page_size is None:
            return {result_key: items}

        page_size = page_size or self.default_list_page_size or 50
        fingerprint = self._cursor_fingerprint(label, items, identity_keys)
        start = 0 if cursor is None else self._decode_cursor(cursor, label=label, fingerprint=fingerprint)
        page = items[start:start + page_size]
        result: dict[str, Any] = {result_key: page}
        next_offset = start + len(page)
        if next_offset < len(items):
            result["nextCursor"] = self._encode_cursor(label, next_offset, fingerprint)
        return result

    # ==== Tool discovery ====

    def discover_tools(self) -> dict[str, Any]:
        """Discover tools via naming convention and runtime registration."""
        tools_by_name: dict[str, dict[str, Any]] = {}
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
                "inputSchema": parameters if parameters else self._empty_object_schema(),
            }
            output_schema = self._tool_output_schema(method)
            if output_schema is not None:
                tool_def["outputSchema"] = output_schema
            annotations = self._infer_tool_annotations(tool_name, method)
            if annotations:
                tool_def["annotations"] = annotations
            tools_by_name[tool_name] = tool_def
        for tool_name, (meta, _callable) in self._dynamic_tools.items():
            tools_by_name[tool_name] = dict(meta)
        return {"tools": self._sort_discovery_items(list(tools_by_name.values()), "name")}

    def register_tool(
        self,
        name: str,
        callable_: Any,
        *,
        description: str | None = None,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        annotations: dict[str, Any] | None = None,
    ) -> None:
        meta: dict[str, Any] = {
            "name": name,
            "description": description or (getdoc(callable_) or f"Execute {name} tool"),
            "inputSchema": dict(input_schema) if input_schema else self._extract_parameters_from_signature(signature(callable_), callable_) or self._empty_object_schema(),
        }
        inferred_output_schema = output_schema if output_schema is not None else self._tool_output_schema(callable_)
        if inferred_output_schema is not None:
            meta["outputSchema"] = dict(inferred_output_schema)
        tool_annotations = annotations if annotations is not None else self._infer_tool_annotations(name, callable_)
        if tool_annotations:
            meta["annotations"] = dict(tool_annotations)
        self._dynamic_tools[name] = (meta, callable_)

    def register_tool_and_notify(self, name: str, callable_: Any, **kwargs: Any) -> None:
        """Register a tool and immediately emit ``tools/list_changed``."""
        self.register_tool(name, callable_, **kwargs)
        self.notify_tool_list_changed()

    def unregister_tool(self, name: str) -> bool:
        return self._dynamic_tools.pop(name, None) is not None

    def unregister_tool_and_notify(self, name: str) -> bool:
        """Unregister a tool and emit ``tools/list_changed`` only on success."""
        removed = self.unregister_tool(name)
        if removed:
            self.notify_tool_list_changed()
        return removed

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
        """Discover prompts via naming convention and runtime registration."""
        prompts_by_name: dict[str, dict[str, Any]] = {}
        for name, method in getmembers(self, predicate=ismethod):
            if not name.startswith('prompt_'):
                continue
            prompt_name = name[7:]
            sig = signature(method)
            doc = getdoc(method) or f"Prompt template {prompt_name}"
            parameters = self._extract_parameters_from_signature(sig, method)
            categories = self._extract_prompt_categories(doc)
            prompts_by_name[prompt_name] = {
                "name": prompt_name,
                "description": doc,
                "inputSchema": parameters or self._empty_object_schema(),
                "categories": categories,
            }
        for prompt_name, (meta, _callable) in self._dynamic_prompts.items():
            prompts_by_name[prompt_name] = dict(meta)
        return {"prompts": self._sort_discovery_items(list(prompts_by_name.values()), "name")}

    def register_prompt(
        self,
        name: str,
        callable_: Any,
        *,
        description: str | None = None,
        input_schema: dict[str, Any] | None = None,
        categories: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        doc = description or (getdoc(callable_) or f"Prompt template {name}")
        meta: dict[str, Any] = {
            "name": name,
            "description": doc,
            "inputSchema": dict(input_schema) if input_schema else self._extract_parameters_from_signature(signature(callable_), callable_) or self._empty_object_schema(),
            "categories": list(categories) if categories is not None else self._extract_prompt_categories(doc),
        }
        self._dynamic_prompts[name] = (meta, callable_)

    def register_prompt_and_notify(self, name: str, callable_: Any, **kwargs: Any) -> None:
        """Register a prompt and immediately emit ``prompts/list_changed``."""
        self.register_prompt(name, callable_, **kwargs)
        self.notify_prompt_list_changed()

    def unregister_prompt(self, name: str) -> bool:
        return self._dynamic_prompts.pop(name, None) is not None

    def unregister_prompt_and_notify(self, name: str) -> bool:
        """Unregister a prompt and emit ``prompts/list_changed`` only on success."""
        removed = self.unregister_prompt(name)
        if removed:
            self.notify_prompt_list_changed()
        return removed

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

    def _has_completion_support(self) -> bool:
        return bool(
            self._completion_providers
            or self.discover_prompts()["prompts"]
            or self.discover_resource_templates()["resourceTemplates"]
        )

    def register_completion_provider(self, ref_type: str, ref_name: str, argument_name: str, callable_: Any) -> None:
        self._completion_providers[(ref_type, ref_name, argument_name)] = callable_

    def _completion_parameter_schema(self, method: Any, argument_name: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        if isinstance(meta, dict):
            input_schema = meta.get("inputSchema")
            if isinstance(input_schema, dict):
                properties = input_schema.get("properties")
                if isinstance(properties, dict):
                    schema = properties.get(argument_name)
                    if isinstance(schema, dict):
                        return schema
        override = getattr(method, "_mcp_resource_template", None) or getattr(method, "_mcp_prompt", None) or {}
        input_schema = override.get("input_schema")
        if isinstance(input_schema, dict):
            properties = input_schema.get("properties")
            if isinstance(properties, dict):
                schema = properties.get(argument_name)
                if isinstance(schema, dict):
                    return schema
        return self._extract_parameters_from_signature(signature(method), method).get("properties", {}).get(argument_name, {})

    def _resolve_completion_target(self, ref: dict[str, Any]) -> tuple[str, str, Any, dict[str, Any]]:
        ref_type = ref.get("type")
        if ref_type == "ref/prompt":
            name = ref.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("Invalid completion ref: prompt name is required")
            if name in self._dynamic_prompts:
                meta, method = self._dynamic_prompts[name]
                return ref_type, name, method, dict(meta)
            method_name = f"prompt_{name}"
            if hasattr(self, method_name):
                method = getattr(self, method_name)
                return ref_type, name, method, {"name": name}
            raise ValueError(f"Unknown prompt ref: {name}")
        if ref_type == "ref/resource":
            ref_name = ref.get("uri") or ref.get("uriTemplate") or ref.get("name")
            if not isinstance(ref_name, str) or not ref_name:
                raise ValueError("Invalid completion ref: resource template identifier is required")
            for name, method, params in self._iter_resource_template_methods():
                meta = self._resource_metadata(name, method, params=params)
                if ref_name in {meta.get("name"), meta.get("uriTemplate")}:
                    return ref_type, str(meta.get("uriTemplate")), method, meta
            for meta, method in self._dynamic_resource_templates:
                if ref_name in {meta.get("name"), meta.get("uriTemplate")}:
                    return ref_type, str(meta.get("uriTemplate")), method, dict(meta)
            raise ValueError(f"Unknown resource template ref: {ref_name}")
        raise ValueError("Invalid completion ref: unsupported ref type")

    def _enum_completion_values(self, method: Any, argument_name: str) -> list[str]:
        try:
            type_hints = get_type_hints(method)
        except (NameError, AttributeError, TypeError):
            type_hints = {}
        annotation = type_hints.get(argument_name, signature(method).parameters.get(argument_name, Parameter("x", Parameter.POSITIONAL_OR_KEYWORD)).annotation)
        schema = self._type_to_json_schema(annotation)
        if not isinstance(schema, dict):
            return []
        values = schema.get("enum") if isinstance(schema.get("enum"), list) else []
        return [str(value) for value in values]

    def _schema_enum_completion_values(self, schema: dict[str, Any]) -> list[str]:
        values = schema.get("enum") if isinstance(schema.get("enum"), list) else []
        return [str(value) for value in values]

    def _normalise_completion_result(self, raw: Any) -> tuple[list[str], int | None, bool | None]:
        if isinstance(raw, dict):
            values = raw.get("values", [])
            total = raw.get("total")
            has_more = raw.get("hasMore")
        else:
            values = raw
            total = None
            has_more = None
        if not isinstance(values, list):
            raise ValueError("Completion provider must return a list or {'values': [...]} result")
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value)
            if text not in seen:
                seen.add(text)
                out.append(text)
        if total is not None and (not isinstance(total, int) or isinstance(total, bool) or total < 0):
            raise ValueError("Completion provider returned invalid total")
        if has_more is not None and not isinstance(has_more, bool):
            raise ValueError("Completion provider returned invalid hasMore")
        return out, total, has_more

    def handle_completion_complete(self, request_id: str | int | None, params: dict[str, Any]) -> dict[str, Any]:
        ref = params.get("ref")
        argument = params.get("argument")
        context = params.get("context") or {}
        requested = params.get("maxValues", self.max_completion_values)
        if not isinstance(ref, dict):
            return self.create_response(request_id, None, self.create_error(-32602, "Invalid params: 'ref' must be an object"))
        if not isinstance(argument, dict):
            return self.create_response(request_id, None, self.create_error(-32602, "Invalid params: 'argument' must be an object"))
        if not isinstance(context, dict):
            return self.create_response(request_id, None, self.create_error(-32602, "Invalid params: 'context' must be an object"))
        if not isinstance(requested, int) or isinstance(requested, bool) or requested <= 0:
            return self.create_response(request_id, None, self.create_error(-32602, "Invalid params: 'maxValues' must be a positive integer"))
        argument_name = argument.get("name")
        if not isinstance(argument_name, str) or not argument_name:
            return self.create_response(request_id, None, self.create_error(-32602, "Invalid params: 'argument.name' must be a non-empty string"))
        prefix = argument.get("value", "")
        if prefix is None:
            prefix = ""
        if not isinstance(prefix, str):
            prefix = str(prefix)
        context_arguments = context.get("arguments", {}) or {}
        if not isinstance(context_arguments, dict):
            return self.create_response(request_id, None, self.create_error(-32602, "Invalid params: 'context.arguments' must be an object"))
        try:
            ref_type, ref_name, method, meta = self._resolve_completion_target(ref)
        except ValueError as exc:
            return self.create_response(request_id, None, self.create_error(-32602, str(exc)))
        if argument_name not in signature(method).parameters:
            return self.create_response(request_id, None, self.create_error(-32602, f"Unknown completion argument: {argument_name}"))

        values = self._schema_enum_completion_values(self._completion_parameter_schema(method, argument_name, meta))
        if not values:
            values = self._enum_completion_values(method, argument_name)
        provider = self._completion_providers.get((ref_type, ref_name, argument_name))
        if provider is not None:
            try:
                provided_values, provided_total, provided_has_more = self._normalise_completion_result(
                    provider(prefix=prefix, arguments=dict(context_arguments), ref=dict(ref), argument=dict(argument))
                )
            except ValueError as exc:
                return self.create_response(request_id, None, self.create_error(-32602, str(exc)))
            except Exception:  # noqa: BLE001
                return self.create_response(request_id, None, self.create_error(-32603, "Completion provider failed"))
            values.extend(provided_values)
        else:
            provided_total = None
            provided_has_more = None

        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if prefix and not value.startswith(prefix):
                continue
            if value not in seen:
                seen.add(value)
                deduped.append(value)
        limit = min(requested, self.max_completion_values)
        total = max(provided_total or 0, len(deduped)) if provided_total is not None else len(deduped)
        has_more = provided_has_more if provided_has_more is not None else total > len(deduped[:limit])
        result: dict[str, Any] = {"completion": {"values": deduped[:limit], "hasMore": has_more}}
        if total != len(result["completion"]["values"]) or has_more:
            result["completion"]["total"] = total
        return self.create_response(request_id, result, None)

    def handle_prompt_get(self, request_id: str | int | None, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``prompts/get``.

        Unlike the earlier implementation, prompts are executed even when the
        caller omits the ``arguments`` field entirely (which matters for
        zero-arg prompts), required arguments are validated consistently, and
        unknown arguments are rejected with ``-32602`` rather than being
        ignored silently.
        """
        prompt_name = params.get('name')
        if not prompt_name:
            error = self.create_error(-32602, "Missing required parameter 'name'")
            return self.create_response(request_id, None, error)
        method_name = f"prompt_{prompt_name}"
        if prompt_name in self._dynamic_prompts:
            _meta, method = self._dynamic_prompts[prompt_name]
        elif hasattr(self, method_name):
            method = getattr(self, method_name)
        else:
            error = self.create_error(-32601, f"Prompt not found: {prompt_name}")
            return self.create_response(request_id, None, error)

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
                    raise ValueError(
                        f"Missing required argument '{p_name}' for prompt {prompt_name}"
                    )
        except ValueError as e:
            error = self.create_error(-32602, str(e))
            return self.create_response(request_id, None, error)

        try:
            ret = method(**kwargs)
        except MCPRequestCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            message = self._remote_safe_failure("Prompt execution failed", f"Prompt execution error: {e}")
            error = self.create_error(-32603, message)
            return self.create_response(request_id, None, error)

        if isinstance(ret, str):
            result_body['messages'] = [
                {'role': 'user', 'content': {'type': 'text', 'text': ret}}
            ]
        elif isinstance(ret, list) and all(
            isinstance(m, dict) and 'role' in m and 'content' in m for m in ret
        ):
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

    # ==== Resource discovery & handling ====

    # MIME-type defaults used when a resource method does not declare one.
    _DEFAULT_TEXT_MIME = "text/plain"
    _DEFAULT_BINARY_MIME = "application/octet-stream"

    @staticmethod
    def _resource_uri_template_to_regex(template: str) -> tuple[Any, list[str]]:
        """Compile a ``{name}``-style URI template into a regex.

        Returns ``(compiled_regex, [param_names])``.  Each ``{name}`` becomes
        a named capture group matching one path segment (no ``/``).  The match
        is anchored to the full URI.
        """
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
        """Default URI for a static resource named *name*."""
        return f"umcp://{self.__class__.__name__}/{name}"

    def _default_resource_template_uri(self, name: str, params: list[str]) -> str:
        """Default URI template for a parameterised resource."""
        slots = "/".join("{" + p + "}" for p in params)
        base = f"umcp://{self.__class__.__name__}/{name}"
        return f"{base}/{slots}" if slots else base

    def _resource_metadata(self, name: str, method: Any, params: list[str]) -> dict[str, Any]:
        """Build the public resource metadata dict for *method*.

        Honours an optional ``_mcp_resource`` (or ``_mcp_resource_template``)
        attribute on the method, which may override ``uri`` / ``uri_template``,
        ``name``, ``title``, ``description``, ``mime_type``, ``size`` and
        ``annotations``.
        """
        override = getattr(method, "_mcp_resource", None) or getattr(method, "_mcp_resource_template", None) or {}
        doc = (getdoc(method) or "").strip()
        description = override.get("description") or (doc.splitlines()[0] if doc else None)
        meta: dict[str, Any] = {
            "name": override.get("name", name),
        }
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
        """Yield ``(name, bound_method)`` pairs for every static resource."""
        out: list[tuple[str, Any]] = []
        for member_name, method in getmembers(self, predicate=ismethod):
            if member_name.startswith("resource_template_"):
                continue
            if not member_name.startswith("resource_"):
                continue
            out.append((member_name[len("resource_"):], method))
        return out

    def _iter_resource_template_methods(self) -> list[tuple[str, Any, list[str]]]:
        """Yield ``(name, bound_method, [param_names])`` for every template."""
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
        return {"resources": self._sort_discovery_items(resources, "name", "uri")}

    def discover_resource_templates(self) -> dict[str, Any]:
        """Return the ``resources/templates/list`` payload."""
        templates: list[dict[str, Any]] = []
        for name, method, params in self._iter_resource_template_methods():
            templates.append(self._resource_metadata(name, method, params=params))
        for meta, _callable in self._dynamic_resource_templates:
            templates.append(dict(meta))
        return {"resourceTemplates": self._sort_discovery_items(templates, "name", "uriTemplate")}

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
        """Register a static resource at runtime.

        *callable_* takes no arguments and returns the resource value
        (``str``, ``bytes``, dict, or list of dicts -- see ``resource_*``
        return-type handling).
        """
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
        """Register a parameterised resource template at runtime.

        *callable_* receives the URI placeholder values as keyword args.
        """
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
        """Convert a resource method's return value into MCP ``contents`` entries.

        Supported return shapes:
        * ``str`` -- text content with ``mimeType`` from override or ``text/plain``.
        * ``bytes`` / ``bytearray`` / ``memoryview`` -- binary content,
          base64-encoded into ``blob``, ``mimeType`` defaults to
          ``application/octet-stream``.
        * ``dict`` -- treated as a single content entry.  ``uri`` is filled
          in automatically if missing.
        * ``list`` -- list of dicts, each treated as a content entry.
        """
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
        # Fallback: stringify.
        return [{
            "uri": uri,
            "mimeType": default_mime or self._DEFAULT_TEXT_MIME,
            "text": str(value),
        }]

    def handle_resources_list(
        self, request_id: str | int | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle ``resources/list`` with optional cursor pagination."""
        try:
            result = self._page_list(
                label="resources",
                items=self.discover_resources()["resources"],
                result_key="resources",
                identity_keys=("uri", "name"),
                params=params,
            )
        except ValueError as exc:
            return self.create_response(request_id, None, self.create_error(-32602, str(exc)))
        return self.create_response(request_id, result, None)

    def handle_resources_templates_list(
        self, request_id: str | int | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle ``resources/templates/list`` with optional cursor pagination."""
        try:
            result = self._page_list(
                label="resourceTemplates",
                items=self.discover_resource_templates()["resourceTemplates"],
                result_key="resourceTemplates",
                identity_keys=("uriTemplate", "name"),
                params=params,
            )
        except ValueError as exc:
            return self.create_response(request_id, None, self.create_error(-32602, str(exc)))
        return self.create_response(request_id, result, None)

    def handle_resources_read(
        self, request_id: str | int | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle ``resources/read`` -- look up by URI and dispatch."""
        uri = params.get("uri")
        if not uri:
            error = self.create_error(-32602, "Missing 'uri' parameter")
            return self.create_response(request_id, None, error)

        # Static resources discovered via naming convention.
        for name, method in self._iter_resource_methods():
            meta = self._resource_metadata(name, method, params=[])
            if meta["uri"] == uri:
                try:
                    value = method()
                except MCPRequestCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 -- wire-level handler
                    self.logger.exception("Resource %s raised", uri)
                    error = self.create_error(
                        -32603,
                        self._remote_safe_failure("Resource read failed", f"Resource read failed: {exc}"),
                    )
                    return self.create_response(request_id, None, error)
                contents = self._normalise_resource_content(uri, meta.get("mimeType"), value)
                return self.create_response(request_id, {"contents": contents}, None)

        # Dynamically registered resources.
        if uri in self._dynamic_resources:
            meta, fn = self._dynamic_resources[uri]
            try:
                value = fn()
            except MCPRequestCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Resource %s raised", uri)
                error = self.create_error(
                    -32603,
                    self._remote_safe_failure("Resource read failed", f"Resource read failed: {exc}"),
                )
                return self.create_response(request_id, None, error)
            contents = self._normalise_resource_content(uri, meta.get("mimeType"), value)
            return self.create_response(request_id, {"contents": contents}, None)

        # Templated resources discovered via naming convention.
        for name, method, params_list in self._iter_resource_template_methods():
            meta = self._resource_metadata(name, method, params=params_list)
            regex, _ = self._resource_uri_template_to_regex(meta["uriTemplate"])
            m = regex.match(uri)
            if m:
                try:
                    value = method(**m.groupdict())
                except MCPRequestCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.logger.exception("Resource template %s raised", uri)
                    error = self.create_error(
                        -32603,
                        self._remote_safe_failure("Resource read failed", f"Resource read failed: {exc}"),
                    )
                    return self.create_response(request_id, None, error)
                contents = self._normalise_resource_content(uri, meta.get("mimeType"), value)
                return self.create_response(request_id, {"contents": contents}, None)

        # Dynamically registered templates.
        for meta, fn in self._dynamic_resource_templates:
            regex, _ = self._resource_uri_template_to_regex(meta["uriTemplate"])
            m = regex.match(uri)
            if m:
                try:
                    value = fn(**m.groupdict())
                except MCPRequestCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.logger.exception("Resource template %s raised", uri)
                    error = self.create_error(
                        -32603,
                        self._remote_safe_failure("Resource read failed", f"Resource read failed: {exc}"),
                    )
                    return self.create_response(request_id, None, error)
                contents = self._normalise_resource_content(uri, meta.get("mimeType"), value)
                return self.create_response(request_id, {"contents": contents}, None)

        # Per spec, unknown resources return a -32002 error with the URI in data.
        error = self.create_error(-32002, "Resource not found", data={"uri": uri})
        return self.create_response(request_id, None, error)

    def handle_resources_subscribe(
        self, request_id: str | int | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle ``resources/subscribe``."""
        uri = params.get("uri")
        if not uri:
            error = self.create_error(-32602, "Missing 'uri' parameter")
            return self.create_response(request_id, None, error)
        session_id = params.get("_session_id")
        if isinstance(session_id, str) and session_id:
            self._resource_session_subscriptions.setdefault(session_id, set()).add(uri)
        else:
            self._resource_subscriptions.add(uri)
        return self.create_response(request_id, {}, None)

    def handle_resources_unsubscribe(
        self, request_id: str | int | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle ``resources/unsubscribe``."""
        uri = params.get("uri")
        if not uri:
            error = self.create_error(-32602, "Missing 'uri' parameter")
            return self.create_response(request_id, None, error)
        session_id = params.get("_session_id")
        if isinstance(session_id, str) and session_id:
            if session_id in self._resource_session_subscriptions:
                self._resource_session_subscriptions[session_id].discard(uri)
                if not self._resource_session_subscriptions[session_id]:
                    self._resource_session_subscriptions.pop(session_id, None)
        else:
            self._resource_subscriptions.discard(uri)
        return self.create_response(request_id, {}, None)

    def _send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_ids: set[str] | None = None,
    ) -> None:
        """Emit a JSON-RPC notification on the active transport.

        For SSE, broadcasts to all connected sessions or a targeted subset.
        For stdio / TCP / file mode, writes one newline-delimited message to
        stdout.
        """
        notification: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            notification["params"] = params
        payload_text = dumps(notification)

        if self._sse_sessions:
            sse_blob = f"event: message\ndata: {payload_text}\n\n".encode()
            with self._sse_lock:
                target_items = list(self._sse_sessions.items())
                if session_ids is not None:
                    target_items = [item for item in target_items if item[0] in session_ids]
                for _sid, q in target_items:
                    try:
                        q.put(sse_blob)
                    except Exception:  # noqa: BLE001
                        pass
            return

        try:
            _stdout_bin.write((payload_text + "\n").encode("utf-8"))
            _stdout_bin.flush()
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed to emit notification %s", method)

    def notify_tool_list_changed(self) -> None:
        """Tell connected clients the tool list has changed."""
        self._send_notification("notifications/tools/list_changed")

    def notify_prompt_list_changed(self) -> None:
        """Tell connected clients the prompt list has changed."""
        self._send_notification("notifications/prompts/list_changed")

    def notify_resource_list_changed(self) -> None:
        """Tell connected clients the resource list has changed."""
        self._send_notification("notifications/resources/list_changed")

    def notify_resource_updated(self, uri: str) -> None:
        """Tell subscribed clients that *uri* has changed.

        Stdio / TCP / file mode uses the transport-global subscription set.
        SSE uses per-session subscriptions so only the subscribing client gets
        the update.
        """
        target_sessions = {
            session_id
            for session_id, uris in self._resource_session_subscriptions.items()
            if uri in uris
        }
        if target_sessions:
            self._send_notification(
                "notifications/resources/updated", {"uri": uri}, session_ids=target_sessions
            )
        elif uri in self._resource_subscriptions:
            self._send_notification(
                "notifications/resources/updated", {"uri": uri}
            )

    _LOG_LEVELS = ("debug", "info", "notice", "warning", "error", "critical", "alert", "emergency")
    _SECRET_KEY_PATTERN = re.compile(r"(?:pass(word)?|secret|token|api[_-]?key|auth(orization)?|cookie|session|credential)", re.IGNORECASE)
    _SECRET_VALUE_PATTERN = re.compile(r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]+|sk-[A-Za-z0-9]+|api[_-]?key\s*[:=]\s*\S+|token\s*[:=]\s*\S+)")

    def _should_emit_log_message(self, level: str) -> bool:
        return self._LOG_LEVELS.index(level) >= self._LOG_LEVELS.index(self.logging_level)

    def _sanitize_log_data(self, data: Any) -> Any:
        if isinstance(data, dict):
            return {
                str(key): ("[redacted]" if self._SECRET_KEY_PATTERN.search(str(key)) else self._sanitize_log_data(value))
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [self._sanitize_log_data(item) for item in data]
        if isinstance(data, tuple):
            return [self._sanitize_log_data(item) for item in data]
        if isinstance(data, str):
            return self._SECRET_VALUE_PATTERN.sub("[redacted]", data)
        return data

    def handle_logging_set_level(self, request_id: str | int | None, params: dict[str, Any]) -> dict[str, Any]:
        level = params.get("level")
        if not isinstance(level, str) or level not in self._LOG_LEVELS:
            return self.create_response(request_id, None, self.create_error(-32602, "Invalid params: 'level' must be one of debug, info, notice, warning, error, critical, alert, emergency"))
        self.logging_level = level
        return self.create_response(request_id, {}, None)

    def notify_log_message(self, level: str, data: Any, *, logger: str | None = None, sanitize: bool = True) -> None:
        if level not in self._LOG_LEVELS or not self._should_emit_log_message(level):
            return
        params: dict[str, Any] = {"level": level, "data": self._sanitize_log_data(data) if sanitize else data}
        if logger:
            params["logger"] = logger
        self._send_notification("notifications/message", params)

    def log_message(self, level: str, data: Any, *, logger: str | None = None, sanitize: bool = True) -> None:
        self.notify_log_message(level, data, logger=logger, sanitize=sanitize)

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

    def _tool_output_schema(self, method: Any) -> dict[str, Any] | None:
        explicit = getattr(method, "_mcp_output_schema", None)
        if explicit is not None:
            return dict(explicit)
        try:
            type_hints = get_type_hints(method)
        except (NameError, AttributeError, TypeError):
            type_hints = {}
        return_type = type_hints.get("return", signature(method).return_annotation)
        if return_type in (Parameter.empty, Signature.empty, Any):
            return None
        return self._type_to_json_schema(return_type)

    def _type_to_json_schema(self, param_type: Any) -> dict[str, Any]:
        """Convert Python type annotation to JSON schema property."""
        # Untyped parameter (no annotation) -> default to string for MCP clients.
        if param_type is Parameter.empty:
            return {"type": "string"}
        if param_type is Any:
            return {}
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

        if isinstance(param_type, type) and issubclass(param_type, Enum):
            values = [member.value for member in param_type]
            if all(isinstance(v, str) for v in values):
                return {"type": "string", "enum": values}
            if all(isinstance(v, int) for v in values):
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
            schema = {"type": "object"}
            if len(args) == 2:
                schema["additionalProperties"] = self._type_to_json_schema(args[1])
            return schema

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

        if isinstance(param_type, type) and is_dataclass(param_type):
            dc_props = {}
            dc_required = []
            dc_hints = get_type_hints(param_type)
            for field in fields(param_type):
                dc_props[field.name] = self._type_to_json_schema(dc_hints.get(field.name, field.type))
                if field.default is MISSING and field.default_factory is MISSING:
                    dc_required.append(field.name)
            dc_schema: dict[str, Any] = {"type": "object", "properties": dc_props, "additionalProperties": False}
            if dc_required:
                dc_schema["required"] = dc_required
            return dc_schema

        return {"type": "string"}

    def _normalise_structured_tool_value(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if is_dataclass(value):
            return {k: self._normalise_structured_tool_value(v) for k, v in asdict(value).items()}
        if isinstance(value, Mapping):
            return {str(k): self._normalise_structured_tool_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._normalise_structured_tool_value(item) for item in value]
        return _STRUCTURED_UNSET

    def _validate_schema_subset(self, value: Any, schema: dict[str, Any], path: str = "$") -> None:
        if not schema:
            return
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"{path} must be one of {schema['enum']}")
        for branch_key in ("oneOf", "anyOf"):
            branches = schema.get(branch_key)
            if isinstance(branches, list) and branches:
                errors = []
                for branch in branches:
                    try:
                        self._validate_schema_subset(value, branch, path)
                        return
                    except ValueError as exc:
                        errors.append(str(exc))
                raise ValueError(errors[0])
        expected_type = schema.get("type")
        if isinstance(expected_type, list):
            errors = []
            for item_type in expected_type:
                try:
                    self._validate_schema_subset(value, {**schema, "type": item_type}, path)
                    return
                except ValueError as exc:
                    errors.append(str(exc))
            raise ValueError(errors[0])
        if expected_type == "null":
            if value is not None:
                raise ValueError(f"{path} must be null")
            return
        if expected_type == "string":
            if not isinstance(value, str):
                raise ValueError(f"{path} must be a string")
            return
        if expected_type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{path} must be an integer")
            return
        if expected_type == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{path} must be a number")
            return
        if expected_type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{path} must be a boolean")
            return
        if expected_type == "array":
            if not isinstance(value, list):
                raise ValueError(f"{path} must be an array")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    self._validate_schema_subset(item, item_schema, f"{path}[{index}]")
            return
        if expected_type == "object" or "properties" in schema or "required" in schema:
            if not isinstance(value, dict):
                raise ValueError(f"{path} must be an object")
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            for key in required:
                if key not in value:
                    raise ValueError(f"{path}.{key} is required")
            additional = schema.get("additionalProperties", True)
            for key, item in value.items():
                if key in properties:
                    self._validate_schema_subset(item, properties[key], f"{path}.{key}")
                elif additional is False:
                    raise ValueError(f"{path}.{key} is not allowed")
                elif isinstance(additional, dict):
                    self._validate_schema_subset(item, additional, f"{path}.{key}")
            return

    def _format_tool_result(self, method: Any, content: Any, output_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        output_schema = dict(output_schema) if output_schema is not None else self._tool_output_schema(method)
        structured = self._normalise_structured_tool_value(content)
        if output_schema is not None:
            candidate = structured if structured is not _STRUCTURED_UNSET else content
            self._validate_schema_subset(candidate, output_schema)
        if isinstance(content, str):
            stringified_content = content
        else:
            try:
                stringified_content = dumps(structured if structured is not _STRUCTURED_UNSET else content, ensure_ascii=False)
            except TypeError:
                stringified_content = str(content)
        result = {
            "content": [{
                "type": "text",
                "text": stringified_content
            }]
        }
        if structured is not _STRUCTURED_UNSET and (isinstance(structured, dict) or (output_schema is not None and not isinstance(content, str))):
            result["structuredContent"] = structured
        return result

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
        result["protocolVersion"] = exact_or_fallback(
            params.get("protocolVersion"), SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return self.create_response(request_id, result, None)

    def handle_tools_list(self, request_id: str | int | None, params: dict[str, Any]) -> dict[str, Any]:
        """List available tools with optional cursor pagination."""
        try:
            result = self._page_list(
                label="tools",
                items=self.discover_tools()["tools"],
                result_key="tools",
                identity_keys=("name",),
                params=params,
            )
        except ValueError as exc:
            return self.create_response(request_id, None, self.create_error(-32602, str(exc)))
        return self.create_response(request_id, result, None)

    def handle_tools_call(self, request_id: str | int | None, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``tools/call`` - delegates to tool implementations."""
        tool_name = params.get('name', '')
        arguments = params.get('arguments', {})

        self.logger.info("TOOL CALL: %s with args: %s", tool_name, list(arguments.keys()) if arguments else [])

        if not tool_name:
            error = self.create_error(-32602, "Missing 'name' parameter")
            return self.create_response(request_id, None, error)

        tool_method_name = f"tool_{tool_name}"

        output_schema: dict[str, Any] | None = None
        if tool_name in self._dynamic_tools:
            meta, method = self._dynamic_tools[tool_name]
            raw_output_schema = meta.get("outputSchema")
            if isinstance(raw_output_schema, dict):
                output_schema = raw_output_schema
        elif hasattr(self, tool_method_name):
            method = getattr(self, tool_method_name)
        else:
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
                self.logger.info("TOOL EXEC: %s (no params)", tool_name)
                content = method()
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

                self.logger.info("TOOL EXEC: %s with kwargs: %s", tool_name, list(kwargs.keys()))
                content = method(**kwargs)
        except ValueError as e:
            error = self.create_error(-32602, str(e))
            return self.create_response(request_id, None, error)
        except MCPRequestCancelled:
            raise
        except Exception as e:
            tb = traceback.format_exc()
            self.logger.error("TOOL ERROR: %s failed with %s: %s\n%s", tool_name, type(e).__name__, e, tb)
            message = self._remote_safe_failure(
                "Tool execution failed",
                f"Tool execution error for {tool_name}: {type(e).__name__}: {str(e)}",
            )
            error = self.create_error(-32603, message)
            return self.create_response(request_id, None, error)

        self.logger.info("TOOL SUCCESS: %s returned type: %s", tool_name, type(content).__name__)

        try:
            result = self._format_tool_result(method, content, output_schema=output_schema)
        except ValueError as exc:
            message = self._remote_safe_failure(
                "Tool output validation failed",
                f"Tool output validation failed for {tool_name}: {exc}",
            )
            return self.create_response(request_id, None, self.create_error(-32603, message))
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

    @staticmethod
    def _remote_safe_failure(public_message: str, detailed_message: str) -> str:
        transport = get_request_context().transport
        if transport in {"streamable-http", "sse", "tcp"}:
            return public_message
        return detailed_message

    def authenticate_request(self, *, method: str, path: str, headers: Mapping[str, str], peer: str | None) -> MCPPrincipal | None:
        legacy = type(self).authenticate
        if legacy is not MCPServer.authenticate:
            return legacy(self, headers, peer)
        return MCPPrincipal(name="anonymous")

    def authorize_request(self, principal: MCPPrincipal | None, *, rpc_method: str | None, tool_name: str | None) -> bool:
        legacy = type(self).authorize
        if legacy is not MCPServer.authorize:
            params: Mapping[str, Any] = {"name": tool_name} if tool_name is not None else {}
            return legacy(self, principal, rpc_method, params)
        return True

    def handle_http_request(self, *, method: str, path: str, headers: Mapping[str, str], body: bytes, peer: str | None) -> MCPHTTPResponse | None:
        return None

    # Back-compat aliases.
    def authenticate(self, headers: Mapping[str, str], peer: Any) -> MCPPrincipal | None:
        request_hook = type(self).authenticate_request
        if request_hook is not MCPServer.authenticate_request:
            return request_hook(self, method="", path="", headers=headers, peer=str(peer) if peer is not None else None)
        return MCPPrincipal(name="anonymous")

    def authorize(self, principal: MCPPrincipal | None, method: str | None, params: Mapping[str, Any]) -> bool:
        request_hook = type(self).authorize_request
        if request_hook is not MCPServer.authorize_request:
            return request_hook(self, principal, rpc_method=method, tool_name=(params.get("name") if isinstance(params, Mapping) else None))
        return True

    def _with_request_context(self, *, transport: str | None, request_id: str | int | None, principal: str | None, peer: str | None, headers: dict[str, str], version: str | None, session_id: str | None = None, progress_token: str | int | None = None):
        return set_request_context(MCPRequestContext(transport=transport, request_id=request_id, progress_token=progress_token, protocol_version=version, session_id=session_id, principal=principal, peer=peer, headers=headers))

    @staticmethod
    def _validate_progress_token(value: Any) -> str | int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("Invalid params: '_meta.progressToken' must be a string or integer")
        return value

    @staticmethod
    def _validate_progress_value(name: str, value: float | int) -> float | int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid progress notification: '{name}' must be a finite non-negative number")
        return value

    @staticmethod
    def _sanitize_progress_message(message: str | None) -> str | None:
        if message is None:
            return None
        text = str(message).replace("\x00", "")
        return text[:4096]

    def _register_request_cancellation(self, request_id: str | int | None, progress_token: str | int | None) -> MCPCancellationState:
        state = MCPCancellationState()
        with self._request_registry_lock:
            if request_id is not None:
                self._active_requests_by_id[request_id] = state
                if progress_token is not None:
                    self._active_requests_by_progress_token.setdefault(progress_token, set()).add(request_id)
        return state

    def _cleanup_request_cancellation(self, request_id: str | int | None, progress_token: str | int | None, state: MCPCancellationState) -> None:
        with self._request_registry_lock:
            if request_id is not None and self._active_requests_by_id.get(request_id) is state:
                self._active_requests_by_id.pop(request_id, None)
            if progress_token is not None and request_id is not None:
                request_ids = self._active_requests_by_progress_token.get(progress_token)
                if request_ids is not None:
                    request_ids.discard(request_id)
                    if not request_ids:
                        self._active_requests_by_progress_token.pop(progress_token, None)

    def _mark_request_cancelled(self, cancel_key: str | int | None) -> None:
        if cancel_key is None or isinstance(cancel_key, bool) or not isinstance(cancel_key, (str, int)):
            return
        with self._request_registry_lock:
            states: list[MCPCancellationState] = []
            direct = self._active_requests_by_id.get(cancel_key)
            if direct is not None:
                states.append(direct)
            for request_id in self._active_requests_by_progress_token.get(cancel_key, ()):
                state = self._active_requests_by_id.get(request_id)
                if state is not None and state not in states:
                    states.append(state)
        for state in states:
            state.mark_cancelled()

    def get_progress_token(self) -> str | int | None:
        return _get_progress_token()

    def is_request_cancelled(self) -> bool:
        return _is_request_cancelled()

    def raise_if_cancelled(self) -> None:
        _raise_if_cancelled()

    def notify_progress(self, progress: float | int, total: float | int | None = None, message: str | None = None) -> None:
        token = self.get_progress_token()
        if token is None:
            return
        progress = self._validate_progress_value("progress", progress)
        params: dict[str, Any] = {"progressToken": token, "progress": progress}
        if total is not None:
            total = self._validate_progress_value("total", total)
            if total < progress:
                raise ValueError("Invalid progress notification: 'total' must be greater than or equal to 'progress'")
            params["total"] = total
        sanitized = self._sanitize_progress_message(message)
        if sanitized:
            params["message"] = sanitized
        self._send_notification("notifications/progress", params)

    def _validate_http_principal(self, principal: MCPPrincipal | None) -> bool:
        return principal is None or isinstance(principal, MCPPrincipal)

    def _validate_http_authorization_result(self, authorized: Any) -> bool:
        return isinstance(authorized, bool)

    def _validate_http_route_response(self, response: Any, *, max_request_bytes: int) -> MCPHTTPResponse | None:
        return validate_http_response(response, max_bytes=max_request_bytes)

    def process_request(
        self, input_data: str, *, context: MCPRequestContext | None = None
    ) -> dict[str, Any] | None:
        """Process a JSON-RPC 2.0 request within an isolated request context."""
        try:
            request = loads(input_data)
        except JSONDecodeError as e:
            self.logger.error("Invalid JSON: %s", e)
            error = self.create_error(-32700, "Parse error")
            return self.create_response(None, None, error)

        if not is_jsonrpc_object(request):
            error = self.create_error(-32600, "Invalid Request: top-level JSON value must be an object")
            return self.create_response(None, None, error)

        jsonrpc = request.get('jsonrpc')
        request_id = request.get('id')
        method = request.get('method')
        params = request.get('params', {})

        if params is None:
            params = {}
        elif not isinstance(params, dict):
            error = self.create_error(-32602, "Invalid params: expected an object")
            return self.create_response(request_id, None, error)

        self.logger.info("Processing method: %s (id: %s)", method, request_id)

        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
        try:
            progress_token = self._validate_progress_token(meta.get("progressToken") if meta else None)
        except ValueError as exc:
            return self.create_response(request_id, None, self.create_error(-32602, str(exc)))

        if jsonrpc != "2.0":
            error = self.create_error(-32600, "Invalid Request: Not a JSON-RPC 2.0 request")
            return self.create_response(request_id, None, error)
        if "id" in request and not is_valid_jsonrpc_id(request_id):
            error = self.create_error(-32600, "Invalid Request: invalid id")
            return self.create_response(None, None, error)

        if method is None and ("result" in request or "error" in request):
            valid_response = is_valid_jsonrpc_response(request)
            if valid_response:
                return None  # A client response is acknowledged by the transport.
            error = self.create_error(-32600, "Invalid Request: malformed response")
            return self.create_response(request_id, None, error)
        if not isinstance(method, str):
            error = self.create_error(-32600, "Invalid Request: method must be a string")
            return self.create_response(request_id, None, error)

        if context is None:
            context = MCPRequestContext(request_id=request_id, progress_token=progress_token)
        else:
            context = MCPRequestContext(
                transport=context.transport,
                request_id=request_id,
                progress_token=progress_token,
                protocol_version=context.protocol_version,
                session_id=context.session_id,
                principal=context.principal,
                peer=context.peer,
                headers=context.headers,
            )
        cancellation_state = self._register_request_cancellation(request_id, progress_token) if request_id is not None else None
        runtime_token = set_request_runtime(MCPRequestRuntime(progress_token=progress_token, cancellation=cancellation_state, progress_callback=self.notify_progress))
        token = set_request_context(context)
        try:
            if method == "notifications/cancelled":
                cancel_id = params.get("requestId")
                if cancel_id is not None and not is_valid_jsonrpc_id(cancel_id):
                    if request_id is None:
                        return None
                    return self.create_response(request_id, None, self.create_error(-32602, "Invalid params: 'requestId' must be a string, integer, or null"))
                self._mark_request_cancelled(cancel_id)
                return None
            if method == "initialize":
                return self.handle_initialize(request_id, params)
            elif method == "tools/list":
                return self.handle_tools_list(request_id, params)
            elif method == "tools/call":
                return self.handle_tools_call(request_id, params)
            elif method == "prompts/list":
                try:
                    result = self._page_list(
                        label="prompts",
                        items=self.discover_prompts()["prompts"],
                        result_key="prompts",
                        identity_keys=("name",),
                        params=params,
                    )
                except ValueError as exc:
                    return self.create_response(request_id, None, self.create_error(-32602, str(exc)))
                return self.create_response(request_id, result, None)
            elif method == "prompts/get":
                return self.handle_prompt_get(request_id, params)
            elif method == "resources/list":
                return self.handle_resources_list(request_id, params)
            elif method == "resources/templates/list":
                return self.handle_resources_templates_list(request_id, params)
            elif method == "resources/read":
                return self.handle_resources_read(request_id, params)
            elif method == "resources/subscribe":
                return self.handle_resources_subscribe(request_id, params)
            elif method == "resources/unsubscribe":
                return self.handle_resources_unsubscribe(request_id, params)
            elif method == "completion/complete":
                return self.handle_completion_complete(request_id, params)
            elif method == "logging/setLevel":
                return self.handle_logging_set_level(request_id, params)
            elif method == "notifications/initialized":
                self.logger.info("Host confirmed toolContract reception with 'notifications/initialized'")
                return None
            else:
                error = self.create_error(-32601, f"Method not found: {method}")
                return self.create_response(request_id, None, error)
        except MCPRequestCancelled:
            if request_id is None:
                return None
            return self.create_response(request_id, None, self.create_error(-32800, "Request cancelled"))
        finally:
            reset_request_context(token)
            reset_request_runtime(runtime_token)
            if cancellation_state is not None:
                self._cleanup_request_cancellation(request_id, progress_token, cancellation_state)

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
                    self.connection.settimeout(30.0)
                    while True:
                        line = self.rfile.readline()
                        if not line:
                            break
                        decoded = line.decode("utf-8", errors="replace").strip()
                        if not decoded:
                            continue
                        server_self.logger.info("SOCKET REQUEST (%s): %s", peer, decoded)
                        peer_name = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) and len(peer) >= 2 else str(peer)
                        response = server_self.process_request(
                            decoded,
                            context=MCPRequestContext(transport="tcp", peer=peer_name),
                        )
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

    # ==== Streamable HTTP transport ====

    def run_streamable_http(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        endpoint: str = "/mcp",
        allowed_origins: list[str] | None = None,
        max_request_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        server_self = self
        allowed_origins = allowed_origins or []

        class _Handler(BaseHTTPRequestHandler):
            _HOOK_FAILURE = object()

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(30.0)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                server_self.logger.debug("HTTP %s - " + format, self.address_string(), *args)

            def _header_counts(self) -> dict[str, int]:
                return {name.lower(): len(self.headers.get_all(name) or []) for name in self.headers.keys()}

            def _headers_lower(self) -> dict[str, str]:
                return {k.lower(): v for k, v in self.headers.items()}

            def _allowed_origin(self) -> str | None:
                origin = self.headers.get("Origin")
                if not origin:
                    return None
                if origin_is_allowed(origin, allowed_origins, local_bind=host in ("127.0.0.1", "localhost", "::1")):
                    return origin
                return None

            def _request_path(self) -> str:
                return request_target_path(self.path)

            def _send_response(self, status: int, *, body: bytes = b"", content_type: str | None = None, allow: str | None = None, www_authenticate: str | None = None, origin: str | None = None, extra_headers: tuple[tuple[str, str], ...] = ()) -> None:
                self.send_response(status)
                if content_type:
                    self.send_header("Content-Type", content_type)
                if allow:
                    self.send_header("Allow", allow)
                if www_authenticate:
                    self.send_header("WWW-Authenticate", www_authenticate)
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                for key, value in extra_headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _json(self, payload: dict[str, Any], status: int = 200, *, origin: str | None = None) -> None:
                self._send_response(status, body=dumps(payload).encode("utf-8"), content_type="application/json", origin=origin)

            def _empty(self, status: int, *, allow: str | None = None, www_authenticate: str | None = None, origin: str | None = None, extra_headers: tuple[tuple[str, str], ...] = ()) -> None:
                self._send_response(status, allow=allow, www_authenticate=www_authenticate, origin=origin, extra_headers=extra_headers)

            def _send_method_not_allowed(self, allow: str = "POST, OPTIONS", *, origin: str | None = None) -> None:
                self._empty(405, allow=allow, origin=origin)

            def _bad_headers(self, *, origin: str | None) -> bool:
                return (
                    has_singleton_header_violations(self._header_counts(), http_version=self.request_version)
                    or has_ambiguous_singleton_values(self._headers_lower())
                )

            def _reject_disallowed_origin(self, origin: str | None) -> bool:
                if self.headers.get("Origin") and not origin:
                    self._empty(403)
                    return True
                return False

            def _read_bounded_body(self, *, origin: str | None) -> bytes | object:
                if self.headers.get("Transfer-Encoding"):
                    self._empty(400, origin=origin)
                    return self._HOOK_FAILURE
                cl = self.headers.get("Content-Length")
                try:
                    n = int(cl) if cl is not None else 0
                except ValueError:
                    self._empty(400, origin=origin)
                    return self._HOOK_FAILURE
                if n < 0:
                    self._empty(400, origin=origin)
                    return self._HOOK_FAILURE
                if n > max_request_bytes:
                    self._empty(413, origin=origin)
                    return self._HOOK_FAILURE
                try:
                    body = self.rfile.read(n)
                except TimeoutError:
                    self._empty(400, origin=origin)
                    return self._HOOK_FAILURE
                if len(body) != n:
                    self._empty(400, origin=origin)
                    return self._HOOK_FAILURE
                return body

            def _call_http_route(self, *, method: str, path: str, body: bytes, origin: str | None) -> MCPHTTPResponse | None | object:
                try:
                    response = server_self.handle_http_request(
                        method=method,
                        path=path,
                        headers=self._headers_lower(),
                        body=body,
                        peer=self.client_address[0] if self.client_address else None,
                    )
                except Exception:
                    server_self.logger.exception("HTTP auxiliary route hook failed for %s %s", method, path)
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                if isawaitable(response):
                    close = getattr(response, "close", None)
                    if close:
                        close()
                    server_self.logger.error("Async auxiliary HTTP hook used with MCPServer")
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                if response is None:
                    return None
                validated = server_self._validate_http_route_response(response, max_request_bytes=max_request_bytes)
                if validated is None:
                    server_self.logger.error("HTTP auxiliary route hook returned invalid response: %r", response)
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                self._send_response(validated.status, body=validated.body, content_type=validated.content_type, origin=origin, extra_headers=validated.headers)
                return validated

            def _call_authenticate(self, *, method: str, path: str, origin: str | None) -> MCPPrincipal | None | object:
                try:
                    principal = server_self.authenticate_request(method=method, path=path, headers=self._headers_lower(), peer=self.client_address[0] if self.client_address else None)
                except Exception:
                    server_self.logger.exception("HTTP authentication hook failed for %s %s", method, path)
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                if isawaitable(principal):
                    close = getattr(principal, "close", None)
                    if close:
                        close()
                    server_self.logger.error("Async authentication hook used with MCPServer")
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                if not server_self._validate_http_principal(principal):
                    server_self.logger.error("HTTP authentication hook returned invalid principal type: %r", type(principal))
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                return principal

            def _call_authorize(self, principal: MCPPrincipal | None, *, rpc_method: str | None, tool_name: str | None, origin: str | None) -> bool | object:
                try:
                    authorized = server_self.authorize_request(principal, rpc_method=rpc_method, tool_name=tool_name)
                except Exception:
                    server_self.logger.exception("HTTP authorization hook failed for rpc_method=%r tool_name=%r", rpc_method, tool_name)
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                if isawaitable(authorized):
                    close = getattr(authorized, "close", None)
                    if close:
                        close()
                    server_self.logger.error("Async authorization hook used with MCPServer")
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                if not server_self._validate_http_authorization_result(authorized):
                    server_self.logger.error("HTTP authorization hook returned invalid result type: %r", type(authorized))
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                return authorized

            def do_OPTIONS(self) -> None:  # noqa: N802
                origin_header = self.headers.get("Origin")
                origin = self._allowed_origin()
                if self._bad_headers(origin=origin):
                    self._empty(400, origin=origin)
                    return
                if self._reject_disallowed_origin(origin):
                    return
                request_path = self._request_path()
                if request_path != endpoint:
                    body = self._read_bounded_body(origin=origin)
                    if body is self._HOOK_FAILURE:
                        return
                    response = self._call_http_route(method="OPTIONS", path=request_path, body=body, origin=origin)
                    if response is self._HOOK_FAILURE:
                        return
                    if response is None:
                        self._send_method_not_allowed(origin=origin)
                    return
                if not origin_header:
                    self._send_method_not_allowed(origin=origin)
                    return
                self._empty(204, origin=origin, extra_headers=(("Access-Control-Allow-Methods", "POST, OPTIONS"), ("Access-Control-Allow-Headers", "Content-Type, Accept, MCP-Protocol-Version, Authorization")))

            def do_GET(self) -> None:
                origin = self._allowed_origin()
                if self._bad_headers(origin=origin):
                    self._empty(400, origin=origin)
                    return
                if self._reject_disallowed_origin(origin):
                    return
                request_path = self._request_path()
                if request_path != endpoint:
                    body = self._read_bounded_body(origin=origin)
                    if body is self._HOOK_FAILURE:
                        return
                    response = self._call_http_route(method="GET", path=request_path, body=body, origin=origin)
                    if response is self._HOOK_FAILURE:
                        return
                    if response is None:
                        self._send_method_not_allowed(origin=origin)
                    return
                self._send_method_not_allowed(origin=origin)

            def do_DELETE(self) -> None:
                origin = self._allowed_origin()
                if self._bad_headers(origin=origin):
                    self._empty(400, origin=origin)
                    return
                if self._reject_disallowed_origin(origin):
                    return
                request_path = self._request_path()
                if request_path != endpoint:
                    body = self._read_bounded_body(origin=origin)
                    if body is self._HOOK_FAILURE:
                        return
                    response = self._call_http_route(method="DELETE", path=request_path, body=body, origin=origin)
                    if response is self._HOOK_FAILURE:
                        return
                    if response is None:
                        self._send_method_not_allowed(origin=origin)
                    return
                self._send_method_not_allowed(origin=origin)

            def do_POST(self) -> None:  # noqa: N802
                self.connection.settimeout(30.0)
                origin = self._allowed_origin()
                if self._bad_headers(origin=origin):
                    self._empty(400, origin=origin)
                    return
                if self._reject_disallowed_origin(origin):
                    return
                request_path = self._request_path()
                body = self._read_bounded_body(origin=origin)
                if body is self._HOOK_FAILURE:
                    return
                if request_path != endpoint:
                    response = self._call_http_route(method="POST", path=request_path, body=body, origin=origin)
                    if response is self._HOOK_FAILURE:
                        return
                    if response is None:
                        self._send_method_not_allowed(origin=origin)
                    return
                if not content_type_is_json(self.headers.get("Content-Type")):
                    self._empty(415, origin=origin); return
                if not media_accepts_json(self.headers.get("Accept")):
                    self._empty(406, origin=origin); return
                principal = self._call_authenticate(method="POST", path=self.path, origin=origin)
                if principal is self._HOOK_FAILURE:
                    return
                if principal is None:
                    self._empty(401, www_authenticate="Bearer", origin=origin); return
                try:
                    req = loads(body.decode("utf-8"))
                except (UnicodeDecodeError, JSONDecodeError):
                    self._json({"jsonrpc": "2.0", "error": server_self.create_error(-32700, "Parse error"), "id": None}, origin=origin); return
                if not isinstance(req, dict):
                    self._json(server_self.create_response(None, None, server_self.create_error(-32600, "Invalid Request")), origin=origin)
                    return
                rpc_method = req.get("method")
                version = self.headers.get("MCP-Protocol-Version")
                if rpc_method != "initialize" and version not in SUPPORTED_PROTOCOL_VERSIONS:
                    self._empty(400, origin=origin); return
                is_response = (
                    rpc_method is None
                    and "id" in req
                    and is_valid_jsonrpc_response(req)
                )
                is_notification = rpc_method is not None and "id" not in req
                if is_response:
                    self._empty(202, origin=origin); return
                params = req.get("params") if isinstance(req.get("params"), dict) else {}
                tool_name = params.get("name") if rpc_method == "tools/call" else None
                authorized = self._call_authorize(principal, rpc_method=rpc_method, tool_name=tool_name, origin=origin)
                if authorized is self._HOOK_FAILURE:
                    return
                if not authorized:
                    self._empty(403, origin=origin)
                    return
                context = MCPRequestContext(transport="streamable-http", request_id=req.get("id"), protocol_version=version, session_id=None, principal=principal.name if principal else None, peer=self.client_address[0] if self.client_address else None, headers=self._headers_lower())
                response = server_self.process_request(dumps(req), context=context)
                if is_notification:
                    response = None
                if response is None:
                    self._empty(202, origin=origin); return
                self._json(response, origin=origin)

        if host not in ("127.0.0.1", "localhost", "::1") and type(self).authenticate_request is MCPServer.authenticate_request and type(self).authenticate is MCPServer.authenticate:
            self.logger.warning("Streamable HTTP is bound beyond loopback without an authentication hook")
        httpd = ThreadingHTTPServer((host, port), _Handler); httpd.daemon_threads = True
        actual_host, actual_port = httpd.server_address[:2]
        print(f"MCP Streamable HTTP Server listening on http://{actual_host}:{actual_port}{endpoint}", flush=True)
        httpd.serve_forever()

    # ==== SSE (Server-Sent Events) HTTP transport ====

    def run_sse(self, host: str = "127.0.0.1", port: int = 0, allowed_origins: list[str] | None = None, max_request_bytes: int = 4 * 1024 * 1024) -> None:
        """Run the MCP server over HTTP with the SSE transport.

        Implements the MCP SSE transport protocol:
          GET  /sse       -> event stream (Content-Type: text/event-stream)
          POST /message   -> JSON-RPC request, response pushed to the SSE stream

        VS Code mcp.json example::

            { "type": "sse", "url": "http://127.0.0.1:<port>/sse" }
        """
        server_self = self
        allowed_origins = allowed_origins or []

        class _Handler(BaseHTTPRequestHandler):
            _HOOK_FAILURE = object()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                server_self.logger.debug("HTTP %s - " + format, self.address_string(), *args)

            def _header_counts(self) -> dict[str, int]:
                return {name.lower(): len(self.headers.get_all(name) or []) for name in self.headers.keys()}

            def _headers_lower(self) -> dict[str, str]:
                return {k.lower(): v for k, v in self.headers.items()}

            def _bad_headers(self) -> bool:
                return (
                    has_singleton_header_violations(self._header_counts(), http_version=self.request_version)
                    or has_ambiguous_singleton_values(self._headers_lower())
                )

            def _allowed_origin(self) -> str | None:
                origin = self.headers.get("Origin")
                if not origin:
                    return None
                if origin_is_allowed(origin, allowed_origins, local_bind=host in ("127.0.0.1", "localhost", "::1")):
                    return origin
                return None

            def _send_response(self, status: int, *, body: bytes = b"", content_type: str | None = None, origin: str | None = None, extra_headers: tuple[tuple[str, str], ...] = ()) -> None:
                self.send_response(status)
                if content_type:
                    self.send_header("Content-Type", content_type)
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                for key, value in extra_headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _empty(self, status: int, *, origin: str | None = None, extra_headers: tuple[tuple[str, str], ...] = ()) -> None:
                self._send_response(status, origin=origin, extra_headers=extra_headers)

            def _reject_disallowed_origin(self, origin: str | None) -> bool:
                if self.headers.get("Origin") and not origin:
                    self._empty(403)
                    return True
                return False

            def _call_authenticate(self, *, method: str, path: str, origin: str | None) -> MCPPrincipal | None | object:
                try:
                    principal = server_self.authenticate_request(method=method, path=path, headers=self._headers_lower(), peer=self.client_address[0] if self.client_address else None)
                except Exception:
                    server_self.logger.exception("SSE authentication hook failed for %s %s", method, path)
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                if isawaitable(principal):
                    close = getattr(principal, "close", None)
                    if close:
                        close()
                    server_self.logger.error("Async authentication hook used with MCPServer")
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                if not server_self._validate_http_principal(principal):
                    server_self.logger.error("SSE authentication hook returned invalid principal type: %r", type(principal))
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                return principal

            def _call_authorize(self, principal: MCPPrincipal | None, *, rpc_method: str | None, tool_name: str | None, origin: str | None) -> bool | object:
                try:
                    authorized = server_self.authorize_request(principal, rpc_method=rpc_method, tool_name=tool_name)
                except Exception:
                    server_self.logger.exception("SSE authorization hook failed for rpc_method=%r tool_name=%r", rpc_method, tool_name)
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                if isawaitable(authorized):
                    close = getattr(authorized, "close", None)
                    if close:
                        close()
                    server_self.logger.error("Async authorization hook used with MCPServer")
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                if not server_self._validate_http_authorization_result(authorized):
                    server_self.logger.error("SSE authorization hook returned invalid result type: %r", type(authorized))
                    self._empty(500, origin=origin)
                    return self._HOOK_FAILURE
                return authorized

            def do_OPTIONS(self) -> None:  # noqa: N802
                origin = self._allowed_origin()
                if self._bad_headers():
                    self._empty(400, origin=origin)
                    return
                if self._reject_disallowed_origin(origin):
                    return
                if request_target_path(self.path) not in {"/sse", "/message"}:
                    self._empty(404, origin=origin)
                    return
                if not self.headers.get("Origin"):
                    self._empty(405, origin=origin, extra_headers=(("Allow", "GET, POST, OPTIONS"),))
                    return
                self._empty(204, origin=origin, extra_headers=(("Access-Control-Allow-Methods", "GET, POST, OPTIONS"), ("Access-Control-Allow-Headers", "Content-Type, Accept, Authorization")))

            def do_GET(self) -> None:  # noqa: N802
                origin = self._allowed_origin()
                if self._bad_headers():
                    self._empty(400, origin=origin)
                    return
                if self._reject_disallowed_origin(origin):
                    return
                if request_target_path(self.path) != "/sse":
                    self._empty(404, origin=origin)
                    return
                if not media_accepts_event_stream(self.headers.get("Accept")):
                    self._empty(406, origin=origin)
                    return
                principal = self._call_authenticate(method="GET", path=self.path, origin=origin)
                if principal is self._HOOK_FAILURE:
                    return
                if principal is None:
                    self._send_response(401, origin=origin, extra_headers=(("WWW-Authenticate", "Bearer"),))
                    return
                authorized = self._call_authorize(principal, rpc_method=None, tool_name=None, origin=origin)
                if authorized is self._HOOK_FAILURE:
                    return
                if not authorized:
                    self._empty(403, origin=origin)
                    return

                session_id = str(uuid4())
                queue: Queue = Queue()
                with server_self._sse_lock:
                    server_self._sse_sessions[session_id] = (queue, principal.name)
                server_self.logger.info("SSE: new session %s from %s", session_id, self.client_address)

                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    if origin:
                        self.send_header("Access-Control-Allow-Origin", origin)
                        self.send_header("Vary", "Origin")
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
                    server_self._resource_session_subscriptions.pop(session_id, None)
                    server_self.logger.info("SSE: session %s closed", session_id)

            def do_POST(self) -> None:  # noqa: N802
                origin = self._allowed_origin()
                if self._bad_headers():
                    self._empty(400, origin=origin)
                    return
                if self._reject_disallowed_origin(origin):
                    return
                if request_target_path(self.path) != "/message":
                    self._empty(404, origin=origin)
                    return
                if self.headers.get("Transfer-Encoding"):
                    self._empty(400, origin=origin)
                    return
                if not content_type_is_json(self.headers.get("Content-Type")):
                    self._empty(415, origin=origin)
                    return
                if not media_accepts_json(self.headers.get("Accept")):
                    self._empty(406, origin=origin)
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    self._empty(400, origin=origin)
                    return
                if length < 0:
                    self._empty(400, origin=origin)
                    return
                if length > max_request_bytes:
                    self._empty(413, origin=origin)
                    return

                qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                session_id = (qs.get("sessionId") or [None])[0]
                principal = self._call_authenticate(method="POST", path=self.path, origin=origin)
                if principal is self._HOOK_FAILURE:
                    return
                if principal is None:
                    self._send_response(401, origin=origin, extra_headers=(("WWW-Authenticate", "Bearer"),))
                    return
                with server_self._sse_lock:
                    session = server_self._sse_sessions.get(session_id) if session_id else None
                if session is None:
                    server_self.logger.warning("SSE: unknown session %s from %s", session_id, self.client_address)
                    self._empty(404, origin=origin)
                    return
                queue, owner = session
                if owner != principal.name:
                    self._empty(403, origin=origin)
                    return
                try:
                    body = self.rfile.read(length) if length > 0 else b""
                except TimeoutError:
                    self._empty(400, origin=origin)
                    return
                if len(body) != length:
                    self._empty(400, origin=origin)
                    return
                try:
                    request_data = body.decode("utf-8")
                except UnicodeDecodeError:
                    self._empty(400, origin=origin)
                    return
                server_self.logger.info("SSE REQUEST (session %s): %s", session_id, request_data[:200])
                try:
                    request_obj = loads(request_data)
                except JSONDecodeError:
                    request_obj = None
                rpc_method = request_obj.get("method") if isinstance(request_obj, dict) else None
                params = request_obj.get("params") if isinstance(request_obj, dict) and isinstance(request_obj.get("params"), dict) else {}
                tool_name = params.get("name") if rpc_method == "tools/call" else None
                authorized = self._call_authorize(principal, rpc_method=rpc_method, tool_name=tool_name, origin=origin)
                if authorized is self._HOOK_FAILURE:
                    return
                if not authorized:
                    self._empty(403, origin=origin)
                    return
                if isinstance(request_obj, dict) and request_obj.get("method") in ("resources/subscribe", "resources/unsubscribe"):
                    request_obj["params"] = dict(params)
                    request_obj["params"]["_session_id"] = session_id
                    request_data = dumps(request_obj)
                response = server_self.process_request(request_data, context=MCPRequestContext(transport="sse", request_id=request_obj.get("id") if isinstance(request_obj, dict) else None, session_id=session_id, principal=principal.name if principal else None, peer=self.client_address[0] if self.client_address else None, headers=self._headers_lower()))
                with server_self._sse_lock:
                    current_session = server_self._sse_sessions.get(session_id)
                    if current_session is not session:
                        self._empty(404, origin=origin)
                        return
                    if response is not None:
                        response_json = dumps(response)
                        queue.put(f"event: message\ndata: {response_json}\n\n".encode())
                self._empty(202, origin=origin)

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

        # ---- Parse CLI flags ----
        port: int | None = None
        host: str = "127.0.0.1"
        use_tcp: bool = False
        transport: str | None = None
        endpoint = "/mcp"
        max_request_bytes = 4 * 1024 * 1024
        allowed_origins: list[str] = []
        remaining: list[str] = []
        i = 0
        while i < len(args):
            if args[i] in ("--port", "-p") and i + 1 < len(args):
                port = int(args[i + 1]); i += 2
            elif args[i] == "--host" and i + 1 < len(args):
                host = args[i + 1]; i += 2
            elif args[i] == "--endpoint" and i + 1 < len(args):
                endpoint = args[i + 1]; i += 2
            elif args[i] == "--max-request-bytes" and i + 1 < len(args):
                max_request_bytes = int(args[i + 1]); i += 2
            elif args[i] == "--allowed-origin" and i + 1 < len(args):
                allowed_origins.append(args[i + 1]); i += 2
            elif args[i] == "--transport" and i + 1 < len(args):
                selected = args[i + 1]
                if transport is not None and transport != selected:
                    raise ValueError("conflicting transport options")
                transport = selected; i += 2
            elif args[i] in ("--tcp", "--http", "--sse"):
                selected = {"--tcp": "tcp", "--http": "streamable-http", "--sse": "sse"}[args[i]]
                if transport is not None and transport != selected:
                    raise ValueError("conflicting transport options")
                use_tcp = selected == "tcp"; transport = selected; i += 1
            else:
                remaining.append(args[i])
                i += 1

        valid_transports = {"stdio", "streamable-http", "sse", "tcp"}
        if transport not in valid_transports | {None}:
            raise ValueError(f"unsupported transport: {transport}")
        if transport not in (None, "stdio") and port is None:
            raise ValueError("network transports require --port")
        if transport == "stdio" and port is not None:
            raise ValueError("stdio transport cannot use --port")
        if port is not None:
            mode = transport or ("tcp" if use_tcp else "sse")
            if mode == "tcp": self.run_socket(host=host, port=port)
            elif mode == "streamable-http": self.run_streamable_http(host=host, port=port, endpoint=endpoint, allowed_origins=allowed_origins, max_request_bytes=max_request_bytes)
            else: self.run_sse(host=host, port=port, allowed_origins=allowed_origins, max_request_bytes=max_request_bytes)
            return

        # ---- Original stdio / file transport ----
        args = remaining

        if args:
            try:
                with open(args[0], encoding='utf-8') as f:
                    input_data = f.read()
                self.logger.info("REQUEST: %s", input_data)
                response = self.process_request(input_data, context=MCPRequestContext(transport="file"))
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
                    response = self.process_request(line, context=MCPRequestContext(transport="stdio"))
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
