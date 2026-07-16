from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")

@dataclass(frozen=True, slots=True)
class MCPPrincipal:
    name: str
    roles: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MCPRequestContext:
    transport: str | None = None
    request_id: str | int | None = None
    protocol_version: str | None = None
    session_id: str | None = None
    principal: str | None = None
    peer: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


_request_context: ContextVar[MCPRequestContext] = ContextVar(
    "umcp_request_context",
    default=MCPRequestContext(),
)


def set_request_context(ctx: MCPRequestContext | None):
    return _request_context.set(ctx)


def reset_request_context(token) -> None:
    _request_context.reset(token)


def get_request_context() -> MCPRequestContext:
    return _request_context.get()


def exact_or_fallback(accepted: str | None, preferred: str) -> str:
    if accepted in SUPPORTED_PROTOCOL_VERSIONS:
        return accepted  # exact match
    return preferred


def is_jsonrpc_object(value: Any) -> bool:
    return isinstance(value, dict)


def media_accepts_json(accept: str | None) -> bool:
    """Return whether an Accept value permits an application/json response."""
    if not accept:
        return True
    for item in accept.split(","):
        parts = [part.strip().lower() for part in item.split(";")]
        media_type = parts[0]
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        if quality > 0 and media_type in ("*/*", "application/*", "application/json"):
            return True
    return False


def content_type_is_json(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def origin_is_allowed(
    origin: str | None,
    allowed_origins: list[str] | tuple[str, ...] = (),
    *,
    local_bind: bool = True,
) -> bool:
    """Validate an Origin exactly; loopback origins are implicit only locally."""
    if not origin:
        return False
    if origin in allowed_origins:
        return True
    if not local_bind:
        return False
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    return parsed.hostname.lower() in {"127.0.0.1", "localhost", "::1"}
