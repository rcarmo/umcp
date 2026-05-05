"""Tests for resource-change notifications.

The library writes notifications to ``umcp._stdout_bin`` (sync) /
``aioumcp._stdout_bin`` (async) when no SSE session is active.  We
monkey-patch that buffer for the test, capture what's written, and
parse the JSON-RPC notifications back out.

Covers:

* notify_resource_list_changed always emits
* notify_resource_updated only emits when the URI is subscribed
* multiple subscriptions are tracked independently
* unsubscribe stops further notifications
* async equivalents
"""

from __future__ import annotations

import asyncio
import json
from io import BytesIO
from queue import Queue

import pytest

import aioumcp
import umcp
from aioumcp import AsyncMCPServer
from umcp import MCPServer


@pytest.fixture
def captured_stdout(monkeypatch):
    """Replace the binary stdout buffers used for notifications with a BytesIO."""
    buf = BytesIO()
    monkeypatch.setattr(umcp, "_stdout_bin", buf)
    monkeypatch.setattr(aioumcp, "_stdout_bin", buf)
    return buf


def _parse_notifications(buf: BytesIO) -> list[dict]:
    """Parse newline-delimited JSON-RPC notifications from the captured buffer."""
    out: list[dict] = []
    for line in buf.getvalue().decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ---------- Sync ------------------------------------------------------------


class _SyncNotifyServer(MCPServer):
    pass


def test_sync_list_changed_always_emits(captured_stdout) -> None:
    s = _SyncNotifyServer()
    s.notify_resource_list_changed()
    notifs = _parse_notifications(captured_stdout)
    assert len(notifs) == 1
    assert notifs[0]["method"] == "notifications/resources/list_changed"
    assert "params" not in notifs[0]


def test_sync_updated_skipped_without_subscription(captured_stdout) -> None:
    s = _SyncNotifyServer()
    s.notify_resource_updated("umcp://nobody/cares")
    assert _parse_notifications(captured_stdout) == []


def test_sync_updated_emits_only_for_subscribed_uri(captured_stdout) -> None:
    s = _SyncNotifyServer()
    s._resource_subscriptions.add("umcp://x/a")
    s.notify_resource_updated("umcp://x/a")
    s.notify_resource_updated("umcp://x/b")  # not subscribed
    notifs = _parse_notifications(captured_stdout)
    assert len(notifs) == 1
    assert notifs[0]["method"] == "notifications/resources/updated"
    assert notifs[0]["params"] == {"uri": "umcp://x/a"}


def test_sync_unsubscribe_stops_notifications(captured_stdout) -> None:
    s = _SyncNotifyServer()
    s._resource_subscriptions.add("umcp://x/a")
    s.notify_resource_updated("umcp://x/a")
    s._resource_subscriptions.discard("umcp://x/a")
    s.notify_resource_updated("umcp://x/a")
    notifs = _parse_notifications(captured_stdout)
    assert len(notifs) == 1


def test_sync_subscribe_via_protocol_then_notify(captured_stdout) -> None:
    """End-to-end: subscribe via JSON-RPC then trigger a notification."""
    s = _SyncNotifyServer()
    sub_resp = s.process_request(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
        "params": {"uri": "umcp://test/a"},
    }))
    assert sub_resp["result"] == {}
    s.notify_resource_updated("umcp://test/a")
    notifs = _parse_notifications(captured_stdout)
    assert len(notifs) == 1
    assert notifs[0]["params"]["uri"] == "umcp://test/a"


def test_sync_sse_updated_targets_only_subscribed_session() -> None:
    """When SSE is active, resource updates should go only to sessions that
    subscribed to that URI, not every connected client."""
    s = _SyncNotifyServer()
    q1: Queue = Queue()
    q2: Queue = Queue()
    s._sse_sessions = {"s1": q1, "s2": q2}
    s._resource_session_subscriptions = {
        "s1": {"umcp://x/a"},
        "s2": {"umcp://x/b"},
    }

    s.notify_resource_updated("umcp://x/a")

    payload1 = q1.get_nowait().decode("utf-8")
    assert 'notifications/resources/updated' in payload1
    assert q2.empty()


def test_sync_sse_list_changed_broadcasts_to_all_sessions() -> None:
    s = _SyncNotifyServer()
    q1: Queue = Queue()
    q2: Queue = Queue()
    s._sse_sessions = {"s1": q1, "s2": q2}

    s.notify_resource_list_changed()

    assert 'notifications/resources/list_changed' in q1.get_nowait().decode("utf-8")
    assert 'notifications/resources/list_changed' in q2.get_nowait().decode("utf-8")


# ---------- Async -----------------------------------------------------------


class _AsyncNotifyServer(AsyncMCPServer):
    pass


def test_async_list_changed_always_emits(captured_stdout) -> None:
    s = _AsyncNotifyServer()
    asyncio.run(s.notify_resource_list_changed())
    notifs = _parse_notifications(captured_stdout)
    assert len(notifs) == 1
    assert notifs[0]["method"] == "notifications/resources/list_changed"


def test_async_updated_gated_by_subscription(captured_stdout) -> None:
    s = _AsyncNotifyServer()

    async def go() -> None:
        await s.notify_resource_updated("umcp://x/a")  # not subscribed
        s._resource_subscriptions.add("umcp://x/a")
        await s.notify_resource_updated("umcp://x/a")

    asyncio.run(go())
    notifs = _parse_notifications(captured_stdout)
    assert len(notifs) == 1
    assert notifs[0]["params"] == {"uri": "umcp://x/a"}


class _FakeAsyncWriter:
    """Minimal StreamWriter stand-in for notification tests."""

    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.payloads.append(data)

    async def drain(self) -> None:
        return None


def test_async_sse_updated_targets_only_subscribed_session() -> None:
    s = _AsyncNotifyServer()
    w1 = _FakeAsyncWriter()
    w2 = _FakeAsyncWriter()
    s._sse_sessions = {
        "s1": (w1, asyncio.Event(), asyncio.Lock()),
        "s2": (w2, asyncio.Event(), asyncio.Lock()),
    }
    s._resource_session_subscriptions = {
        "s1": {"umcp://x/a"},
        "s2": {"umcp://x/b"},
    }

    asyncio.run(s.notify_resource_updated("umcp://x/a"))

    assert len(w1.payloads) == 1
    assert b'notifications/resources/updated' in w1.payloads[0]
    assert w2.payloads == []


def test_async_sse_list_changed_broadcasts_to_all_sessions() -> None:
    s = _AsyncNotifyServer()
    w1 = _FakeAsyncWriter()
    w2 = _FakeAsyncWriter()
    s._sse_sessions = {
        "s1": (w1, asyncio.Event(), asyncio.Lock()),
        "s2": (w2, asyncio.Event(), asyncio.Lock()),
    }

    asyncio.run(s.notify_resource_list_changed())

    assert len(w1.payloads) == 1
    assert len(w2.payloads) == 1
    assert b'notifications/resources/list_changed' in w1.payloads[0]
    assert b'notifications/resources/list_changed' in w2.payloads[0]
