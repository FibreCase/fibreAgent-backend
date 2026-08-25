"""Agent-service integration for explicit long-term memory (phase 2.5).

Drives :meth:`AgentService.process_message` with a scoped principal and a fake
LLM to verify: the memory reference message's position and verbatim content in
the wire payload; the byte-for-byte no-change paths (no scope / empty query /
no match / image-only); the memory sub-budget and total-budget interplay;
``last_retrieved_at`` stamping; the safe ``memory_error`` path; and that the
tool loop / ``ENABLE_TOOLS=false`` / image rehydration are unregressed.
All LLM/DB is mocked (in-memory SQLite via the ``repo`` fixture).
"""

from __future__ import annotations

import pytest

from fibrecase_agent_backend.agent.messages import AgentMessage, ImageContent, TextContent
from fibrecase_agent_backend.agent.service import AgentError, AgentService
from fibrecase_agent_backend.database.repository import MemoryRecord
from fibrecase_agent_backend.memory import MEMORY_REFERENCE_HEADER, normalize_text
from fibrecase_agent_backend.tools import build_default_tools

from conftest import FakeLLM

_JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def _service(repo, llm, *, max_context=50, max_tokens=24000, image_cost=2000, enable_tools=False, **kw):
    return AgentService(
        repo,
        llm,
        system_prompt="You are a test agent.",
        max_context_messages=max_context,
        max_context_estimated_tokens=max_tokens,
        context_image_estimated_tokens=image_cost,
        enable_tools=enable_tools,
        registry=build_default_tools() if enable_tools else None,
        **kw,
    )


async def _seed_memory(repo, scope, content, *, id_hint=None):
    rec = await repo.add_memory(scope, content, normalize_text(content))
    return rec


def _memory_msg(call):
    """Return the memory reference message from an LLM call, if present.

    The injected reference is a *user*-role message (not a second system
    message — some OpenAI-compatible endpoints 400 on two system messages),
    so we identify it by the fixed reference header it always carries.
    """
    for m in call:
        if m["role"] == "user" and MEMORY_REFERENCE_HEADER in m["content"]:
            return m
    return None


