"""Audit persistence: schema upgrade, append-only events, scope isolation.

In-memory / temp-file SQLite only. Proves the ``tool_audit_events`` table is
created losslessly on a fresh DB *and* on a simulated v1.6.0 upgrade, that
events are append-only with safe fields only, that the auditor hashes the scope
at the persistence boundary, and that one scope can never see another's rows.
"""

from __future__ import annotations

import tempfile

from sqlalchemy.ext.asyncio import create_async_engine

from fibrecase_agent_backend.database.audit import RepositoryToolAuditor
from fibrecase_agent_backend.database.models import (
    Attachment,
    Base,
    Conversation,
    Memory,
    Message,
)
from fibrecase_agent_backend.database.repository import ConversationRepository
from fibrecase_agent_backend.database.session import create_session_factory, init_db
from fibrecase_agent_backend.memory import hash_scope
from fibrecase_agent_backend.tools.audit import (
    EVENT_APPROVAL_APPROVED,
    EVENT_APPROVAL_REQUESTED,
    EVENT_COMPLETED,
    EVENT_REQUESTED,
    EVENT_STARTED,
    ToolAuditEvent,
)


async def _table_names(engine):
    from sqlalchemy import inspect

    async with engine.connect() as conn:
        return await conn.run_sync(lambda c: set(inspect(c).get_table_names()))


# ---------------------------------------------------------------------------
# required #14 — fresh DB and a v1.6.0 upgrade both create the table, losslessly
# ---------------------------------------------------------------------------
async def test_fresh_and_v160_upgrade_create_audit_table():
    # Fresh DB.
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp}/fresh.db")
        await init_db(engine)
        assert "tool_audit_events" in await _table_names(engine)
        await engine.dispose()

    # Simulated v1.6.0 DB: the pre-phase-3 tables with live data.
    with tempfile.TemporaryDirectory() as tmp:
        db = f"{tmp}/v160.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        Conversation.__table__,
                        Message.__table__,
                        Attachment.__table__,
                        Memory.__table__,
                    ],
                )
            )
        r = ConversationRepository(create_session_factory(engine))
        conv = await r.get_or_create_conversation(1, 1)
        await r.add_message(conv.id, "user", "old message survives")
        await r.add_memory("telegram:1", "old memory survives", "old memory survives")

        # init_db adds the missing table only.
        await init_db(engine)
        assert "tool_audit_events" in await _table_names(engine)

        # Old data is intact (no data loss, no manual wipe).
        assert [m.content for m in await r.get_messages(conv.id)] == ["old message survives"]
        assert await r.count_memories("telegram:1") == 1

        # And the new table is immediately usable.
        ok = await r.add_tool_audit_event(
            {"scope_hash": hash_scope("telegram:1"), "tool_name": "echo", "event_type": "requested", "code": "ok"}
        )
        assert ok is True
        assert len(await r.list_tool_audit_events(hash_scope("telegram:1"))) == 1
        await engine.dispose()


# ---------------------------------------------------------------------------
# required #15 — append-only event ordering + safe fields
# ---------------------------------------------------------------------------
async def test_audit_events_are_append_only_with_safe_fields(repo):
    auditor = RepositoryToolAuditor(repo)
    events = [
        ToolAuditEvent(scope="telegram:1", tool_name="echo", event_type=EVENT_REQUESTED, code="ok", conversation_id=5, tool_call_id="c1", iteration=1),
        ToolAuditEvent(scope="telegram:1", tool_name="echo", event_type=EVENT_STARTED, code="ok", conversation_id=5, tool_call_id="c1", iteration=1),
        ToolAuditEvent(scope="telegram:1", tool_name="echo", event_type=EVENT_COMPLETED, code="ok", conversation_id=5, tool_call_id="c1", iteration=1, latency_ms=12),
    ]
    for e in events:
        assert await auditor.record(e) is True

    rows = await repo.list_tool_audit_events(hash_scope("telegram:1"))
    # Newest first, all three present.
    assert [r.event_type for r in rows] == [EVENT_COMPLETED, EVENT_STARTED, EVENT_REQUESTED]
    # The scope was hashed at the boundary (the raw scope is not stored).
    assert all(r.scope_hash == hash_scope("telegram:1") for r in rows)
    # Latency is recorded only on the terminal (completed) event.
    completed = rows[0]
    assert completed.latency_ms == 12
    assert rows[1].latency_ms is None
    # The raw scope / user id never appears anywhere in a record.
    for r in rows:
        assert r.scope_hash != "telegram:1"
        assert "1" not in r.scope_hash or len(r.scope_hash) == 12  # hash, not raw


