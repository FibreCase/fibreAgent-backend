"""Agent service: process_message, error mapping, and per-conversation locking."""

from __future__ import annotations

import json

import pytest

from fibrecase_agent_backend.agent.service import AgentError, AgentService
from fibrecase_agent_backend.llm.client import LLMError, LLMResult
from fibrecase_agent_backend.tools import build_default_tools

from conftest import FakeLLM, RecordingLLM


def _service(repo, llm, max_context=50, **kwargs):
    return AgentService(
        repo,
        llm,
        system_prompt="You are a test agent.",
        max_context_messages=max_context,
        **kwargs,
    )


def _tc(name, args, cid="call_1"):
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class ScriptedToolLLM:
    """Returns a scripted list of results, one per complete() call."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        return self.results.pop(0)


class AlwaysCallsToolLLM:
    """Always requests a tool — never produces a final text answer."""

    def __init__(self, name="echo", args=None):
        self._tc = _tc(name, args or {"message": "x"})
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        return LLMResult(content=None, tool_calls=[self._tc])


async def test_process_message_saves_and_returns(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    service = _service(repo, FakeLLM(replies=["Alice."]))

    reply = await service.process_message(conv.id, "My name is Alice.")
    assert reply == "Alice."

    records = await repo.get_messages(conv.id)
    assert [(r.role, r.content) for r in records] == [
        ("user", "My name is Alice."),
        ("assistant", "Alice."),
    ]


async def test_process_message_rebuilds_context_across_turns(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = FakeLLM(replies=["ok"])
    service = _service(repo, llm)

    await service.process_message(conv.id, "My name is Alice.")
    await service.process_message(conv.id, "What is my name?")

    # The second call must include the prior turns as context, with the
    # system prompt pinned first.
    second_call = llm.calls[1]
    assert second_call[0] == {"role": "system", "content": "You are a test agent."}
    sent_text = [m["content"] for m in second_call]
    assert "My name is Alice." in sent_text
    assert "What is my name?" in sent_text


async def test_process_message_empty_input_short_circuits(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = FakeLLM(replies=["should not be called"])
    service = _service(repo, llm)

    reply = await service.process_message(conv.id, "   ")
    assert reply == ""
    assert llm.calls == []  # no LLM call
    assert await repo.count_messages(conv.id) == 0  # nothing persisted


async def test_process_message_maps_timeout_to_user_safe(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = FakeLLM(raise_error=LLMError("timeout"))
    service = _service(repo, llm)

    with pytest.raises(AgentError) as excinfo:
        await service.process_message(conv.id, "hello")
    assert excinfo.value.category == "timeout"
    assert "超时" in excinfo.value.user_safe  # user-safe, no internal detail


@pytest.mark.parametrize(
    "category,expected_fragment",
    [
        ("timeout", "超时"),
        ("http_error", "不可用"),
        ("connection", "重试"),
        ("empty_response", "不可用"),
        ("error", "不可用"),
    ],
)
async def test_process_message_error_categories_are_user_safe(repo, category, expected_fragment):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = FakeLLM(raise_error=LLMError(category))
    service = _service(repo, llm)
    with pytest.raises(AgentError) as excinfo:
        await service.process_message(conv.id, "hello")
    assert expected_fragment in excinfo.value.user_safe
    # The message the user sees must not leak stack traces or keys.
    assert "Traceback" not in excinfo.value.user_safe
    assert "Bearer" not in excinfo.value.user_safe


async def test_reset_starts_fresh_conversation(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    service = _service(repo, FakeLLM(replies=["ok"]))
    await service.process_message(conv.id, "I am Bob")

    new_id = await service.reset(1, 1)
    assert new_id != conv.id
    assert await repo.count_messages(new_id) == 0


async def test_same_conversation_is_serialised(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = RecordingLLM(delay=0.05, replies=["ok"])
    service = _service(repo, llm)

    # Two messages to the SAME conversation: must never overlap in the LLM.
    await service.process_message(conv.id, "A")
    await service.process_message(conv.id, "B")

    # Because they run sequentially, at most one completion is ever in flight.
    assert llm.max_active == 1
    # Both user messages were persisted, in order, with a reply after each.
    records = await repo.get_messages(conv.id)
    roles = [r.role for r in records]
    assert roles == ["user", "assistant", "user", "assistant"]


async def test_different_conversations_run_concurrently(repo):
    a = await repo.get_or_create_conversation(1, 1)
    b = await repo.get_or_create_conversation(2, 1)
    llm = RecordingLLM(delay=0.05, replies=["ok"])
    service = _service(repo, llm)

    import asyncio

    # Same-conversation pairs (to keep the per-conversation lock exercised)
    # issued to two *different* conversations overlap in the LLM.
    await asyncio.gather(
        service.process_message(a.id, "A1"),
        service.process_message(b.id, "B1"),
        service.process_message(a.id, "A2"),
        service.process_message(b.id, "B2"),
    )

    # At least two completions ran at once (different conversations).
    assert llm.max_active >= 2
    # But each conversation was internally serialised: for a given chat, the
    # user/assistant alternation holds.
    a_records = await repo.get_messages(a.id)
    assert [r.role for r in a_records] == ["user", "assistant", "user", "assistant"]
    b_records = await repo.get_messages(b.id)
    assert [r.role for r in b_records] == ["user", "assistant", "user", "assistant"]


# ---------------------------------------------------------------------------
# tool-calling integration (phase 2.1)
# ---------------------------------------------------------------------------
async def test_enabled_tools_persist_only_user_and_final_assistant(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = ScriptedToolLLM([
        LLMResult(content=None, tool_calls=[_tc("get_current_time", {})]),
        LLMResult(content="It's 12:00."),
    ])
    service = _service(
        repo, llm,
        registry=build_default_tools(), enable_tools=True, max_tool_iterations=5,
    )

    reply = await service.process_message(conv.id, "what time is it?")
    assert reply == "It's 12:00."

    # Only the user turn and the final assistant turn are stored — the
    # intermediate tool-call / tool-result turns are NOT persisted.
    records = await repo.get_messages(conv.id)
    assert [(r.role, r.content) for r in records] == [
        ("user", "what time is it?"),
        ("assistant", "It's 12:00."),
    ]


async def test_enabled_tools_limit_raises_user_safe_error(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = AlwaysCallsToolLLM()
    service = _service(
        repo, llm,
        registry=build_default_tools(), enable_tools=True, max_tool_iterations=5,
    )

    with pytest.raises(AgentError) as excinfo:
        await service.process_message(conv.id, "keep calling")

    assert excinfo.value.category == "tool_limit"
    assert "Traceback" not in excinfo.value.user_safe
    assert "Bearer" not in excinfo.value.user_safe
    # The loop made exactly max_tool_iterations LLM calls before giving up.
    assert llm.calls == 5
    # On failure no assistant message is persisted.
    assert [r.role for r in await repo.get_messages(conv.id)] == ["user"]


async def test_tools_disabled_stays_single_call(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = FakeLLM(replies=["plain reply"])
    # enable_tools=False (the default): even with a registry present, no tools.
    service = _service(repo, llm, registry=build_default_tools(), enable_tools=False)

    reply = await service.process_message(conv.id, "hi")
    assert reply == "plain reply"
    assert len(llm.calls) == 1
    assert [r.role for r in await repo.get_messages(conv.id)] == ["user", "assistant"]
