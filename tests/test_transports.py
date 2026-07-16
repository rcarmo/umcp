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
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

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


# ---------- sync streamable HTTP transport ---------------------------------


def _http_request(host: str, port: int, request: bytes, *, shutdown_write: bool = False) -> bytes:
    with closing(socket.create_connection((host, port), timeout=2.0)) as sock:
        sock.settimeout(2.0)
        sock.sendall(request)
        if shutdown_write:
            sock.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        return buf


def test_sync_streamable_http_round_trip_and_error_paths() -> None:
    proc = _spawn("calculator_server.py", "--port", "0", "--http")
    try:
        port = _wait_for_port(proc.stdout)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode("utf-8")
        response = _http_request(
            "127.0.0.1",
            port,
            (
                b"POST /mcp HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Accept: application/json\r\n"
                b"MCP-Protocol-Version: 2025-03-26\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            ),
        )
        assert b"200 OK" in response and b'"tools"' in response

        disallowed_get = _http_request(
            "127.0.0.1", port,
            b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://evil.example\r\n\r\n",
        )
        assert b"403 Forbidden" in disallowed_get

        disallowed_path = _http_request(
            "127.0.0.1", port,
            b"POST /wrong HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://evil.example\r\nContent-Length: 0\r\n\r\n",
        )
        assert b"403 Forbidden" in disallowed_path

        no_origin = _http_request(
            "127.0.0.1", port,
            b"OPTIONS /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n",
        )
        assert b"405 Method Not Allowed" in no_origin
        assert b"Allow: POST, OPTIONS" in no_origin

        oversized_header = _http_request(
            "127.0.0.1",
            port,
            b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Oversized: "
            + (b"x" * 65536)
            + b"\r\nContent-Length: 0\r\n\r\n",
        )
        assert b"431 Line too long" in oversized_header

        short_body = _http_request(
            "127.0.0.1",
            port,
            b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nAccept: application/json\r\nMCP-Protocol-Version: 2025-03-26\r\nContent-Length: 5\r\n\r\n{}",
            shutdown_write=True,
        )
        assert b"400 Bad Request" in short_body
    finally:
        _kill(proc)


def test_sync_streamable_http_auth_401_and_403() -> None:
    script = NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(ROOT))
    try:
        script.write(
            "from umcp import MCPServer\n"
            "from umcp_shared import MCPPrincipal\n"
            "class S(MCPServer):\n"
            "    def authenticate_request(self, *, method, path, headers, peer):\n"
            "        return MCPPrincipal(name='alice') if headers.get('authorization') == 'Bearer ok' else None\n"
            "    def authorize_request(self, principal, *, rpc_method, tool_name):\n"
            "        return tool_name != 'forbidden'\n"
            "    def tool_forbidden(self):\n"
            "        return 'x'\n"
            "S().run()\n"
        )
        script.flush(); script.close()
        proc = subprocess.Popen(
            [sys.executable, script.name, "--port", "0", "--http"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            cwd=str(ROOT),
        )
        try:
            port = _wait_for_port(proc.stdout)
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "forbidden"}}).encode("utf-8")
            unauthorized = _http_request(
                "127.0.0.1",
                port,
                (
                    b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nAccept: application/json\r\nMCP-Protocol-Version: 2025-03-26\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
                ),
            )
            assert b"401 Unauthorized" in unauthorized
            forbidden = _http_request(
                "127.0.0.1",
                port,
                (
                    b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nAccept: application/json\r\nAuthorization: Bearer ok\r\nMCP-Protocol-Version: 2025-03-26\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
                ),
            )
            assert b"403 Forbidden" in forbidden
        finally:
            _kill(proc)
    finally:
        os.unlink(script.name)