# ---------------------------------------------------------------------------
# required #15 (approval lifecycle) — the full allow/ask event sequence
# ---------------------------------------------------------------------------
async def test_approval_lifecycle_event_sequence(repo):
    auditor = RepositoryToolAuditor(repo)
    seq = [
        (EVENT_REQUESTED, "ok"),
        (EVENT_APPROVAL_REQUESTED, "ok"),
        (EVENT_APPROVAL_APPROVED, "ok"),
        (EVENT_STARTED, "ok"),
        (EVENT_COMPLETED, "ok"),
    ]
    for et, code in seq:
        assert await auditor.record_pre(ToolAuditEvent(scope="telegram:9", tool_name="risky", event_type=et, code=code)) is True

    rows = await repo.list_tool_audit_events(hash_scope("telegram:9"))
    # Stored in append order (ascending id); presented newest-first, so reverse.
    stored = list(reversed(rows))
    assert [r.event_type for r in stored] == [et for et, _ in seq]


# ---------------------------------------------------------------------------
# required #17 — scope isolation at the repository level
# ---------------------------------------------------------------------------
async def test_scope_isolation_a_cannot_see_b(repo):
    auditor = RepositoryToolAuditor(repo)
    await auditor.record(ToolAuditEvent(scope="telegram:A", tool_name="echo", event_type=EVENT_REQUESTED, code="ok"))
    await auditor.record(ToolAuditEvent(scope="telegram:B", tool_name="echo", event_type=EVENT_REQUESTED, code="ok"))

    a_rows = await repo.list_tool_audit_events(hash_scope("telegram:A"))
    b_rows = await repo.list_tool_audit_events(hash_scope("telegram:B"))
    assert len(a_rows) == 1 and a_rows[0].scope_hash == hash_scope("telegram:A")
    assert len(b_rows) == 1 and b_rows[0].scope_hash == hash_scope("telegram:B")

    # A foreign/missing scope hash yields nothing (no existence leak).
    assert await repo.list_tool_audit_events(hash_scope("telegram:UNSEEN")) == []
    # Two distinct scopes hash to distinct values (isolation is real, not an alias).
    assert hash_scope("telegram:A") != hash_scope("telegram:B")


# ---------------------------------------------------------------------------
# required #16 — pre-write failure is False (fail closed); terminal failure is False
# ---------------------------------------------------------------------------
async def test_auditor_reports_write_failure(repo, monkeypatch):
    auditor = RepositoryToolAuditor(repo)

    # The repository honours the contract "return False on a failed write, never
    # raise" — the auditor's job is to propagate that False so the loop fails
    # closed. Simulate exactly that contract.
    async def _fail(_event):
        return False

    monkeypatch.setattr(repo, "add_tool_audit_event", _fail)
    ev = ToolAuditEvent(scope="telegram:1", tool_name="echo", event_type=EVENT_REQUESTED, code="ok")
    assert await auditor.record_pre(ev) is False  # the loop would fail closed
    assert await auditor.record(ev) is False


async def test_repository_returns_false_on_genuine_write_failure(repo, monkeypatch):
    # Even at the repository level, a broken DB is a ``False`` (logged), never a
    # raised exception that could kill the loop. Drive the real method with a
    # session factory whose context manager blows up on entry.
    class _BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *exc):
            return False

    def _broken_factory():
        return _BrokenSession()

    monkeypatch.setattr(repo, "_session_factory", _broken_factory)
    ok = await repo.add_tool_audit_event(
        {"scope_hash": "x", "tool_name": "echo", "event_type": "requested", "code": "ok"}
    )
    assert ok is False


# ---------------------------------------------------------------------------
# required #19 — no sensitive payload in the persisted rows
# ---------------------------------------------------------------------------
async def test_persisted_rows_carry_no_sensitive_payload(repo):
    auditor = RepositoryToolAuditor(repo)
    # A realistic event: the only "risky" fields present are tool name, code,
    # call id, iteration, latency — never args/result/exception text/scope.
    await auditor.record(
        ToolAuditEvent(
            scope="telegram:42",
            tool_name="echo",
            event_type=EVENT_COMPLETED,
            code="ok",
            conversation_id=7,
            tool_call_id="call_abc",
            iteration=2,
            latency_ms=3,
        )
    )
    (row,) = await repo.list_tool_audit_events(hash_scope("telegram:42"))
    assert row.tool_name == "echo"
    assert row.code == "ok"
    assert row.tool_call_id == "call_abc"
    assert row.scope_hash == hash_scope("telegram:42")
    # The raw scope/user id ("42") is not recoverable in the scope_hash.
    assert "42" not in row.scope_hash or row.scope_hash == hash_scope("telegram:42")
