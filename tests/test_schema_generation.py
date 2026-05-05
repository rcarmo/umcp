"""Schema-generation tests covering the type-hint -> JSON Schema mapping.

For each supported type, register a tool with that signature on the sync
base and then assert what ``tools/list`` reports.  The async base
delegates to the same generator, so it's smoke-tested separately.
"""

from __future__ import annotations

from typing import Literal, TypedDict

import pytest

from aioumcp import AsyncMCPServer
from umcp import MCPServer


def _schema_for(server: MCPServer, tool_name: str) -> dict:
    tools = server.discover_tools()["tools"]
    matches = [t for t in tools if t["name"] == tool_name]
    assert matches, f"tool {tool_name!r} not discovered; got {[t['name'] for t in tools]}"
    return matches[0]["inputSchema"]


# ---------- Primitives -------------------------------------------------------


class _Primitives(MCPServer):
    def tool_str(self, value: str) -> str:
        """str param."""
        return value

    def tool_int(self, value: int) -> int:
        """int param."""
        return value

    def tool_float(self, value: float) -> float:
        """float param."""
        return value

    def tool_bool(self, value: bool) -> bool:
        """bool param."""
        return value


def test_primitive_types_map_to_jsonschema() -> None:
    s = _Primitives()
    assert _schema_for(s, "str")["properties"]["value"]["type"] == "string"
    assert _schema_for(s, "int")["properties"]["value"]["type"] == "integer"
    assert _schema_for(s, "float")["properties"]["value"]["type"] == "number"
    assert _schema_for(s, "bool")["properties"]["value"]["type"] == "boolean"


def test_required_vs_optional_by_default() -> None:
    class S(MCPServer):
        def tool_a(self, required: str, optional: str = "x") -> str:
            """."""
            return required

    schema = _schema_for(S(), "a")
    assert "required" in schema
    assert schema["required"] == ["required"]
    # The default value may or may not be carried -- both shapes are
    # acceptable as long as the parameter isn't required.
    assert "optional" not in schema["required"]


def test_additional_properties_is_false() -> None:
    class S(MCPServer):
        def tool_a(self, x: str) -> str:
            """."""
            return x

    schema = _schema_for(S(), "a")
    assert schema["additionalProperties"] is False


def test_no_args_tool_gets_empty_object_schema() -> None:
    class S(MCPServer):
        def tool_ping(self) -> str:
            """."""
            return "pong"

    schema = _schema_for(S(), "ping")
    assert schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


# ---------- Optional / Union / None -----------------------------------------


def test_optional_via_pep604_union_with_none() -> None:
    class S(MCPServer):
        def tool_a(self, value: str | None = None) -> str:
            """."""
            return value or ""

    schema = _schema_for(S(), "a")
    # ``value`` is optional; either omitted from required or marked nullable.
    assert "value" not in schema.get("required", [])


def test_union_of_two_concrete_types_maps_to_array_of_types() -> None:
    class S(MCPServer):
        def tool_a(self, value: int | str) -> str:
            """."""
            return str(value)

    schema = _schema_for(S(), "a")
    prop = schema["properties"]["value"]
    # Library may emit ``oneOf``, ``anyOf``, or ``type: [a, b]`` -- all legal.
    if "type" in prop and isinstance(prop["type"], list):
        assert set(prop["type"]) == {"integer", "string"}
    elif "oneOf" in prop:
        types = {alt.get("type") for alt in prop["oneOf"]}
        assert types == {"integer", "string"}
    elif "anyOf" in prop:
        types = {alt.get("type") for alt in prop["anyOf"]}
        assert types == {"integer", "string"}
    else:  # pragma: no cover -- debugging aid
        pytest.fail(f"unexpected union schema shape: {prop}")


# ---------- Literal -> enum --------------------------------------------------


def test_literal_becomes_enum() -> None:
    class S(MCPServer):
        def tool_a(self, mode: Literal["dry_run", "best_effort", "strict"] = "strict") -> str:
            """."""
            return mode

    schema = _schema_for(S(), "a")
    prop = schema["properties"]["mode"]
    assert sorted(prop["enum"]) == ["best_effort", "dry_run", "strict"]


# ---------- TypedDict -------------------------------------------------------


class _Patch(TypedDict):
    """A patch-like nested object."""
    op: str
    path: str
    value: str


class _PartialPatch(TypedDict, total=False):
    op: str
    path: str
    value: str


def test_typeddict_required_keys_map_to_object_schema() -> None:
    class S(MCPServer):
        def tool_apply(self, change: _Patch) -> str:
            """."""
            return change["op"]

    schema = _schema_for(S(), "apply")
    change_prop = schema["properties"]["change"]
    assert change_prop["type"] == "object"
    # All three keys are required because the TypedDict isn't total=False.
    assert sorted(change_prop["required"]) == ["op", "path", "value"]


def test_typeddict_total_false_has_no_required_keys() -> None:
    class S(MCPServer):
        def tool_apply(self, change: _PartialPatch) -> str:
            """."""
            return change.get("op", "")

    schema = _schema_for(S(), "apply")
    change_prop = schema["properties"]["change"]
    # ``required`` should be absent or empty.
    assert not change_prop.get("required")


# ---------- Containers ------------------------------------------------------


def test_list_of_strings() -> None:
    class S(MCPServer):
        def tool_a(self, items: list[str]) -> int:
            """."""
            return len(items)

    schema = _schema_for(S(), "a")
    prop = schema["properties"]["items"]
    assert prop["type"] == "array"
    assert prop["items"]["type"] == "string"


def test_dict_of_str_to_int() -> None:
    class S(MCPServer):
        def tool_a(self, mapping: dict[str, int]) -> int:
            """."""
            return sum(mapping.values())

    schema = _schema_for(S(), "a")
    prop = schema["properties"]["mapping"]
    assert prop["type"] == "object"


# ---------- Async base goes through the same generator ----------------------


def test_async_base_generates_same_shape() -> None:
    class S(AsyncMCPServer):
        async def tool_a(self, value: str) -> str:
            """."""
            return value

    schemas = {t["name"]: t["inputSchema"] for t in S().discover_tools()["tools"]}
    assert schemas["a"]["properties"]["value"]["type"] == "string"
    assert schemas["a"]["additionalProperties"] is False
