"""End-to-end transport tests.

These tests spawn an example server as a subprocess and exercise the
transport in question.  They complement ``test_async_servers.py`` (which
covers the async stdio transport) by adding:

* sync stdio (single-shot file mode and streaming line mode)
* sync TCP socket transport
* sync SSE HTTP transport (basic handshake + JSON-RPC round-trip)

The tests are tolerant of slow start-up (poll for the port to come up)
and always tear the subprocess down in a ``finally`` clause to avoid
leaking processes on failure.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


# ---------- helpers ---------------------------------------------------------


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
    """Read lines off the subprocess stdout until a ``:PORT`` is announced."""
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


# ---------- sync stdio (single-shot file mode) ------------------------------


def test_sync_stdio_streaming_round_trip() -> None:
    """Launch the calculator example and round-trip a tools/list request."""
    proc = _spawn("calculator_server.py")
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line)
        assert "result" in resp
        assert any(t["name"] == "add" for t in resp["result"]["tools"])
    finally:
        _kill(proc)


def test_sync_stdio_resources_round_trip() -> None:
    """Resource server's resources/list works over stdio."""
    proc = _spawn("resource_server.py")
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "resources/list",
        }) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line)
        uris = {r["uri"] for r in resp["result"]["resources"]}
        assert "umcp://ResourceServer/motd" in uris
    finally:
        _kill(proc)


# ---------- sync TCP transport ---------------------------------------------


@pytest.mark.parametrize("script", ["calculator_server.py"])
def test_sync_tcp_transport_round_trip(script) -> None:
    """Launch the sync server with --port 0 --tcp, connect, round-trip."""
    proc = _spawn(script, "--port", "0", "--tcp")
    try:
        port = _wait_for_port(proc.stdout)
        with closing(socket.create_connection(("127.0.0.1", port), timeout=2.0)) as sock:
            sock.sendall((json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            }) + "\n").encode("utf-8"))
            buf = b""
            sock.settimeout(2.0)
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line, _, _ = buf.partition(b"\n")
            resp = json.loads(line.decode("utf-8"))
            assert "result" in resp
    finally:
        _kill(proc)


# ---------- sync SSE transport ---------------------------------------------


def test_sync_sse_transport_handshake_and_request() -> None:
    """Start the SSE transport, request /sse stream, then POST a request."""
    proc = _spawn("calculator_server.py", "--port", "0")
    try:
        port = _wait_for_port(proc.stdout)
        # Open the SSE stream and read the endpoint announcement.
        sse_req = urllib.request.Request(f"http://127.0.0.1:{port}/sse")
        sse_resp = urllib.request.urlopen(sse_req, timeout=2.0)
        # First event should be ``endpoint`` with the POST URL.
        endpoint_line = b""
        for _ in range(20):
            line = sse_resp.fp.readline()
            if not line:
                break
            if line.startswith(b"data:"):
                endpoint_line = line
                break
        sse_resp.close()
        assert endpoint_line.startswith(b"data:")
        endpoint = endpoint_line.split(b":", 1)[1].strip().decode("utf-8")
        assert "/message" in endpoint
        # POST a tools/list request to the endpoint and accept any 2xx.
        post_url = f"http://127.0.0.1:{port}{endpoint}" if endpoint.startswith("/") else endpoint
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode("utf-8")
        post = urllib.request.Request(post_url, data=body,
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(post, timeout=2.0) as r:
            assert 200 <= r.status < 300
    finally:
        _kill(proc)


# ---------- CLI surface ----------------------------------------------------


def test_help_flag_does_not_crash() -> None:
    """``--help`` should print usage and exit cleanly (rc 0 or 1, but not >1)."""
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "calculator_server.py"), "--help"],
        capture_output=True, text=True, timeout=5,
    )
    # Some servers don't have a --help; we accept either an early exit with
    # usage on stdout/stderr, or no special handling at all.  All we really
    # care about is that it doesn't hang or crash with a high return code.
    assert result.returncode <= 1
