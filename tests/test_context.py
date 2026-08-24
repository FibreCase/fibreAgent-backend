"""Context builder behaviour."""

from __future__ import annotations

from fibrecase_agent_backend.agent.context import ChatMessage, build_context


def test_context_pins_system_prompt_to_front():
    ctx = build_context("SYS", [ChatMessage("user", "hi")], max_messages=10)
    assert ctx[0].role == "system"
    assert ctx[0].content == "SYS"


def test_context_includes_all_short_history():
    hist = [ChatMessage("user", f"m{i}") for i in range(3)]
    ctx = build_context("SYS", hist, max_messages=10)
    # system + 3 history
    assert [m.content for m in ctx] == ["SYS", "m0", "m1", "m2"]


def test_context_limits_to_most_recent_n():
    hist = [ChatMessage("user", f"m{i}") for i in range(10)]
    ctx = build_context("SYS", hist, max_messages=3)
    # system + the three most recent (m7, m8, m9), in order
    assert [m.content for m in ctx] == ["SYS", "m7", "m8", "m9"]


def test_context_preserves_chronological_order():
    hist = [ChatMessage("user", "a"), ChatMessage("assistant", "b"), ChatMessage("user", "c")]
    ctx = build_context("SYS", hist, max_messages=5)
    assert [m.content for m in ctx] == ["SYS", "a", "b", "c"]


def test_context_with_no_history():
    ctx = build_context("SYS", [], max_messages=10)
    assert [m.to_dict() for m in ctx] == [{"role": "system", "content": "SYS"}]


def test_messages_are_openai_shaped():
    ctx = build_context("SYS", [ChatMessage("user", "hi")], max_messages=5)
    assert ctx[0].to_dict() == {"role": "system", "content": "SYS"}
    assert ctx[1].to_dict() == {"role": "user", "content": "hi"}
