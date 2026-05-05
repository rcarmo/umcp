"""Tests for the lenient stringy-client argument coercion path."""

from __future__ import annotations

import json

from umcp import MCPServer


class _Coerce(MCPServer):
    def tool_int_op(self, n: int) -> int:
        """Return n + 1."""
        return n + 1

    def tool_float_op(self, n: float) -> float:
        """Return n + 0.5."""
        return n + 0.5

    def tool_bool_op(self, flag: bool) -> str:
        """Stringify the flag."""
        return f"flag={flag!r}"


def _call(server: MCPServer, name: str, args: dict) -> dict:
    return server.process_request(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }))


def _content_text(resp: dict) -> str:
    """Extract the text from a successful tools/call response."""
    content = resp["result"]["content"]
    return content[0]["text"]


def test_string_to_int_is_coerced() -> None:
    resp = _call(_Coerce(), "int_op", {"n": "41"})
    assert "error" not in resp
    assert _content_text(resp) == "42"


def test_string_to_float_is_coerced() -> None:
    resp = _call(_Coerce(), "float_op", {"n": "1.5"})
    assert "error" not in resp
    assert _content_text(resp).startswith("2")


def test_string_true_to_bool_is_coerced() -> None:
    resp = _call(_Coerce(), "bool_op", {"flag": "true"})
    assert "error" not in resp
    assert "True" in _content_text(resp)


def test_native_types_pass_through_unchanged() -> None:
    resp = _call(_Coerce(), "int_op", {"n": 7})
    assert _content_text(resp) == "8"
