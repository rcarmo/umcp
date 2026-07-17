from __future__ import annotations

import asyncio
import json
import threading
import time
from io import BytesIO
from queue import Queue

import pytest

import aioumcp
import umcp
from aioumcp import AsyncMCPServer, get_progress_token as async_get_progress_token, is_request_cancelled as async_is_request_cancelled, notify_progress as async_notify_progress, raise_if_cancelled as async_raise_if_cancelled
from umcp import MCPServer, get_progress_token as sync_get_progress_token, is_request_cancelled as sync_is_request_cancelled, notify_progress as sync_notify_progress, raise_if_cancelled as sync_raise_if_cancelled


@pytest.fixture
def captured_stdout(monkeypatch):
    buf = BytesIO()
    monkeypatch.setattr(umcp, "_stdout_bin", buf)
    monkeypatch.setattr(aioumcp, "_stdout_bin", buf)
    return buf


def _parse_notifications(buf: BytesIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().decode("utf-8").splitlines() if line.strip()]


class SyncProgressServer(MCPServer):
    def tool_emit_progress(self):
        token = sync_get_progress_token()
        sync_notify_progress(1, 2, "hello")
        return {"token": token, "cancelled": sync_is_request_cancelled()}

    def tool_no_progress_token(self):
        sync_notify_progress(1, 2, "ignored")
        return {"token": sync_get_progress_token()}

    def tool_bad_progress(self):
        sync_notify_progress(float("nan"))
        return {"ok": True}


class AsyncProgressServer(AsyncMCPServer):
    async def tool_emit_progress(self):
        token = async_get_progress_token()
        await async_notify_progress(3, 5, "hello")
        return {"token": token, "cancelled": async_is_request_cancelled()}


class SyncCancelServer(MCPServer):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()

    def tool_wait_for_cancel(self):
        self.started.set()
        for _ in range(200):
            if sync_is_request_cancelled():
                sync_raise_if_cancelled()
            time.sleep(0.01)
        return {"cancelled": False}

    def tool_identity(self, label: str):
        self.started.set()
        for _ in range(50):
            if sync_is_request_cancelled():
                sync_raise_if_cancelled()
            time.sleep(0.002)
        return {"label": label, "cancelled": False}


class AsyncCancelServer(AsyncMCPServer):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def tool_wait_for_cancel(self):
        self.started.set()
        while True:
            await asyncio.sleep(1)

    async def tool_identity(self, label: str, delay: float = 0.01):
        await asyncio.sleep(delay)
        return {"label": label, "cancelled": async_is_request_cancelled()}


class _AsyncWriter:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.payloads.append(data)

    async def drain(self) -> None:
        return None


def test_sync_progress_exact_payload_and_absent_token_noop(captured_stdout) -> None:
    server = SyncProgressServer()
    response = server.process_request(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "emit_progress", "_meta": {"progressToken": "tok-1"}},
    }))
    notifications = _parse_notifications(captured_stdout)
    assert notifications == [{
        "jsonrpc": "2.0",
        "method": "notifications/progress",
        "params": {"progressToken": "tok-1", "progress": 1, "total": 2, "message": "hello"},
    }]
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload == {"token": "tok-1", "cancelled": False}

    captured_stdout.seek(0)
    captured_stdout.truncate(0)
    response = server.process_request(json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "no_progress_token"},
    }))
    assert _parse_notifications(captured_stdout) == []
    assert json.loads(response["result"]["content"][0]["text"]) == {"token": None}


def test_async_progress_exact_payload_for_integer_token(captured_stdout) -> None:
    server = AsyncProgressServer()
    response = asyncio.run(server.process_request_async(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "emit_progress", "_meta": {"progressToken": 9}},
    })))
    assert _parse_notifications(captured_stdout) == [{
        "jsonrpc": "2.0",
        "method": "notifications/progress",
        "params": {"progressToken": 9, "progress": 3, "total": 5, "message": "hello"},
    }]
    assert json.loads(response["result"]["content"][0]["text"]) == {"token": 9, "cancelled": False}


def test_invalid_progress_token_and_invalid_progress_are_rejected(captured_stdout) -> None:
    server = SyncProgressServer()
    response = server.process_request(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "emit_progress", "_meta": {"progressToken": True}},
    }))
    assert response["error"]["code"] == -32602

    response = server.process_request(json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "bad_progress", "_meta": {"progressToken": "tok"}},
    }))
    assert response["error"]["code"] in {-32602, -32603}
    assert _parse_notifications(captured_stdout) == []


