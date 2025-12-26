# 📝 Prompt Templates Guide

This guide provides comprehensive documentation for implementing and using prompt templates in MicroMCP.

## Overview

Prompt templates in MicroMCP allow you to create reusable, structured prompt definitions that can be dynamically parameterized. They mirror the tool discovery system but return message scaffolds instead of tool results.

## Basic Concepts

### Naming Convention

Any method named `prompt_<name>` is automatically treated as a prompt definition.

### Introspection

- The method docstring becomes the prompt description
- The function signature is introspected for JSON Schema input definition
- Optional categories can be embedded in the docstring

### Return Types

Prompt methods can return:

- `str` - Wrapped as a single user message
- `list` - List of message dictionaries (used directly)
- `dict` - Dictionary containing a `messages` list
- Any other object - JSON-serialized into a user message

## Categories

### Syntax Options

You can declare categories in the docstring using any of these forms (case-insensitive):

```text
Category: review
Categories: code, quality
[category: explanation]
[categories: summarization, docs]
```

### Processing

- All category tokens are lower-cased
- Duplicates are removed
- Returned as a list in the prompt definition

## Implementation Examples

### Simple String Prompt

```python
def prompt_greet_user(self, name: str, formal: bool = False) -> str:
    """Generate a user greeting prompt.\nCategories: greeting, interaction"""
    if formal:
        return f"Please provide a formal greeting for {name}."
    else:
        return f"Say hello to {name} in a friendly way."
```

### Multi-Message Prompt

```python
def prompt_code_review(self, filename: str, issues: int = 0) -> list:
    """Generate a structured code review prompt.\nCategories: code, review"""
    return [
        {
            "role": "system",
            "content": {
                "type": "text",
                "text": "You are a senior software engineer conducting a code review."
            }
        },
        {
            "role": "user",
            "content": {
                "type": "text",
                "text": f"Please review '{filename}'. Assume ~{issues} pre-identified issues. Focus on security, performance, and maintainability."
            }
        }
    ]
```

### Dictionary Response

```python
def prompt_analysis(self, data: str, focus_areas: list = None) -> dict:
    """Create an analysis prompt with context.\n[categories: analysis, data]"""
    if focus_areas is None:
        focus_areas = ["trends", "anomalies", "insights"]

    return {
        "messages": [
            {
                "role": "system",
                "content": {
                    "type": "text",
                    "text": "You are a data analyst specializing in pattern recognition."
                }
            },
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": f"Analyze this data: {data}\n\nFocus on: {', '.join(focus_areas)}"
                }
            }
        ],
        "metadata": {
            "focus_areas": focus_areas,
            "data_length": len(data)
        }
    }
```

### Async Prompt

```python
async def prompt_fetch_and_summarize(self, url: str, max_length: int = 500) -> str:
    """Fetch content and create a summary prompt.\nCategories: web, summarization"""
    # Simulate fetching content
    await asyncio.sleep(0.1)
    mock_content = f"Content from {url} (truncated for example)"

    return f"Please summarize this content in under {max_length} characters: {mock_content[:max_length]}"
```

## Protocol Operations

### Listing Prompts

**Request:**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "prompts/list"
}
```

**Response:**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "prompts": [
      {
        "name": "greet_user",
        "description": "Generate a user greeting prompt.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "name": { "type": "string" },
            "formal": { "type": "boolean", "default": false }
          },
          "required": ["name"]
        },
        "categories": ["greeting", "interaction"]
      },
      {
        "name": "code_review",
        "description": "Generate a structured code review prompt.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "filename": { "type": "string" },
            "issues": { "type": "integer", "default": 0 }
          },
          "required": ["filename"]
        },
        "categories": ["code", "review"]
      }
    ]
  }
}
```

### Getting a Prompt (Description Only)

**Request:**

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "prompts/get",
  "params": { "name": "code_review" }
}
```

**Response:**

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "description": "Generate a structured code review prompt.",
    "categories": ["code", "review"]
  }
}
```

### Getting a Prompt (With Arguments)

**Request:**

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "prompts/get",
  "params": {
    "name": "code_review",
    "arguments": {
      "filename": "main.py",
      "issues": 3
    }
  }
}
```

**Response:**

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "description": "Generate a structured code review prompt.",
    "categories": ["code", "review"],
    "messages": [
      {
        "role": "system",
        "content": {
          "type": "text",
          "text": "You are a senior software engineer conducting a code review."
        }
      },
      {
        "role": "user",
        "content": {
          "type": "text",
          "text": "Please review 'main.py'. Assume ~3 pre-identified issues. Focus on security, performance, and maintainability."
        }
      }
    ]
  }
}
```

