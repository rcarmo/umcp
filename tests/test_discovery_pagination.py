from __future__ import annotations

import asyncio
import json
from io import BytesIO

import aioumcp
import umcp
from aioumcp import AsyncMCPServer
from umcp import MCPServer
from umcp_shared import MCPRequestContext


def _send_sync(server: MCPServer, request: dict, *, context: MCPRequestContext | None = None) -> dict:
    return server.process_request(json.dumps(request), context=context)


async def _send_async_inner(server: AsyncMCPServer, request: dict, *, context: MCPRequestContext | None = None) -> dict:
    return await server.process_request_async(json.dumps(request), context=context)


def _send_async(server: AsyncMCPServer, request: dict, *, context: MCPRequestContext | None = None) -> dict:
    return asyncio.run(_send_async_inner(server, request, context=context))


class _SyncDiscovery(MCPServer):
    def tool_zeta(self) -> str:
        return "z"

    def tool_alpha(self) -> str:
        return "a"

    def prompt_zulu(self) -> str:
        return "z"

    def prompt_alpha(self) -> str:
        return "a"

    def resource_zeta(self) -> str:
        return "z"

    def resource_alpha(self) -> str:
        return "a"

    def resource_template_zulu(self, value: str) -> str:
        return value

    def resource_template_alpha(self, value: str) -> str:
        return value


class _AsyncDiscovery(AsyncMCPServer):
    async def tool_zeta(self) -> str:
        return "z"

    async def tool_alpha(self) -> str:
        return "a"

    async def prompt_zulu(self) -> str:
        return "z"

    async def prompt_alpha(self) -> str:
        return "a"

    async def resource_zeta(self) -> str:
        return "z"

    async def resource_alpha(self) -> str:
        return "a"

    async def resource_template_zulu(self, value: str) -> str:
        return value

    async def resource_template_alpha(self, value: str) -> str:
        return value


def _collect_names(first: dict, key: str) -> list[str]:
    names = [item["name"] for item in first["result"][key]]
    cursor = first["result"].get("nextCursor")
    return names, cursor


def test_sync_dynamic_discovery_registration_and_pagination() -> None:
    server = _SyncDiscovery()

    def dyn_tool(city: str = "Lisbon") -> str:
        return city

    def dyn_prompt(topic: str = "umcp") -> str:
        return topic

    server.register_tool("mid", dyn_tool)
    server.register_prompt("mid", dyn_prompt, categories=["dynamic"])

    tools_page_1 = _send_sync(server, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"pageSize": 2},
    })
    assert [item["name"] for item in tools_page_1["result"]["tools"]] == ["alpha", "mid"]
    assert tools_page_1["result"]["nextCursor"]

    tools_page_2 = _send_sync(server, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        "params": {"cursor": tools_page_1["result"]["nextCursor"], "pageSize": 2},
    })
    assert [item["name"] for item in tools_page_2["result"]["tools"]] == ["zeta"]
    assert "nextCursor" not in tools_page_2["result"]

    prompts_page_1 = _send_sync(server, {
        "jsonrpc": "2.0", "id": 3, "method": "prompts/list", "params": {"pageSize": 2},
    })
    assert [item["name"] for item in prompts_page_1["result"]["prompts"]] == ["alpha", "mid"]
    prompt = next(item for item in server.discover_prompts()["prompts"] if item["name"] == "mid")
    assert prompt["categories"] == ["dynamic"]

    resources_page_1 = _send_sync(server, {
        "jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {"pageSize": 1},
    })
    assert [item["name"] for item in resources_page_1["result"]["resources"]] == ["alpha"]
    resources_page_2 = _send_sync(server, {
        "jsonrpc": "2.0", "id": 5, "method": "resources/list",
        "params": {"cursor": resources_page_1["result"]["nextCursor"], "pageSize": 1},
    })
    assert [item["name"] for item in resources_page_2["result"]["resources"]] == ["zeta"]

    templates_page_1 = _send_sync(server, {
        "jsonrpc": "2.0", "id": 6, "method": "resources/templates/list", "params": {"pageSize": 1},
    })
    assert [item["name"] for item in templates_page_1["result"]["resourceTemplates"]] == ["alpha"]

    assert _send_sync(server, {
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "mid", "arguments": {"city": "Porto"}},
    })["result"]["content"][0]["text"] == "Porto"
    assert _send_sync(server, {
        "jsonrpc": "2.0", "id": 8, "method": "prompts/get",
        "params": {"name": "mid", "arguments": {"topic": "prompts"}},
    })["result"]["messages"][0]["content"]["text"] == "prompts"

    assert server.unregister_tool("mid") is True
    assert server.unregister_prompt("mid") is True
    assert server.unregister_tool("mid") is False
    assert server.unregister_prompt("mid") is False
    assert [item["name"] for item in server.discover_tools()["tools"]] == ["alpha", "zeta"]
    assert [item["name"] for item in server.discover_prompts()["prompts"]] == ["alpha", "zulu"]


