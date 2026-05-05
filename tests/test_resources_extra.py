"""Extended resource tests covering edges the basic suite misses.

* Multi-parameter URI templates
* Resource methods that raise -> -32603
* list[dict] return -> multiple content entries
* Dynamic register_resource_template + read it back
* Custom URI schemes (file://, https://, custom://)
* Annotations carried through (audience, priority, lastModified)
* _mcp_resource override of name and uri
* Pagination cursor in list params is accepted (and ignored cleanly)
* Async dynamic register_resource works
* Async resource that raises returns -32603
"""

from __future__ import annotations

import asyncio
import json

from aioumcp import AsyncMCPServer
from umcp import MCPServer


def _send(s: MCPServer, request: dict) -> dict:
    return s.process_request(json.dumps(request))


def _send_async(s: AsyncMCPServer, request: dict) -> dict:
    return asyncio.run(s.process_request_async(json.dumps(request)))


# ---------- multi-param templates -------------------------------------------


class _MultiParam(MCPServer):
    def resource_template_org_repo(self, org: str, repo: str) -> dict:
        """Two-parameter template."""
        return {"text": f"{org}/{repo}", "mimeType": "text/plain"}


def test_multi_param_template_listed_with_both_placeholders() -> None:
    s = _MultiParam()
    resp = _send(s, {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"})
    template = resp["result"]["resourceTemplates"][0]
    assert "{org}" in template["uriTemplate"]
    assert "{repo}" in template["uriTemplate"]


def test_multi_param_template_read_binds_both_groups() -> None:
    s = _MultiParam()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://_MultiParam/org_repo/acme/widgets"},
    })
    assert resp["result"]["contents"][0]["text"] == "acme/widgets"


# ---------- raising resources -----------------------------------------------


class _Raises(MCPServer):
    def resource_broken(self) -> str:
        """Always raises."""
        raise RuntimeError("nope")

    def resource_template_broken(self, name: str) -> str:
        """Raises with the supplied name in the message."""
        raise ValueError(f"no such {name}")


def test_static_resource_raise_is_caught_as_minus_32603() -> None:
    s = _Raises()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://_Raises/broken"},
    })
    assert resp["error"]["code"] == -32603


def test_template_resource_raise_is_caught_as_minus_32603() -> None:
    s = _Raises()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://_Raises/broken/foo"},
    })
    assert resp["error"]["code"] == -32603


# ---------- list[dict] return ------------------------------------------------


class _MultiContent(MCPServer):
    def resource_pair(self) -> list:
        """Returns two content entries."""
        return [
            {"text": "first", "mimeType": "text/plain"},
            {"text": "second", "mimeType": "text/plain"},
        ]


def test_list_of_dicts_return_yields_multiple_content_entries() -> None:
    s = _MultiContent()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://_MultiContent/pair"},
    })
    contents = resp["result"]["contents"]
    assert len(contents) == 2
    assert {c["text"] for c in contents} == {"first", "second"}


# ---------- dynamic registration --------------------------------------------


def test_register_resource_template_then_read() -> None:
    class S(MCPServer):
        pass

    s = S()
    s.register_resource_template(
        "config://{section}",
        lambda section: f"section={section}",
        name="config",
        title="Config sections",
        mime_type="text/plain",
    )
    listing = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/templates/list",
    })
    templates = {t["uriTemplate"] for t in listing["result"]["resourceTemplates"]}
    assert "config://{section}" in templates

    resp = _send(s, {
        "jsonrpc": "2.0", "id": 2, "method": "resources/read",
        "params": {"uri": "config://main"},
    })
    assert resp["result"]["contents"][0]["text"] == "section=main"


# ---------- custom URI schemes ----------------------------------------------


class _CustomURIs(MCPServer):
    def resource_readme(self) -> str:
        """README under file://."""
        return "# README"
    resource_readme._mcp_resource = {
        "uri": "file:///README.md",
        "mime_type": "text/markdown",
    }

    def resource_homepage(self) -> str:
        """Web page under https://."""
        return "<html></html>"
    resource_homepage._mcp_resource = {
        "uri": "https://example.com/index.html",
        "mime_type": "text/html",
    }

    def resource_settings(self) -> str:
        """Custom scheme."""
        return "{}"
    resource_settings._mcp_resource = {
        "uri": "settings://workspace",
        "mime_type": "application/json",
    }


