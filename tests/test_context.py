"""Context builder + phase-2.4 budget planner behaviour."""

from __future__ import annotations

from fibrecase_agent_backend.agent.context import (
    ChatMessage,
    TurnCandidate,
    build_context,
    estimate_parts_cost,
    estimate_text_cost,
    group_turns,
    message_cost,
    plan_context,
)


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


# ---------------------------------------------------------------------------
# Phase 2.4 — estimator + planner (pure, deterministic, model-agnostic)
# ---------------------------------------------------------------------------
def _turns(n: int, start_id: int = 1, text: str = "q", a_text: str = "a") -> list[TurnCandidate]:
    """n complete user/assistant turns, oldest first, sequential message ids."""
    out: list[TurnCandidate] = []
    mid = start_id
    for _ in range(n):
        out.append(TurnCandidate(role="user", text=text, message_id=mid)); mid += 1
        out.append(TurnCandidate(role="assistant", text=a_text, message_id=mid)); mid += 1
    return out


def _selected(plan):
    return [m.message_id for m in plan.selected]


# 1 — estimator values are stable and explicit
def test_estimator_values_are_stable():
    assert estimate_text_cost("") == 0
    assert estimate_text_cost(None) == 0  # type: ignore[arg-type]
    # ASCII runs: ceil(n/4), 1 unit per 4 chars.
    assert estimate_text_cost("abcd") == 1
    assert estimate_text_cost("abcdefgh") == 2
    assert estimate_text_cost("a") == 1
    assert estimate_text_cost("abcde") == 2
    # CJK codepoints: 1 unit each.
    assert estimate_text_cost("你好") == 2
    assert estimate_text_cost("你好吗") == 3
    # Other Unicode (emoji): 1 unit each, and flushes any ASCII run.
    assert estimate_text_cost("😀") == 1
    assert estimate_text_cost("hi😀") == estimate_text_cost("hi") + 1
    # Mixed: the ASCII run flushes when the CJK char arrives.
    assert estimate_text_cost("hello你好") == estimate_text_cost("hello") + 2
    # Per-message envelope is added on top of the parts cost.
    assert message_cost("", 2000) == 4
    assert message_cost("hi", 2000) == 4 + estimate_text_cost("hi")
    assert estimate_parts_cost("hi", 2000) == estimate_text_cost("hi")
    assert estimate_parts_cost([{"type": "text", "text": "hi"}, {"type": "image_url"}], 2000) == (
        estimate_text_cost("hi") + 2000
    )


# 2 — system always first + in budget; unchanged behaviour when budget is ample
def test_system_first_and_ample_budget_unchanged():
    hist = [
        ChatMessage("user", "a"),
        ChatMessage("assistant", "b"),
        ChatMessage("user", "c"),
        ChatMessage("assistant", "d"),
    ]
    planned = plan_context(
        "SYS",
        TurnCandidate("user", "now", 99),
        [TurnCandidate(role=m.role, text=m.content, message_id=i) for i, m in enumerate(hist, 1)],
        max_messages=50,
        max_estimated_tokens=1_000_000,
        image_cost=2000,
    )
    assert planned.status == "ok"
    # Every history message kept, chronological (oldest first).
    assert [m.message_id for m in planned.selected] == [1, 2, 3, 4]
    # The system prompt is counted in the estimate and is strictly first when the
    # caller assembles the wire messages (the plan is history-only).
    assert planned.system_cost == estimate_text_cost("SYS")
    # Legacy builder is byte-for-byte unchanged for the same input.
    ctx = build_context("SYS", hist, max_messages=50)
    assert ctx[0].to_dict() == {"role": "system", "content": "SYS"}
    assert [m.to_dict() for m in ctx[1:]] == [m.to_dict() for m in hist]
    # estimate is system + current + selected (a stable, verifiable total).
    assert planned.estimated_cost == planned.system_cost + planned.current_cost + sum(
        message_cost(m.text, 2000) for m in planned.selected
    )


# 3 — message cap only: newest complete turns, never split a normal pair
def test_message_cap_picks_complete_newest_turns():
    hist = _turns(5)  # ids 1..10, 5 turns
    plan = plan_context(
        "SYS",
        TurnCandidate("user", "now", 11),
        hist,
        max_messages=4,  # current(1) + 3 slots -> the single newest turn (2 msgs)
        max_estimated_tokens=1_000_000,
        image_cost=2000,
    )
    assert plan.status == "ok"
    # Only the newest complete user/assistant pair; it is not split.
    assert _selected(plan) == [9, 10]
    roles = [m.role for m in plan.selected]
    assert roles == ["user", "assistant"]