def test_sync_streamable_http_legacy_auth_alias_cors_errors_and_length_guards() -> None:
    script = NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(ROOT))
    try:
        script.write(
            "from umcp import MCPServer\n"
            "from umcp_shared import MCPPrincipal\n"
            "class S(MCPServer):\n"
            "    def authenticate(self, headers, peer):\n"
            "        return MCPPrincipal(name='legacy') if headers.get('authorization') == 'Bearer ok' else None\n"
            "    def authorize(self, principal, method, params):\n"
            "        return params.get('name') != 'forbidden'\n"
            "    def tool_forbidden(self):\n"
            "        return 'x'\n"
            "S().run()\n"
        )
        script.flush(); script.close()
        proc = subprocess.Popen(
            [sys.executable, script.name, "--port", "0", "--http", "--allowed-origin", "http://allowed"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            cwd=str(ROOT),
        )
        try:
            port = _wait_for_port(proc.stdout)
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "forbidden"}}).encode("utf-8")
            requests = [
                (
                    b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://allowed\r\nContent-Length: 0\r\n\r\n",
                    b"405 Method Not Allowed",
                ),
                (
                    b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://allowed\r\nContent-Type: text/plain\r\nAccept: application/json\r\nAuthorization: Bearer ok\r\nContent-Length: 0\r\n\r\n",
                    b"415 Unsupported Media Type",
                ),
                (
                    b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://allowed\r\nContent-Type: application/json\r\nAccept: application/json\r\nMCP-Protocol-Version: 2025-03-26\r\nContent-Length: 0\r\n\r\n",
                    b"401 Unauthorized",
                ),
                (
                    b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://allowed\r\nContent-Type: application/json\r\nAccept: application/json\r\nAuthorization: Bearer ok\r\nMCP-Protocol-Version: 2025-03-26\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
                    b"400 Bad Request",
                ),
                (
                    b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://allowed\r\nContent-Type: application/json\r\nAccept: application/json\r\nAuthorization: Bearer ok\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n",
                    b"400 Bad Request",
                ),
                (
                    b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://allowed\r\nContent-Type: application/json\r\nAccept: application/json\r\nAuthorization: Bearer ok\r\nMCP-Protocol-Version: 2025-03-26\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body,
                    b"403 Forbidden",
                ),
            ]
            for request, status in requests:
                response = _http_request("127.0.0.1", port, request)
                assert status in response
                assert b"Access-Control-Allow-Origin: http://allowed" in response
                assert b"Vary: Origin" in response
        finally:
            _kill(proc)
    finally:
        os.unlink(script.name)


def test_sync_streamable_http_query_host_duplicate_headers_and_options_path() -> None:
    proc = _spawn("calculator_server.py", "--port", "0", "--http")
    try:
        port = _wait_for_port(proc.stdout)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
        query = _http_request(
            "127.0.0.1", port,
            b"POST /mcp?via=test HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nAccept: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body,
        )
        assert b"200 OK" in query
        missing_host = _http_request(
            "127.0.0.1", port,
            b"POST /mcp HTTP/1.1\r\nContent-Type: application/json\r\nAccept: application/json\r\nContent-Length: 0\r\n\r\n",
        )
        assert b"400 Bad Request" in missing_host
        dup_host = _http_request(
            "127.0.0.1", port,
            b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nHost: localhost\r\nContent-Type: application/json\r\nAccept: application/json\r\nContent-Length: 0\r\n\r\n",
        )
        assert b"400 Bad Request" in dup_host
        wrong_options = _http_request(
            "127.0.0.1", port,
            b"OPTIONS /wrong HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://localhost\r\nContent-Length: 0\r\n\r\n",
        )
        assert b"405 Method Not Allowed" in wrong_options
    finally:
        _kill(proc)


