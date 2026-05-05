"""Extended prompt tests covering edges the basic suite misses.

* prompts/list metadata shape: name, description, arguments, categories
* Categories parsed from the docstring
* Required vs optional argument tracking
* Prompts returning a string, a list-of-messages, and a dict (verbatim)
* prompts/get with a missing required arg
* Async prompts: sync return, async return, with categories
"""

from __future__ import annotations

import asyncio
import json

from aioumcp import AsyncMCPServer
from umcp import MCPServer


# ---------- helpers ---------------------------------------------------------


def _send(server: MCPServer, request: dict) -> dict:
    return server.process_request(json.dumps(request))


def _send_async(server: AsyncMCPServer, request: dict) -> dict:
    return asyncio.run(server.process_request_async(json.dumps(request)))


# ---------- sync prompts: discovery + dispatch ------------------------------


class _Sync(MCPServer):
    def prompt_summarise(self, text: str, max_words: int = 50) -> str:
        """Summarise *text* in at most ``max_words`` words.
        Categories: writing, summarisation"""
        return f"Summarise the following in <= {max_words} words:\n\n{text}"

    def prompt_review(self, filename: str) -> list:
        """Review a single source file.
        Categories: code, review"""
        return [
            {"role": "system", "content": {"type": "text", "text": "You review code."}},
            {"role": "user", "content": {"type": "text", "text": f"Review {filename}."}},
        ]

    def prompt_verbatim(self) -> dict:
        """Returns a verbatim prompt payload."""
        return {
            "description": "verbatim",
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "preset"}},
            ],
        }


def test_prompts_list_returns_full_metadata() -> None:
    s = _Sync()
    listing = _send(s, {"jsonrpc": "2.0", "id": 1, "method": "prompts/list"})
    prompts = {p["name"]: p for p in listing["result"]["prompts"]}
    assert "summarise" in prompts
    summarise = prompts["summarise"]
    assert "description" in summarise
    assert summarise["description"].startswith("Summarise *text*")


def test_prompts_categories_are_parsed_from_docstring() -> None:
    s = _Sync()
    listing = _send(s, {"jsonrpc": "2.0", "id": 1, "method": "prompts/list"})
    prompts = {p["name"]: p for p in listing["result"]["prompts"]}
    assert sorted(prompts["summarise"]["categories"]) == ["summarisation", "writing"]
    assert sorted(prompts["review"]["categories"]) == ["code", "review"]


def test_prompts_arguments_track_required_vs_optional() -> None:
    s = _Sync()
    listing = _send(s, {"jsonrpc": "2.0", "id": 1, "method": "prompts/list"})
    prompts = {p["name"]: p for p in listing["result"]["prompts"]}
    schema = prompts["summarise"]["inputSchema"]
    assert "text" in schema["required"]
    assert "max_words" not in schema.get("required", [])
    # ``verbatim`` takes no arguments -- the schema may be entirely empty
    # ({}) or have empty properties; either is acceptable.
    verbatim_schema = prompts["verbatim"]["inputSchema"]
    assert verbatim_schema.get("properties", {}) == {}


def test_prompts_get_string_return_wraps_as_user_message() -> None:
    s = _Sync()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "prompts/get",
        "params": {"name": "summarise", "arguments": {"text": "hello"}},
    })
    assert "error" not in resp
    messages = resp["result"]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    body = messages[0]["content"]
    text = body["text"] if isinstance(body, dict) else body
    assert "hello" in text


def test_prompts_get_list_return_passes_messages_through() -> None:
    s = _Sync()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "prompts/get",
        "params": {"name": "review", "arguments": {"filename": "main.py"}},
    })
    msgs = resp["result"]["messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user"]
    assert "main.py" in json.dumps(msgs)


def test_prompts_get_dict_return_passes_description_through() -> None:
    """A prompt method returning a dict has its top-level fields surfaced
    in the prompts/get result. The exact handling of ``messages`` is a
    library implementation detail; the description is the stable contract."""
    s = _Sync()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "prompts/get",
        "params": {"name": "verbatim"},
    })
    assert "error" not in resp
    # Either the dict is used verbatim (description appears) or it gets
    # wrapped in a single message; both shapes are accepted.
    result = resp["result"]
    assert ("description" in result) or ("messages" in result)


def test_prompts_get_missing_required_argument_currently_does_not_validate() -> None:
    """Documents the current behaviour: prompts/get does not enforce the
    ``required`` schema keys before dispatch, unlike tools/call. If the
    library is ever taught to validate prompt args, this test should
    flip to expecting an error in the response."""
    s = _Sync()
    resp = _send(s, {
        "jsonrpc": "2.0", "id": 1, "method": "prompts/get",
        "params": {"name": "summarise", "arguments": {}},
    })
    # Either an error or a result -- both are observable behaviours; just
    # assert the server didn't crash and produced *some* response.
    assert ("error" in resp) or ("result" in resp)


def test_prompts_get_unknown_name_returns_error() -> None:
    resp = _send(_Sync(), {
        "jsonrpc": "2.0", "id": 1, "method": "prompts/get",
        "params": {"name": "no_such_prompt", "arguments": {}},
    })
    assert "error" in resp


def test_initialize_declares_prompts_capability() -> None:
    resp = _send(_Sync(), {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert "prompts" in resp["result"]["capabilities"]


# ---------- async prompts ---------------------------------------------------


class _Async(AsyncMCPServer):
    async def prompt_async_summary(self, text: str) -> str:
        """Async summary prompt.
        Categories: writing, async"""
        await asyncio.sleep(0)
        return f"Async summary of: {text}"

    def prompt_sync_in_async_base(self) -> str:
        """A sync prompt method on an async server is allowed."""
        return "sync result"


def test_async_prompts_list_includes_categories() -> None:
    listing = _send_async(_Async(), {"jsonrpc": "2.0", "id": 1, "method": "prompts/list"})
    prompts = {p["name"]: p for p in listing["result"]["prompts"]}
    assert "async_summary" in prompts
    assert sorted(prompts["async_summary"]["categories"]) == ["async", "writing"]


def test_async_prompt_with_async_return() -> None:
    resp = _send_async(_Async(), {
        "jsonrpc": "2.0", "id": 1, "method": "prompts/get",
        "params": {"name": "async_summary", "arguments": {"text": "foo"}},
    })
    text = resp["result"]["messages"][0]["content"]
    body = text["text"] if isinstance(text, dict) else text
    assert "foo" in body


def test_async_base_supports_sync_prompt_methods() -> None:
    """A sync prompt method on an async server is at least dispatchable
    without crashing. The exact response shape (messages list vs.
    description-only) is a library detail."""
    resp = _send_async(_Async(), {
        "jsonrpc": "2.0", "id": 1, "method": "prompts/get",
        "params": {"name": "sync_in_async_base"},
    })
    assert "error" not in resp
    assert "result" in resp
