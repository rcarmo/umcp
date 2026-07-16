from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import time

import pytest
import aioumcp as aioumcp_module
import umcp as umcp_module
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
    def prompt_fail(self):
        raise RuntimeError("secret prompt details")

    def tool_ctx(self):
        ctx = get_request_context()
        return {"transport": ctx.transport, "request_id": ctx.request_id, "principal": ctx.principal, "peer": ctx.peer}

    def tool_fail(self):
        raise RuntimeError("sync boom")

    def resource_broken(self):
        raise RuntimeError("sync resource boom")


class AsyncCtx(AsyncMCPServer):
    async def prompt_fail(self):
        raise RuntimeError("secret prompt details")

    async def tool_ctx(self):
        ctx = get_request_context()
        return {"transport": ctx.transport, "request_id": ctx.request_id, "principal": ctx.principal, "peer": ctx.peer}

    async def tool_fail(self):
        raise RuntimeError("async boom")

    async def resource_broken(self):
        raise RuntimeError("async resource boom")


class AuthSync(MCPServer):
    def authenticate_request(self, *, method: str, path: str, headers: dict[str, str], peer: str | None):
        if headers.get("authorization") == "Bearer ok":
            return MCPPrincipal(name="alice")
        return None

    def authorize_request(self, principal: MCPPrincipal | None, *, rpc_method: str | None, tool_name: str | None) -> bool:
        return tool_name != "forbidden"

    def tool_forbidden(self):
        return "nope"


class ConcurrentSync(MCPServer):
    def tool_identity(self, delay: float = 0.0):
        time.sleep(delay)
        ctx = get_request_context()
        return {"principal": ctx.principal, "request_id": ctx.request_id}


class ConcurrentAsync(AsyncMCPServer):
    async def tool_identity(self, delay: float = 0.0):
        await asyncio.sleep(delay)
        ctx = get_request_context()
        return {"principal": ctx.principal, "request_id": ctx.request_id}


class TransportSync(MCPServer):
    def tool_transport(self):
        return get_request_context().transport


class TransportAsync(AsyncMCPServer):
    async def tool_transport(self):
        return get_request_context().transport


class AuthAsync(AsyncMCPServer):
    def authenticate_request(self, *, method: str, path: str, headers: dict[str, str], peer: str | None):
        if headers.get("authorization") == "Bearer ok":
            return MCPPrincipal(name="alice")
        return None

    def authorize_request(self, principal: MCPPrincipal | None, *, rpc_method: str | None, tool_name: str | None) -> bool:
        return tool_name != "forbidden"

    def tool_ctx(self):
        ctx = get_request_context()
        return {"transport": ctx.transport, "principal": ctx.principal, "peer": ctx.peer}

    async def tool_forbidden(self):
        return "nope"


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


def test_context_isolated_across_threads_and_async_tasks() -> None:
    request = lambda request_id, delay: dumps({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": "identity", "arguments": {"delay": delay}},
    })

    sync_server = ConcurrentSync()
    def sync_call(name: str, request_id: int, delay: float):
        response = sync_server.process_request(
            request(request_id, delay),
            context=MCPRequestContext(transport="streamable-http", principal=name),
        )
        return loads(response["result"]["content"][0]["text"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(sync_call, "alice", 1, 0.03)
        second = pool.submit(sync_call, "bob", 2, 0.0)
        assert {first.result()["principal"], second.result()["principal"]} == {"alice", "bob"}
        assert {first.result()["request_id"], second.result()["request_id"]} == {1, 2}

    async def async_calls():
        server = ConcurrentAsync()
        async def call(name: str, request_id: int, delay: float):
            response = await server.process_request_async(
                request(request_id, delay),
                context=MCPRequestContext(transport="streamable-http", principal=name),
            )
            return loads(response["result"]["content"][0]["text"])
        return await asyncio.gather(call("alice", 1, 0.03), call("bob", 2, 0.0))
    results = asyncio.run(async_calls())
    assert {(item["principal"], item["request_id"]) for item in results} == {("alice", 1), ("bob", 2)}
    assert get_request_context().transport is None


def test_stdio_and_file_modes_expose_transport_context(monkeypatch, tmp_path) -> None:
    request = dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "transport"}})

    def result_transport(output: io.BytesIO) -> str:
        response = loads(output.getvalue().decode().strip())
        return response["result"]["content"][0]["text"]

    sync_out = io.BytesIO()
    monkeypatch.setattr(umcp_module, "_stdin_bin", io.BytesIO((request + "\n").encode()))
    monkeypatch.setattr(umcp_module, "_stdout_bin", sync_out)
    TransportSync().run([])
    assert result_transport(sync_out) == "stdio"

    request_file = tmp_path / "request.json"
    request_file.write_text(request)
    sync_out = io.BytesIO()
    monkeypatch.setattr(umcp_module, "_stdout_bin", sync_out)
    TransportSync().run([str(request_file)])
    assert result_transport(sync_out) == "file"

    async_out = io.BytesIO()
    monkeypatch.setattr(aioumcp_module, "_stdin_bin", io.BytesIO((request + "\n").encode()))
    monkeypatch.setattr(aioumcp_module, "_stdout_bin", async_out)
    asyncio.run(TransportAsync().run_async([]))
    assert result_transport(async_out) == "stdio"

    async_out = io.BytesIO()
    monkeypatch.setattr(aioumcp_module, "_stdout_bin", async_out)
    asyncio.run(TransportAsync().run_async([str(request_file)]))
    assert result_transport(async_out) == "file"


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