def test_sync_streamable_http_rejects_async_auth_hooks_cleanly() -> None:
    script = NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(ROOT))
    try:
        script.write(
            "from umcp import MCPServer\n"
            "class S(MCPServer):\n"
            "    async def authenticate_request(self, *, method, path, headers, peer):\n"
            "        return None\n"
            "S().run()\n"
        )
        script.flush(); script.close()
        proc = subprocess.Popen(
            [sys.executable, script.name, "--port", "0", "--http"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=str(ROOT),
        )
        try:
            port = _wait_for_port(proc.stdout)
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
            response = _http_request(
                "127.0.0.1", port,
                b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nAccept: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode() + body,
            )
            assert b"500 Internal Server Error" in response
            assert proc.poll() is None
        finally:
            _kill(proc)
    finally:
        os.unlink(script.name)


# ---------- sync SSE transport ---------------------------------------------


def test_sync_sse_security_origin_and_body_guards() -> None:
    script = NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(ROOT))
    try:
        script.write(
            "from umcp import MCPServer\n"
            "from umcp_shared import MCPPrincipal\n"
            "class S(MCPServer):\n"
            "    def authenticate_request(self, *, method, path, headers, peer):\n"
            "        return MCPPrincipal(name='alice') if headers.get('authorization') == 'Bearer ok' else None\n"
            "    def authorize_request(self, principal, *, rpc_method, tool_name):\n"
            "        return True\n"
            "S().run()\n"
        )
        script.flush(); script.close()
        proc = subprocess.Popen([sys.executable, script.name, "--port", "0", "--sse", "--allowed-origin", "http://allowed"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, cwd=str(ROOT))
        try:
            port = _wait_for_port(proc.stdout)
            bad_origin = _http_request("127.0.0.1", port, b"GET /sse HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://evil.example\r\nAccept: text/event-stream\r\nAuthorization: Bearer ok\r\nContent-Length: 0\r\n\r\n")
            assert b"403 Forbidden" in bad_origin
            unauth = _http_request("127.0.0.1", port, b"GET /sse HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://allowed\r\nAccept: text/event-stream\r\nContent-Length: 0\r\n\r\n")
            assert b"401 Unauthorized" in unauth and b"Access-Control-Allow-Origin: http://allowed" in unauth
            bad_media = _http_request("127.0.0.1", port, b"POST /message?sessionId=missing HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://allowed\r\nContent-Type: text/plain\r\nAccept: application/json\r\nAuthorization: Bearer ok\r\nContent-Length: 0\r\n\r\n")
            assert b"415 Unsupported Media Type" in bad_media and b"Access-Control-Allow-Origin: http://allowed" in bad_media
            dup_host = _http_request("127.0.0.1", port, b"GET /sse HTTP/1.1\r\nHost: 127.0.0.1\r\nHost: localhost\r\nAccept: text/event-stream\r\nAuthorization: Bearer ok\r\nContent-Length: 0\r\n\r\n")
            assert b"400 Bad Request" in dup_host
        finally:
            _kill(proc)
    finally:
        os.unlink(script.name)


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


def test_sync_sse_session_cleanup_race_returns_404() -> None:
    script = NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(ROOT))
    try:
        script.write(
            "from umcp import MCPServer\n"
            "class S(MCPServer):\n"
            "    def authorize_request(self, principal, *, rpc_method, tool_name):\n"
            "        with self._sse_lock: self._sse_sessions.clear()\n"
            "        return True\n"
            "S().run()\n"
        )
        script.flush(); script.close()
        proc = subprocess.Popen(
            [sys.executable, script.name, "--port", "0", "--sse"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=str(ROOT),
        )
        try:
            port = _wait_for_port(proc.stdout)
            sse = urllib.request.urlopen(f"http://127.0.0.1:{port}/sse", timeout=2)
            endpoint = ""
            for _ in range(20):
                line = sse.fp.readline()
                if line.startswith(b"data:"):
                    endpoint = line.split(b":", 1)[1].strip().decode()
                    break
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{endpoint}", data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=2)
            assert error.value.code == 404
            sse.close()
        finally:
            _kill(proc)
    finally:
        os.unlink(script.name)


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