def test_sync_cooperative_cancellation_and_cleanup() -> None:
    server = SyncCancelServer()
    response_box: dict[str, dict] = {}

    def run_request() -> None:
        response_box["response"] = server.process_request(json.dumps({
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "wait_for_cancel", "_meta": {"progressToken": "cancel-me"}},
        }))

    worker = threading.Thread(target=run_request)
    worker.start()
    assert server.started.wait(timeout=1)
    notification_response = server.process_request(json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 11},
    }))
    worker.join(timeout=2)

    assert notification_response is None
    assert response_box["response"]["error"] == {"code": -32800, "message": "Request cancelled"}
    assert server._active_requests_by_id == {}
    assert server._active_requests_by_progress_token == {}


def test_sync_cancellation_isolated_between_concurrent_requests() -> None:
    server = SyncCancelServer()
    results: dict[str, dict] = {}

    def call(name: str, request_id: int, token: str) -> None:
        results[name] = server.process_request(json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": "identity", "arguments": {"label": name}, "_meta": {"progressToken": token}},
        }))

    first = threading.Thread(target=call, args=("one", 1, "p1"))
    second = threading.Thread(target=call, args=("two", 2, "p2"))
    first.start()
    second.start()
    assert server.started.wait(timeout=1)
    server.process_request(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}))
    first.join(timeout=2)
    second.join(timeout=2)

    assert results["one"]["error"]["code"] == -32800
    assert json.loads(results["two"]["result"]["content"][0]["text"]) == {"label": "two", "cancelled": False}
    assert server._active_requests_by_id == {}
    assert server._active_requests_by_progress_token == {}


@pytest.mark.asyncio
async def test_async_active_cancellation_and_cleanup() -> None:
    server = AsyncCancelServer()
    request_task = asyncio.create_task(server.process_request_async(json.dumps({
        "jsonrpc": "2.0",
        "id": 21,
        "method": "tools/call",
        "params": {"name": "wait_for_cancel", "_meta": {"progressToken": "at-21"}},
    })))
    await asyncio.wait_for(server.started.wait(), timeout=1)
    notification_response = await server.process_request_async(json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 21},
    }))
    response = await asyncio.wait_for(request_task, timeout=1)

    assert notification_response is None
    assert response["error"] == {"code": -32800, "message": "Request cancelled"}
    assert server._active_requests_by_id == {}
    assert server._active_requests_by_progress_token == {}


@pytest.mark.asyncio
async def test_async_cancellation_isolated_between_concurrent_requests() -> None:
    server = AsyncCancelServer()
    first = asyncio.create_task(server.process_request_async(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "wait_for_cancel", "_meta": {"progressToken": "tok-1"}},
    })))
    await asyncio.wait_for(server.started.wait(), timeout=1)
    second = asyncio.create_task(server.process_request_async(json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "identity", "arguments": {"label": "two", "delay": 0.01}, "_meta": {"progressToken": "tok-2"}},
    })))
    await server.process_request_async(json.dumps({
        "jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1},
    }))
    first_response, second_response = await asyncio.gather(first, second)
    assert first_response["error"]["code"] == -32800
    assert json.loads(second_response["result"]["content"][0]["text"]) == {"label": "two", "cancelled": False}
    assert server._active_requests_by_id == {}
    assert server._active_requests_by_progress_token == {}


def test_sync_progress_uses_sse_transport_when_present() -> None:
    server = SyncProgressServer()
    q = Queue()
    server._sse_sessions = {"s1": q}
    response = server.process_request(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "emit_progress", "_meta": {"progressToken": "sse-token"}},
    }))
    payload = q.get_nowait().decode("utf-8")
    assert '"method": "notifications/progress"' in payload
    assert '"progressToken": "sse-token"' in payload
    assert response["result"]["content"]


@pytest.mark.asyncio
async def test_async_progress_uses_sse_transport_when_present() -> None:
    server = AsyncProgressServer()
    writer = _AsyncWriter()
    server._sse_sessions = {"s1": (writer, asyncio.Event(), asyncio.Lock(), "anonymous")}
    response = await server.process_request_async(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "emit_progress", "_meta": {"progressToken": "sse-async"}},
    }))
    assert any(b'notifications/progress' in payload for payload in writer.payloads)
    assert any(b'"progressToken": "sse-async"' in payload for payload in writer.payloads)
    assert response["result"]["content"]