def test_sync_invalid_cursor_and_default_no_cursor_compatibility() -> None:
    server = _SyncDiscovery()
    full = _send_sync(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [item["name"] for item in full["result"]["tools"]] == ["alpha", "zeta"]
    assert "nextCursor" not in full["result"]

    bad = _send_sync(server, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"cursor": "bogus"},
    })
    assert bad["error"]["code"] == -32602


def test_sync_cursor_is_principal_safe() -> None:
    server = _SyncDiscovery()
    first = _send_sync(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"pageSize": 1}},
        context=MCPRequestContext(transport="streamable-http", principal="alice"),
    )
    second = _send_sync(
        server,
        {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            "params": {"cursor": first["result"]["nextCursor"], "pageSize": 1},
        },
        context=MCPRequestContext(transport="streamable-http", principal="bob"),
    )
    assert second["error"]["code"] == -32602


def test_async_dynamic_discovery_registration_and_pagination() -> None:
    server = _AsyncDiscovery()

    async def dyn_tool(city: str = "Lisbon") -> str:
        return city

    async def dyn_prompt(topic: str = "umcp") -> str:
        return topic

    server.register_tool("mid", dyn_tool)
    server.register_prompt("mid", dyn_prompt, categories=["dynamic"])

    tools_page_1 = _send_async(server, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"pageSize": 2},
    })
    assert [item["name"] for item in tools_page_1["result"]["tools"]] == ["alpha", "mid"]
    tools_page_2 = _send_async(server, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        "params": {"cursor": tools_page_1["result"]["nextCursor"], "pageSize": 2},
    })
    assert [item["name"] for item in tools_page_2["result"]["tools"]] == ["zeta"]

    prompts_page_1 = _send_async(server, {
        "jsonrpc": "2.0", "id": 3, "method": "prompts/list", "params": {"pageSize": 2},
    })
    assert [item["name"] for item in prompts_page_1["result"]["prompts"]] == ["alpha", "mid"]

    resources_page_1 = _send_async(server, {
        "jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {"pageSize": 1},
    })
    assert [item["name"] for item in resources_page_1["result"]["resources"]] == ["alpha"]

    templates_page_1 = _send_async(server, {
        "jsonrpc": "2.0", "id": 5, "method": "resources/templates/list", "params": {"pageSize": 1},
    })
    assert [item["name"] for item in templates_page_1["result"]["resourceTemplates"]] == ["alpha"]

    assert _send_async(server, {
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "mid", "arguments": {"city": "Porto"}},
    })["result"]["content"][0]["text"] == "Porto"
    assert _send_async(server, {
        "jsonrpc": "2.0", "id": 7, "method": "prompts/get",
        "params": {"name": "mid", "arguments": {"topic": "prompts"}},
    })["result"]["messages"][0]["content"]["text"] == "prompts"

    bad = _send_async(server, {
        "jsonrpc": "2.0", "id": 8, "method": "prompts/list", "params": {"cursor": "bogus"},
    })
    assert bad["error"]["code"] == -32602


def test_list_changed_notifications_target_existing_transports(monkeypatch) -> None:
    buf = BytesIO()
    monkeypatch.setattr(umcp, "_stdout_bin", buf)
    monkeypatch.setattr(aioumcp, "_stdout_bin", buf)

    sync_server = _SyncDiscovery()
    sync_server.notify_tool_list_changed()
    sync_server.notify_prompt_list_changed()

    methods = [json.loads(line)["method"] for line in buf.getvalue().decode("utf-8").splitlines() if line.strip()]
    assert methods == ["notifications/tools/list_changed", "notifications/prompts/list_changed"]

    class _Writer:
        def __init__(self) -> None:
            self.payloads: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.payloads.append(data)

        async def drain(self) -> None:
            return None

    async_server = _AsyncDiscovery()
    writer = _Writer()
    async_server._sse_sessions = {"s1": (writer, asyncio.Event(), asyncio.Lock(), "anonymous")}
    asyncio.run(async_server.notify_tool_list_changed())
    asyncio.run(async_server.notify_prompt_list_changed())
    assert b"notifications/tools/list_changed" in writer.payloads[0]
    assert b"notifications/prompts/list_changed" in writer.payloads[1]
