"""OpenAI-compatible LLM client: request shape and error translation.

The OpenAI SDK's network call is mocked — no real HTTP happens.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from fibrecase_agent_backend.llm.client import ChatMessage, LLMError, OpenAIClient


def _make_client() -> OpenAIClient:
    return OpenAIClient(base_url="https://example.test/v1", api_key="k", model="gpt-test", timeout=5)


def _response(content: str):
    choice = object.__new__(type("Choice", (), {}))
    message = object.__new__(type("Message", (), {}))
    message.content = content
    choice.message = message
    resp = object.__new__(type("Resp", (), {}))
    resp.choices = [choice]
    resp.usage = None
    return resp


def _tool_response(tool_calls: list):
    """A response whose assistant message has tool_calls and no text content."""
    choice = object.__new__(type("Choice", (), {}))
    message = object.__new__(type("Message", (), {}))
    message.content = None
    message.tool_calls = tool_calls
    choice.message = message
    resp = object.__new__(type("Resp", (), {}))
    resp.choices = [choice]
    resp.usage = None
    return resp


async def test_complete_builds_openai_request():
    client = _make_client()
    create = AsyncMock(return_value=_response("hi there"))
    with patch.object(client._client.chat.completions, "create", new=create):
        result = await client.complete(
            [ChatMessage("system", "sys"), ChatMessage("user", "hello")]
        )
    assert result.text == "hi there"
    kwargs = create.call_args.kwargs
    assert kwargs["model"] == "gpt-test"
    assert kwargs["stream"] is False
    assert kwargs["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]


async def test_complete_timeout_translated():
    client = _make_client()
    create = AsyncMock(side_effect=APITimeoutError(request=httpx2.Request("POST", "http://x")))
    with patch.object(client._client.chat.completions, "create", new=create):
        with pytest.raises(LLMError) as excinfo:
            await client.complete([ChatMessage("user", "hello")])
    assert excinfo.value.category == "timeout"


async def test_complete_http_error_translated():
    client = _make_client()
    response = httpx2.Response(503, request=httpx2.Request("POST", "http://x"))
    create = AsyncMock(side_effect=APIStatusError("upstream", response=response, body=None))
    with patch.object(client._client.chat.completions, "create", new=create):
        with pytest.raises(LLMError) as excinfo:
            await client.complete([ChatMessage("user", "hello")])
    assert excinfo.value.category == "http_error"


async def test_complete_connection_error_translated():
    client = _make_client()
    create = AsyncMock(
        side_effect=APIConnectionError(request=httpx2.Request("POST", "http://x"))
    )
    with patch.object(client._client.chat.completions, "create", new=create):
        with pytest.raises(LLMError) as excinfo:
            await client.complete([ChatMessage("user", "hello")])
    assert excinfo.value.category == "connection"


async def test_complete_empty_response_translated():
    client = _make_client()
    create = AsyncMock(return_value=_response(None))
    with patch.object(client._client.chat.completions, "create", new=create):
        with pytest.raises(LLMError) as excinfo:
            await client.complete([ChatMessage("user", "hello")])
    assert excinfo.value.category == "empty_response"


async def test_stream_not_implemented_yet():
    client = _make_client()
    with pytest.raises(NotImplementedError):
        await client.complete([ChatMessage("user", "hello")], stream=True)


# ---------------------------------------------------------------------------
# tool calling (phase 2.1)
# ---------------------------------------------------------------------------
_TOOLS = [
    {
        "type": "function",
        "function": {"name": "echo", "description": "d", "parameters": {"type": "object"}},
    }
]


def _tool_call_obj(id, name, args):
    """An SDK-style ChatCompletionMessageToolCall-shaped object."""
    fn = object.__new__(type("Fn", (), {}))
    fn.name = name
    fn.arguments = args
    tc = object.__new__(type("TC", (), {}))
    tc.id = id
    tc.type = "function"
    tc.function = fn
    return tc


async def test_complete_passes_tools_through():
    client = _make_client()
    create = AsyncMock(return_value=_response("hi"))
    with patch.object(client._client.chat.completions, "create", new=create):
        await client.complete([ChatMessage("user", "hi")], tools=_TOOLS)
    assert create.call_args.kwargs["tools"] == _TOOLS


async def test_complete_omits_tools_when_not_given():
    client = _make_client()
    create = AsyncMock(return_value=_response("hi"))
    with patch.object(client._client.chat.completions, "create", new=create):
        await client.complete([ChatMessage("user", "hi")])
    assert "tools" not in create.call_args.kwargs


async def test_complete_normalises_tool_calls_in_result():
    client = _make_client()
    create = AsyncMock(return_value=_tool_response([_tool_call_obj("call_1", "echo", '{"message":"x"}')]))
    with patch.object(client._client.chat.completions, "create", new=create):
        result = await client.complete([ChatMessage("user", "hi")], tools=_TOOLS)

    assert result.tool_calls is not None
    tc = result.tool_calls[0]
    assert tc["id"] == "call_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "echo"
    assert tc["function"]["arguments"] == '{"message":"x"}'
    # A tool-call turn has no text content; it is NOT an empty response.
    assert result.text == ""


async def test_tool_call_turn_with_blank_content_is_not_empty_response():
    # Regression: content None/blank but tool_calls present -> a valid turn.
    client = _make_client()
    create = AsyncMock(return_value=_tool_response([_tool_call_obj("c", "echo", "")]))
    with patch.object(client._client.chat.completions, "create", new=create):
        result = await client.complete([ChatMessage("user", "hi")], tools=_TOOLS)
    assert result.tool_calls and result.tool_calls[0]["function"]["name"] == "echo"


async def test_plain_empty_response_still_raises():
    # No content AND no tool_calls -> still empty_response (phase-one behaviour).
    client = _make_client()
    create = AsyncMock(return_value=_response(None))
    with patch.object(client._client.chat.completions, "create", new=create):
        with pytest.raises(LLMError) as excinfo:
            await client.complete([ChatMessage("user", "hi")])
    assert excinfo.value.category == "empty_response"
