"""Database / repository behaviour (in-memory SQLite)."""

from __future__ import annotations

import pytest

from fibrecase_agent_backend.database.session import create_engine, create_session_factory, init_db
from fibrecase_agent_backend.database.repository import ConversationRepository
from fibrecase_agent_backend.database.models import (
    SCHEDULE_CHAT_ID_BASE,
    SCHEDULE_CHAT_ID_MAX,
    schedule_chat_id,
)


async def test_database_initialisation():
    # init_db on a file-backed engine creates the parent dir and tables.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "nested" / "agent.db"
        engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
        await init_db(engine)
        try:
            assert db_path.exists(), "SQLite file should be created on init"
        finally:
            await engine.dispose()


async def test_create_conversation(repo: ConversationRepository):
    conv = await repo.get_or_create_conversation(telegram_chat_id=111, telegram_user_id=1)
    assert conv.id is not None
    assert conv.telegram_chat_id == 111
    assert conv.telegram_user_id == 1

    # get_or_create must be idempotent per chat id.
    again = await repo.get_or_create_conversation(telegram_chat_id=111, telegram_user_id=1)
    assert again.id == conv.id


async def test_create_conversation_distinct_chats(repo: ConversationRepository):
    a = await repo.get_or_create_conversation(telegram_chat_id=1, telegram_user_id=1)
    b = await repo.get_or_create_conversation(telegram_chat_id=2, telegram_user_id=1)
    assert a.id != b.id


async def test_save_message(repo: ConversationRepository):
    conv = await repo.get_or_create_conversation(telegram_chat_id=1, telegram_user_id=1)
    msg = await repo.add_message(conv.id, "user", "hello")
    assert msg.id is not None
    assert msg.role == "user"
    assert msg.content == "hello"
    assert await repo.count_messages(conv.id) == 1


async def test_load_conversation_history(repo: ConversationRepository):
    conv = await repo.get_or_create_conversation(telegram_chat_id=1, telegram_user_id=1)
    await repo.add_message(conv.id, "user", "m1")
    await repo.add_message(conv.id, "assistant", "m2")
    await repo.add_message(conv.id, "user", "m3")

    records = await repo.get_messages(conv.id)
    assert [(r.role, r.content) for r in records] == [
        ("user", "m1"),
        ("assistant", "m2"),
        ("user", "m3"),
    ]


async def test_reset_conversation(repo: ConversationRepository):
    conv = await repo.get_or_create_conversation(telegram_chat_id=1, telegram_user_id=1)
    await repo.add_message(conv.id, "user", "secret")

    fresh = await repo.reset_conversation(telegram_chat_id=1, telegram_user_id=1)
    assert fresh.id != conv.id  # a brand-new conversation
    assert await repo.count_messages(fresh.id) == 0

    # Old conversation is gone: it is no longer returned for this chat id.
    current = await repo.get_conversation(telegram_chat_id=1)
    assert current.id == fresh.id
    assert await repo.count_messages(current.id) == 0


async def test_reset_does_not_affect_other_chats(repo: ConversationRepository):
    a = await repo.get_or_create_conversation(telegram_chat_id=1, telegram_user_id=1)
    b = await repo.get_or_create_conversation(telegram_chat_id=2, telegram_user_id=1)
    await repo.add_message(a.id, "user", "in chat 1")
    await repo.add_message(b.id, "user", "in chat 2")

    await repo.reset_conversation(telegram_chat_id=1, telegram_user_id=1)

    # Chat 2 is untouched.
    b_current = await repo.get_conversation(telegram_chat_id=2)
    assert await repo.count_messages(b_current.id) == 1
    records = await repo.get_messages(b_current.id)
    assert records[0].content == "in chat 2"


# ===========================================================================
# Phase 9 (Automation) — the scheduled-run venue: delete + startup sweep +
# reset self-heal. The reserved range is
# ``SCHEDULE_CHAT_ID_BASE < telegram_chat_id < SCHEDULE_CHAT_ID_MAX``; only ids
# in that range are ever treated as ephemeral (scheduled-run) venues, so a real
# interactive conversation can never be swept.
# ===========================================================================
def _synthetic_chat_id(name: str) -> int:
    return schedule_chat_id(name)


async def test_delete_conversation_removes_row_and_messages(repo: ConversationRepository):
    conv = await repo.get_or_create_conversation(
        telegram_chat_id=_synthetic_chat_id("t1"), telegram_user_id=1
    )
    await repo.add_message(conv.id, "user", "body")
    assert await repo.count_messages(conv.id) == 1

    assert await repo.delete_conversation(conv.id) is True
    # The row is gone for that telegram_chat_id, and the messages are gone too.
    assert await repo.get_conversation(_synthetic_chat_id("t1")) is None
    assert await repo.count_messages(conv.id) == 0


async def test_delete_conversation_missing_id_is_noop(repo: ConversationRepository):
    # A missing / already-gone id is a no-op returning False (the caller treats
    # both the "deleted" and "already gone" outcomes as success).
    assert await repo.delete_conversation(999_999) is False


async def test_clear_ephemeral_only_reserved_range(repo: ConversationRepository):
    # A real interactive chat, a synthetic venue (name-derived, in range), and an
    # out-of-range id that merely *looks* synthetic but is just below the range.
    real = await repo.get_or_create_conversation(telegram_chat_id=1, telegram_user_id=1)
    await repo.add_message(real.id, "user", "real body")
    in_range = await repo.get_or_create_conversation(
        telegram_chat_id=_synthetic_chat_id("sweep"), telegram_user_id=1
    )
    await repo.add_message(in_range.id, "assistant", "synthetic body")
    # Just below the base: a positive int a real (if huge) chat could in theory
    # use, and which must be treated as *not* ephemeral.
    just_below = await repo.get_or_create_conversation(
        telegram_chat_id=SCHEDULE_CHAT_ID_BASE - 1, telegram_user_id=1
    )
    await repo.add_message(just_below.id, "user", "below body")

    cleared = await repo.clear_ephemeral_conversations()
    assert cleared == 1  # only the reserved-range venue was swept

    # The in-range venue is gone; the real and below-range chats survive.
    assert await repo.get_conversation(_synthetic_chat_id("sweep")) is None
    assert await repo.get_conversation(1) is not None
    assert await repo.get_conversation(SCHEDULE_CHAT_ID_BASE - 1) is not None
    assert await repo.count_messages(real.id) == 1


async def test_clear_ephemeral_noop_when_nothing(repo: ConversationRepository):
    await repo.get_or_create_conversation(telegram_chat_id=1, telegram_user_id=1)
    assert await repo.clear_ephemeral_conversations() == 0


async def test_reset_self_heals_leftover_synthetic_row(repo: ConversationRepository):
    # A run whose process was killed (or whose venue delete failed) leaves a row
    # with a body. The *next* run's reset_conversation(self_heal) must start from
    # an empty conversation for the same name-derived id.
    chat_id = _synthetic_chat_id("nightly")
    leftover = await repo.get_or_create_conversation(telegram_chat_id=chat_id, telegram_user_id=7)
    await repo.add_message(leftover.id, "assistant", "stale body from a killed run")
    assert await repo.count_messages(leftover.id) == 1

    fresh = await repo.reset_conversation(chat_id, 7)
    assert fresh.id != leftover.id  # a brand-new conversation
    assert await repo.count_messages(fresh.id) == 0  # empty at process time
    # The same name still resolves to the same telegram_chat_id (self-heal is
    # by the reserved id, so a repeated run keeps its venue stable).
    assert (await repo.get_conversation(chat_id)).id == fresh.id
