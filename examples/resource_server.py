#!/usr/bin/env python3
"""
resource_server.py -- example umcp server demonstrating MCP resources.

Exposes:

* ``resource_motd``        -- a static text resource at ``umcp://ResourceServer/motd``.
* ``resource_readme``      -- a static markdown resource (custom URI + annotations).
* ``resource_logo``        -- a static binary resource (returns bytes -> base64 blob).
* ``resource_template_user``  -- a parameterised template resolved by ``user_id``.
* ``resource_template_file``  -- a parameterised template under ``file://`` returning an
                                  in-memory file's contents.
* ``tool_touch_motd``      -- mutates the MOTD and emits ``notifications/resources/updated``.
* ``tool_add_file``        -- adds a new in-memory file and emits the list-changed
                              notification.

Run with stdio (default), or ``--port N`` for SSE / ``--port N --tcp`` for raw TCP.
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone

from umcp import MCPServer


class ResourceServer(MCPServer):
    """Demonstration MCP server for the resources/* methods."""

    def __init__(self) -> None:
        super().__init__()
        self._motd: str = "Welcome to umcp resources."
        self._files: dict[str, str] = {
            "hello.txt": "Hello, world!\n",
            "notes.md": "# Notes\n\n* First note.\n",
        }

    def get_instructions(self) -> str:
        return (
            "Demonstration server for MCP resources. Use resources/list and "
            "resources/templates/list to discover what's available, then call "
            "resources/read with one of the URIs."
        )

    # ---- Static resources --------------------------------------------------

    def resource_motd(self) -> str:
        """Current message of the day."""
        return self._motd
    resource_motd._mcp_resource = {
        "title": "Message of the Day",
        "mime_type": "text/plain",
        "annotations": {"audience": ["user"], "priority": 0.5},
    }

    def resource_readme(self) -> str:
        """Server README in Markdown."""
        return (
            "# ResourceServer\n\n"
            "A small example exposing static and templated MCP resources.\n"
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

    def resource_logo(self) -> bytes:
        """Tiny 1x1 transparent PNG, served as a binary blob."""
        # Raw bytes of a 1x1 transparent PNG.
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

    def resource_template_user(self, user_id: str) -> dict:
        """Synthesised user profile, keyed by ``user_id``."""
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

    def resource_template_file(self, name: str) -> dict:
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
    #
    # Any tool that changes resource state is responsible for emitting the
    # appropriate notification.  ``notify_resource_updated`` only fires for
    # URIs the client has subscribed to; ``notify_resource_list_changed`` is
    # unconditional.

    def tool_touch_motd(self, message: str) -> str:
        """Set the message of the day. Notifies subscribers of the MOTD URI."""
        self._motd = message
        self.notify_resource_updated("umcp://ResourceServer/motd")
        return self._motd

    def tool_add_file(self, name: str, content: str) -> str:
        """Add or replace an in-memory file.

        Emits ``notifications/resources/list_changed`` so clients re-discover
        what's available, plus ``notifications/resources/updated`` for the
        file's URI in case anyone is subscribed.
        """
        self._files[name] = content
        self.notify_resource_list_changed()
        self.notify_resource_updated(f"file:///{name}")
        return f"Stored {name} ({len(content)} bytes)."


if __name__ == "__main__":
    ResourceServer().run()
