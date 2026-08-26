"""Phase 4.x — the SQLite-backed OAuth credential storage.

Proves the storage contract that the manager depends on: the single-active
credential per ``(telegram_user_id, provider, mcp_server)`` triple with
**user isolation** (a foreign user's credential is indistinguishable from a
missing one), **single-use** pending states (a consumed state cannot be read
again), upsert-not-duplicate on re-authorization, timezone normalisation of the
datetimes SQLite hands back, and — critically — that ``/new``
(``reset_conversation``) **never** touches OAuth credentials or pending states.
All on in-memory SQLite; no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from fibrecase_agent_backend.database.models import Base, OAuthCredential, OAuthAuthorizationState
from fibrecase_agent_backend.database.oauth import OAuthStorageImpl
from fibrecase_agent_backend.database.repository import ConversationRepository
from fibrecase_agent_backend.mcp.auth.models import CredentialRecord, PendingAuthorizationRecord


@pytest.fixture
async def oauth_storage():
    """A fresh in-memory DB (FK on) with the OAuth storage over it."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield OAuthStorageImpl(factory)
    await engine.dispose()


def _credential(*, user=1, provider="google", server="gcal", at="AT", rt="RT",
                expires_in=3600.0) -> CredentialRecord:
    now = datetime.now(timezone.utc)
    return CredentialRecord(
        telegram_user_id=user,
        provider=provider,
        mcp_server=server,
        access_token=at,
        refresh_token=rt,
        expires_at=now + timedelta(seconds=expires_in) if expires_in is not None else None,
        scopes="s",
        updated_at=now,
    )


def _pending(*, state="st-1", user=1, chat=100, provider="google", server="gcal",
             ttl=600.0) -> PendingAuthorizationRecord:
    return PendingAuthorizationRecord(
        state=state,
        telegram_user_id=user,
        chat_id=chat,
        provider=provider,
        mcp_server=server,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
    )


# ---------------------------------------------------------------------------
# credentials: save / read / upsert
# ---------------------------------------------------------------------------
async def test_save_then_get_roundtrip(oauth_storage):
    await oauth_storage.save_credential(_credential(at="AT1", rt="RT1"))
    rec = await oauth_storage.get_credential(telegram_user_id=1, provider="google", mcp_server="gcal")
    assert rec is not None
    assert rec.access_token == "AT1"
    assert rec.refresh_token == "RT1"
    assert rec.telegram_user_id == 1
    # The stored datetimes come back tz-aware (SQLite stores naive).
    assert rec.expires_at is not None and rec.expires_at.tzinfo is not None
    assert rec.updated_at.tzinfo is not None


async def test_null_expiry_roundtrips_as_none(oauth_storage):
    await oauth_storage.save_credential(_credential(expires_in=None, rt=None))
    rec = await oauth_storage.get_credential(telegram_user_id=1, provider="google", mcp_server="gcal")
    assert rec.expires_at is None
    assert rec.refresh_token is None


async def test_reauthorization_upserts_not_duplicates(oauth_storage):
    await oauth_storage.save_credential(_credential(at="AT1", rt="RT1"))
    await oauth_storage.save_credential(_credential(at="AT2", rt="RT2"))
    rec = await oauth_storage.get_credential(telegram_user_id=1, provider="google", mcp_server="gcal")
    assert rec.access_token == "AT2"
    assert rec.refresh_token == "RT2"
    async with oauth_storage._session_factory() as session:
        count = (await session.execute(select(OAuthCredential.id))).scalars().all()
    assert len(count) == 1  # one active credential per triple


async def test_distinct_triples_coexist(oauth_storage):
    await oauth_storage.save_credential(_credential(user=1, server="gcal", at="A"))
    await oauth_storage.save_credential(_credential(user=2, server="gcal", at="B"))  # other user
    await oauth_storage.save_credential(_credential(user=1, server="other", at="C"))  # other server
    assert (await oauth_storage.get_credential(telegram_user_id=1, provider="google", mcp_server="gcal")).access_token == "A"
    assert (await oauth_storage.get_credential(telegram_user_id=2, provider="google", mcp_server="gcal")).access_token == "B"
    assert (await oauth_storage.get_credential(telegram_user_id=1, provider="google", mcp_server="other")).access_token == "C"