## Best Practices

### 1. Clear Descriptions

Use the first line of the docstring for a concise summary:

```python
def prompt_analyze_logs(self, log_content: str) -> str:
    """Analyze application logs for errors and patterns.\nCategories: logging, analysis"""
    # Implementation
```

### 2. Meaningful Categories

Use consistent, lowercase categories:

```python
def prompt_security_scan(self, code: str) -> str:
    """Perform security vulnerability analysis.\nCategories: security, scanning, code"""
    # Implementation
```

### 3. Type Safety

Always include type hints for parameters:

```python
def prompt_generate_report(self, data: str, format_type: str = "markdown") -> str:
    """Generate a formatted report from data.\nCategories: reporting, documentation"""
    # Implementation
```

### 4. Default Values

Provide sensible defaults for optional parameters:

```python
def prompt_summarize(self, content: str, length: int = 200, style: str = "concise") -> str:
    """Create a summary of specified length and style.\nCategories: summarization, content"""
    # Implementation
```

### 5. Error Handling

Handle edge cases gracefully:

```python
def prompt_validate_input(self, input_data: str, schema_type: str = "json") -> str:
    """Validate input data against specified schema.\nCategories: validation, data"""
    if not input_data.strip():
        return "Please provide non-empty input data for validation."
    # Continue with normal processing
```

## Testing Prompts

### Unit Testing

```python
def test_prompt_code_review():
    server = MyServer()

    # Test without arguments (description only)
    result = server.handle_prompt_get({"name": "code_review"})
    assert "description" in result
    assert "categories" in result
    assert "messages" not in result

    # Test with arguments
    result = server.handle_prompt_get({
        "name": "code_review",
        "arguments": {"filename": "test.py", "issues": 2}
    })
    assert "messages" in result
    assert len(result["messages"]) == 2
```

### Integration Testing

```bash
# Test prompt listing
echo '{"jsonrpc": "2.0", "method": "prompts/list", "id": 1}' | python ./your_server.py

# Test prompt retrieval
echo '{"jsonrpc": "2.0", "method": "prompts/get", "params": {"name": "your_prompt"}, "id": 2}' | python ./your_server.py
```

## Advanced Patterns

### Conditional Prompts

```python
def prompt_adaptive_review(self, code: str, complexity: str = "medium") -> list:
    """Adapt review prompt based on code complexity.\nCategories: code, review, adaptive"""

    system_messages = {
        "simple": "You are reviewing straightforward code.",
        "medium": "You are reviewing moderately complex code.",
        "complex": "You are reviewing highly complex, critical code."
    }

    return [
        {
            "role": "system",
            "content": {"type": "text", "text": system_messages.get(complexity, system_messages["medium"])}
        },
        {
            "role": "user",
            "content": {"type": "text", "text": f"Review this code: {code}"}
        }
    ]
```

### Template Composition

```python
def prompt_composite_analysis(self, text: str, aspects: list = None) -> dict:
    """Create a composite analysis prompt.\nCategories: analysis, composite"""

    if aspects is None:
        aspects = ["sentiment", "topics", "entities"]

    base_prompt = f"Analyze this text focusing on: {', '.join(aspects)}\n\nText: {text}"

    return {
        "messages": [
            {"role": "system", "content": {"type": "text", "text": "You are a comprehensive text analyst."}},
            {"role": "user", "content": {"type": "text", "text": base_prompt}}
        ],
        "metadata": {
            "aspects": aspects,
            "text_length": len(text)
        }
    }
```

## Migration from Tools

If you have existing tools that you want to convert to prompts:

```python
# Before (tool)
def tool_analyze_sentiment(self, text: str) -> dict:
    """Analyze sentiment of text."""
    # Perform analysis
    return {"sentiment": "positive", "confidence": 0.85}

# After (prompt)
def prompt_sentiment_analysis(self, text: str) -> str:
    """Create a sentiment analysis prompt.\nCategories: sentiment, analysis"""
    return f"Please analyze the sentiment of this text: {text}\n\nProvide sentiment (positive/negative/neutral) and confidence score."
```

## Performance Considerations

- **Description-only requests** are lightweight and don't invoke the prompt method
- **Complex prompts** with heavy computation should use async methods
- **Caching** can be implemented for expensive prompt generation
- **Parameter validation** happens before prompt method invocation

## Error Handling

Common error scenarios:

1. **Invalid prompt name** - Returns standard MCP error
2. **Missing required arguments** - Returns validation error
3. **Type conversion errors** - Returns parameter error
4. **Prompt method exceptions** - Returns execution error

All errors follow the JSON-RPC 2.0 error format with appropriate error codes.
