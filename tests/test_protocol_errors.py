"""JSON-RPC and MCP protocol error-path tests for both bases.

Covers the failure cases the happy-path suites don't exercise:

* malformed JSON input
* wrong jsonrpc version
* unknown methods (-32601)
* tools/call without name (-32602)
* tools/call with unknown name (-32601)
* tools/call with unknown argument (additionalProperties: false)
* tool method that raises -- the wire-level handler must trap it
* prompts/get with unknown name
* notifications/initialized -- must not produce a response
"""

from __future__ import annotations

import asyncio
import json

from aioumcp import AsyncMCPServer
from umcp import MCPServer


# ---------- Sync server fixtures ---------------------------------------------


class _SyncProtoServer(MCPServer):
    """Tiny sync server with one happy tool and one that always raises."""

    def tool_echo(self, message: str) -> str:
        """Return the supplied message."""
        return message

    def tool_explode(self) -> str:
        """Always raises -- used to test wire-level error handling."""
        raise RuntimeError("boom")

    def prompt_hello(self, name: str = "world") -> str:
        """A trivial prompt."""
        return f"Hello, {name}!"


def _send_sync(server: MCPServer, request: dict | str) -> dict | None:
    payload = request if isinstance(request, str) else json.dumps(request)
    return server.process_request(payload)


def test_sync_invalid_json_returns_parse_error() -> None:
    s = _SyncProtoServer()
    resp = _send_sync(s, "not json at all")
    assert resp is not None
    assert resp["error"]["code"] == -32700  # JSON-RPC parse error


def test_sync_non_object_top_level_json_is_invalid_request() -> None:
    s = _SyncProtoServer()
    resp = _send_sync(s, "[]")
    assert resp["error"]["code"] == -32600


def test_sync_wrong_jsonrpc_version_rejected() -> None:
    s = _SyncProtoServer()
    resp = _send_sync(s, {"jsonrpc": "1.0", "id": 1, "method": "tools/list"})
    assert resp["error"]["code"] == -32600


def test_sync_unknown_method_returns_minus_32601() -> None:
    s = _SyncProtoServer()
    resp = _send_sync(s, {"jsonrpc": "2.0", "id": 1, "method": "tools/wat"})
    assert resp["error"]["code"] == -32601


def test_sync_non_object_params_returns_invalid_params() -> None:
    s = _SyncProtoServer()
    resp = _send_sync(s, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []
    })
    assert resp["error"]["code"] == -32602


def test_sync_tools_call_missing_name() -> None:
    s = _SyncProtoServer()
    resp = _send_sync(s, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
    assert resp["error"]["code"] == -32602


def test_sync_tools_call_unknown_tool() -> None:
    s = _SyncProtoServer()
    resp = _send_sync(s, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "no_such_tool"},
    })
    assert resp["error"]["code"] == -32601


def test_sync_tools_call_with_unknown_argument_is_rejected() -> None:
    s = _SyncProtoServer()
    resp = _send_sync(s, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "echo", "arguments": {"message": "hi", "force": True}},
    })
    assert resp["error"]["code"] == -32602


def test_sync_tool_that_raises_does_not_crash_server() -> None:
    s = _SyncProtoServer()
    resp = _send_sync(s, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "explode", "arguments": {}},
    })
    assert resp["error"]["code"] == -32603


def test_sync_prompts_get_unknown_returns_error() -> None:
    s = _SyncProtoServer()
    resp = _send_sync(s, {
        "jsonrpc": "2.0", "id": 1, "method": "prompts/get",
        "params": {"name": "no_such_prompt"},
    })
    assert "error" in resp


def test_sync_notifications_initialized_returns_none() -> None:
    s = _SyncProtoServer()
    # Notifications have no id and the server must not produce a response.
    resp = _send_sync(s, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp is None


# ---------- Async server -----------------------------------------------------


class _AsyncProtoServer(AsyncMCPServer):
    async def tool_echo(self, message: str) -> str:
        """Return the supplied message."""
        return message

    async def tool_explode(self) -> str:
        """Always raises -- used to test wire-level error handling."""
        raise RuntimeError("async boom")


def _send_async(server: AsyncMCPServer, request: dict | str) -> dict | None:
    payload = request if isinstance(request, str) else json.dumps(request)
    return asyncio.run(server.process_request_async(payload))


def test_async_invalid_json_returns_parse_error() -> None:
    s = _AsyncProtoServer()
    resp = _send_async(s, "definitely not json")
    assert resp is not None
    assert resp["error"]["code"] == -32700


def test_async_non_object_top_level_json_is_invalid_request() -> None:
    s = _AsyncProtoServer()
    resp = _send_async(s, "[]")
    assert resp["error"]["code"] == -32600


def test_async_unknown_method_returns_minus_32601() -> None:
    s = _AsyncProtoServer()
    resp = _send_async(s, {"jsonrpc": "2.0", "id": 1, "method": "tools/nope"})
    assert resp["error"]["code"] == -32601


def test_async_non_object_params_returns_invalid_params() -> None:
    s = _AsyncProtoServer()
    resp = _send_async(s, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []
    })
    assert resp["error"]["code"] == -32602


def test_async_tools_call_unknown_tool() -> None:
    s = _AsyncProtoServer()
    resp = _send_async(s, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "ghost"},
    })
    assert resp["error"]["code"] == -32601


def test_async_tool_that_raises_does_not_crash_server() -> None:
    s = _AsyncProtoServer()
    resp = _send_async(s, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "explode", "arguments": {}},
    })
    assert resp["error"]["code"] == -32603


def test_async_notifications_initialized_returns_none() -> None:
    s = _AsyncProtoServer()
    resp = _send_async(s, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp is None
