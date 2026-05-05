#!/usr/bin/env python3
"""
async_resource_server.py -- async version of resource_server.py.

Same surface as the synchronous example, but the resource methods are
``async def`` and the notification helpers are awaited.  Useful as a
template for resource handlers that fetch from network services.
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import asyncio
from datetime import datetime, timezone

from aioumcp import AsyncMCPServer


class AsyncResourceServer(AsyncMCPServer):
    """Async MCP server demonstrating resources/* with async resource methods."""

    def __init__(self) -> None:
        super().__init__()
        self._motd: str = "Welcome to umcp resources (async)."
        self._files: dict[str, str] = {
            "hello.txt": "Hello, world!\n",
            "notes.md": "# Notes\n\n* First note.\n",
        }

    def get_instructions(self) -> str:
        return (
            "Async demonstration server for MCP resources. Use resources/list "
            "and resources/templates/list to discover what's available."
        )

    # ---- Static resources --------------------------------------------------

    async def resource_motd(self) -> str:
        """Current message of the day (async fetch)."""
        await asyncio.sleep(0)  # placeholder for a real network call
        return self._motd
    resource_motd._mcp_resource = {
        "title": "Message of the Day",
        "mime_type": "text/plain",
        "annotations": {"audience": ["user"], "priority": 0.5},
    }

    async def resource_readme(self) -> str:
        """Server README in Markdown (async)."""
        return (
            "# AsyncResourceServer\n\n"
            "A small example exposing async MCP resources.\n"
        )
    resource_readme._mcp_resource = {
        "uri": "file:///README.md",
        "title": "README",
        "mime_type": "text/markdown",
        "annotations": {
            "audience": ["user", "assistant"],
            "priority": 0.8,
            "lastModified": datetime.now(timezone.utc).isoformat(),
        },
    }

    async def resource_logo(self) -> bytes:
        """Tiny 1x1 transparent PNG, served as a binary blob."""
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?"
            b"\x00\x05\xfe\x02\xfe\xa3\x9b\x14\xa1\x00\x00\x00\x00IEND\xaeB`\x82"
        )
    resource_logo._mcp_resource = {
        "title": "Logo (PNG)",
        "mime_type": "image/png",
    }

    # ---- Resource templates ------------------------------------------------

    async def resource_template_user(self, user_id: str) -> dict:
        """Synthesised user profile, keyed by ``user_id``."""
        await asyncio.sleep(0)
        return {
            "mimeType": "application/json",
            "text": (
                "{"
                f"\"id\": \"{user_id}\", "
                f"\"name\": \"User {user_id}\", "
                "\"role\": \"member\""
                "}"
            ),
        }

    async def resource_template_file(self, name: str) -> dict:
        """One of the in-memory files exposed under ``file://``."""
        if name not in self._files:
            raise FileNotFoundError(f"No such file: {name}")
        return {
            "mimeType": "text/plain",
            "text": self._files[name],
        }
    resource_template_file._mcp_resource_template = {
        "uri_template": "file:///{name}",
        "title": "In-memory files",
        "description": "Read one of the in-memory files exposed by the server.",
        "mime_type": "text/plain",
    }

    # ---- Tools that mutate resources --------------------------------------

    async def tool_touch_motd(self, message: str) -> str:
        """Set the MOTD; notifies subscribers."""
        self._motd = message
        await self.notify_resource_updated("umcp://AsyncResourceServer/motd")
        return self._motd

    async def tool_add_file(self, name: str, content: str) -> str:
        """Add or replace an in-memory file; notifies list-changed plus URI."""
        self._files[name] = content
        await self.notify_resource_list_changed()
        await self.notify_resource_updated(f"file:///{name}")
        return f"Stored {name} ({len(content)} bytes)."


if __name__ == "__main__":
    AsyncResourceServer().run()
