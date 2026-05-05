"""Tests for tool-annotation inference and explicit overrides."""

from __future__ import annotations

from umcp import MCPServer


def _annotations_for(server: MCPServer, tool_name: str) -> dict:
    tools = server.discover_tools()["tools"]
    return next(t for t in tools if t["name"] == tool_name).get("annotations", {})


class _PrefixServer(MCPServer):
    def tool_get_status(self) -> str:
        """."""
        return ""

    def tool_list_things(self) -> str:
        """."""
        return ""

    def tool_search_index(self) -> str:
        """."""
        return ""

    def tool_delete_widget(self) -> str:
        """."""
        return ""

    def tool_clear_cache(self) -> str:
        """."""
        return ""

    def tool_web_fetch(self) -> str:
        """."""
        return ""

    def tool_azure_call(self) -> str:
        """."""
        return ""

    def tool_compute_thing(self) -> str:
        """No special prefix; should not be read-only or destructive."""
        return ""


def test_read_prefix_marks_read_only() -> None:
    s = _PrefixServer()
    for name in ("get_status", "list_things", "search_index"):
        ann = _annotations_for(s, name)
        assert ann["readOnlyHint"] is True
        assert ann["destructiveHint"] is False


def test_destructive_prefix_marks_destructive() -> None:
    s = _PrefixServer()
    for name in ("delete_widget", "clear_cache"):
        ann = _annotations_for(s, name)
        assert ann["destructiveHint"] is True
        assert ann["readOnlyHint"] is False


def test_open_world_prefix_marks_open_world() -> None:
    s = _PrefixServer()
    assert _annotations_for(s, "web_fetch")["openWorldHint"] is True
    assert _annotations_for(s, "azure_call")["openWorldHint"] is True


def test_no_recognised_prefix_yields_neutral_annotations() -> None:
    s = _PrefixServer()
    ann = _annotations_for(s, "compute_thing")
    assert ann["readOnlyHint"] is False
    assert ann["destructiveHint"] is False
    assert "openWorldHint" not in ann


def test_explicit_mcp_annotations_override_inference() -> None:
    class S(MCPServer):
        def tool_delete_thing(self) -> str:
            """Despite the prefix, this is a no-op rehearsal -- not destructive."""
            return ""
        tool_delete_thing._mcp_annotations = {  # type: ignore[attr-defined]
            "readOnlyHint": True,
            "destructiveHint": False,
        }

    ann = _annotations_for(S(), "delete_thing")
    assert ann == {"readOnlyHint": True, "destructiveHint": False}