# ---------------------------------------------------------------------------
# credentials: user isolation
# ---------------------------------------------------------------------------
async def test_foreign_user_cannot_read_credential(oauth_storage):
    await oauth_storage.save_credential(_credential(user=1, at="SECRET"))
    # A different user's lookup is indistinguishable from a missing one.
    assert await oauth_storage.get_credential(telegram_user_id=2, provider="google", mcp_server="gcal") is None
    assert await oauth_storage.has_credential(telegram_user_id=2, mcp_server="gcal") is False
    assert await oauth_storage.has_credential(telegram_user_id=1, mcp_server="gcal") is True


# ---------------------------------------------------------------------------
# pending states: single-use
# ---------------------------------------------------------------------------
async def test_pending_is_single_use(oauth_storage):
    await oauth_storage.create_pending(state="st-1", record=_pending(state="st-1"))
    first = await oauth_storage.consume_pending(state="st-1")
    assert first is not None
    assert first.telegram_user_id == 1
    assert first.chat_id == 100
    assert first.mcp_server == "gcal"
    assert first.expires_at.tzinfo is not None
    # A replay finds nothing.
    assert await oauth_storage.consume_pending(state="st-1") is None


async def test_consume_unknown_state_is_none(oauth_storage):
    assert await oauth_storage.consume_pending(state="never-was") is None


async def test_delete_pending_is_best_effort(oauth_storage):
    await oauth_storage.create_pending(state="st-1", record=_pending(state="st-1"))
    await oauth_storage.delete_pending(state="st-1")
    assert await oauth_storage.consume_pending(state="st-1") is None
    # Deleting a state that does not exist is a safe no-op.
    await oauth_storage.delete_pending(state="never-was")


# ---------------------------------------------------------------------------
# /new must never touch OAuth state
# ---------------------------------------------------------------------------
async def test_reset_conversation_leaves_oauth_untouched(oauth_storage, repo):
    await oauth_storage.save_credential(_credential(user=7, at="KEEP", rt="KEEP-RT"))
    await oauth_storage.create_pending(state="st-keep", record=_pending(state="st-keep", user=7))

    # A real conversation for chat 55, then a /new on it.
    conv = await repo.get_or_create_conversation(telegram_chat_id=55, telegram_user_id=7)
    await repo.add_message(conv.id, role="user", content="hi")
    new_conv = await repo.reset_conversation(telegram_chat_id=55, telegram_user_id=7)

    assert new_conv.id != conv.id
    # The credential and the pending state survive the reset.
    rec = await oauth_storage.get_credential(telegram_user_id=7, provider="google", mcp_server="gcal")
    assert rec is not None and rec.access_token == "KEEP" and rec.refresh_token == "KEEP-RT"
    pending = await oauth_storage.consume_pending(state="st-keep")
    assert pending is not None


# ---------------------------------------------------------------------------
# credentials survive a process restart (acceptance #7)
# ---------------------------------------------------------------------------
async def test_credential_survives_restart(tmp_path):
    """A credential saved before a "restart" is still there after a fresh
    engine/session factory reopens the same SQLite file — the user is not asked
    to re-authenticate."""

    def _build_factory(db_path):
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_fk(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

        return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    db_file = tmp_path / "agent.db"

    # --- "process 1": save a credential, then shut down.
    engine1, factory1 = _build_factory(db_file)
    async with engine1.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    store1 = OAuthStorageImpl(factory1)
    await store1.save_credential(_credential(user=42, at="AT-persist", rt="RT-persist"))
    await engine1.dispose()

    # --- "process 2": a brand-new engine + storage over the same file.
    engine2, factory2 = _build_factory(db_file)
    store2 = OAuthStorageImpl(factory2)
    rec = await store2.get_credential(telegram_user_id=42, provider="google", mcp_server="gcal")
    assert rec is not None
    assert rec.access_token == "AT-persist"
    assert rec.refresh_token == "RT-persist"
    await engine2.dispose()