# 4 — token budget only: keep the newest *continuous* tail of turns
def test_token_budget_keeps_newest_continuous_tail():
    hist = _turns(5)  # ids 1..10
    plan = plan_context(
        "SYS",
        TurnCandidate("user", "now", 11),
        hist,
        max_messages=50,
        max_estimated_tokens=140,  # room for ~3 turns of history, not all 5
        image_cost=2000,
    )
    assert plan.status == "ok"
    sel = _selected(plan)
    # A contiguous run of the newest ids (no gaps — it never skips a newer turn).
    assert sel == list(range(sel[0], sel[0] + len(sel)))
    assert sel[-1] == 10  # ends at the newest
    # system + current + selected stays within the budget.
    assert plan.estimated_cost <= 140


# 5 — a turn whose full (image) form won't fit but text-only will -> downgraded
def test_downgrade_full_image_turn_to_text_only():
    # Newest turn carries one image; a tighter budget can't hold full+image but
    # can hold its two text messages.
    newest_user = TurnCandidate(
        role="user", text="pic", message_id=9,
        attachments=(("digest123", "image/png", None, 0),), image_count=1,
    )
    newest_asst = TurnCandidate(role="assistant", text="seen", message_id=10)
    older = _turns(1)  # one plain older turn, ids 1..2
    plan = plan_context(
        "SYS",
        TurnCandidate("user", "now", 11),
        [*older, newest_user, newest_asst],
        max_messages=50,
        max_estimated_tokens=100,  # far below full(image) cost, above text-only
        image_cost=2000,
    )
    assert plan.status == "ok"
    by_id = {m.message_id: m for m in plan.selected}
    # Both text messages of the image turn are kept…
    assert 9 in by_id and 10 in by_id
    # …but marked to skip the image (no blob read, no image sent).
    assert by_id[9].keep_images is False
    assert by_id[9].attachments == (("digest123", "image/png", None, 0),)


# 6 — text-only also doesn't fit -> stop, no skipping to older turns
def test_stop_when_text_only_also_overflow():
    # Two long assistant messages make the text-only cost of the newest turn large.
    long_asst = TurnCandidate(role="assistant", text="x" * 400, message_id=10)
    newest_user = TurnCandidate(role="user", text="q", message_id=9)
    older = _turns(1)  # ids 1..2
    plan = plan_context(
        "SYS",
        TurnCandidate("user", "now", 11),
        [*older, newest_user, long_asst],
        max_messages=50,
        max_estimated_tokens=60,  # even text-only of the newest turn overflows
        image_cost=2000,
    )
    assert plan.status == "ok"
    # Stops entirely: nothing older is pulled in, and it is a contiguous tail.
    sel = _selected(plan)
    assert 1 not in sel and 2 not in sel
    assert sel == list(range(sel[0], sel[0] + len(sel))) if sel else True


# 7 — current request (with image) over budget -> stable current_over_budget
def test_current_request_over_budget_is_stable():
    cur = TurnCandidate("user", "now", 11, image_count=2)  # two images = 4000 units
    plan = plan_context(
        "SYS",
        cur,
        _turns(3),
        max_messages=50,
        max_estimated_tokens=200,  # below system + current's estimate
        image_cost=2000,
    )
    assert plan.status == "current_over_budget"
    assert plan.selected == ()  # no partial current message is emitted
    # A text-only current request under a huge image cost is also flagged.
    plan2 = plan_context(
        "SYS",
        TurnCandidate("user", "y" * 1000, 11),
        _turns(1),
        max_messages=50,
        max_estimated_tokens=50,
        image_cost=2000,
    )
    assert plan2.status == "current_over_budget"
    assert plan2.selected == ()


# 8 — anomalous history rows group deterministically and never raise
def test_anomalous_rows_group_safely():
    # leading assistant, consecutive assistants, unanswered trailing user.
    rows = [
        TurnCandidate("assistant", "a0", 1),
        TurnCandidate("user", "u1", 2),
        TurnCandidate("assistant", "a1", 3),
        TurnCandidate("assistant", "a1b", 4),
        TurnCandidate("user", "u2", 5),
    ]
    turns = group_turns(rows)
    # Deterministic grouping: [a0], [u1,a1,a1b], [u2].
    assert [len(t) for t in turns] == [1, 3, 1]
    # Selection over this history is deterministic and crash-free.
    plan = plan_context(
        "SYS",
        TurnCandidate("user", "now", 6),
        rows,
        max_messages=4,
        max_estimated_tokens=1_000_000,
        image_cost=2000,
    )
    assert plan.status == "ok"
    sel = _selected(plan)
    # cap=4 leaves 3 slots after the current turn: the trailing unanswered-user
    # turn [5] fits, but the 3-message turn [2,3,4] is one too big — so it is
    # dropped whole (never split) and nothing older is pulled in.
    assert sel == [5]
    # And a generous cap keeps all three grouped turns, unsplit and in order.
    plan_all = plan_context(
        "SYS",
        TurnCandidate("user", "now", 6),
        rows,
        max_messages=20,
        max_estimated_tokens=1_000_000,
        image_cost=2000,
    )
    assert _selected(plan_all) == [1, 2, 3, 4, 5]
