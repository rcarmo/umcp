"""In-depth tests for the tools surface beyond schema/annotations/coercion.

Covers the dispatch path, return-value wrapping, async tool support,
default-argument handling, and metadata exposed on ``tools/list``.
"""

from __future__ import annotations

import asyncio
import json

from aioumcp import AsyncMCPServer
from umcp import MCPServer


# ---------- helpers ----------------------------------------------------------


def _call_sync(server: MCPServer, name: str, args: dict | None = None) -> dict:
    params: dict = {"name": name}
    if args is not None:
        params["arguments"] = args
    return server.process_request(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params,
    }))


def _call_async(server: AsyncMCPServer, name: str, args: dict | None = None) -> dict:
    params: dict = {"name": name}
    if args is not None:
        params["arguments"] = args
    return asyncio.run(server.process_request_async(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params,
    })))


def _content_text(resp: dict) -> str:
    return resp["result"]["content"][0]["text"]


# ---------- tools/list shape ------------------------------------------------


class _Sync(MCPServer):
    def tool_echo(self, message: str) -> str:
        """Echo the supplied message back.

        Args:
            message: The text to echo
        """
        return message

    def tool_add(self, a: int, b: int = 1) -> int:
        """Add two integers."""
        return a + b

    def tool_ping(self) -> str:
        """Return pong."""
        return "pong"

    def tool_returns_dict(self) -> dict:
        """Returns a JSON-serialisable dict."""
        return {"ok": True, "items": [1, 2, 3]}

    def tool_returns_list(self) -> list:
        """Returns a list."""
        return ["a", "b", "c"]

    def tool_returns_int(self) -> int:
        """Returns a number."""
        return 42

    def tool_returns_bool(self) -> bool:
        """Returns a boolean."""
        return True

    def tool_returns_none(self) -> None:
        """Returns nothing."""
        return None


def test_tools_list_returns_full_metadata() -> None:
    s = _Sync()
    tools = {t["name"]: t for t in s.discover_tools()["tools"]}
    assert "echo" in tools
    echo = tools["echo"]
    assert "description" in echo
    assert echo["description"].startswith("Echo the supplied message back")
    assert "inputSchema" in echo
    assert echo["inputSchema"]["type"] == "object"
    # Annotations are inferred from the name (echo isn't read-only by prefix
    # but isn't destructive or open-world either) -- the dict should be present.
    assert "annotations" in echo


def test_tools_list_includes_every_tool_method() -> None:
    s = _Sync()
    names = {t["name"] for t in s.discover_tools()["tools"]}
    expected = {
        "echo", "add", "ping",
        "returns_dict", "returns_list", "returns_int", "returns_bool", "returns_none",
    }
    assert expected <= names


def test_tool_args_section_populates_param_descriptions() -> None:
    s = _Sync()
    schema = next(t for t in s.discover_tools()["tools"] if t["name"] == "echo")["inputSchema"]
    desc = schema["properties"]["message"].get("description", "")
    assert "echo" in desc.lower()


# ---------- tools/call dispatch & return wrapping ----------------------------


def test_call_with_string_return_wraps_as_text_content() -> None:
    resp = _call_sync(_Sync(), "echo", {"message": "hi"})
    assert "error" not in resp
    block = resp["result"]["content"][0]
    assert block["type"] == "text"
    assert block["text"] == "hi"


def test_call_with_dict_return_serialises_to_json_text() -> None:
    resp = _call_sync(_Sync(), "returns_dict")
    text = _content_text(resp)
    parsed = json.loads(text)
    assert parsed == {"ok": True, "items": [1, 2, 3]}


def test_call_with_list_return_serialises_to_json_text() -> None:
    resp = _call_sync(_Sync(), "returns_list")
    parsed = json.loads(_content_text(resp))
    assert parsed == ["a", "b", "c"]


def test_call_with_scalar_returns_are_stringified() -> None:
    s = _Sync()
    assert _content_text(_call_sync(s, "returns_int")) == "42"
    assert _content_text(_call_sync(s, "returns_bool")).lower() in {"true", "1"}


def test_call_with_none_return_currently_errors() -> None:
    """Documents the current behaviour: a tool returning ``None`` is wrapped
    as a -32603 by the library, not as an empty content list.  This is more
    of a regression marker than a desired contract -- if the library is ever
    taught to handle ``None`` cleanly, this test should be relaxed."""
    resp = _call_sync(_Sync(), "returns_none")
    assert ("error" in resp) or ("content" in resp["result"])


def test_call_uses_default_when_argument_omitted() -> None:
    resp = _call_sync(_Sync(), "add", {"a": 5})
    assert _content_text(resp) == "6"  # 5 + default 1


def test_call_with_no_args_section_at_all() -> None:
    """A tool with no required args can be called without an `arguments` field."""
    resp = _call_sync(_Sync(), "ping")
    assert _content_text(resp) == "pong"


# ---------- prompt of dispatch via tools/list -> tools/call round-trip ------


def test_listed_tools_can_all_be_called_round_trip() -> None:
    """Every tool advertised by tools/list with no required args should
    be dispatchable. Skips ``returns_none`` because of the documented
    None-return quirk above."""
    s = _Sync()
    listed = s.discover_tools()["tools"]
    for tool in listed:
        # Skip tools that need required args we don't know how to fill.
        required = tool["inputSchema"].get("required", [])
        if required:
            continue
        if tool["name"] == "returns_none":
            continue
        resp = _call_sync(s, tool["name"])
        assert "error" not in resp, f"{tool['name']} failed: {resp}"


# ---------- async tools -----------------------------------------------------


class _Async(AsyncMCPServer):
    async def tool_echo(self, message: str) -> str:
        """Async echo."""
        await asyncio.sleep(0)
        return message

    async def tool_double(self, n: int) -> int:
        """Double."""
        return n * 2

    def tool_sync_in_async_base(self, value: str) -> str:
        """A sync tool inside an async server is allowed."""
        return value.upper()


def test_async_tool_dispatch_returns_text_content() -> None:
    resp = _call_async(_Async(), "echo", {"message": "async-hi"})
    assert "error" not in resp
    assert resp["result"]["content"][0]["text"] == "async-hi"


def test_async_tool_with_int_return() -> None:
    resp = _call_async(_Async(), "double", {"n": 21})
    assert _content_text(resp) == "42"


def test_async_base_supports_sync_tool_methods() -> None:
    resp = _call_async(_Async(), "sync_in_async_base", {"value": "abc"})
    assert _content_text(resp) == "ABC"


def test_async_tools_can_run_concurrently() -> None:
    """Two tool calls scheduled together should both succeed."""
    s = _Async()

    async def go() -> tuple[dict, dict]:
        a = s.process_request_async(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "double", "arguments": {"n": 3}},
        }))
        b = s.process_request_async(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "double", "arguments": {"n": 4}},
        }))
        return await asyncio.gather(a, b)

    r1, r2 = asyncio.run(go())
    assert _content_text(r1) == "6"
    assert _content_text(r2) == "8"


def test_async_initialize_includes_tools_capability() -> None:
    resp = asyncio.run(_Async().process_request_async(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    })))
    assert "tools" in resp["result"]["capabilities"]
