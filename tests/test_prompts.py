#!/usr/bin/env python3
"""
test_prompts.py - Tests for prompt discovery and retrieval (synchronous server)
"""
import json
import sys
from pathlib import Path

# Ensure parent directory (repo root) is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from umcp import MCPServer  # noqa: E402


class PromptTestServer(MCPServer):
    """Server with sample prompt templates for testing.

    Provides a couple of prompt_ methods to exercise prompts/list and prompts/get.
    """

    def prompt_code_review(self, filename: str, issues: int = 0) -> str:
        """Generate a code review prompt for a given file.
        Categories: code, review
        Args:
            filename: Name of the file being reviewed
            issues: Approximate number of known issues (for context)
        Returns a natural language instruction for a model.
        """
        return (f"Please perform a concise code review for '{filename}'. "
                f"Assume there are about {issues} known issues. List key problems and improvements.")

    def prompt_summary(self, topic: str, bullets: int = 5):
        """Return a list of messages forming a summarization conversation.
        [categories: summary, documentation]
        Generates a system and user message for multi-turn style.
        """
        return [
            {"role": "system", "content": {"type": "text", "text": "You are a precise technical summarizer."}},
            {"role": "user", "content": {"type": "text", "text": f"Summarize the topic '{topic}' in {bullets} bullet points."}}
        ]


def test_prompts_list():
    server = PromptTestServer()
    result = server.discover_prompts()
    names = {p['name'] for p in result['prompts']}
    assert 'code_review' in names
    assert 'summary' in names
    # Ensure categories parsed
    code_review = next(p for p in result['prompts'] if p['name'] == 'code_review')
    assert 'code' in code_review.get('categories', [])


def test_prompts_get_description_only():
    server = PromptTestServer()
    # Call without arguments -> description only
    resp = server.handle_prompt_get(1, {"name": "code_review"})
    assert 'result' in resp
    assert 'description' in resp['result']
    assert 'messages' not in resp['result']


def test_prompts_get_with_arguments():
    server = PromptTestServer()
    resp = server.handle_prompt_get(2, {"name": "code_review", "arguments": {"filename": "foo.py", "issues": 3}})
    assert 'messages' in resp['result']
    msg = resp['result']['messages'][0]
    assert msg['role'] == 'user'
    assert 'foo.py' in msg['content']['text']


def test_prompts_get_list_messages():
    server = PromptTestServer()
    resp = server.handle_prompt_get(3, {"name": "summary", "arguments": {"topic": "MCP", "bullets": 3}})
    msgs = resp['result']['messages']
    assert len(msgs) == 2
    roles = [m['role'] for m in msgs]
    assert roles == ['system', 'user']


if __name__ == '__main__':  # Manual run helper
    # Run tests manually without pytest
    tests = [
        test_prompts_list,
        test_prompts_get_description_only,
        test_prompts_get_with_arguments,
        test_prompts_get_list_messages,
    ]
    results = []
    for t in tests:
        try:
            t()
            results.append((t.__name__, 'OK'))
        except AssertionError as e:
            results.append((t.__name__, f'FAIL: {e}'))
    print(json.dumps(results, indent=2))
