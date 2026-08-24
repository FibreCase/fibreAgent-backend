"""Database / repository behaviour (in-memory SQLite)."""

from __future__ import annotations

import pytest

from fibrecase_agent_backend.database.session import create_engine, create_session_factory, init_db
from fibrecase_agent_backend.database.repository import ConversationRepository


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
