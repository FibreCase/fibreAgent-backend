"""Repository-level tests for explicit long-term memory (phase 2.5).

Covers schema creation (fresh DB + a simulated v1.5.0 upgrade), the scope + id
isolation guarantee, timestamps, detached records, and ``/new`` not touching
memories. SQLite is in-memory / temp-file only — no real data/ directory.
"""

from __future__ import annotations

import tempfile

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fibrecase_agent_backend.database.models import Base, Attachment, Conversation, Message
from fibrecase_agent_backend.database.repository import ConversationRepository
from fibrecase_agent_backend.database.session import create_session_factory, init_db


async def _table_names(engine):
    from sqlalchemy import inspect

    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    return names


# ---------------------------------------------------------------------------
# schema: fresh DB and a simulated v1.5.0 upgrade both create `memories`
# ---------------------------------------------------------------------------
async def test_fresh_db_and_v15_upgrade_create_memories_table():
    # Fresh DB: create_all builds every table, including memories.
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp}/fresh.db")
        await init_db(engine)
        names = await _table_names(engine)
        assert {"conversations", "messages", "attachments", "memories"} <= names
        await engine.dispose()

    # Simulated v1.5.0 DB: only the pre-memory tables exist, with live data.
    with tempfile.TemporaryDirectory() as tmp:
        db = f"{tmp}/v15.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c, tables=[Conversation.__table__, Message.__table__, Attachment.__table__]
                )
            )
        r = ConversationRepository(create_session_factory(engine))
        conv = await r.get_or_create_conversation(1, 1)
        await r.add_message(conv.id, "user", "pre-existing message")

        # init_db adds the *missing* table only — no data loss, no manual wipe.
        await init_db(engine)
        names = await _table_names(engine)
        assert "memories" in names

        # Old data survives the upgrade.
        msgs = await r.get_messages(conv.id)
        assert [m.content for m in msgs] == ["pre-existing message"]
        # And the new table is immediately usable.
        rec = await r.add_memory("telegram:1", "a fact", "a fact")
        assert (await r.count_memories("telegram:1")) == 1
        await engine.dispose()


# ---------------------------------------------------------------------------
# add / list / delete / clear + timestamps + length guard + detached records
# ---------------------------------------------------------------------------
async def test_add_list_delete_clear_and_timestamps(repo):
    # The repository stores exactly what it is handed (verbatim); trimming and
    # length limits are the *service's* job, not the repository's.
    rec = await repo.add_memory("telegram:1", "I live in Shanghai.", "i live in shanghai")
    assert rec.id > 0
    assert rec.content == "I live in Shanghai."
    assert rec.normalized_content == "i live in shanghai"
    assert rec.created_at is not None
    assert rec.updated_at is not None
    assert rec.last_retrieved_at is None

    listed = await repo.list_memories("telegram:1")
    assert [m.id for m in listed] == [rec.id]

    deleted = await repo.delete_memory("telegram:1", rec.id)
    assert deleted is True
    assert await repo.list_memories("telegram:1") == []

    # clear on an empty scope is a no-op returning 0
    assert await repo.clear_memories("telegram:1") == 0


async def test_records_are_detached_and_safe_after_session_close(repo):
    rec = await repo.add_memory("telegram:1", "some fact", "some fact")
    # Access every field after the session that produced it has closed.
    assert rec.scope == "telegram:1"
    assert rec.content == "some fact"
    assert rec.normalized_content == "some fact"
    assert rec.created_at is not None
    assert rec.id > 0


async def test_count_memories(repo):
    assert await repo.count_memories("telegram:1") == 0
    await repo.add_memory("telegram:1", "a", "a")
    await repo.add_memory("telegram:1", "b", "b")
    assert await repo.count_memories("telegram:1") == 2
    assert await repo.count_memories("telegram:2") == 0


# ---------------------------------------------------------------------------
# scope isolation: A's memories never appear in B, and B cannot see/delete A's
# ---------------------------------------------------------------------------
async def test_scope_isolation_list_search_delete(repo):
    a = await repo.add_memory("telegram:A", "only A's fact", "only a s fact")
    await repo.add_memory("telegram:B", "only B's fact", "only b s fact")

    # List: each scope sees only its own.
    assert [m.id for m in await repo.list_memories("telegram:A")] == [a.id]
    assert [m.id for m in await repo.list_memories("telegram:B")] != [a.id]

    # Search candidates: A's row is not in B's candidate set.
    b_cands = await repo.list_memories_for_search("telegram:B")
    assert all(m.id != a.id for m in b_cands)

    # get_memory: B querying A's id returns None (indistinguishable from missing).
    assert await repo.get_memory("telegram:B", a.id) is None
    assert await repo.get_memory("telegram:A", a.id) is not None

    # delete: B cannot delete A's memory; the row survives.
    assert await repo.delete_memory("telegram:B", a.id) is False
    assert await repo.get_memory("telegram:A", a.id) is not None

    # clear: B clearing affects only B.
    b_rec = (await repo.list_memories("telegram:B"))[0]
    assert await repo.clear_memories("telegram:B") == 1
    assert await repo.get_memory("telegram:B", b_rec.id) is None
    assert await repo.get_memory("telegram:A", a.id) is not None  # A untouched


async def test_delete_missing_id_returns_false(repo):
    assert await repo.delete_memory("telegram:1", 999) is False


async def test_mark_memories_retrieved_sets_timestamp(repo):
    a = await repo.add_memory("telegram:1", "a", "a")
    b = await repo.add_memory("telegram:1", "b", "b")
    # Only the injected one is stamped.
    await repo.mark_memories_retrieved("telegram:1", [a.id])
    stamped = await repo.get_memory("telegram:1", a.id)
    unstamped = await repo.get_memory("telegram:1", b.id)
    assert stamped.last_retrieved_at is not None
    assert unstamped.last_retrieved_at is None


# ---------------------------------------------------------------------------
# /new must not touch memories
# ---------------------------------------------------------------------------
async def test_reset_does_not_touch_memories(repo):
    rec = await repo.add_memory("telegram:1", "survives /new", "survives /new")
    conv = await repo.get_or_create_conversation(7, 1)
    await repo.add_message(conv.id, "user", "chat line")

    new_conv = await repo.reset_conversation(7, 1)
    assert new_conv.id > conv.id

    # The conversation was reset but the memory survived.
    assert await repo.count_messages(new_conv.id) == 0
    assert await repo.get_memory("telegram:1", rec.id) is not None