def test_async_http_rejects_missing_version_bad_accept_and_unauthorized() -> None:
    s = AuthAsync()
    body = dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    req = f"POST /mcp HTTP/1.1\nContent-Type: application/json\nAccept: text/plain\nAuthorization: Bearer ok\nContent-Length: {len(body)}"
    assert b"406 Not Acceptable" in b"".join(asyncio.run(_run_async(s, req, body)).chunks)
    req = f"POST /mcp HTTP/1.1\nContent-Type: text/plain\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: {len(body)}"
    assert b"415 Unsupported Media Type" in b"".join(asyncio.run(_run_async(s, req, body)).chunks)
    req = f"POST /mcp HTTP/1.1\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: {len(body)}"
    assert b"400 Bad Request" in b"".join(asyncio.run(_run_async(s, req, body)).chunks)
    req = f"POST /mcp HTTP/1.1\nContent-Type: application/json\nAccept: application/json\nMCP-Protocol-Version: 2025-03-26\nContent-Length: {len(body)}"
    assert b"401 Unauthorized" in b"".join(asyncio.run(_run_async(s, req, body)).chunks)


def test_async_http_preflight_requires_valid_origin_and_authorization_can_forbid() -> None:
    s = AuthAsync()
    no_origin = asyncio.run(_run_async(s, "OPTIONS /mcp HTTP/1.1\nContent-Length: 0"))
    assert b"405 Method Not Allowed" in b"".join(no_origin.chunks)
    bad_origin = asyncio.run(_run_async(s, "OPTIONS /mcp HTTP/1.1\nOrigin: http://evil.example\nContent-Length: 0"))
    assert b"403 Forbidden" in b"".join(bad_origin.chunks)
    body = dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "forbidden"}}).encode()
    req = f"POST /mcp HTTP/1.1\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nMCP-Protocol-Version: 2025-03-26\nContent-Length: {len(body)}"
    denied = asyncio.run(_run_async(s, req, body))
    assert b"403 Forbidden" in b"".join(denied.chunks)


def test_remote_safe_prompt_errors() -> None:
    request = dumps({"jsonrpc": "2.0", "id": 1, "method": "prompts/get", "params": {"name": "fail"}})
    context = MCPRequestContext(transport="streamable-http")
    sync_response = SyncCtx().process_request(request, context=context)
    async_response = asyncio.run(AsyncCtx().process_request_async(request, context=context))
    for response in (sync_response, async_response):
        assert response["error"]["message"] == "Prompt execution failed"
        assert "secret" not in dumps(response)


def test_remote_transports_hide_internal_errors() -> None:
    sync_server = SyncCtx()
    async_server = AsyncCtx()
    sync_resp = sync_server.process_request(
        dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "fail"}}),
        context=MCPRequestContext(transport="tcp"),
    )
    assert sync_resp["error"]["message"] == "Tool execution failed"
    sync_resource = sync_server.process_request(
        dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "umcp://SyncCtx/broken"}}),
        context=MCPRequestContext(transport="sse"),
    )
    assert sync_resource["error"]["message"] == "Resource read failed"
    async_resp = asyncio.run(async_server.process_request_async(
        dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "fail"}}),
        context=MCPRequestContext(transport="tcp"),
    ))
    assert async_resp["error"]["message"] == "Tool execution failed"
    async_resource = asyncio.run(async_server.process_request_async(
        dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "umcp://AsyncCtx/broken"}}),
        context=MCPRequestContext(transport="sse"),
    ))
    assert async_resource["error"]["message"] == "Resource read failed"


def test_authentication_and_authorization_paths() -> None:
    s = AuthSync()
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert s.process_request(dumps(req)) is not None
    assert s.authenticate_request(method="POST", path="/mcp", headers={"authorization": "nope"}, peer="127.0.0.1") is None
