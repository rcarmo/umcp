from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

PROTOCOL_VERSION = "2025-03-26"
SERVER_FIXTURE = Path(__file__).parent / "fixtures" / "streamable_http_server.py"


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _running_server(mode: str) -> Iterator[int]:
    port = _unused_port()
    process = subprocess.Popen(
        [sys.executable, str(SERVER_FIXTURE), mode, str(port)],
        cwd=SERVER_FIXTURE.parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"server exited early: {stdout!r} {stderr!r}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                break
        except OSError:
            time.sleep(0.02)
    else:
        process.terminate()
        raise AssertionError("server did not start")
    try:
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)


@pytest.fixture(params=("sync", "async"))
def streamable_server(request: pytest.FixtureRequest) -> Iterator[tuple[str, int]]:
    mode = str(request.param)
    with _running_server(mode) as port:
        yield mode, port


def _post(
    connection: http.client.HTTPConnection,
    payload: dict[str, object],
    *,
    principal: str = "alice",
    session_id: str | None = None,
    close: bool = False,
    origin: str | None = None,
) -> http.client.HTTPResponse:
    body = json.dumps(payload).encode()
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {principal}",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    if payload.get("method") != "initialize":
        headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if close:
        headers["Connection"] = "close"
    if origin:
        headers["Origin"] = origin
    connection.request("POST", "/mcp", body=body, headers=headers)
    return connection.getresponse()


def _initialize(
    connection: http.client.HTTPConnection,
    *,
    origin: str | None = None,
) -> tuple[str, dict[str, object]]:
    response = _post(
        connection,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "uMCP tests", "version": "1"},
            },
        },
        origin=origin,
    )
    payload = json.loads(response.read())
    session_id = response.getheader("Mcp-Session-Id")
    assert response.status == 200
    assert response.version == 11
    assert response.will_close is False
    assert session_id
    assert payload["result"]["protocolVersion"] == PROTOCOL_VERSION
    return session_id, payload


def _read_raw_response(stream) -> tuple[bytes, dict[bytes, bytes], bytes]:
    status = stream.readline().rstrip(b"\r\n")
    headers: dict[bytes, bytes] = {}
    while True:
        line = stream.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, value = line.split(b":", 1)
        headers[name.lower()] = value.strip()
    length = int(headers.get(b"content-length", b"0"))
    return status, headers, stream.read(length)


def _debug_sessions(port: int) -> dict[str, int]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    connection.request("GET", "/debug/sessions")
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    assert response.status == 200
    return payload


def _stateless_tools_request(*, connection: str) -> bytes:
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
    return (
        b"POST /mcp HTTP/1.0\r\n"
        b"Host: test\r\n"
        b"Accept: application/json\r\n"
        b"Authorization: Bearer alice\r\n"
        b"Content-Type: application/json\r\n"
        + f"MCP-Protocol-Version: {PROTOCOL_VERSION}\r\n".encode()
        + f"Content-Length: {len(body)}\r\n".encode()
        + f"Connection: {connection}\r\n\r\n".encode()
        + body
    )


def test_http_11_reuses_connection_and_explicitly_closes(streamable_server: tuple[str, int]) -> None:
    _mode, port = streamable_server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    session_id, _payload = _initialize(connection)
    original_socket = connection.sock

    response = _post(
        connection,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        session_id=session_id,
    )
    assert response.status == 200
    response.read()
    assert response.will_close is False
    assert connection.sock is original_socket

    response = _post(
        connection,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        session_id=session_id,
        close=True,
    )
    assert response.status == 200
    response.read()
    assert response.getheader("Connection") == "close"
    assert response.will_close is True
    connection.close()


def test_http_10_close_and_keep_alive_are_explicit(streamable_server: tuple[str, int]) -> None:
    _mode, port = streamable_server
    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as sock:
        stream = sock.makefile("rb")
        sock.sendall(_stateless_tools_request(connection="close"))
        status, headers, _body = _read_raw_response(stream)
        assert b"200 OK" in status
        assert headers[b"connection"] == b"close"
        assert stream.read(1) == b""

    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as sock:
        stream = sock.makefile("rb")
        sock.sendall(_stateless_tools_request(connection="keep-alive"))
        status, headers, _body = _read_raw_response(stream)
        assert b"200 OK" in status
        assert headers[b"connection"] == b"keep-alive"
        sock.sendall(_stateless_tools_request(connection="close"))
        status, headers, _body = _read_raw_response(stream)
        assert b"200 OK" in status
        assert headers[b"connection"] == b"close"


def _open_event_stream(port: int, session_id: str, principal: str = "alice"):
    sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    sock.sendall(
        (
            "GET /mcp HTTP/1.1\r\n"
            "Host: test\r\n"
            "Accept: text/event-stream\r\n"
            f"Authorization: Bearer {principal}\r\n"
            f"MCP-Protocol-Version: {PROTOCOL_VERSION}\r\n"
            f"Mcp-Session-Id: {session_id}\r\n\r\n"
        ).encode()
    )
    return sock, sock.makefile("rb")