def test_custom_uri_schemes_round_trip() -> None:
    s = _CustomURIs()
    listing = _send(s, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    uris = {r["uri"] for r in listing["result"]["resources"]}
    assert {
        "file:///README.md",
        "https://example.com/index.html",
        "settings://workspace",
    } <= uris

    for uri in (
        "file:///README.md",
        "https://example.com/index.html",
        "settings://workspace",
    ):
        resp = _send(s, {
            "jsonrpc": "2.0", "id": 2, "method": "resources/read",
            "params": {"uri": uri},
        })
        assert "error" not in resp
        assert resp["result"]["contents"][0]["uri"] == uri


# ---------- annotations carried through -------------------------------------


class _Annotated(MCPServer):
    def resource_doc(self) -> str:
        """Doc with annotations."""
        return "x"
    resource_doc._mcp_resource = {
        "annotations": {
            "audience": ["user", "assistant"],
            "priority": 0.7,
            "lastModified": "2025-01-12T15:00:58Z",
        },
        "mime_type": "text/plain",
    }


def test_resource_annotations_are_exposed_on_list() -> None:
    s = _Annotated()
    resp = _send(s, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    doc = next(r for r in resp["result"]["resources"] if r["name"] == "doc")
    assert doc["annotations"]["audience"] == ["user", "assistant"]
    assert doc["annotations"]["priority"] == 0.7
    assert doc["annotations"]["lastModified"] == "2025-01-12T15:00:58Z"


# ---------- _mcp_resource override of name and uri -------------------------


class _Renamed(MCPServer):
    def resource_internal_name(self) -> str:
        """."""
        return "ok"
    resource_internal_name._mcp_resource = {
        "uri": "preset://overridden",
        "name": "PublicName",
        "title": "Friendly title",
    }


def test_override_attribute_replaces_default_uri_and_name() -> None:
    s = _Renamed()
    listing = _send(s, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    res = listing["result"]["resources"][0]
    assert res["uri"] == "preset://overridden"
    assert res["name"] == "PublicName"
    assert res["title"] == "Friendly title"


# ---------- pagination cursor accepted but ignored --------------------------


class _Tiny(MCPServer):
    def resource_only(self) -> str:
        """."""
        return "x"


def test_resources_list_accepts_cursor_param() -> None:
    s = _Tiny()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/list",
        "params": {"cursor": "ignored"},
    })
    # Should succeed even though we don't paginate.
    assert "error" not in resp
    assert any(r["name"] == "only" for r in resp["result"]["resources"])


def test_resources_templates_list_accepts_cursor_param() -> None:
    s = _Tiny()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/templates/list",
        "params": {"cursor": "ignored"},
    })
    assert "error" not in resp


# ---------- async dynamic registration --------------------------------------


def test_async_register_resource_then_read() -> None:
    class S(AsyncMCPServer):
        pass

    s = S()

    async def fetch() -> str:
        return "dynamic-async"

    s.register_resource("dyn://main", fetch, mime_type="text/plain")
    resp = _send_async(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "dyn://main"},
    })
    assert resp["result"]["contents"][0]["text"] == "dynamic-async"


def test_async_resource_that_raises_is_caught_as_minus_32603() -> None:
    class S(AsyncMCPServer):
        async def resource_broken(self) -> str:
            """Always raises."""
            raise RuntimeError("async-nope")

    resp = _send_async(S(), {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "umcp://S/broken"},
    })
    assert resp["error"]["code"] == -32603


# ---------- subscribe / unsubscribe edge cases ------------------------------


def test_subscribe_to_unknown_uri_is_still_accepted() -> None:
    """Spec doesn't require the URI to exist at subscribe time -- only at read time."""
    s = _Tiny()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
        "params": {"uri": "umcp://does/not/exist"},
    })
    assert resp["result"] == {}
    assert "umcp://does/not/exist" in s._resource_subscriptions


def test_unsubscribe_unknown_uri_is_a_noop() -> None:
    s = _Tiny()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/unsubscribe",
        "params": {"uri": "umcp://never/subscribed"},
    })
    assert resp["result"] == {}


def test_subscribe_without_uri_returns_invalid_params() -> None:
    s = _Tiny()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
        "params": {},
    })
    assert resp["error"]["code"] == -32602
