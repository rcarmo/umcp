from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from time import monotonic

from aioumcp import AsyncMCPServer
from umcp import MCPServer
from umcp_shared import MCPHTTPResponse, MCPPrincipal


class SyncServer(MCPServer):
    def __init__(self) -> None:
        super().__init__()
        self.streamable_http_keepalive_seconds = 0.05
        self.streamable_http_request_timeout_seconds = 0.3
        self.streamable_http_session_ttl_seconds = 3.0

    def authenticate_request(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        peer: str | None,
    ) -> MCPPrincipal | None:
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return None
        return MCPPrincipal(name=authorization.removeprefix("Bearer "))

    def handle_http_request(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        peer: str | None,
    ) -> MCPHTTPResponse | None:
        if method == "GET" and path == "/debug/sessions":
            with self._streamable_http_lock:
                self._expire_streamable_http_sessions_locked(monotonic())
                payload = {
                    "sessions": len(self._streamable_http_sessions),
                    "streams": sum(
                        session.stream_open
                        for session in self._streamable_http_sessions.values()
                    ),
                }
            return MCPHTTPResponse(
                200,
                body=json.dumps(payload).encode(),
                content_type="application/json",
            )
        return None

    def tool_emit(self) -> str:
        self.notify_tool_list_changed()
        return "emitted"


class AsyncServer(AsyncMCPServer):
    def __init__(self) -> None:
        super().__init__()
        self.streamable_http_keepalive_seconds = 0.05
        self.streamable_http_request_timeout_seconds = 0.3
        self.streamable_http_session_ttl_seconds = 3.0

    async def authenticate_request(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        peer: str | None,
    ) -> MCPPrincipal | None:
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return None
        return MCPPrincipal(name=authorization.removeprefix("Bearer "))

    async def handle_http_request_async(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        peer: str | None,
    ) -> MCPHTTPResponse | None:
        if method == "GET" and path == "/debug/sessions":
            self._expire_streamable_http_sessions(monotonic())
            payload = {
                "sessions": len(self._streamable_http_sessions),
                "streams": sum(
                    session.writer is not None
                    for session in self._streamable_http_sessions.values()
                ),
            }
            return MCPHTTPResponse(
                200,
                body=json.dumps(payload).encode(),
                content_type="application/json",
            )
        return None

    async def tool_emit(self) -> str:
        await self.notify_tool_list_changed()
        return "emitted"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("sync", "async"))
    parser.add_argument("port", type=int)
    args = parser.parse_args()
    if args.mode == "sync":
        SyncServer().run_streamable_http(port=args.port)
    else:
        asyncio.run(AsyncServer().run_streamable_http_async(port=args.port))


if __name__ == "__main__":
    main()
