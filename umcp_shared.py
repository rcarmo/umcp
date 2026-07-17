from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib.parse import urlparse, urlsplit

SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")
SINGLETON_HTTP_HEADERS = frozenset({
    "host",
    "authorization",
    "origin",
    "accept",
    "content-type",
    "mcp-protocol-version",
    "content-length",
    "transfer-encoding",
})

@dataclass(frozen=True, slots=True)
class MCPPrincipal:
    name: str
    roles: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class MCPRequestContext:
    transport: str | None = None
    request_id: str | int | None = None
    progress_token: str | int | None = None
    protocol_version: str | None = None
    session_id: str | None = None
    principal: str | None = None
    peer: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(slots=True)
class MCPCancellationState:
    cancelled: bool = False
    _lock: Lock = field(default_factory=Lock, repr=False)

    def mark_cancelled(self) -> None:
        with self._lock:
            self.cancelled = True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self.cancelled


@dataclass(frozen=True, slots=True)
class MCPRequestRuntime:
    progress_token: str | int | None = None
    cancellation: MCPCancellationState | None = None
    progress_callback: Callable[[float | int, float | int | None, str | None], Any] | None = None


class MCPRequestCancelled(RuntimeError):
    pass


_request_context: ContextVar[MCPRequestContext] = ContextVar(
    "umcp_request_context",
    default=MCPRequestContext(),
)

_request_runtime: ContextVar[MCPRequestRuntime | None] = ContextVar(
    "umcp_request_runtime",
    default=None,
)


def set_request_context(ctx: MCPRequestContext):
    return _request_context.set(ctx)


def reset_request_context(token) -> None:
    _request_context.reset(token)


def get_request_context() -> MCPRequestContext:
    return _request_context.get()


def set_request_runtime(runtime: MCPRequestRuntime | None):
    return _request_runtime.set(runtime)


def reset_request_runtime(token) -> None:
    _request_runtime.reset(token)


def get_request_runtime() -> MCPRequestRuntime | None:
    return _request_runtime.get()


def get_progress_token() -> str | int | None:
    runtime = get_request_runtime()
    if runtime is not None:
        return runtime.progress_token
    return get_request_context().progress_token


def is_request_cancelled() -> bool:
    runtime = get_request_runtime()
    return bool(runtime and runtime.cancellation and runtime.cancellation.is_cancelled())


def raise_if_cancelled() -> None:
    if is_request_cancelled():
        raise MCPRequestCancelled("Request cancelled")


def exact_or_fallback(accepted: str | None, preferred: str) -> str:
    if accepted in SUPPORTED_PROTOCOL_VERSIONS:
        return accepted  # exact match
    return preferred


def is_jsonrpc_object(value: Any) -> bool:
    return isinstance(value, dict)


def is_valid_jsonrpc_id(value: Any) -> bool:
    """Accept interoperable JSON-RPC IDs: strings, integers, or null."""
    return value is None or isinstance(value, (str, int)) and not isinstance(value, bool)


def is_valid_jsonrpc_response(value: Mapping[str, Any]) -> bool:
    if "id" not in value or not is_valid_jsonrpc_id(value.get("id")):
        return False
    if ("result" in value) == ("error" in value):
        return False
    if "error" not in value:
        return True
    error = value["error"]
    return (
        isinstance(error, Mapping)
        and isinstance(error.get("code"), int)
        and not isinstance(error.get("code"), bool)
        and isinstance(error.get("message"), str)
    )


def media_accepts(accept: str | None, *media_types: str) -> bool:
    """Return whether an Accept value permits any of *media_types*."""
    if not accept:
        return True
    normalized = {item.lower() for item in media_types}
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
        if quality <= 0:
            continue
        if media_type == "*/*":
            return True
        major, _, minor = media_type.partition("/")
        if minor == "*" and any(candidate.startswith(f"{major}/") for candidate in normalized):
            return True
        if media_type in normalized:
            return True
    return False


def media_accepts_json(accept: str | None) -> bool:
    """Return whether an Accept value permits an application/json response."""
    return media_accepts(accept, "application/json")


def media_accepts_event_stream(accept: str | None) -> bool:
    """Return whether an Accept value permits a text/event-stream response."""
    return media_accepts(accept, "text/event-stream")


def content_type_is_json(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def split_request_target(target: str):
    return urlsplit(target)


def request_target_path(target: str) -> str:
    return split_request_target(target).path or "/"


def has_ambiguous_singleton_values(headers: Mapping[str, str]) -> bool:
    """Reject comma-joined values for headers that cannot be safely combined."""
    comma_forbidden = {
        "host", "authorization", "origin", "mcp-protocol-version", "content-length",
    }
    return any("," in headers.get(name, "") for name in comma_forbidden)


def has_singleton_header_violations(
    header_counts: Mapping[str, int],
    *,
    http_version: str,
) -> bool:
    for name in SINGLETON_HTTP_HEADERS:
        count = header_counts.get(name, 0)
        if name == "host":
            if http_version == "HTTP/1.1":
                if count != 1:
                    return True
            elif count > 1:
                return True
        elif count > 1:
            return True
    return False


def origin_is_allowed(
    origin: str | None,
    allowed_origins: list[str] | tuple[str, ...] = (),
    *,
    local_bind: bool = True,
) -> bool:
    """Validate an Origin exactly; loopback origins are implicit only locally."""
    if not origin:
        return False
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        return False
    try:
        parsed.port
    except ValueError:
        return False
    if origin in allowed_origins:
        return True
    if allowed_origins or not local_bind:
        return False
    return parsed.hostname.lower() in {"127.0.0.1", "localhost", "::1"}
