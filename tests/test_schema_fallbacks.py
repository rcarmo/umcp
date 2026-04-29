#!/usr/bin/env python3
"""
test_schema_fallbacks.py - Regression tests for schema extraction fallbacks.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aioumcp import AsyncMCPServer  # noqa: E402
from umcp import MCPServer  # noqa: E402


class SyncSchemaFallbackServer(MCPServer):
    def tool_forward_ref(self, n: 'NumberLike'):
        return n

    def tool_unannotated(self, x):
        return x

    def tool_typed(self, count: int):
        return count


class AsyncSchemaFallbackServer(AsyncMCPServer):
    async def tool_forward_ref(self, n: 'NumberLike'):
        return n

    async def tool_unannotated(self, x):
        return x

    async def tool_typed(self, count: int):
        return count


def _tool_map(server):
    return {tool['name']: tool for tool in server.discover_tools()['tools']}


def test_sync_schema_falls_back_to_signature_annotation_and_string_default():
    tools = _tool_map(SyncSchemaFallbackServer())
    assert tools['forward_ref']['inputSchema']['properties']['n']['type'] == 'string'
    assert tools['unannotated']['inputSchema']['properties']['x']['type'] == 'string'
    assert tools['typed']['inputSchema']['properties']['count']['type'] == 'integer'


def test_async_schema_falls_back_to_signature_annotation_and_string_default():
    tools = _tool_map(AsyncSchemaFallbackServer())
    assert tools['forward_ref']['inputSchema']['properties']['n']['type'] == 'string'
    assert tools['unannotated']['inputSchema']['properties']['x']['type'] == 'string'
    assert tools['typed']['inputSchema']['properties']['count']['type'] == 'integer'


if __name__ == '__main__':
    tests = [
        test_sync_schema_falls_back_to_signature_annotation_and_string_default,
        test_async_schema_falls_back_to_signature_annotation_and_string_default,
    ]
    results = []
    for test in tests:
        try:
            test()
            results.append((test.__name__, 'OK'))
        except AssertionError as exc:
            results.append((test.__name__, f'FAIL: {exc}'))
    print(json.dumps(results, indent=2))
    if any(status != 'OK' for _, status in results):
        raise SystemExit(1)
