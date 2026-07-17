from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from io import BytesIO

import pytest

import aioumcp
import umcp
from aioumcp import AsyncMCPServer
from umcp import MCPServer
from umcp_shared import MCPRequestContext


class _Writer:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.payloads.append(data)

    async def drain(self) -> None:
        return None


@pytest.fixture
def captured_stdout(monkeypatch):
    buf = BytesIO()
    monkeypatch.setattr(umcp, "_stdout_bin", buf)
    monkeypatch.setattr(aioumcp, "_stdout_bin", buf)
    return buf


def _parse_notifications(buf: BytesIO) -> list[dict]:
    return [
        json.loads(line)
        for line in buf.getvalue().decode("utf-8").splitlines()
        if line.strip()
    ]


def _call_sync(server: MCPServer, name: str) -> dict:
    return server.process_request(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name},
    }))


async def _call_async_inner(server: AsyncMCPServer, name: str, *, context: MCPRequestContext | None = None) -> dict:
    return await server.process_request_async(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name},
    }), context=context)


def _call_async(server: AsyncMCPServer, name: str, *, context: MCPRequestContext | None = None) -> dict:
    return asyncio.run(_call_async_inner(server, name, context=context))


class RuntimeNotifySync(MCPServer):
    pass


class RuntimeNotifyAsync(AsyncMCPServer):
    pass


def test_sync_register_unregister_and_notify_stdio(captured_stdout) -> None:
    server = RuntimeNotifySync()

    def tool_hello() -> str:
        return "hello"

    def prompt_hello() -> str:
        return "hello"

    server.register_tool_and_notify("hello", tool_hello)
    server.register_prompt_and_notify("hello", prompt_hello)
    assert server.unregister_tool_and_notify("hello") is True
    assert server.unregister_prompt_and_notify("hello") is True
    assert server.unregister_tool_and_notify("missing") is False
    assert server.unregister_prompt_and_notify("missing") is False

    methods = [item["method"] for item in _parse_notifications(captured_stdout)]
    assert methods == [
        "notifications/tools/list_changed",
        "notifications/prompts/list_changed",
        "notifications/tools/list_changed",
        "notifications/prompts/list_changed",
    ]


def test_sync_register_unregister_and_notify_sse() -> None:
    server = RuntimeNotifySync()
    q = __import__("queue").Queue()
    server._sse_sessions = {"s1": q}

    def tool_hello() -> str:
        return "hello"

    def prompt_hello() -> str:
        return "hello"

    server.register_tool_and_notify("hello", tool_hello)
    server.register_prompt_and_notify("hello", prompt_hello)
    assert server.unregister_tool_and_notify("hello") is True
    assert server.unregister_prompt_and_notify("hello") is True
    assert server.unregister_tool_and_notify("missing") is False
    assert server.unregister_prompt_and_notify("missing") is False

    payloads = [q.get_nowait().decode("utf-8") for _ in range(4)]
    assert 'notifications/tools/list_changed' in payloads[0]
    assert 'notifications/prompts/list_changed' in payloads[1]
    assert 'notifications/tools/list_changed' in payloads[2]
    assert 'notifications/prompts/list_changed' in payloads[3]
    assert q.empty()


def test_async_register_unregister_and_notify_stdio(captured_stdout) -> None:
    server = RuntimeNotifyAsync()

    async def run() -> None:
        def tool_hello() -> str:
            return "hello"

        def prompt_hello() -> str:
            return "hello"

        await server.register_tool_and_notify("hello", tool_hello)
        await server.register_prompt_and_notify("hello", prompt_hello)
        assert await server.unregister_tool_and_notify("hello") is True
        assert await server.unregister_prompt_and_notify("hello") is True
        assert await server.unregister_tool_and_notify("missing") is False
        assert await server.unregister_prompt_and_notify("missing") is False

    asyncio.run(run())
    methods = [item["method"] for item in _parse_notifications(captured_stdout)]
    assert methods == [
        "notifications/tools/list_changed",
        "notifications/prompts/list_changed",
        "notifications/tools/list_changed",
        "notifications/prompts/list_changed",
    ]


