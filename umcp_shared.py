from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Mapping

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
