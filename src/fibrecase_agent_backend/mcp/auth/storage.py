"""The credential-storage contract for user-level MCP OAuth.

The :mod:`.manager` depends only on this :class:`OAuthStorage` interface — it
never imports SQLAlchemy. The concrete SQLite-backed implementation lives in
:mod:`..database.oauth` (the only layer that touches the ORM), so the storage
can later be swapped (e.g. for an encrypted / secret-manager backend) without
touching the manager.

Tokens are **sensitive**: every method that accepts them is the only boundary
where an ``access_token`` / ``refresh_token`` moves to or from the store. Callers
must never log them, and the Telegram layer must never see them.
"""

from __future__ import annotations

import abc
from datetime import datetime

from .models import CredentialRecord, PendingAuthorizationRecord


class OAuthStorage(abc.ABC):
    """Persistence for OAuth credentials and in-flight authorization states.

    Credentials are keyed by ``(telegram_user_id, provider, mcp_server)`` and
    are **always** queried with the user id in the SQL, so one user can never
    read another user's credential (isolation is enforced in storage, not only
    by the Telegram allow-list).
    """

    # ----------------------------------------------------------- credentials
    @abc.abstractmethod
    async def save_credential(self, record: CredentialRecord) -> None:
        """Insert or replace the single active credential for its triple.

        A re-authorization for the same ``(user, provider, server)`` **updates**
        the existing credential rather than creating a second one (one active
        credential per triple — no unlimited duplicates).
        """

    @abc.abstractmethod
    async def get_credential(
        self, *, telegram_user_id: int, provider: str, mcp_server: str
    ) -> CredentialRecord | None:
        """The active credential for this exact triple, or ``None``.

        The ``telegram_user_id`` filter is in the query, so a foreign user's
        credential is indistinguishable from a missing one (no existence leak).
        """

    # ----------------------------------------------------- pending (state)
    @abc.abstractmethod
    async def create_pending(
        self, *, state: str, record: PendingAuthorizationRecord
    ) -> None:
        """Persist an in-flight authorization (its ``state`` + expiry)."""

    @abc.abstractmethod
    async def consume_pending(self, *, state: str) -> PendingAuthorizationRecord | None:
        """Atomically **consume** the pending authorization for ``state``.

        Returns the record and deletes it in one step, so a state is
        single-use: a second call (a replay) returns ``None``. An unknown state
        also returns ``None``.
        """

    @abc.abstractmethod
    async def delete_pending(self, *, state: str) -> None:
        """Discard the pending authorization for ``state`` (best-effort)."""

    # --------------------------------------------------------------- status
    @abc.abstractmethod
    async def has_credential(self, *, telegram_user_id: int, mcp_server: str) -> bool:
        """Whether this user has *any* active credential for ``mcp_server``.

        Returns a boolean only (never the credential) so it is safe to expose
        as a "connected" status. The user id is in the query (isolation).
        """
