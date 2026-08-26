"""SQLite-backed :class:`~..mcp.auth.storage.OAuthStorage` (phase 4.x).

The *only* module that touches the ORM for OAuth credentials. It implements the
channel-/protocol-agnostic :class:`~..mcp.auth.storage.OAuthStorage` contract on
top of the ``oauth_credentials`` / ``oauth_authorization_states`` tables, so the
OAuth manager never imports SQLAlchemy.

Security properties enforced here:

* **User isolation** — every credential read / existence check is filtered by
  ``telegram_user_id`` *in the SQL*, so one user can neither read nor delete
  another user's credential (a foreign user is indistinguishable from a missing
  one; no existence leak).
* **Single-use state** — :meth:`consume_pending` selects and deletes the pending
  row in one unit of work, so a replayed ``state`` finds nothing.
* **No duplication** — a re-authorization upserts the single active credential
  for its ``(user, provider, server)`` triple.
* **Sensitive tokens** — ``access_token`` / ``refresh_token`` are written and
  read here only; this module **never** logs a token (the log lines below carry
  at most the provider, server name, and an id).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..mcp.auth.models import CredentialRecord, PendingAuthorizationRecord
from ..mcp.auth.storage import OAuthStorage
from .models import OAuthAuthorizationState, OAuthCredential

logger = logging.getLogger("database")


def _aware(dt: datetime | None) -> datetime | None:
    """Normalise a stored datetime to tz-aware UTC.

    SQLite stores naive datetimes; the rest of the codebase (and the manager's
    expiry comparisons) assumes tz-aware UTC. A ``None`` stays ``None``.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class OAuthStorageImpl(OAuthStorage):
    """The concrete :class:`OAuthStorage` over an async session factory."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            yield session

    # ----------------------------------------------------------- credentials
    async def save_credential(self, record: CredentialRecord) -> None:
        """Insert or replace the single active credential for its triple.

        A re-authorization for the same ``(user, provider, server)`` **updates**
        the existing row rather than creating a second one (one active
        credential per triple — no unlimited duplicates).
        """
        async with self._session() as session:
            result = await session.execute(
                select(OAuthCredential).where(
                    OAuthCredential.telegram_user_id == record.telegram_user_id,
                    OAuthCredential.provider == record.provider,
                    OAuthCredential.mcp_server == record.mcp_server,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = OAuthCredential(
                    telegram_user_id=record.telegram_user_id,
                    provider=record.provider,
                    mcp_server=record.mcp_server,
                )
                session.add(row)
            # Always overwrite the sensitive + expiry fields (a re-auth or a
            # refresh replaces them).
            row.access_token = record.access_token
            row.refresh_token = record.refresh_token
            row.expires_at = record.expires_at
            row.scopes = record.scopes
            await session.commit()
            logger.info(
                "oauth credential saved",
                extra={"provider": record.provider, "mcp_server": record.mcp_server, "id": row.id},
            )

    async def get_credential(
        self, *, telegram_user_id: int, provider: str, mcp_server: str
    ) -> CredentialRecord | None:
        """The active credential for this exact triple, or ``None``.

        The ``telegram_user_id`` filter is in the query, so a foreign user's
        credential is indistinguishable from a missing one (no existence leak).
        """
        async with self._session() as session:
            result = await session.execute(
                select(OAuthCredential).where(
                    OAuthCredential.telegram_user_id == telegram_user_id,
                    OAuthCredential.provider == provider,
                    OAuthCredential.mcp_server == mcp_server,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return CredentialRecord(
                telegram_user_id=row.telegram_user_id,
                provider=row.provider,
                mcp_server=row.mcp_server,
                access_token=row.access_token,
                refresh_token=row.refresh_token,
                expires_at=_aware(row.expires_at),
                scopes=row.scopes,
                updated_at=_aware(row.updated_at),
            )

    # ----------------------------------------------------- pending (state)
    async def create_pending(self, *, state: str, record: PendingAuthorizationRecord) -> None:
        """Persist an in-flight authorization (its ``state`` + expiry)."""
        async with self._session() as session:
            session.add(
                OAuthAuthorizationState(
                    state=state,
                    telegram_user_id=record.telegram_user_id,
                    chat_id=record.chat_id,
                    provider=record.provider,
                    mcp_server=record.mcp_server,
                    expires_at=record.expires_at,
                )
            )
            await session.commit()

    async def consume_pending(self, *, state: str) -> PendingAuthorizationRecord | None:
        """Atomically **consume** the pending authorization for ``state``.

        Returns the record and deletes it in one step, so a state is single-use:
        a second call (a replay) returns ``None``. An unknown state also returns
        ``None``.
        """
        async with self._session() as session:
            result = await session.execute(
                select(OAuthAuthorizationState).where(OAuthAuthorizationState.state == state)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            record = PendingAuthorizationRecord(
                state=row.state,
                telegram_user_id=row.telegram_user_id,
                chat_id=row.chat_id,
                provider=row.provider,
                mcp_server=row.mcp_server,
                expires_at=_aware(row.expires_at),
            )
            await session.delete(row)
            await session.commit()
            return record

    async def delete_pending(self, *, state: str) -> None:
        """Discard the pending authorization for ``state`` (best-effort)."""
        async with self._session() as session:
            await session.execute(delete(OAuthAuthorizationState).where(OAuthAuthorizationState.state == state))
            await session.commit()

    # --------------------------------------------------------------- status
    async def has_credential(self, *, telegram_user_id: int, mcp_server: str) -> bool:
        """Whether this user has *any* active credential for ``mcp_server``.

        Returns a boolean only (never the credential) so it is safe to expose
        as a "connected" status. The user id is in the query (isolation).
        """
        async with self._session() as session:
            result = await session.execute(
                select(OAuthCredential.id).where(
                    OAuthCredential.telegram_user_id == telegram_user_id,
                    OAuthCredential.mcp_server == mcp_server,
                ).limit(1)
            )
            return result.first() is not None
