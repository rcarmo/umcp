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

    async def wait_closed(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed

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


class LegacyAuthSync(MCPServer):
    def authenticate(self, headers: dict[str, str], peer: object):
        if headers.get("authorization") == "Bearer old":
            return MCPPrincipal(name="legacy")
        return None

    def authorize(self, principal: MCPPrincipal | None, method: str | None, params: dict[str, object]) -> bool:
        return params.get("name") != "forbidden"


class NewAuthSync(MCPServer):
    def authenticate_request(self, *, method: str, path: str, headers: dict[str, str], peer: str | None):
        if headers.get("authorization") == "Bearer new":
            return MCPPrincipal(name="new")
        return None

    def authorize_request(self, principal: MCPPrincipal | None, *, rpc_method: str | None, tool_name: str | None) -> bool:
        return rpc_method == "initialize" or tool_name != "forbidden"


class LegacyAuthAsync(AsyncMCPServer):
    def authenticate(self, headers: dict[str, str], peer: object):
        if headers.get("authorization") == "Bearer old":
            return MCPPrincipal(name="legacy")
        return None

    def authorize(self, principal: MCPPrincipal | None, method: str | None, params: dict[str, object]) -> bool:
        return params.get("name") != "forbidden"


class NewAuthAsync(AsyncMCPServer):
    async def authenticate_request(self, *, method: str, path: str, headers: dict[str, str], peer: str | None):
        if headers.get("authorization") == "Bearer new":
            return MCPPrincipal(name="new")
        return None

    async def authorize_request(self, principal: MCPPrincipal | None, *, rpc_method: str | None, tool_name: str | None) -> bool:
        return rpc_method == "initialize" or tool_name != "forbidden"


class BadAuthTypeAsync(AsyncMCPServer):
    async def authenticate_request(self, *, method: str, path: str, headers: dict[str, str], peer: str | None):
        return "nope"


class HookExplodesAsync(AsyncMCPServer):
    async def authenticate_request(self, *, method: str, path: str, headers: dict[str, str], peer: str | None):
        raise RuntimeError("boom")


class BadAuthorizeTypeAsync(AuthAsync):
    async def authorize_request(self, principal: MCPPrincipal | None, *, rpc_method: str | None, tool_name: str | None):
        return "nope"


class AuthorizeExplodesAsync(AuthAsync):
    async def authorize_request(self, principal: MCPPrincipal | None, *, rpc_method: str | None, tool_name: str | None) -> bool:
        raise RuntimeError("boom")


async def _run_async(
    server: AsyncMCPServer, request: str, body: bytes = b"",
    max_request_bytes: int = 1024, *, add_host: bool = True,
):
    if add_host and "HTTP/1.1" in request and "\nHost:" not in request:
        first, rest = request.split("\n", 1)
        request = f"{first}\nHost: test\n{rest}"
    writer = Writer([])
    await server._handle_streamable_http_client(Reader(request, body), writer, "/mcp", ["http://allowed"], max_request_bytes)
    return writer


async def _run_async_sse(server: AsyncMCPServer, request: str, body: bytes = b"", max_request_bytes: int = 1024):
    writer = Writer([])
    try:
        await asyncio.wait_for(
            server._sse_handle_client(
                Reader(request, body), writer, ["http://allowed"], max_request_bytes
            ),
            timeout=0.05,
        )
    except TimeoutError:
        pass  # A successful SSE GET remains open until the client disconnects.
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
    assert not origin_is_allowed("http://localhost:3000", ["https://ui.example"])
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


def test_sync_auth_hook_aliases_and_new_hooks_are_bidirectional() -> None:
    legacy = LegacyAuthSync()
    assert legacy.authenticate_request(method="POST", path="/mcp", headers={"authorization": "Bearer old"}, peer="127.0.0.1").name == "legacy"
    assert legacy.authorize_request(MCPPrincipal(name="legacy"), rpc_method="tools/call", tool_name="ok") is True
    assert legacy.authorize_request(MCPPrincipal(name="legacy"), rpc_method="tools/call", tool_name="forbidden") is False

    new = NewAuthSync()
    assert new.authenticate({"authorization": "Bearer new"}, ("127.0.0.1", 1)).name == "new"
    assert new.authorize(MCPPrincipal(name="new"), "tools/call", {"name": "ok"}) is True
    assert new.authorize(MCPPrincipal(name="new"), "tools/call", {"name": "forbidden"}) is False


def test_async_auth_hook_aliases_and_new_hooks_are_bidirectional() -> None:
    legacy = LegacyAuthAsync()
    principal = asyncio.run(legacy.authenticate_request_async(method="POST", path="/mcp", headers={"authorization": "Bearer old"}, peer="127.0.0.1"))
    assert principal.name == "legacy"
    assert asyncio.run(legacy.authorize_request_async(MCPPrincipal(name="legacy"), rpc_method="tools/call", tool_name="ok")) is True
    assert asyncio.run(legacy.authorize_request_async(MCPPrincipal(name="legacy"), rpc_method="tools/call", tool_name="forbidden")) is False
    assert asyncio.run(legacy.authenticate_async({"authorization": "Bearer old"}, ("127.0.0.1", 1))).name == "legacy"
    assert asyncio.run(legacy.authorize_async(MCPPrincipal(name="legacy"), "tools/call", {"name": "ok"})) is True

    new = NewAuthAsync()
    with pytest.raises(TypeError, match="authenticate_async"):
        new.authenticate({"authorization": "Bearer new"}, ("127.0.0.1", 1))
    with pytest.raises(TypeError, match="authorize_async"):
        new.authorize(MCPPrincipal(name="new"), "tools/call", {"name": "ok"})
    assert asyncio.run(new.authenticate_async({"authorization": "Bearer new"}, ("127.0.0.1", 1))).name == "new"
    assert asyncio.run(new.authorize_async(MCPPrincipal(name="new"), "tools/call", {"name": "ok"})) is True
    assert asyncio.run(new.authorize_async(MCPPrincipal(name="new"), "tools/call", {"name": "forbidden"})) is False


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


@pytest.mark.parametrize(
    ("raw_request", "body", "status"),
    [
        ("GET /mcp HTTP/1.1\nOrigin: http://allowed\nContent-Length: 0", b"", b"405 Method Not Allowed"),
        ("POST /mcp HTTP/1.1\nOrigin: http://allowed\nContent-Type: text/plain\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: 0", b"", b"415 Unsupported Media Type"),
        ("POST /mcp HTTP/1.1\nOrigin: http://allowed\nContent-Type: application/json\nAccept: text/plain\nAuthorization: Bearer ok\nContent-Length: 0", b"", b"406 Not Acceptable"),
        ("POST /mcp HTTP/1.1\nOrigin: http://allowed\nContent-Type: application/json\nAccept: application/json\nMCP-Protocol-Version: 2025-03-26\nContent-Length: 0", b"", b"401 Unauthorized"),
        ("POST /mcp HTTP/1.1\nOrigin: http://allowed\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nMCP-Protocol-Version: 2025-03-26\nContent-Length: 0", b"", b"200 OK"),
        ("POST /mcp HTTP/1.1\nOrigin: http://allowed\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: nope", b"", b"400 Bad Request"),
        ("POST /mcp HTTP/1.1\nOrigin: http://allowed\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: 9", b"", b"413 Payload Too Large"),
    ],
)
def test_async_http_allowed_origin_gets_cors_on_terminal_responses(raw_request: str, body: bytes, status: bytes) -> None:
    wire = b"".join(asyncio.run(_run_async(AuthAsync(), raw_request, body, max_request_bytes=8)).chunks)
    assert status in wire
    assert b"Access-Control-Allow-Origin: http://allowed" in wire
    assert b"Vary: Origin" in wire


def test_async_http_allowed_origin_gets_cors_on_json_errors_and_duplicate_length_and_transfer_encoding() -> None:
    s = AuthAsync()
    bad_json = b"{"
    parse_request = "\n".join([
        "POST /mcp HTTP/1.1",
        "Origin: http://allowed",
        "Content-Type: application/json",
        "Accept: application/json",
        "Authorization: Bearer ok",
        f"Content-Length: {len(bad_json)}",
    ])
    wire = b"".join(asyncio.run(_run_async(s, parse_request, bad_json)).chunks)
    assert b"200 OK" in wire and b"Access-Control-Allow-Origin: http://allowed" in wire and b"Vary: Origin" in wire

    invalid_request_body = b"[]"
    invalid_request = "\n".join([
        "POST /mcp HTTP/1.1",
        "Origin: http://allowed",
        "Content-Type: application/json",
        "Accept: application/json",
        "Authorization: Bearer ok",
        f"Content-Length: {len(invalid_request_body)}",
    ])
    wire = b"".join(asyncio.run(_run_async(s, invalid_request, invalid_request_body)).chunks)
    assert b"200 OK" in wire and b"Access-Control-Allow-Origin: http://allowed" in wire and b"Vary: Origin" in wire

    dup_cl = "\n".join([
        "POST /mcp HTTP/1.1",
        "Origin: http://allowed",
        "Content-Type: application/json",
        "Accept: application/json",
        "Authorization: Bearer ok",
        "Content-Length: 0",
        "Content-Length: 0",
    ])
    wire = b"".join(asyncio.run(_run_async(s, dup_cl)).chunks)
    assert b"400 Bad Request" in wire and b"Access-Control-Allow-Origin: http://allowed" in wire

    transfer_encoding = "\n".join([
        "POST /mcp HTTP/1.1",
        "Origin: http://allowed",
        "Content-Type: application/json",
        "Accept: application/json",
        "Authorization: Bearer ok",
        "Transfer-Encoding: chunked",
        "Content-Length: 0",
    ])
    wire = b"".join(asyncio.run(_run_async(s, transfer_encoding)).chunks)
    assert b"400 Bad Request" in wire and b"Access-Control-Allow-Origin: http://allowed" in wire


def test_origin_validation_rejects_malformed_loopback_forms() -> None:
    bad_origins = [
        "http://user@localhost",
        "http://localhost/",
        "http://localhost/path",
        "http://localhost?x=1",
        "http://localhost#frag",
        "http://localhost:99999",
        "http://127.0.0.1.evil.example",
    ]
    for origin in bad_origins:
        assert not origin_is_allowed(origin)
    assert origin_is_allowed("http://localhost")
    assert origin_is_allowed("http://localhost:3000")
    assert not origin_is_allowed("https://ui.example/path", ["https://ui.example/path"], local_bind=False)
    assert origin_is_allowed("https://ui.example", ["https://ui.example"], local_bind=False)


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


def test_async_http_rejects_invalid_utf8_version_and_ambiguous_host() -> None:
    invalid_utf8 = (
        "POST /mcp HTTP/1.1\nHost: x\nContent-Type: application/json\n"
        "Accept: application/json\nAuthorization: Bearer ok\nContent-Length: 1"
    )
    wire = b"".join(asyncio.run(_run_async(AuthAsync(), invalid_utf8, b"\x80")).chunks)
    assert b"200 OK" in wire and b'"code": -32700' in wire

    bad_version = (
        "POST /mcp HTTP/9.9\nHost: x\nContent-Type: application/json\n"
        "Accept: application/json\nContent-Length: 0"
    )
    assert b"400 Bad Request" in b"".join(asyncio.run(_run_async(AuthAsync(), bad_version)).chunks)

    comma_host = (
        "POST /mcp HTTP/1.1\nHost: good, evil\nContent-Type: application/json\n"
        "Accept: application/json\nContent-Length: 0"
    )
    assert b"400 Bad Request" in b"".join(asyncio.run(_run_async(AuthAsync(), comma_host)).chunks)


def test_async_streamable_http_rejects_invalid_host_and_duplicate_singleton_headers() -> None:
    body = dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
    cases = [
        "POST /mcp HTTP/1.1\nContent-Type: application/json\nAccept: application/json\nContent-Length: %d" % len(body),
        "POST /mcp HTTP/1.1\nHost: a\nHost: b\nContent-Type: application/json\nAccept: application/json\nContent-Length: %d" % len(body),
        "POST /mcp HTTP/1.1\nHost: a\nAuthorization: one\nAuthorization: two\nContent-Type: application/json\nAccept: application/json\nContent-Length: %d" % len(body),
        "POST /mcp HTTP/1.1\nHost: a\nMCP-Protocol-Version: 2025-03-26\nMCP-Protocol-Version: 2025-03-26\nContent-Type: application/json\nAccept: application/json\nContent-Length: %d" % len(body),
        "POST /mcp HTTP/1.1\nHost: a\nTransfer-Encoding: chunked\nContent-Type: application/json\nAccept: application/json\nContent-Length: %d" % len(body),
    ]
    for request in cases:
        wire = b"".join(asyncio.run(_run_async(
            AuthAsync(), request, body,
            add_host=request.startswith("POST /mcp HTTP/1.1\nHost:"),
        )).chunks)
        assert b"400 Bad Request" in wire


def test_async_streamable_http_http10_allows_missing_host_and_query_path() -> None:
    body = dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
    request = f"POST /mcp?via=test HTTP/1.0\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: {len(body)}"
    wire = b"".join(asyncio.run(_run_async(AuthAsync(), request, body)).chunks)
    assert b"200 OK" in wire


def test_async_streamable_http_options_only_on_endpoint_and_hook_failures_are_500() -> None:
    wrong = b"".join(asyncio.run(_run_async(AuthAsync(), "OPTIONS /wrong HTTP/1.1\nHost: x\nOrigin: http://allowed\nContent-Length: 0")).chunks)
    assert b"405 Method Not Allowed" in wrong
    body = dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
    request = f"POST /mcp HTTP/1.1\nHost: x\nOrigin: http://allowed\nContent-Type: application/json\nAccept: application/json\nContent-Length: {len(body)}"
    for server in (HookExplodesAsync(), BadAuthTypeAsync()):
        wire = b"".join(asyncio.run(_run_async(server, request, body)).chunks)
        assert b"500 Internal Server Error" in wire and b"Access-Control-Allow-Origin: http://allowed" in wire
    authz_request = f"POST /mcp HTTP/1.1\nHost: x\nOrigin: http://allowed\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: {len(body)}"
    for server in (AuthorizeExplodesAsync(), BadAuthorizeTypeAsync()):
        wire = b"".join(asyncio.run(_run_async(server, authz_request, body)).chunks)
        assert b"500 Internal Server Error" in wire and b"Access-Control-Allow-Origin: http://allowed" in wire


def test_async_sse_origin_auth_media_and_body_rules() -> None:
    get_ok = b"".join(asyncio.run(_run_async_sse(AuthAsync(), "GET /sse HTTP/1.1\nHost: x\nOrigin: http://allowed\nAccept: text/event-stream\nAuthorization: Bearer ok\nContent-Length: 0")).chunks)
    assert b"200 OK" in get_ok and b"Access-Control-Allow-Origin: http://allowed" in get_ok
    get_bad_accept = b"".join(asyncio.run(_run_async_sse(AuthAsync(), "GET /sse HTTP/1.1\nHost: x\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: 0")).chunks)
    assert b"406 Not Acceptable" in get_bad_accept
    post_missing_session = b"".join(asyncio.run(_run_async_sse(AuthAsync(), "POST /message?sessionId=missing HTTP/1.1\nHost: x\nOrigin: http://allowed\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: 2", b"{}")).chunks)
    assert b"404 Not Found" in post_missing_session and b"Access-Control-Allow-Origin: http://allowed" in post_missing_session
    too_big = b"".join(asyncio.run(_run_async_sse(AuthAsync(), "POST /message?sessionId=missing HTTP/1.1\nHost: x\nContent-Type: application/json\nAccept: application/json\nAuthorization: Bearer ok\nContent-Length: 9", max_request_bytes=8)).chunks)
    assert b"413 Payload Too Large" in too_big


def test_async_sse_binds_sessions_to_authenticated_principal() -> None:
    server = AuthAsync()
    server._sse_sessions["s1"] = (Writer([]), asyncio.Event(), asyncio.Lock(), "bob")
    body = dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    request = (
        "POST /message?sessionId=s1 HTTP/1.1\nHost: x\n"
        "Content-Type: application/json\nAccept: application/json\n"
        f"Authorization: Bearer ok\nContent-Length: {len(body)}"
    )
    wire = b"".join(asyncio.run(_run_async_sse(server, request, body)).chunks)
    assert b"403 Forbidden" in wire


def test_async_sse_rejects_invalid_utf8_body() -> None:
    server = AuthAsync()
    server._sse_sessions["s1"] = (Writer([]), asyncio.Event(), asyncio.Lock(), "alice")
    request = (
        "POST /message?sessionId=s1 HTTP/1.1\nHost: x\n"
        "Content-Type: application/json\nAccept: application/json\n"
        "Authorization: Bearer ok\nContent-Length: 1"
    )
    wire = b"".join(asyncio.run(_run_async_sse(server, request, b"\xff")).chunks)
    assert b"400 Bad Request" in wire


def test_async_sse_session_cleanup_race_returns_404() -> None:
    class RaceServer(AuthAsync):
        def authorize_request(self, principal, *, rpc_method, tool_name):
            self._sse_sessions.pop("s1", None)
            return True

    server = RaceServer()
    server._sse_sessions["s1"] = (Writer([]), asyncio.Event(), asyncio.Lock(), "alice")
    body = dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    request = (
        "POST /message?sessionId=s1 HTTP/1.1\nHost: x\n"
        "Content-Type: application/json\nAccept: application/json\n"
        f"Authorization: Bearer ok\nContent-Length: {len(body)}"
    )
    wire = b"".join(asyncio.run(_run_async_sse(server, request, body)).chunks)
    assert b"404 Not Found" in wire


def test_async_sse_hook_failures_and_duplicate_host_are_500_or_400() -> None:
    req = "GET /sse HTTP/1.1\nHost: x\nHost: y\nAccept: text/event-stream\nContent-Length: 0"
    assert b"400 Bad Request" in b"".join(asyncio.run(_run_async_sse(AuthAsync(), req)).chunks)
    req = "GET /sse HTTP/1.1\nHost: x\nOrigin: http://allowed\nAccept: text/event-stream\nContent-Length: 0"
    for server in (HookExplodesAsync(), BadAuthTypeAsync()):
        wire = b"".join(asyncio.run(_run_async_sse(server, req)).chunks)
        assert b"500 Internal Server Error" in wire and b"Access-Control-Allow-Origin: http://allowed" in wire


def test_authentication_and_authorization_paths() -> None:
    s = AuthSync()
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert s.process_request(dumps(req)) is not None
    assert s.authenticate_request(method="POST", path="/mcp", headers={"authorization": "nope"}, peer="127.0.0.1") is None
