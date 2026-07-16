from __future__ import annotations

import asyncio
from dataclasses import dataclass
from json import dumps, loads

from aioumcp import AsyncMCPServer
from umcp import MCPServer
from umcp_shared import get_request_context


@dataclass
class DummyWriter:
    chunks: list[bytes]
    closed: bool = False

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str):
        return ("127.0.0.1", 5555) if name == "peername" else None


class DummyReader:
    def __init__(self, request: str, body: bytes = b""):
        self.lines = [line.encode() + b"\r\n" for line in request.splitlines()]
        self.lines.append(b"\r\n")
        self._body = body

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    async def readexactly(self, n: int) -> bytes:
        return self._body[:n]


class ContextToolSync(MCPServer):
    def tool_ctx(self):
        ctx = get_request_context(); return {"transport": ctx.transport, "request_id": ctx.request_id, "principal": ctx.principal}


class ContextToolAsync(AsyncMCPServer):
    async def tool_ctx(self):
        ctx = get_request_context(); return {"transport": ctx.transport, "request_id": ctx.request_id, "principal": ctx.principal}


class SyncHTTPServer(MCPServer):
    def __init__(self):
        super().__init__()
        self.requests = []

    def process_request(self, input_data: str, **kwargs):
        self.requests.append(kwargs.get("context", get_request_context()))
        return {"jsonrpc": "2.0", "result": {"ok": True}, "id": 1}


class AsyncHTTPServer(AsyncMCPServer):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def process_request_async(self, input_data: str, **kwargs):
        self.requests.append(kwargs.get("context", get_request_context()))
        return {"jsonrpc": "2.0", "result": {"ok": True}, "id": 1}



def test_sync_tool_context_from_process_request():
    server = ContextToolSync()
    response = server.process_request(dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "ctx"}, "id": 1}))
    payload = loads(response["result"]["content"][0]["text"]); assert payload["transport"] is None


async def _invoke_async_handler(server: AsyncHTTPServer, request: str, body: bytes):
    reader = DummyReader(request, body)
    writer = DummyWriter([])
    await server._handle_streamable_http_client(reader, writer, "/mcp", ["http://allowed"], 1024)
    return writer


def test_async_tool_context_from_process_request():
    server = ContextToolAsync()
    response = asyncio.run(server.process_request_async(dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "ctx"}, "id": 1})))
    payload = loads(response["result"]["content"][0]["text"]); assert payload["transport"] is None


def test_async_streamable_http_accepts_json_and_context():
    server = AsyncHTTPServer()
    body = dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}).encode()
    request = "\n".join([
        "POST /mcp HTTP/1.1",
        "Content-Type: application/json",
        "Accept: application/json",
        "Origin: http://allowed",
        f"Content-Length: {len(body)}",
    ])
    writer = asyncio.run(_invoke_async_handler(server, request, body))
    writer.chunks
    assert any(b"200 OK" in chunk or b"202 Accepted" in chunk for chunk in writer.chunks)