def test_session_binding_notifications_reconnect_and_delete(streamable_server: tuple[str, int]) -> None:
    _mode, port = streamable_server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    session_id, _payload = _initialize(connection)

    denied = _post(
        connection,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        principal="mallory",
        session_id=session_id,
    )
    assert denied.status == 403
    denied.read()

    stream_socket, stream = _open_event_stream(port, session_id)
    status, headers, _body = _read_raw_response(stream)
    assert b"200 OK" in status
    assert headers[b"content-type"] == b"text/event-stream"
    assert stream.readline().startswith(b": connected")
    assert stream.readline() in (b"\r\n", b"\n")

    conflict_socket, conflict_stream = _open_event_stream(port, session_id)
    conflict_status, _conflict_headers, _conflict_body = _read_raw_response(conflict_stream)
    assert b"409 Conflict" in conflict_status
    conflict_stream.close()
    conflict_socket.close()

    emitted = _post(
        connection,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "emit", "arguments": {}},
        },
        session_id=session_id,
    )
    assert emitted.status == 200
    emitted.read()
    event = b""
    deadline = time.monotonic() + 2.0
    while b"notifications/tools/list_changed" not in event and time.monotonic() < deadline:
        event += stream.readline()
    assert b"event: message" in event
    assert b"notifications/tools/list_changed" in event

    stream.close()
    stream_socket.close()
    time.sleep(0.2)
    stream_socket, stream = _open_event_stream(port, session_id)
    status, _headers, _body = _read_raw_response(stream)
    assert b"200 OK" in status
    stream.readline()
    stream.readline()

    connection.request(
        "DELETE",
        "/mcp",
        headers={
            "Authorization": "Bearer alice",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Session-Id": session_id,
        },
    )
    deleted = connection.getresponse()
    assert deleted.status == 200
    deleted.read()
    stream_socket.settimeout(2.0)
    while stream.readline() != b"":
        pass
    stream.close()
    stream_socket.close()

    missing_socket, missing_stream = _open_event_stream(port, session_id)
    status, _headers, _body = _read_raw_response(missing_stream)
    assert b"404 Not Found" in status
    missing_stream.close()
    missing_socket.close()
    assert _debug_sessions(port) == {"sessions": 0, "streams": 0}
    connection.close()


def test_cors_exposes_session_and_allows_stream_headers(streamable_server: tuple[str, int]) -> None:
    _mode, port = streamable_server
    origin = f"http://127.0.0.1:{port}"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    connection.request(
        "OPTIONS",
        "/mcp",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Mcp-Session-Id, MCP-Protocol-Version",
        },
    )
    preflight = connection.getresponse()
    assert preflight.status == 204
    preflight.read()
    assert preflight.getheader("Access-Control-Allow-Origin") == origin
    assert preflight.getheader("Access-Control-Allow-Methods") == "GET, POST, DELETE, OPTIONS"
    allowed_headers = preflight.getheader("Access-Control-Allow-Headers")
    assert "Mcp-Session-Id" in allowed_headers
    assert "Last-Event-ID" in allowed_headers

    response = _post(
        connection,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "uMCP tests", "version": "1"},
            },
        },
        origin=origin,
    )
    assert response.status == 200
    assert response.getheader("Mcp-Session-Id")
    assert response.getheader("Access-Control-Expose-Headers") == "Mcp-Session-Id"
    response.read()
    connection.close()


def test_concurrent_session_posts_are_isolated(streamable_server: tuple[str, int]) -> None:
    _mode, port = streamable_server
    bootstrap = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    session_id, _payload = _initialize(bootstrap)
    bootstrap.close()

    def list_tools(request_id: int) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
        response = _post(
            connection,
            {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"},
            session_id=session_id,
        )
        status = response.status
        response.read()
        connection.close()
        return status

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(list_tools, range(20, 36)))
    assert statuses == [200] * 16
    assert _debug_sessions(port) == {"sessions": 1, "streams": 0}


def test_idle_timeout_expiry_and_duplicate_session_headers(streamable_server: tuple[str, int]) -> None:
    _mode, port = streamable_server
    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as idle:
        idle.settimeout(1.0)
        time.sleep(0.5)
        assert idle.recv(1) == b""

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    session_id, _payload = _initialize(connection)
    connection.close()
    body = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as sock:
        stream = sock.makefile("rb")
        sock.sendall(
            (
                "POST /mcp HTTP/1.1\r\n"
                "Host: test\r\n"
                "Accept: application/json\r\n"
                "Authorization: Bearer alice\r\n"
                "Content-Type: application/json\r\n"
                f"MCP-Protocol-Version: {PROTOCOL_VERSION}\r\n"
                f"Mcp-Session-Id: {session_id}\r\n"
                f"Mcp-Session-Id: {session_id}\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode()
            + body
        )
        status, headers, _body = _read_raw_response(stream)
        assert b"400 Bad Request" in status
        assert headers[b"connection"] == b"close"

    time.sleep(3.1)
    expired_connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    expired = _post(
        expired_connection,
        {"jsonrpc": "2.0", "id": 10, "method": "tools/list"},
        session_id=session_id,
    )
    assert expired.status == 404
    expired.read()
    expired_connection.close()
    assert _debug_sessions(port) == {"sessions": 0, "streams": 0}


def test_event_stream_requires_session_owner_and_version(streamable_server: tuple[str, int]) -> None:
    _mode, port = streamable_server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    session_id, _payload = _initialize(connection)
    connection.close()

    unauthenticated = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    unauthenticated.request(
        "GET",
        "/mcp",
        headers={
            "Accept": "text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Session-Id": session_id,
        },
    )
    response = unauthenticated.getresponse()
    assert response.status == 401
    response.read()
    unauthenticated.close()

    wrong_version = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    wrong_version.request(
        "GET",
        "/mcp",
        headers={
            "Accept": "text/event-stream",
            "Authorization": "Bearer alice",
            "MCP-Protocol-Version": "2024-11-05",
            "Mcp-Session-Id": session_id,
        },
    )
    response = wrong_version.getresponse()
    assert response.status == 400
    response.read()
    wrong_version.close()