def test_async_register_unregister_and_notify_sse() -> None:
    server = RuntimeNotifyAsync()
    writer = _Writer()
    server._sse_sessions = {"s1": (writer, asyncio.Event(), asyncio.Lock(), "anonymous")}

    async def run() -> None:
        def tool_hello() -> str:
            return "hello"

        def prompt_hello() -> str:
            return "hello"

        await server.register_tool_and_notify("hello", tool_hello)
        await server.register_prompt_and_notify("hello", prompt_hello)
        assert await server.unregister_tool_and_notify("hello") is True
        assert await server.unregister_prompt_and_notify("hello") is True
        assert await server.unregister_tool_and_notify("missing") is False
        assert await server.unregister_prompt_and_notify("missing") is False

    asyncio.run(run())
    assert len(writer.payloads) == 4
    assert b"notifications/tools/list_changed" in writer.payloads[0]
    assert b"notifications/prompts/list_changed" in writer.payloads[1]
    assert b"notifications/tools/list_changed" in writer.payloads[2]
    assert b"notifications/prompts/list_changed" in writer.payloads[3]


@dataclass
class _Person:
    name: str
    age: int


class OutputSchemaSync(MCPServer):
    def tool_mapping(self) -> dict[str, int]:
        return {"count": 2}

    def tool_typed(self) -> _Person:
        return _Person(name="Ada", age=37)

    def tool_bad_static(self) -> dict[str, int]:
        return {"count": "oops"}  # type: ignore[return-value]

    def tool_bad_attr(self) -> dict:
        return {"count": "oops"}

    tool_bad_attr._mcp_output_schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }


class OutputSchemaAsync(AsyncMCPServer):
    async def tool_mapping(self) -> dict[str, int]:
        return {"count": 2}

    async def tool_typed(self) -> _Person:
        return _Person(name="Ada", age=37)

    async def tool_bad_static(self) -> dict[str, int]:
        return {"count": "oops"}  # type: ignore[return-value]

    async def tool_bad_attr(self) -> dict:
        return {"count": "oops"}

    tool_bad_attr._mcp_output_schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }


def test_tools_list_advertises_inferred_and_explicit_output_schemas() -> None:
    server = OutputSchemaSync()

    def dyn() -> dict[str, bool]:
        return {"ok": True}

    server.register_tool(
        "dyn",
        dyn,
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )

    tools = {tool["name"]: tool for tool in server.discover_tools()["tools"]}
    assert tools["mapping"]["outputSchema"]["type"] == "object"
    assert tools["typed"]["outputSchema"]["properties"]["name"]["type"] == "string"
    assert tools["dyn"]["outputSchema"]["properties"]["ok"]["type"] == "boolean"


def test_sync_mapping_return_adds_structured_content() -> None:
    resp = _call_sync(OutputSchemaSync(), "mapping")
    assert resp["result"]["structuredContent"] == {"count": 2}
    assert json.loads(resp["result"]["content"][0]["text"]) == {"count": 2}


def test_sync_typed_return_adds_structured_content() -> None:
    resp = _call_sync(OutputSchemaSync(), "typed")
    assert resp["result"]["structuredContent"] == {"name": "Ada", "age": 37}
    assert json.loads(resp["result"]["content"][0]["text"]) == {"name": "Ada", "age": 37}


def test_async_mapping_return_adds_structured_content() -> None:
    resp = _call_async(OutputSchemaAsync(), "mapping")
    assert resp["result"]["structuredContent"] == {"count": 2}


def test_async_typed_return_adds_structured_content() -> None:
    resp = _call_async(OutputSchemaAsync(), "typed")
    assert resp["result"]["structuredContent"] == {"name": "Ada", "age": 37}


def test_sync_static_malformed_output_is_local_detailed() -> None:
    resp = _call_sync(OutputSchemaSync(), "bad_static")
    assert resp["error"]["code"] == -32603
    assert "count" in resp["error"]["message"]


def test_sync_dynamic_malformed_output_is_remote_safe() -> None:
    server = OutputSchemaSync()

    def bad_dynamic() -> dict:
        return {"count": "oops"}

    server.register_tool(
        "bad_dynamic",
        bad_dynamic,
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
    )
    resp = server.process_request(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bad_dynamic"}}),
        context=MCPRequestContext(transport="sse"),
    )
    assert resp["error"] == {"code": -32603, "message": "Tool output validation failed"}


def test_async_static_malformed_output_is_local_detailed() -> None:
    resp = _call_async(OutputSchemaAsync(), "bad_static")
    assert resp["error"]["code"] == -32603
    assert "count" in resp["error"]["message"]


def test_async_dynamic_malformed_output_is_remote_safe() -> None:
    server = OutputSchemaAsync()

    async def bad_dynamic() -> dict:
        return {"count": "oops"}

    server.register_tool(
        "bad_dynamic",
        bad_dynamic,
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
    )
    resp = _call_async(server, "bad_dynamic", context=MCPRequestContext(transport="sse"))
    assert resp["error"] == {"code": -32603, "message": "Tool output validation failed"}