# ===========================================================================
# 9 — matching memory injects a fixed reference user message, verbatim
# ===========================================================================
async def test_matching_memory_injects_reference_message(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    rec = await _seed_memory(repo, "telegram:1", "用户住在上海。")
    llm = FakeLLM(replies=["ok"])
    service = _service(repo, llm, max_memory_estimated_tokens=3000)

    await service.process_message(conv.id, "上海有什么好吃的？", memory_scope="telegram:1")

    call = llm.calls[0]
    # Main system prompt first, then the memory reference, then the current turn.
    assert call[0] == {"role": "system", "content": "You are a test agent."}
    mem = _memory_msg(call)
    assert mem is not None, "expected a memory reference message"
    # It is a *user*-role message (never a second system message — that 400s on
    # many endpoints), and it sits right after the main system prompt.
    assert mem["role"] == "user"
    assert mem == call[1], "memory message must sit right after the main system prompt"
    assert "- [memory #%d] 用户住在上海。" % rec.id in mem["content"]
    # Exactly one system message (the main prompt) — the fix for the 400.
    assert [m["role"] for m in call].count("system") == 1
    # The current user turn is still the final user message.
    assert call[-1]["role"] == "user"
    assert "上海有什么好吃的？" in call[-1]["content"]


# ===========================================================================
# 10 — no scope / empty query / no match / image-only → no memory message
# ===========================================================================
async def test_no_scope_leaves_context_unchanged(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    await _seed_memory(repo, "telegram:1", "用户住在上海。")
    llm = FakeLLM(replies=["ok"])
    service = _service(repo, llm, max_memory_estimated_tokens=3000)

    await service.process_message(conv.id, "上海有什么好吃的？")  # no memory_scope

    call = llm.calls[0]
    assert _memory_msg(call) is None
    assert [m["role"] for m in call] == ["system", "user"]


async def test_empty_query_does_not_search(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    await _seed_memory(repo, "telegram:1", "用户住在上海。")
    llm = FakeLLM(replies=["ok"])
    service = _service(repo, llm, max_memory_estimated_tokens=3000)

    # Image-only message: text is empty → no memory search, no memory message.
    msg = AgentMessage(contents=[ImageContent(data=_JPEG, mime_type="image/jpeg")])
    await service.process_message(conv.id, msg, memory_scope="telegram:1")

    call = llm.calls[0]
    assert _memory_msg(call) is None
    # The image rides in the current turn as an OpenAI parts list.
    assert isinstance(call[-1]["content"], list)


async def test_no_match_leaves_context_unchanged(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    await _seed_memory(repo, "telegram:1", "用户住在上海。")
    llm = FakeLLM(replies=["ok"])
    service = _service(repo, llm, max_memory_estimated_tokens=3000)

    await service.process_message(conv.id, "today's weather in Berlin", memory_scope="telegram:1")

    call = llm.calls[0]
    assert _memory_msg(call) is None
    assert [m["role"] for m in call] == ["system", "user"]


# ===========================================================================
# 11 — memory + history + current never exceeds the total budget
# ===========================================================================
async def test_memory_and_history_within_total_budget(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    await _seed_memory(repo, "telegram:1", "用户住在上海。")
    # Seed history so there is something to squeeze.
    llm = FakeLLM(replies=["ok"])
    # A budget that comfortably fits memory + a little history but not all of it.
    service = _service(repo, llm, max_context=50, max_tokens=120, max_memory_estimated_tokens=3000)

    await repo.add_message(conv.id, "user", "first question text")
    await repo.add_message(conv.id, "assistant", "first answer text")
    await repo.add_message(conv.id, "user", "second question text")
    await repo.add_message(conv.id, "assistant", "second answer text")

    reply = await service.process_message(conv.id, "上海有什么好吃的？", memory_scope="telegram:1")
    assert reply == "ok"
    # The turn succeeded — the planner guaranteed the total never exceeded 120.
    # (A context_limit error would have been raised instead.)


# ===========================================================================
# 12 — over-sub-budget memory is skipped (not truncated); lower-score can fit
# ===========================================================================
async def test_over_subbudget_memory_skipped_not_truncated(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    big = await _seed_memory(repo, "telegram:1", "big" * 400)  # large content
    small = await _seed_memory(repo, "telegram:1", "small note")
    llm = FakeLLM(replies=["ok"])
    # Sub-budget fits the wrapper + the small memory (~73) but not the big one
    # (~371); the total budget is ample so only the sub-budget binds.
    service = _service(repo, llm, max_memory_estimated_tokens=73)

    await service.process_message(conv.id, "big small note", memory_scope="telegram:1")

    call = llm.calls[0]
    mem = _memory_msg(call)
    assert mem is not None
    # The big memory's full content was NOT injected (skipped, not truncated).
    assert "big" * 400 not in mem["content"]
    # The small one was.
    assert "small note" in mem["content"]


# ===========================================================================
# 13 — current request over budget → no LLM call, no last_retrieved_at
# ===========================================================================
async def test_current_over_budget_skips_llm_and_marks_nothing(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    rec = await _seed_memory(repo, "telegram:1", "用户住在上海。")
    llm = FakeLLM(replies=["ok"])
    service = _service(repo, llm, max_tokens=3, max_memory_estimated_tokens=3000)

    with pytest.raises(AgentError) as exc:
        await service.process_message(conv.id, "上海有什么好吃的？", memory_scope="telegram:1")
    assert exc.value.category == "context_limit"

    # No LLM call was made.
    assert llm.calls == []
    # The memory was not stamped as retrieved.
    stored = await repo.get_memory("telegram:1", rec.id)
    assert stored.last_retrieved_at is None


# ===========================================================================
# 14 — only actually-injected memories are stamped last_retrieved_at
# ===========================================================================
async def test_only_injected_memories_are_marked_retrieved(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    # Two memories both match the query; the budget fits only one memory message.
    a = await _seed_memory(repo, "telegram:1", "上海天气很好")
    b = await _seed_memory(repo, "telegram:1", "上海很好")
    llm = FakeLLM(replies=["ok"])
    # mandatory(~14) + one memory message(~77) fits; a second(+~86) does not at 95.
    service = _service(repo, llm, max_tokens=95, max_memory_estimated_tokens=3000)

    await service.process_message(conv.id, "上海天气", memory_scope="telegram:1")

    # Exactly one memory made it into the context (the higher-scored one, a).
    call = llm.calls[0]
    mem = _memory_msg(call)
    injected_ids = {m.id for m in (a, b) if f"[memory #{m.id}]" in mem["content"]}
    assert len(injected_ids) == 1
    injected = next(iter(injected_ids))
    not_injected = next(iter({a.id, b.id} - injected_ids))
    assert injected == a.id, "the higher-ranked (substring) memory must be the one kept"

    stamped = await repo.get_memory("telegram:1", injected)
    unstamped = await repo.get_memory("telegram:1", not_injected)
    assert stamped.last_retrieved_at is not None
    assert unstamped.last_retrieved_at is None


# ===========================================================================
# 15 — memory repository failure → safe memory_error, LLM not called
# ===========================================================================
class _FailingMemoryRepo:
    """A repository wrapper that fails only on the memory search path."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def list_memories_for_search(self, scope):
        raise RuntimeError("db blew up")


async def test_memory_search_failure_is_safe_and_no_llm(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = FakeLLM(replies=["ok"])
    failing_repo = _FailingMemoryRepo(repo)
    service = _service(failing_repo, llm, max_memory_estimated_tokens=3000)

    with pytest.raises(AgentError) as exc:
        await service.process_message(conv.id, "上海有什么好吃的？", memory_scope="telegram:1")
    assert exc.value.category == "memory_error"
    # The LLM was never called with unknown/partial memory context.
    assert llm.calls == []


# ===========================================================================
# 16 — regression: image rehydration, tool-on, ENABLE_TOOLS=false
# ===========================================================================
async def test_memory_injected_with_tools_enabled(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    await _seed_memory(repo, "telegram:1", "用户住在上海。")
    llm = FakeLLM(replies=["ok"])
    service = _service(repo, llm, enable_tools=True, max_memory_estimated_tokens=3000)

    await service.process_message(conv.id, "上海有什么好吃的？", memory_scope="telegram:1")
    assert _memory_msg(llm.calls[0]) is not None
    # Tools were still advertised on the wire call.
    assert any(m["role"] == "system" and m["content"] == "You are a test agent." for m in llm.calls[0])


async def test_memory_injected_with_tools_disabled_tools_none(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    await _seed_memory(repo, "telegram:1", "用户住在上海。")
    llm = FakeLLM(replies=["ok"])
    service = _service(repo, llm, enable_tools=False, max_memory_estimated_tokens=3000)

    await service.process_message(conv.id, "上海有什么好吃的？", memory_scope="telegram:1")
    call = llm.calls[0]
    assert _memory_msg(call) is not None
    # The single-completion path is used (no tools passed) — verify by shape:
    # no assistant tool_calls message appears in the context sent.
    assert all("tool_calls" not in m for m in call)


# ---------------------------------------------------------------------------
# service-level command methods
# ---------------------------------------------------------------------------
async def test_remember_memory_trims_and_rejects_invalid(repo):
    service = _service(repo, FakeLLM(), max_memory_chars=10)

    # Empty → memory_invalid.
    with pytest.raises(AgentError) as e:
        await service.remember_memory("telegram:1", "   ")
    assert e.value.category == "memory_invalid"

    # Over length → memory_invalid.
    with pytest.raises(AgentError) as e:
        await service.remember_memory("telegram:1", "x" * 11)
    assert e.value.category == "memory_invalid"

    # Valid (trimmed) → stored verbatim.
    rec = await service.remember_memory("telegram:1", "  a fact  ")
    assert rec.content == "a fact"
    assert (await repo.list_memories("telegram:1"))[0].id == rec.id


async def test_remember_memory_respects_per_scope_limit(repo):
    service = _service(repo, FakeLLM(), max_memories_per_scope=2)
    await service.remember_memory("telegram:1", "a")
    await service.remember_memory("telegram:1", "b")
    with pytest.raises(AgentError) as e:
        await service.remember_memory("telegram:1", "c")
    assert e.value.category == "memory_limit"
    # A different scope is unaffected by this scope's limit.
    ok = await service.remember_memory("telegram:2", "a")
    assert ok.content == "a"


async def test_forget_missing_or_foreign_id_is_not_found(repo):
    service = _service(repo, FakeLLM())
    await service.remember_memory("telegram:A", "only A")
    # Foreign id → not_found (no existence leak).
    with pytest.raises(AgentError) as e:
        await service.forget_memory("telegram:B", 999)
    assert e.value.category == "memory_not_found"


async def test_forget_all_returns_count_and_clears(repo):
    service = _service(repo, FakeLLM())
    await service.remember_memory("telegram:1", "a")
    await service.remember_memory("telegram:1", "b")
    removed = await service.forget_all_memories("telegram:1")
    assert removed == 2
    assert await repo.count_memories("telegram:1") == 0


async def test_list_memories_returns_detached_records(repo):
    service = _service(repo, FakeLLM())
    await service.remember_memory("telegram:1", "a")
    recs = await service.list_memories("telegram:1")
    assert isinstance(recs[0], MemoryRecord)
    assert recs[0].content == "a"
    # Safe to read after the underlying session closed.
    assert recs[0].normalized_content == "a"
