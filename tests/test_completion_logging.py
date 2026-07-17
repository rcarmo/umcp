from __future__ import annotations

import asyncio
import json
from enum import Enum
from typing import Literal
from io import BytesIO
from queue import Queue

import pytest

import aioumcp
import umcp
from aioumcp import AsyncMCPServer
from umcp import MCPServer


class Color(Enum):
    RED = "red"
    BLUE = "blue"


class _SyncCompletionLoggingServer(MCPServer):
    def prompt_choose_literal(self, value: Literal["alpha", "beta", "gamma"]) -> str:
        return value

    def prompt_choose_enum(self, color: Color) -> str:
        return color.value

    def resource_template_palette(self, shade: Color, region: str = "global") -> str:
        return f"{shade.value}:{region}"


class _AsyncCompletionLoggingServer(AsyncMCPServer):
    async def prompt_choose_literal(self, value: Literal["alpha", "beta", "gamma"]) -> str:
        return value

    async def prompt_choose_enum(self, color: Color) -> str:
        return color.value

    async def resource_template_palette(self, shade: Color, region: str = "global") -> str:
        return f"{shade.value}:{region}"


class _AsyncWriter:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.payloads.append(data)

    async def drain(self) -> None:
        return None


@pytest.fixture
def captured_stdout(monkeypatch):
    buf = BytesIO()
    monkeypatch.setattr(umcp, "_stdout_bin", buf)
    monkeypatch.setattr(aioumcp, "_stdout_bin", buf)
    return buf


def _parse_stdio_notifications(buf: BytesIO) -> list[dict]:
    return [
        json.loads(line)
        for line in buf.getvalue().decode("utf-8").splitlines()
        if line.strip()
    ]


def _send_sync(server: MCPServer, request: dict) -> dict:
    return server.process_request(json.dumps(request))


def _send_async(server: AsyncMCPServer, request: dict) -> dict:
    return asyncio.run(server.process_request_async(json.dumps(request)))


def _completion_request(ref: dict, argument_name: str, value: str = "", **extra: object) -> dict:
    params = {
        "ref": ref,
        "argument": {"name": argument_name, "value": value},
    }
    params.update(extra)
    return {"jsonrpc": "2.0", "id": 1, "method": "completion/complete", "params": params}


