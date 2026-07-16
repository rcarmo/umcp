from __future__ import annotations

import asyncio
import json
from types import MappingProxyType

import pytest

from aioumcp import AsyncMCPServer
from umcp import MCPServer
from umcp_shared import (
    MCPPrincipal,
    MCPRequestContext,
    SUPPORTED_PROTOCOL_VERSIONS,
    exact_or_fallback,
    get_request_context,
    reset_request_context,
    set_request_context,
)


def test_exact_or_fallback_prefers_supported_version():
    assert exact_or_fallback("2024-11-05", "2025-03-26") == "2024-11-05"
    assert exact_or_fallback("bogus", "2025-03-26") == "2025-03-26"


def test_request_context_roundtrip():
    token = set_request_context(
        MCPRequestContext(
            transport="streamable-http",
            request_id="abc",
            protocol_version=SUPPORTED_PROTOCOL_VERSIONS[0],
            session_id="sess",
            principal="anonymous",
            peer="127.0.0.1",
            headers={"content-type": "application/json"},
        )
    )
    try:
        ctx = get_request_context()
        assert ctx.transport == "streamable-http"
        assert ctx.request_id == "abc"
        assert ctx.session_id == "sess"
        assert ctx.headers["content-type"] == "application/json"
    finally:
        reset_request_context(token)
    assert get_request_context().transport is None


def test_sync_initialize_negotiates_supported_and_falls_back():
    server = MCPServer()
    for offered, expected in (
        ("2024-11-05", "2024-11-05"),
        ("2099-01-01", SUPPORTED_PROTOCOL_VERSIONS[0]),
        (None, SUPPORTED_PROTOCOL_VERSIONS[0]),
    ):
        params = {} if offered is None else {"protocolVersion": offered}
        response = server.process_request(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params,
        }))
        assert response["result"]["protocolVersion"] == expected


def test_async_initialize_negotiation_matches_sync():
    server = AsyncMCPServer()
    response = asyncio.run(server.process_request_async(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    })))
    assert response["result"]["protocolVersion"] == "2024-11-05"


def test_request_context_headers_are_defensively_immutable():
    headers = {"x-test": "one"}
    ctx = MCPRequestContext(headers=headers)
    headers["x-test"] = "two"
    assert isinstance(ctx.headers, MappingProxyType)
    assert ctx.headers["x-test"] == "one"
    with pytest.raises(TypeError):
        ctx.headers["x-test"] = "nope"  # type: ignore[index]


def test_principal_metadata_is_defensively_immutable():
    metadata = {"scope": "read"}
    principal = MCPPrincipal(name="alice", roles=["admin"], metadata=metadata)
    metadata["scope"] = "write"
    assert principal.roles == ("admin",)
    assert isinstance(principal.metadata, MappingProxyType)
    assert principal.metadata["scope"] == "read"
    with pytest.raises(TypeError):
        principal.metadata["scope"] = "nope"  # type: ignore[index]
