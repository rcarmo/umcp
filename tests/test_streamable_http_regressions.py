from __future__ import annotations

import asyncio

import pytest
from dataclasses import dataclass
from json import dumps, loads

from aioumcp import AsyncMCPServer
from umcp import MCPServer
from umcp_shared import (
    MCPPrincipal, MCPRequestContext, get_request_context,
    media_accepts_json, origin_is_allowed,
)


@dataclass
class Writer:
    chunks: list[bytes]
    closed: bool = False

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    def flush(self) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def get_extra_info(self, name: str):
        return ("127.0.0.1", 9999) if name == "peername" else None


class Reader:
    def __init__(self, request: str, body: bytes = b""):
        self.lines = [line.encode() + b"\r\n" for line in request.splitlines()]
        self.lines.append(b"\r\n")
        self.body = body

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    async def readexactly(self, n: int) -> bytes:
        return self.body[:n]


class SyncCtx(MCPServer):
    def tool_ctx(self):
        ctx = get_request_context()
        return {"transport": ctx.transport, "request_id": ctx.request_id, "principal": ctx.principal, "peer": ctx.peer}


class AsyncCtx(AsyncMCPServer):
    async def tool_ctx(self):
        ctx = get_request_context()
        return {"transport": ctx.transport, "request_id": ctx.request_id, "principal": ctx.principal, "peer": ctx.peer}


class AuthSync(MCPServer):
    def authenticate_request(self, *, method: str, path: str, headers: dict[str, str], peer: str | None):
        if headers.get("authorization") == "Bearer ok":
            return MCPPrincipal(name="alice")
        return None


class AuthAsync(AsyncMCPServer):
    def authenticate_request(self, *, method: str, path: str, headers: dict[str, str], peer: str | None):
        if headers.get("authorization") == "Bearer ok":
            return MCPPrincipal(name="alice")
        return None

    def tool_ctx(self):
        ctx = get_request_context()
        return {"transport": ctx.transport, "principal": ctx.principal, "peer": ctx.peer}


async def _run_async(server: AsyncMCPServer, request: str, body: bytes = b"", max_request_bytes: int = 1024):
    writer = Writer([])
    await server._handle_streamable_http_client(Reader(request, body), writer, "/mcp", ["http://allowed"], max_request_bytes)
    return writer


def test_sync_non_dict_json_rpcs_return_invalid_request() -> None:
    s = SyncCtx()
    resp = s.process_request(dumps([1, 2, 3]))
    assert resp["error"]["code"] == -32600


def test_async_non_dict_json_rpcs_return_invalid_request() -> None:
    s = AsyncCtx()
    resp = asyncio.run(s.process_request_async(dumps([1, 2, 3])))
    assert resp["error"]["code"] == -32600


def test_async_context_propagates_into_sync_tool() -> None:
    s = AuthAsync()
    req = dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ctx"}})
    context = MCPRequestContext(transport="streamable-http", principal="alice", peer="127.0.0.1")
    resp = asyncio.run(s.process_request_async(req, context=context))
    value = loads(resp["result"]["content"][0]["text"])
    assert value == {"transport": "streamable-http", "principal": "alice", "peer": "127.0.0.1"}
    assert get_request_context().transport is None


def test_async_http_202_for_notification_and_response_object() -> None:
    s = AuthAsync()
    for payload in (
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 9, "result": {}},
    ):
        body = dumps(payload).encode()
        request = f"POST /mcp HTTP/1.1\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nMCP-Protocol-Version: 2025-03-26\nContent-Length: {len(body)}"
        writer = asyncio.run(_run_async(s, request, body))
        assert any(chunk.startswith(b"HTTP/1.1 202 Accepted") for chunk in writer.chunks)


def test_async_http_tool_sees_authenticated_context() -> None:
    s = AuthAsync()
    body = dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "ctx"}}).encode()
    request = f"POST /mcp HTTP/1.1\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nMCP-Protocol-Version: 2025-03-26\nContent-Length: {len(body)}"
    writer = asyncio.run(_run_async(s, request, body))
    wire = b"".join(writer.chunks)
    payload = loads(wire.split(b"\r\n\r\n", 1)[1])
    value = loads(payload["result"]["content"][0]["text"])
    assert value["transport"] == "streamable-http"
    assert value["principal"] == "alice"
    assert get_request_context().transport is None


def test_origin_validation_is_exact_and_remote_safe() -> None:
    assert origin_is_allowed("http://localhost:3000")
    assert not origin_is_allowed("http://127.0.0.1.evil.example")
    assert not origin_is_allowed("http://localhost:3000", local_bind=False)
    assert origin_is_allowed("https://ui.example", ["https://ui.example"], local_bind=False)


def test_accept_negotiation_matches_actual_response_type() -> None:
    assert media_accepts_json("application/json")
    assert media_accepts_json("application/*;q=0.5")
    assert not media_accepts_json("application/json;q=0")
    assert not media_accepts_json("application/mcp+json")


def test_cli_rejects_unknown_conflicting_and_incomplete_transports() -> None:
    sync = MCPServer()
    async_server = AsyncMCPServer()
    invalid = (
        ["--transport", "bogus", "--port", "1"],
        ["--http", "--tcp", "--port", "1"],
        ["--transport", "streamable-http"],
        ["--transport", "stdio", "--port", "1"],
    )
    for args in invalid:
        with pytest.raises(ValueError):
            sync.run(args)
        with pytest.raises(ValueError):
            asyncio.run(async_server.run_async(args))


def test_async_cors_headers_are_returned_on_preflight_and_post() -> None:
    server = AuthAsync()
    preflight = "OPTIONS /mcp HTTP/1.1\nOrigin: http://allowed\nContent-Length: 0"
    writer = asyncio.run(_run_async(server, preflight))
    assert b"Access-Control-Allow-Origin: http://allowed" in b"".join(writer.chunks)

    body = dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
    request = f"POST /mcp HTTP/1.1\nOrigin: http://allowed\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: {len(body)}"
    writer = asyncio.run(_run_async(server, request, body))
    assert b"Access-Control-Allow-Origin: http://allowed" in b"".join(writer.chunks)


def test_async_http_rejects_bad_content_length_and_oversize() -> None:
    s = AuthAsync()
    bad = asyncio.run(_run_async(s, "POST /mcp HTTP/1.1\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: nope"))
    assert any(b"400 Bad Request" in chunk for chunk in bad.chunks)
    big = asyncio.run(_run_async(s, "POST /mcp HTTP/1.1\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: 9", max_request_bytes=8))
    assert any(b"413 Payload Too Large" in chunk for chunk in big.chunks)


def test_authentication_and_authorization_paths() -> None:
    s = AuthSync()
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert s.process_request(dumps(req)) is not None
    assert s.authenticate_request(method="POST", path="/mcp", headers={"authorization": "nope"}, peer="127.0.0.1") is None
