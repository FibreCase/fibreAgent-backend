"""The tool-calling loop: run_tool_loop.

All LLM interactions are mocked (``FakeToolLLM`` plays back a scripted list of
results) — no real LLM or tool provider is ever contacted. Covers the required
behaviours: normal chat without tools, a single tool call, multiple tool calls
in one turn, and the infinite-loop iteration cap.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fibrecase_agent_backend.agent.context import ChatMessage
from fibrecase_agent_backend.agent.tool_loop import (
    ToolLoopLimitError,
    run_tool_loop,
)
from fibrecase_agent_backend.llm.client import LLMResult
from fibrecase_agent_backend.tools import build_default_tools


class FakeToolLLM:
    """Replays a list of results, one per ``complete`` call, and records calls.

    Each scripted result is an :class:`LLMResult` (``content`` and/or
    ``tool_calls``). A final ``Exception`` entry is raised instead of returned.
    """

    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, *, tools=None):
        self.calls.append(
            {"messages": [m.to_dict() for m in messages], "tools": tools}
        )
        if not self.results:
            raise AssertionError("FakeToolLLM exhausted: no more scripted results")
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _tc(name: str, args: dict[str, Any], cid: str = "call_1") -> dict[str, Any]:
    """A normalised tool-call dict, the shape the real client produces."""
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _context() -> list[ChatMessage]:
    return [ChatMessage("system", "SYS"), ChatMessage("user", "what time is it")]


# ---------------------------------------------------------------------------
# required #4 — normal chat without tools
# ---------------------------------------------------------------------------
async def test_loop_model_ignores_tools_is_single_call():
    # Tools are available, but the model answers directly (no tool_calls), so
    # the loop returns after exactly one LLM call.
    llm = FakeToolLLM([LLMResult(content="hello!")])
    reg = build_default_tools()

    result = await run_tool_loop(llm, _context(), reg, max_iterations=5)

    assert result.text == "hello!"
    assert len(llm.calls) == 1
    # Tools were advertised on the single call (a non-empty registry).
    assert len(llm.calls[0]["tools"]) == 3
    assert llm.calls[0]["messages"][0] == {"role": "system", "content": "SYS"}
    # And the model's plain answer came back without any tool turns.
    assert [m["role"] for m in llm.calls[0]["messages"]] == ["system", "user"]


async def test_loop_empty_registry_degrades_to_single_call():
    # An enabled service that has no registered tools behaves like phase one.
    from fibrecase_agent_backend.tools import ToolRegistry

    llm = FakeToolLLM([LLMResult(content="just chat")])
    result = await run_tool_loop(llm, _context(), ToolRegistry(), max_iterations=5)
    assert result.text == "just chat"
    assert len(llm.calls) == 1
    assert llm.calls[0]["tools"] is None


async def test_loop_none_registry_is_single_call():
    llm = FakeToolLLM([LLMResult(content="plain")])
    result = await run_tool_loop(llm, _context(), None, max_iterations=5)
    assert result.text == "plain"
    assert len(llm.calls) == 1


# ---------------------------------------------------------------------------
# required #5 — single tool call
# ---------------------------------------------------------------------------
async def test_single_tool_call_round_trip():
    llm = FakeToolLLM(
        [
            LLMResult(content=None, tool_calls=[_tc("get_current_time", {})]),
            LLMResult(content="It is the time."),
        ]
    )
    reg = build_default_tools()

    result = await run_tool_loop(llm, _context(), reg, max_iterations=5)

    assert result.text == "It is the time."
    assert len(llm.calls) == 2

    # First call sent the tools schema.
    assert len(llm.calls[0]["tools"]) == 3
    # Second call carried the assistant tool-call + the tool result back.
    second = llm.calls[1]["messages"]
    roles = [m["role"] for m in second]
    assert roles[-2:] == ["assistant", "tool"]

    assistant = second[-2]
    assert assistant["tool_calls"][0]["function"]["name"] == "get_current_time"
    tool = second[-1]
    assert tool["tool_call_id"] == "call_1"
    # get_current_time returns "YYYY-MM-DD HH:MM:SS".
    assert len(tool["content"]) == 19


# ---------------------------------------------------------------------------
# required #6 — multiple tool calls in one turn
# ---------------------------------------------------------------------------
async def test_multiple_tool_calls_single_turn():
    llm = FakeToolLLM(
        [
            LLMResult(
                content=None,
                tool_calls=[
                    _tc("echo", {"message": "A"}, cid="call_a"),
                    _tc("system_info", {}, cid="call_b"),
                ],
            ),
            LLMResult(content="done with both"),
        ]
    )
    reg = build_default_tools()

    result = await run_tool_loop(llm, _context(), reg, max_iterations=5)

    assert result.text == "done with both"
    assert len(llm.calls) == 2

    second = llm.calls[1]["messages"]
    # One assistant turn with two tool_calls, then two tool results (in order).
    assistant = second[-3]
    names = [tc["function"]["name"] for tc in assistant["tool_calls"]]
    assert names == ["echo", "system_info"]

    tool_a = second[-2]
    tool_b = second[-1]
    assert tool_a["tool_call_id"] == "call_a"
    assert tool_a["content"] == "A"
    assert tool_b["tool_call_id"] == "call_b"
    assert '"hostname"' in tool_b["content"]  # system_info JSON


async def test_loop_multiple_sequential_tool_rounds():
    # Two tool rounds before the final answer: A -> tool -> B -> tool -> final.
    llm = FakeToolLLM(
        [
            LLMResult(content=None, tool_calls=[_tc("echo", {"message": "1"}, cid="c1")]),
            LLMResult(content=None, tool_calls=[_tc("echo", {"message": "2"}, cid="c2")]),
            LLMResult(content="final"),
        ]
    )
    reg = build_default_tools()

    result = await run_tool_loop(llm, _context(), reg, max_iterations=5)
    assert result.text == "final"
    assert len(llm.calls) == 3


# ---------------------------------------------------------------------------
# required #7 — infinite tool loop limit
# ---------------------------------------------------------------------------
def _always_calls_time(n: int = 10) -> list[Any]:
    """A model that *always* requests the time tool, never a final answer.

    ``n`` results are provided; the loop is expected to stop at its iteration
    limit (well below ``n``), so the tail is simply never consumed.
    """
    return [
        LLMResult(content=None, tool_calls=[_tc("get_current_time", {})])
        for _ in range(n)
    ]


async def test_loop_iteration_limit_raises():
    llm = FakeToolLLM(_always_calls_time())
    reg = build_default_tools()

    with pytest.raises(ToolLoopLimitError) as excinfo:
        await run_tool_loop(llm, _context(), reg, max_iterations=5)

    assert excinfo.value.max_iterations == 5
    # Exactly 5 LLM calls were made before the loop gave up.
    assert len(llm.calls) == 5


async def test_loop_respects_small_limit():
    llm = FakeToolLLM(_always_calls_time())
    reg = build_default_tools()

    with pytest.raises(ToolLoopLimitError):
        await run_tool_loop(llm, _context(), reg, max_iterations=2)
    assert len(llm.calls) == 2


async def test_llm_error_propagates_through_loop():
    from fibrecase_agent_backend.llm.client import LLMError

    llm = FakeToolLLM([LLMResult(content=None, tool_calls=[_tc("echo", {"message": "x"})]), LLMError("timeout")])
    reg = build_default_tools()

    with pytest.raises(LLMError) as excinfo:
        await run_tool_loop(llm, _context(), reg, max_iterations=5)
    assert excinfo.value.category == "timeout"


async def test_unknown_tool_request_yields_error_result_not_crash():
    # Model asks for a tool that isn't registered; the loop feeds back an error
    # string and lets the model produce a final answer.
    llm = FakeToolLLM(
        [
            LLMResult(content=None, tool_calls=[_tc("fly", {}, cid="f1")]),
            LLMResult(content="I cannot fly."),
        ]
    )
    reg = build_default_tools()

    result = await run_tool_loop(llm, _context(), reg, max_iterations=5)
    assert result.text == "I cannot fly."
    second = llm.calls[1]["messages"]
    tool_msg = second[-1]
    assert tool_msg["role"] == "tool"
    # The unknown tool yields a stable, non-echoing error result (phase 3): a
    # JSON error with the ``unknown_tool`` code — never a raw trace/exception.
    payload = json.loads(tool_msg["content"])
    assert payload == {"error": {"code": "unknown_tool", "message": "This tool is not available."}}
