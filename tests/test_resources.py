#!/usr/bin/env python3
"""
test_resources.py -- exercise the resources/* MCP methods against both
the synchronous (umcp.MCPServer) and asynchronous (aioumcp.AsyncMCPServer)
base classes.

Covers:
  * resources/list                         -- static resource discovery
  * resources/templates/list               -- templated resource discovery
  * resources/read (static text)           -- text contents
  * resources/read (static bytes)          -- base64-encoded blob
  * resources/read (template)              -- URI template parameter binding
  * resources/read (unknown URI)           -- -32002 not-found error
  * resources/subscribe / unsubscribe      -- subscription tracking
  * dynamic register_resource              -- runtime registration
  * initialize capability declaration      -- resources capability flags
"""

import asyncio
import json
import sys
from base64 import b64decode
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from umcp import MCPServer  # noqa: E402
from aioumcp import AsyncMCPServer  # noqa: E402


# ---------- Sync server fixture ------------------------------------------------


class _SyncServer(MCPServer):
    """Sync server with one of every resource shape."""

    def __init__(self) -> None:
        super().__init__()
        self.value = "v1"

    def resource_motd(self) -> str:
        """Message of the day."""
        return self.value
    resource_motd._mcp_resource = {"mime_type": "text/plain", "title": "MOTD"}

    def resource_logo(self) -> bytes:
        """Binary blob."""
        return b"\x00\x01\x02\x03"
    resource_logo._mcp_resource = {"mime_type": "image/png"}

    def resource_template_user(self, user_id: str) -> dict:
        """User profile template."""
        return {"text": f"id={user_id}", "mimeType": "application/json"}


def _send_sync(server: MCPServer, request: dict) -> dict:
    return server.process_request(json.dumps(request))


def test_sync_resources_list_includes_static() -> None:
    s = _SyncServer()
    resp = _send_sync(s, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    uris = {r["uri"] for r in resp["result"]["resources"]}
    assert "umcp://_SyncServer/motd" in uris
    assert "umcp://_SyncServer/logo" in uris


def test_sync_templates_list_includes_template() -> None:
    s = _SyncServer()
    resp = _send_sync(s, {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"})
    templates = {t["uriTemplate"] for t in resp["result"]["resourceTemplates"]}
    assert "umcp://_SyncServer/user/{user_id}" in templates


def test_sync_read_text_resource() -> None:
    s = _SyncServer()
    resp = _send_sync(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://_SyncServer/motd"},
    })
    contents = resp["result"]["contents"]
    assert contents == [{
        "uri": "umcp://_SyncServer/motd",
        "mimeType": "text/plain",
        "text": "v1",
    }]


def test_sync_read_binary_resource_is_base64() -> None:
    s = _SyncServer()
    resp = _send_sync(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://_SyncServer/logo"},
    })
    entry = resp["result"]["contents"][0]
    assert entry["mimeType"] == "image/png"
    assert "blob" in entry and "text" not in entry
    assert b64decode(entry["blob"]) == b"\x00\x01\x02\x03"


def test_sync_read_template_resource_binds_param() -> None:
    s = _SyncServer()
    resp = _send_sync(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://_SyncServer/user/42"},
    })
    contents = resp["result"]["contents"][0]
    assert contents["text"] == "id=42"
    assert contents["mimeType"] == "application/json"
    assert contents["uri"] == "umcp://_SyncServer/user/42"


def test_sync_read_unknown_uri_returns_minus_32002() -> None:
    s = _SyncServer()
    resp = _send_sync(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://nope"},
    })
    assert resp["error"]["code"] == -32002
    assert resp["error"]["data"] == {"uri": "umcp://nope"}


def test_sync_subscribe_unsubscribe_roundtrip() -> None:
    s = _SyncServer()
    uri = "umcp://_SyncServer/motd"
    r1 = _send_sync(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
        "params": {"uri": uri},
    })
    assert r1["result"] == {}
    assert uri in s._resource_subscriptions
    r2 = _send_sync(s, {
        "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
        "params": {"uri": uri},
    })
    assert r2["result"] == {}
    assert uri not in s._resource_subscriptions


def test_sync_dynamic_register_resource() -> None:
    s = _SyncServer()
    s.register_resource(
        "config://flags",
        lambda: "feature.enabled=true",
        mime_type="text/plain",
        title="Feature flags",
    )
    listing = _send_sync(s, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    assert "config://flags" in {r["uri"] for r in listing["result"]["resources"]}
    read = _send_sync(s, {
        "jsonrpc": "2.0", "id": 2, "method": "resources/read",
        "params": {"uri": "config://flags"},
    })
    assert read["result"]["contents"][0]["text"] == "feature.enabled=true"


def test_sync_initialize_declares_resources_capability() -> None:
    s = _SyncServer()
    resp = _send_sync(s, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    caps = resp["result"]["capabilities"]
    assert caps["resources"] == {"subscribe": True, "listChanged": True}


# ---------- Async server -------------------------------------------------------


class _AsyncServer(AsyncMCPServer):
    """Async server with sync and async resource methods."""

    def __init__(self) -> None:
        super().__init__()

    async def resource_motd(self) -> str:
        """Async MOTD."""
        return "async-v1"
    resource_motd._mcp_resource = {"mime_type": "text/plain"}

    def resource_logo(self) -> bytes:
        """Sync binary inside an async server -- both should work."""
        return b"AB"
    resource_logo._mcp_resource = {"mime_type": "image/png"}

    async def resource_template_doc(self, doc_id: str) -> str:
        """Async templated resource."""
        return f"document {doc_id}"


def _send_async(server: AsyncMCPServer, request: dict) -> dict:
    return asyncio.run(server.process_request_async(json.dumps(request)))


def test_async_resources_list() -> None:
    s = _AsyncServer()
    resp = _send_async(s, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    uris = {r["uri"] for r in resp["result"]["resources"]}
    assert "umcp://_AsyncServer/motd" in uris
    assert "umcp://_AsyncServer/logo" in uris


def test_async_read_async_text_resource() -> None:
    s = _AsyncServer()
    resp = _send_async(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://_AsyncServer/motd"},
    })
    assert resp["result"]["contents"][0]["text"] == "async-v1"


def test_async_read_sync_method_in_async_server() -> None:
    s = _AsyncServer()
    resp = _send_async(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://_AsyncServer/logo"},
    })
    entry = resp["result"]["contents"][0]
    assert b64decode(entry["blob"]) == b"AB"


def test_async_read_template_resource() -> None:
    s = _AsyncServer()
    resp = _send_async(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://_AsyncServer/doc/abc"},
    })
    assert resp["result"]["contents"][0]["text"] == "document abc"


def test_async_unknown_uri_returns_minus_32002() -> None:
    s = _AsyncServer()
    resp = _send_async(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://missing"},
    })
    assert resp["error"]["code"] == -32002


def test_async_initialize_declares_resources_capability() -> None:
    s = _AsyncServer()
    resp = _send_async(s, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["result"]["capabilities"]["resources"] == {
        "subscribe": True, "listChanged": True,
    }


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
