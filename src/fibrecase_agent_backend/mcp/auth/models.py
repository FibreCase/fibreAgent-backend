"""Channel-, protocol-, and ORM-free data types for user-level MCP OAuth.

This module holds the *value objects* that move between the OAuth manager, the
credential storage, and the OAuth provider. It imports **none** of Telegram, the
OpenAI SDK, or SQLAlchemy — it is pure data plus a small set of stable,
user-safe exceptions.

The sensitive fields (``access_token`` / ``refresh_token``) are carried only in
memory between the manager and the storage; they are never logged and are never
handed to the Telegram layer (which only ever sees a boolean "connected" state
and a fixed, token-free success message).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def utcnow() -> datetime:
    """A timezone-aware "now" (UTC). Kept local so this module stays DB-free."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# stable, non-echoing failure codes (surfaced in logs; never with a token)
# ---------------------------------------------------------------------------
CODE_CALLBACK_NOT_CONFIGURED = "oauth_callback_not_configured"
CODE_PROVIDER_NOT_CONFIGURED = "oauth_provider_not_configured"
CODE_SERVER_NOT_OAUTH = "mcp_server_not_oauth"
CODE_UNKNOWN_SERVER = "mcp_unknown_server"
CODE_STATE_INVALID = "oauth_state_invalid"
CODE_STATE_EXPIRED = "oauth_state_expired"
CODE_PROVIDER_ERROR = "oauth_provider_error"


class OAuthError(Exception):
    """A failure in the OAuth flow, safe to surface to a user.

    ``code`` is a stable, non-echoing tag (logged and used to pick a safe
    message); ``user_safe`` is an English, secret-free string that may be shown
    directly to the user. It never carries a token, a client secret, an
    authorization code, or a full callback URL.
    """

    def __init__(self, code: str, user_safe: str) -> None:
        super().__init__(user_safe)
        self.code = code
        self.user_safe = user_safe


class OAuthProviderError(Exception):
    """A failure talking to the OAuth provider (token exchange / refresh).

    Carries only the provider name and a short, non-sensitive reason — never
    the provider's response body (which can echo an endpoint or a token).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# provider token result
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TokenResponse:
    """The result of a token exchange or refresh, in provider-agnostic form.

    ``refresh_token`` is ``None`` when the provider did not return one (e.g.
    Google only issues it on the first consent) — the caller then keeps the
    existing refresh token. ``expires_at`` is ``None`` when the provider did not
    report an ``expires_in`` (treated as no known expiry).
    """

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: str | None = None


# ---------------------------------------------------------------------------
# stored credential (detached, in-memory)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CredentialRecord:
    """One active credential for a (user, provider, mcp_server) triple.

    Bound to the Telegram **user** (``telegram_user_id``) — never to a
    conversation or chat — so it survives ``/new`` and restarts. The two token
    fields are sensitive and must never be logged or exposed outside the
    manager/storage boundary.
    """

    telegram_user_id: int
    provider: str
    mcp_server: str
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: str | None
    updated_at: datetime


# ---------------------------------------------------------------------------
# pending authorization (in-flight state)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PendingAuthorizationRecord:
    """A row from the pending-authorization table, detached (safe after close)."""

    state: str
    telegram_user_id: int
    chat_id: int
    provider: str
    mcp_server: str
    expires_at: datetime


@dataclass(frozen=True)
class PendingAuthorization:
    """The result of :meth:`OAuthManager.initiate` — what the Telegram layer
    sends to the user as an OAuth login button."""

    state: str
    telegram_user_id: int
    chat_id: int
    provider: str
    mcp_server: str
    authorization_url: str
    expires_at: datetime
    expires_in_seconds: int


# ---------------------------------------------------------------------------
# callback outcome (rendered by the minimal callback HTTP server)
# ---------------------------------------------------------------------------
class AuthorizationStatus:
    SUCCESS = "success"
    DENIED = "denied"
    INVALID = "invalid"
    EXPIRED = "expired"
    ERROR = "error"


@dataclass(frozen=True)
class AuthorizationOutcome:
    """The result of :meth:`OAuthManager.complete_authorization`.

    ``title`` / ``detail`` are fixed, secret-free strings the callback server
    renders to the browser. They never carry a token, an authorization code, a
    client secret, or a full callback URL.
    """

    status: str
    title: str
    detail: str
