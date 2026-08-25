"""Pure long-term-memory logic (phase 2.5): normalization, term extraction,
deterministic ranking, and the fixed injection wrapper. No I/O, no ORM, no
Telegram — this is the lexical retrieval the service relies on.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fibrecase_agent_backend.memory import (
    MEMORY_REFERENCE_HEADER,
    MemoryCandidate,
    build_memory_reference_text,
    extract_terms,
    hash_scope,
    normalize_text,
    rank_memories,
)

_TZ = timezone.utc


def _cand(id: int, content: str, *, when: datetime | None = None) -> MemoryCandidate:
    return MemoryCandidate(
        id=id,
        content=content,
        normalized_content=normalize_text(content),
        updated_at=when or datetime(2020, 1, 1, tzinfo=_TZ),
    )


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def test_normalize_is_casefold_trim_and_collapse():
    assert normalize_text("  Hello   WORLD  ") == "hello world"
    assert normalize_text("A\tB\nC") == "a b c"
    assert normalize_text("  ") == ""
    assert normalize_text("") == ""


def test_normalize_keeps_cjk_and_emoji_deterministically():
    # CJK has no whitespace to collapse; emoji survives normalization as-is.
    assert normalize_text("你好  世界") == "你好 世界"
    assert normalize_text("a 🚀 b") == "a 🚀 b"
    # Repeated runs of whitespace collapse to a single space.
    assert normalize_text("x\n\n  y") == "x y"


def test_normalize_punctuation_only():
    # Punctuation is *not* stripped by normalization (it is ignored later by
    # term extraction, so such a query simply yields no terms).
    assert normalize_text("...!!?") == "...!!?"


# ---------------------------------------------------------------------------
# term extraction
# ---------------------------------------------------------------------------
def test_terms_ascii_words():
    assert extract_terms("hello world") == frozenset({"hello", "world"})


def test_terms_drop_short_ascii():
    # ASCII tokens shorter than 2 characters are dropped; "cd" is kept.
    assert extract_terms("a b cd") == frozenset({"cd"})


def test_terms_cjk_single_codepoints():
    assert extract_terms("你 好 世界") == frozenset({"你", "好", "世", "界"})


def test_terms_emoji_and_punctuation_produce_nothing():
    assert extract_terms("...🚀") == frozenset()


def test_terms_dedup():
    # extract_terms operates on already-normalized (casefolded) text.
    assert extract_terms("the the the") == frozenset({"the"})


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------
def test_rank_substring_beats_term_overlap():
    # A full normalized-query substring hit outranks a mere term overlap.
    full = _cand(1, "上海天气很好")          # contains the whole query "上海天气"
    partial = _cand(2, "上海 的 温度")       # overlaps one term "上海"
    ranked = rank_memories("上海天气", [partial, full], limit=5)
    assert [c.id for c in ranked] == [1, 2]


def test_rank_term_overlap_ordering():
    two_terms = _cand(1, "apple banana cherry")  # shares "apple" and "banana"
    one_term = _cand(2, "apple kiwi")            # shares only "apple"
    ranked = rank_memories("apple banana", [one_term, two_terms], limit=5)
    assert [c.id for c in ranked] == [1, 2]


def test_rank_tiebreak_by_updated_then_id():
    older = _cand(1, "the same text", when=datetime(2019, 1, 1, tzinfo=_TZ))
    newer = _cand(2, "the same text", when=datetime(2021, 1, 1, tzinfo=_TZ))
    ranked = rank_memories("same", [older, newer], limit=5)
    # Same substring + overlap -> newer updated_at wins.
    assert [c.id for c in ranked] == [2, 1]


def test_rank_tiebreak_by_id_when_same_timestamp():
    a = _cand(5, "identical", when=datetime(2020, 1, 1, tzinfo=_TZ))
    b = _cand(9, "identical", when=datetime(2020, 1, 1, tzinfo=_TZ))
    ranked = rank_memories("identical", [a, b], limit=5)
    # Same score, same time -> larger id wins.
    assert [c.id for c in ranked] == [9, 5]


def test_rank_zero_score_never_returned():
    irrelevant = _cand(1, "completely unrelated text")
    relevant = _cand(2, "the relevant one")
    ranked = rank_memories("relevant", [irrelevant, relevant], limit=5)
    assert [c.id for c in ranked] == [2]  # the zero-score row is excluded


def test_rank_empty_query_returns_nothing():
    ranked = rank_memories("", [_cand(1, "anything")], limit=5)
    assert ranked == []


def test_rank_punctuation_only_query_returns_nothing():
    ranked = rank_memories("!!! ...", [_cand(1, "anything")], limit=5)
    assert ranked == []


def test_rank_respects_limit():
    cands = [_cand(i, f"shared token{i % 7}") for i in range(10)]
    ranked = rank_memories("shared", cands, limit=3)
    assert len(ranked) <= 3


def test_rank_single_cjk_char_query():
    a = _cand(1, "我住在上海")
    b = _cand(2, "我喜欢北京")
    ranked = rank_memories("上", [b, a], limit=5)
    assert [c.id for c in ranked] == [1]


# ---------------------------------------------------------------------------
# injection wrapper
# ---------------------------------------------------------------------------
def test_reference_text_is_fixed_wrapper_plus_verbatim_bullets():
    a = _cand(12, "用户住在上海。")
    b = _cand(27, "用户偏好中文回答。")
    text = build_memory_reference_text([a, b])
    assert text.startswith(MEMORY_REFERENCE_HEADER)
    assert "- [memory #12] 用户住在上海。" in text
    assert "- [memory #27] 用户偏好中文回答。" in text
    # Content is shown verbatim (original casing/punctuation), not normalized.
    assert "用户住在上海。" in text


def test_reference_text_empty():
    assert build_memory_reference_text([]) == MEMORY_REFERENCE_HEADER


def test_hash_scope_is_stable_and_hides_raw_scope():
    assert hash_scope("telegram:123") == hash_scope("telegram:123")
    assert hash_scope("telegram:123") != hash_scope("telegram:999")
    h = hash_scope("telegram:123")
    assert len(h) == 12
    assert "telegram:123" not in h  # the raw scope (and user id) is not recoverable
