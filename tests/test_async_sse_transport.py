"""End-to-end tests for the async SSE transport.

These go beyond the sync SSE smoke test and exercise the async server's
actual HTTP/SSE path, including multi-client resource subscription
isolation.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def _spawn(script: str, *extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(EXAMPLES / script), *extra],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _wait_for_port(stdout, *, timeout: float = 5.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = stdout.readline()
        if not line:
            time.sleep(0.05)
            continue
        m = re.search(r":(\d{4,5})", line)
        if m:
            return int(m.group(1))
    raise TimeoutError("server did not announce a port in time")


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        pass


@dataclass
class _SSEEvent:
    event: str
    data: str


class _SSEClient:
    """Tiny SSE reader that turns an HTTP stream into queued events."""

    def __init__(self, url: str) -> None:
        self.response = urllib.request.urlopen(url, timeout=3.0)
        self.events: queue.Queue[_SSEEvent] = queue.Queue()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        event_name = "message"
        data_lines: list[str] = []
        try:
            while True:
                raw = self.response.fp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        self.events.put(_SSEEvent(event_name, "\n".join(data_lines)))
                    event_name = "message"
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue  # keepalive comment
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].strip())
        except Exception:
            pass

    def next_event(self, timeout: float = 2.0) -> _SSEEvent:
        return self.events.get(timeout=timeout)

    def expect_no_event(self, timeout: float = 0.75) -> None:
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty:
            return
        raise AssertionError(f"unexpected SSE event: {event}")

    def close(self) -> None:
        try:
            self.response.close()
        except Exception:  # noqa: BLE001
            pass


def _post_json(url: str, payload: dict) -> int:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=3.0) as resp:
        return resp.status


def test_async_sse_handshake_and_request_round_trip() -> None:
    proc = _spawn("async_calculator_server.py", "--port", "0")
    client = None
    try:
        port = _wait_for_port(proc.stdout)
        client = _SSEClient(f"http://127.0.0.1:{port}/sse")
        endpoint_evt = client.next_event()
        assert endpoint_evt.event == "endpoint"
        endpoint = endpoint_evt.data
        assert endpoint.startswith("/message?sessionId=")

        status = _post_json(
            f"http://127.0.0.1:{port}{endpoint}",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert 200 <= status < 300

        message_evt = client.next_event()
        assert message_evt.event == "message"
        payload = json.loads(message_evt.data)
        assert "result" in payload
        assert any(t["name"] == "add" for t in payload["result"]["tools"])
    finally:
        if client is not None:
            client.close()
        _kill(proc)


def test_async_sse_resource_updates_are_isolated_per_session() -> None:
    proc = _spawn("async_resource_server.py", "--port", "0")
    c1 = None
    c2 = None
    try:
        port = _wait_for_port(proc.stdout)
        c1 = _SSEClient(f"http://127.0.0.1:{port}/sse")
        c2 = _SSEClient(f"http://127.0.0.1:{port}/sse")

        e1 = c1.next_event()
        e2 = c2.next_event()
        endpoint1 = f"http://127.0.0.1:{port}{e1.data}"
        endpoint2 = f"http://127.0.0.1:{port}{e2.data}"

        assert e1.event == e2.event == "endpoint"
        assert endpoint1 != endpoint2

        # Session 1 subscribes to the MOTD resource.
        status = _post_json(endpoint1, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/subscribe",
            "params": {"uri": "umcp://AsyncResourceServer/motd"},
        })
        assert 200 <= status < 300
        sub_reply = json.loads(c1.next_event().data)
        assert sub_reply["result"] == {}

        # Trigger a change through session 1.
        status = _post_json(endpoint1, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "touch_motd",
                "arguments": {"message": "changed via SSE"},
            },
        })
        assert 200 <= status < 300

        # Session 1 should see both an updated notification and the tool call response.
        seen_updated = False
        seen_tool_response = False
        deadline = time.time() + 3.0
        while time.time() < deadline and not (seen_updated and seen_tool_response):
            evt = c1.next_event(timeout=2.0)
            payload = json.loads(evt.data)
            if payload.get("method") == "notifications/resources/updated":
                assert payload["params"] == {"uri": "umcp://AsyncResourceServer/motd"}
                seen_updated = True
            elif payload.get("id") == 2:
                seen_tool_response = True
        assert seen_updated, "subscribed session never got resources/updated"
        assert seen_tool_response, "subscribed session never got the tool response"

        # Session 2 never subscribed, so it should not see the update notification.
        c2.expect_no_event(timeout=1.0)

        # And a POST to its own endpoint still works, proving the session is alive.
        status = _post_json(endpoint2, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "touch_motd",
                "arguments": {"message": "changed from session 2"},
            },
        })
        assert 200 <= status < 300
        evt = c2.next_event(timeout=2.0)
        payload = json.loads(evt.data)
        assert payload.get("id") == 3
    finally:
        if c1 is not None:
            c1.close()
        if c2 is not None:
            c2.close()
        _kill(proc)