def test_sync_initialize_only_advertises_logging_without_completion_support() -> None:
    response = _send_sync(MCPServer(), {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert response["result"]["capabilities"] == {
        "tools": {"listChanged": True},
        "prompts": {"get": True, "listChanged": True},
        "resources": {"subscribe": True, "listChanged": True},
        "logging": {},
    }


def test_sync_initialize_advertises_completions_exactly_when_available() -> None:
    response = _send_sync(_SyncCompletionLoggingServer(), {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert response["result"]["capabilities"] == {
        "tools": {"listChanged": True},
        "prompts": {"get": True, "listChanged": True},
        "resources": {"subscribe": True, "listChanged": True},
        "logging": {},
        "completions": {},
    }


def test_async_initialize_advertises_completions_exactly_when_available() -> None:
    response = _send_async(_AsyncCompletionLoggingServer(), {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert response["result"]["capabilities"] == {
        "tools": {"listChanged": True},
        "prompts": {"get": True, "listChanged": True},
        "resources": {"subscribe": True, "listChanged": True},
        "logging": {},
        "completions": {},
    }


def test_sync_completion_supports_prompt_and_resource_refs_literal_and_enum() -> None:
    server = _SyncCompletionLoggingServer()

    literal = _send_sync(server, _completion_request({"type": "ref/prompt", "name": "choose_literal"}, "value", "b"))
    assert literal["result"]["completion"] == {"values": ["beta"], "hasMore": False}

    enum_prompt = _send_sync(server, _completion_request({"type": "ref/prompt", "name": "choose_enum"}, "color"))
    assert enum_prompt["result"]["completion"] == {"values": ["red", "blue"], "hasMore": False}

    enum_resource = _send_sync(server, _completion_request({"type": "ref/resource", "uriTemplate": "umcp://_SyncCompletionLoggingServer/palette/{shade}/{region}"}, "shade", "b"))
    assert enum_resource["result"]["completion"] == {"values": ["blue"], "hasMore": False}


@pytest.mark.parametrize("async_mode", [False, True])
def test_completion_supports_registered_sync_and_async_providers_and_context(async_mode: bool) -> None:
    calls: list[dict] = []

    if async_mode:
        server = _AsyncCompletionLoggingServer()

        async def provider(**kwargs):
            calls.append(kwargs)
            await asyncio.sleep(0)
            return {"values": ["beta", "bonus", "beta"], "total": 7}

        request = _completion_request(
            {"type": "ref/prompt", "name": "choose_literal"},
            "value",
            "b",
            context={"arguments": {"topic": "letters"}},
        )
        server.register_completion_provider("ref/prompt", "choose_literal", "value", provider)
        response = _send_async(server, request)
    else:
        server = _SyncCompletionLoggingServer()

        def provider(**kwargs):
            calls.append(kwargs)
            return {"values": ["beta", "bonus", "beta"], "total": 7}

        request = _completion_request(
            {"type": "ref/prompt", "name": "choose_literal"},
            "value",
            "b",
            context={"arguments": {"topic": "letters"}},
        )
        server.register_completion_provider("ref/prompt", "choose_literal", "value", provider)
        response = _send_sync(server, request)

    assert response["result"]["completion"] == {"values": ["beta", "bonus"], "hasMore": True, "total": 7}
    assert calls == [{
        "prefix": "b",
        "arguments": {"topic": "letters"},
        "ref": {"type": "ref/prompt", "name": "choose_literal"},
        "argument": {"name": "value", "value": "b"},
    }]


@pytest.mark.parametrize("async_mode", [False, True])
def test_completion_supports_schema_enums_for_registered_prompts(async_mode: bool) -> None:
    def prompt(mode: str) -> str:
        return mode

    input_schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["fast", "safe", "full"]},
        },
        "required": ["mode"],
        "additionalProperties": False,
    }

    if async_mode:
        server = _AsyncCompletionLoggingServer()
        server.register_prompt("schema_modes", prompt, input_schema=input_schema)
        response = _send_async(server, _completion_request({"type": "ref/prompt", "name": "schema_modes"}, "mode", "s"))
    else:
        server = _SyncCompletionLoggingServer()
        server.register_prompt("schema_modes", prompt, input_schema=input_schema)
        response = _send_sync(server, _completion_request({"type": "ref/prompt", "name": "schema_modes"}, "mode", "s"))

    assert response["result"]["completion"] == {"values": ["safe"], "hasMore": False}


@pytest.mark.parametrize("async_mode", [False, True])
def test_completion_limits_to_max_100_and_sets_total_and_has_more(async_mode: bool) -> None:
    values = [f"item-{i:03d}" for i in range(150)]

    def plain_prompt(value: str) -> str:
        return value

    if async_mode:
        server = _AsyncCompletionLoggingServer()

        async def provider(**_: object):
            return {"values": values, "total": 150}

        sender = _send_async
    else:
        server = _SyncCompletionLoggingServer()

        def provider(**_: object):
            return {"values": values, "total": 150}

        sender = _send_sync

    server.register_prompt("bulk_values", plain_prompt)
    server.register_completion_provider("ref/prompt", "bulk_values", "value", provider)
    response = sender(server, _completion_request({"type": "ref/prompt", "name": "bulk_values"}, "value", "", maxValues=999))
    completion = response["result"]["completion"]
    assert len(completion["values"]) == 100
    assert completion["values"][0] == "item-000"
    assert completion["values"][-1] == "item-099"
    assert completion["total"] == 150
    assert completion["hasMore"] is True


@pytest.mark.parametrize("async_mode", [False, True])
def test_completion_invalid_refs_args_and_outputs_are_remote_safe(async_mode: bool) -> None:
    if async_mode:
        server = _AsyncCompletionLoggingServer()

        async def exploding(**_: object):
            raise RuntimeError("super secret stack detail")

        sender = _send_async
    else:
        server = _SyncCompletionLoggingServer()

        def exploding(**_: object):
            raise RuntimeError("super secret stack detail")

        sender = _send_sync

    server.register_completion_provider("ref/prompt", "choose_literal", "value", exploding)

    bad_ref = sender(server, _completion_request({"type": "ref/prompt", "name": "missing"}, "value"))
    assert bad_ref["error"]["code"] == -32602
    assert bad_ref["error"]["message"] == "Unknown prompt ref: missing"

    bad_arg = sender(server, _completion_request({"type": "ref/prompt", "name": "choose_literal"}, "missing"))
    assert bad_arg["error"]["code"] == -32602
    assert bad_arg["error"]["message"] == "Unknown completion argument: missing"

    boom = sender(server, _completion_request({"type": "ref/prompt", "name": "choose_literal"}, "value"))
    assert boom["error"]["code"] == -32603
    assert "super secret" not in boom["error"]["message"].lower()


@pytest.mark.parametrize("async_mode", [False, True])
def test_logging_set_level_accepts_standard_levels_and_threshold_ordering(async_mode: bool) -> None:
    levels = ["debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"]
    server = _AsyncCompletionLoggingServer() if async_mode else _SyncCompletionLoggingServer()

    for level in levels:
        response = _send_async(server, {"jsonrpc": "2.0", "id": 1, "method": "logging/setLevel", "params": {"level": level}}) if async_mode else _send_sync(server, {"jsonrpc": "2.0", "id": 1, "method": "logging/setLevel", "params": {"level": level}})
        assert response["result"] == {}
        assert server.logging_level == level

    invalid = _send_async(server, {"jsonrpc": "2.0", "id": 2, "method": "logging/setLevel", "params": {"level": "verbose"}}) if async_mode else _send_sync(server, {"jsonrpc": "2.0", "id": 2, "method": "logging/setLevel", "params": {"level": "verbose"}})
    assert invalid["error"]["code"] == -32602
    assert server.logging_level == "emergency"

    server.logging_level = "warning"
    assert server._should_emit_log_message("warning") is True
    assert server._should_emit_log_message("error") is True
    assert server._should_emit_log_message("notice") is False
    assert server._should_emit_log_message("debug") is False


def test_sync_logging_stdio_payload_redacts_sensitive_keys_recursively(captured_stdout) -> None:
    server = _SyncCompletionLoggingServer()
    server.notify_log_message("info", {
        "token": "abc",
        "nested": [{"authorization": "Bearer xyz", "note": "ok"}],
        "tuple": ({"password": "hidden"}, "api_key=secret"),
        "public": "Bearer visible",
    })
    notifications = _parse_stdio_notifications(captured_stdout)
    assert notifications == [{
        "jsonrpc": "2.0",
        "method": "notifications/message",
        "params": {
            "level": "info",
            "data": {
                "token": "[redacted]",
                "nested": [{"authorization": "[redacted]", "note": "ok"}],
                "tuple": [{"password": "[redacted]"}, "[redacted]"],
                "public": "[redacted]",
            },
        },
    }]


def test_sync_logging_stdio_payload_preserves_data_when_sanitize_false(captured_stdout) -> None:
    server = _SyncCompletionLoggingServer()
    server.notify_log_message("info", {"token": "abc", "nested": {"authorization": "Bearer xyz"}}, sanitize=False)
    notifications = _parse_stdio_notifications(captured_stdout)
    assert notifications == [{
        "jsonrpc": "2.0",
        "method": "notifications/message",
        "params": {
            "level": "info",
            "data": {"token": "abc", "nested": {"authorization": "Bearer xyz"}},
        },
    }]


def test_sync_logging_sse_payload_exact_and_logger_optional() -> None:
    server = _SyncCompletionLoggingServer()
    q: Queue = Queue()
    server._sse_sessions = {"s1": q}

    server.notify_log_message("notice", {"ok": True})
    server.notify_log_message("error", {"ok": False}, logger="runtime")

    first = q.get_nowait().decode("utf-8")
    second = q.get_nowait().decode("utf-8")
    assert first == 'event: message\ndata: {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "notice", "data": {"ok": true}}}\n\n'
    assert second == 'event: message\ndata: {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "error", "data": {"ok": false}, "logger": "runtime"}}\n\n'


def test_async_logging_stdio_and_sse_payloads(captured_stdout) -> None:
    server = _AsyncCompletionLoggingServer()

    async def run() -> tuple[str, str]:
        writer = _AsyncWriter()
        server._sse_sessions = {"s1": (writer, asyncio.Event(), asyncio.Lock(), "anonymous")}
        await server.notify_log_message("info", {"session": "abc", "message": "Bearer xyz"})
        await server.notify_log_message("warning", {"ok": True}, logger="runtime", sanitize=False)
        return writer.payloads[0].decode("utf-8"), writer.payloads[1].decode("utf-8")

    first, second = asyncio.run(run())
    stdio_notifications = _parse_stdio_notifications(captured_stdout)
    assert stdio_notifications == []
    assert first == 'event: message\ndata: {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": {"session": "[redacted]", "message": "[redacted]"}}}\n\n'
    assert second == 'event: message\ndata: {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "warning", "data": {"ok": true}, "logger": "runtime"}}\n\n'
