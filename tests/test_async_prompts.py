#!/usr/bin/env python3
"""
test_async_prompts.py - Tests for prompt discovery and retrieval (async server)
"""
import asyncio
import json
import sys
from pathlib import Path

# Ensure parent directory (repo root) is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from aioumcp import AsyncMCPServer  # noqa: E402


class AsyncPromptTestServer(AsyncMCPServer):
    """Async server with sample prompt templates for testing."""

    def prompt_brainstorm(self, topic: str, ideas: int = 3) -> str:
        """Create a brainstorming prompt.
        Categories: ideation, creative
        Provide a single user message prompt body.
        """
        return (f"Brainstorm {ideas} innovative ideas about '{topic}'. "
                "Return concise bullet points.")

    async def prompt_dialog_async(self, persona: str = "assistant"):
        """Return a list of messages (async version).
        [categories: conversation,test]
        Simulates async work while generating structured messages.
        """
        await asyncio.sleep(0.01)
        return [
            {"role": "system", "content": {"type": "text", "text": f"You are acting as {persona}."}},
            {"role": "user", "content": {"type": "text", "text": "Introduce yourself briefly."}},
        ]


def test_async_prompts_list():
    server = AsyncPromptTestServer()
    result = server.discover_prompts()
    names = {p['name'] for p in result['prompts']}
    assert 'brainstorm' in names
    assert 'dialog_async' in names
    brainstorm = next(p for p in result['prompts'] if p['name'] == 'brainstorm')
    assert 'ideation' in brainstorm.get('categories', [])


def test_async_prompt_get_sync_return():
    server = AsyncPromptTestServer()
    # Use JSON-RPC pathway to exercise process_request_async pipeline
    req = json.dumps({
        "jsonrpc": "2.0",
        "id": 10,
        "method": "prompts/get",
        "params": {"name": "brainstorm", "arguments": {"topic": "MCP", "ideas": 4}}
    })
    resp = asyncio.run(server.process_request_async(req))
    assert 'result' in resp
    msgs = resp['result']['messages']
    assert len(msgs) == 1
    assert msgs[0]['role'] == 'user'
    assert 'MCP' in msgs[0]['content']['text']


def test_async_prompt_get_async_return():
    server = AsyncPromptTestServer()
    req = json.dumps({
        "jsonrpc": "2.0",
        "id": 11,
        "method": "prompts/get",
        "params": {"name": "dialog_async", "arguments": {"persona": "tester"}}
    })
    resp = asyncio.run(server.process_request_async(req))
    msgs = resp['result']['messages']
    assert len(msgs) == 2
    assert msgs[0]['role'] == 'system'
    assert 'tester' in msgs[0]['content']['text']


def test_async_prompts_description_only():
    server = AsyncPromptTestServer()
    req = json.dumps({
        "jsonrpc": "2.0",
        "id": 12,
        "method": "prompts/get",
        "params": {"name": "brainstorm"}
    })
    resp = asyncio.run(server.process_request_async(req))
    assert 'messages' not in resp['result']  # no args => description only


if __name__ == '__main__':  # Manual run helper
    tests = [
        test_async_prompts_list,
        test_async_prompt_get_sync_return,
        test_async_prompt_get_async_return,
        test_async_prompts_description_only,
    ]
    out = []
    for t in tests:
        try:
            t()
            out.append((t.__name__, 'OK'))
        except AssertionError as e:
            out.append((t.__name__, f'FAIL: {e}'))
    print(json.dumps(out, indent=2))
