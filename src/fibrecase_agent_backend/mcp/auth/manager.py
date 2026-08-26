"""The provider-agnostic OAuth manager for user-level MCP authentication.

This is the heart of phase 4.x. It drives the OAuth 2.0 **authorization-code**
flow against *any* :class:`~.provider.OAuthProvider`, holding **no**
Google-specific logic: the Google endpoints / scopes / client id+secret live in
:class:`.provider.GoogleOAuthProvider` (and in the startup config that builds
it). A future GitHub / Microsoft provider is a new provider subclass plus a new
config block — no ``if provider == "google"`` branch exists anywhere.

The manager knows, per configured MCP server, *which* provider authenticates it
(``server_providers``) and exposes the four operations the rest of the system
needs:

* :meth:`initiate` — generate a single-use ``state``, persist the pending
  authorization (bound to the Telegram **user** + chat), and return the
  provider authorization URL;
* :meth:`complete_authorization` — the callback: validate + **consume** the
  state (single-use, expiry, user binding), exchange the code, save the
  credential, and notify the user;
* :meth:`valid_access_token` — return a usable access token for
  (user, mcp_server), **auto-refreshing** when expired (and persisting a rotated
  refresh token);
* :meth:`authenticated` — a token-free "connected" boolean for status.

It depends only on the :class:`~.storage.OAuthProvider`/
:class:`~.storage.OAuthStorage` interfaces and the standard library (plus the
providers' own HTTP). It **never** imports Telegram or the OpenAI SDK; user
notification is delegated to an injected async ``notifier``. It never logs a
token, a client secret, an authorization code, or a full callback URL — only the
stable code and the MCP server name.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta

from .models import (
    CODE_CALLBACK_NOT_CONFIGURED,
    CODE_PROVIDER_NOT_CONFIGURED,
    CODE_SERVER_NOT_OAUTH,
    AuthorizationOutcome,
    AuthorizationStatus,
    CredentialRecord,
    OAuthError,
    OAuthProviderError,
    PendingAuthorization,
    PendingAuthorizationRecord,
    utcnow,
)
from .provider import OAuthProvider
from .storage import OAuthStorage

logger = logging.getLogger("mcp.auth")

# A refresh is attempted this far before the real expiry, so a token that is
# "still valid by the clock" but about to die is not handed to a request that
# will then be rejected mid-flight.
_EXPIRY_SKEW = timedelta(seconds=60)
# ``secrets.token_urlsafe`` length for the ``state`` (>= 32 bytes of entropy).
_STATE_BYTES = 32

# An async notification hook: (telegram_user_id, chat_id, mcp_server, ok).
# Injected by the composition root (the Telegram layer) — the manager never
# imports Telegram. ``ok`` is True on a successful connection.
Notifier = Callable[[int, int, str, bool], Awaitable[None]]


class OAuthManager:
    """Coordinates the user-level OAuth flow over an :class:`OAuthStorage`.

    Construct with the credential storage, the available providers keyed by
    name, the ``mcp_server -> provider_name`` mapping (from MCP config), the
    public callback base URL (``None`` = OAuth is not configured), the state
    TTL, and an optional async ``notifier`` for user notifications.
    """

    def __init__(
        self,
        *,
        storage: OAuthStorage,
        providers: Mapping[str, OAuthProvider],
        server_providers: Mapping[str, str],
        callback_base_url: str | None,
        state_ttl_seconds: float = 600.0,
        notifier: Notifier | None = None,
    ) -> None:
        self._storage = storage
        self._providers = dict(providers)
        self._server_providers = dict(server_providers)
        self._callback_base_url = callback_base_url.rstrip("/") if callback_base_url else None
        self._state_ttl = timedelta(seconds=state_ttl_seconds)
        self._notifier = notifier
        # Serialize refresh per (user, server) so two concurrent calls for the
        # same credential do not both rotate the refresh token.
        self._refresh_locks: dict[tuple[int, str], asyncio.Lock] = {}

    # ------------------------------------------------------------- config
    @property
    def callback_configured(self) -> bool:
        """Whether an OAuth callback URL is configured (i.e. OAuth can start)."""
        return self._callback_base_url is not None

    @property
    def oauth_servers(self) -> "frozenset[str]":
        """The MCP server names that require OAuth authentication."""
        return frozenset(self._server_providers)

    def authorization_redirect_uri(self) -> str:
        """The public callback URL (``<base>/oauth/callback``).

        Raises :class:`OAuthError` if no callback base URL is configured — a
        caller should check :attr:`callback_configured` first, but this keeps a
        misconfiguration from ever producing a usable-looking URL.
        """
        if self._callback_base_url is None:
            raise OAuthError(
                CODE_CALLBACK_NOT_CONFIGURED,
                "OAuth is not configured (no callback URL). Set OAUTH_CALLBACK_BASE_URL.",
            )
        return f"{self._callback_base_url}/oauth/callback"

    # ------------------------------------------------------------- initiate
    async def initiate(
        self, *, telegram_user_id: int, chat_id: int, mcp_server: str
    ) -> PendingAuthorization:
        """Begin an OAuth flow for this user + MCP server.

        Generates a single-use ``state``, persists the pending authorization
        (bound to the **Telegram user** and the originating chat), and returns
        the provider authorization URL for an OAuth login button.

        Raises :class:`OAuthError` (with a stable, user-safe ``code``) when the
        callback is not configured, the server is not an OAuth server, or the
        provider for that server is not configured.
        """
        provider_name = self._server_providers.get(mcp_server)
        if provider_name is None:
            raise OAuthError(
                CODE_SERVER_NOT_OAUTH,
                f"'{mcp_server}' does not require (or support) OAuth authentication.",
            )
        provider = self._providers.get(provider_name)
        if provider is None:
            raise OAuthError(
                CODE_PROVIDER_NOT_CONFIGURED,
                f"The OAuth provider '{provider_name}' is not configured.",
            )
        if self._callback_base_url is None:
            raise OAuthError(
                CODE_CALLBACK_NOT_CONFIGURED,
                "OAuth is not configured (no callback URL). Set OAUTH_CALLBACK_BASE_URL.",
            )

        state = secrets.token_urlsafe(_STATE_BYTES)
        now = utcnow()
        expires_at = now + self._state_ttl
        pending = PendingAuthorizationRecord(
            state=state,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            provider=provider_name,
            mcp_server=mcp_server,
            expires_at=expires_at,
        )
        await self._storage.create_pending(state=state, record=pending)
        logger.info(
            "oauth initiated",
            extra={"provider": provider_name, "mcp_server": mcp_server, "ttl": int(self._state_ttl.total_seconds())},
        )
        authorization_url = provider.authorization_url(
            redirect_uri=self.authorization_redirect_uri(), state=state
        )
        return PendingAuthorization(
            state=state,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            provider=provider_name,
            mcp_server=mcp_server,
            authorization_url=authorization_url,
            expires_at=expires_at,
            expires_in_seconds=int(self._state_ttl.total_seconds()),
        )

    # ------------------------------------------------------------- callback
    async def complete_authorization(self, query: Mapping[str, str]) -> AuthorizationOutcome:
        """Process one ``GET /oauth/callback`` query (the OAuth provider's redirect).

        Handles the success path (``code`` + ``state``), a user denial
        (``error=access_denied``), and the invalid / expired / unknown state
        cases. The state is **consumed** (single-use) before the code is
        exchanged, so a replay is rejected. Errors never echo a token or a full
        URL.
        """
        error = query.get("error")
        state = query.get("state")
        code = query.get("code")

        # The user pressed "deny" (or the provider reported an error). Discard
        # the pending state if present and report a fixed, non-leaking message.
        if error:
            if state:
                await self._storage.delete_pending(state=state)
            return AuthorizationOutcome(
                status=AuthorizationStatus.DENIED,
                title="Authorization not completed",
                detail="You did not authorize access. You can try again from Telegram with /mcp auth.",
            )

        if not state:
            return _INVALID
        if not code:
            # A state with no code and no error is malformed — treat as invalid.
            await self._storage.delete_pending(state=state)
            return _INVALID

        # Single-use: consume the state. Unknown or already-consumed -> None.
        pending = await self._storage.consume_pending(state=state)
        if pending is None:
            return _INVALID
        if pending.expires_at < utcnow():
            return AuthorizationOutcome(
                status=AuthorizationStatus.EXPIRED,
                title="Authorization expired",
                detail="This authorization link expired. Start a new one from Telegram with /mcp auth.",
            )

        provider = self._providers.get(pending.provider)
        if provider is None:
            logger.error(
                "oauth provider missing at callback",
                extra={"provider": pending.provider, "mcp_server": pending.mcp_server},
            )
            return AuthorizationOutcome(
                status=AuthorizationStatus.ERROR,
                title="Authorization failed",
                detail="An internal configuration error occurred. Please try again from Telegram.",
            )

        try:
            token = await provider.exchange_code(
                code=code, redirect_uri=self.authorization_redirect_uri()
            )
        except OAuthProviderError:
            # Log only the stable code — never the provider's body.
            logger.warning(
                "oauth token exchange failed",
                extra={"provider": pending.provider, "mcp_server": pending.mcp_server, "code": "oauth_provider_error"},
            )
            return AuthorizationOutcome(
                status=AuthorizationStatus.ERROR,
                title="Authorization failed",
                detail="The provider could not be reached. Please try again from Telegram with /mcp auth.",
            )

        record = CredentialRecord(
            telegram_user_id=pending.telegram_user_id,
            provider=pending.provider,
            mcp_server=pending.mcp_server,
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=token.expires_at,
            scopes=token.scopes,
            updated_at=utcnow(),
        )
        await self._storage.save_credential(record)
        logger.info(
            "oauth credential saved",
            extra={"provider": pending.provider, "mcp_server": pending.mcp_server},
        )
        await self._notify(pending.telegram_user_id, pending.chat_id, pending.mcp_server, ok=True)
        return AuthorizationOutcome(
            status=AuthorizationStatus.SUCCESS,
            title=f"{pending.mcp_server} connected",
            detail=f"Your {pending.provider} account is now available to the Agent.",
        )

    # -------------------------------------------------------------- access
    async def valid_access_token(self, *, telegram_user_id: int, mcp_server: str) -> "str | None":
        """Return a usable access token for (user, mcp_server), or ``None``.

        Returns the stored access token when it is still valid; otherwise it
        **refreshes** it (persisting a rotated refresh token when the provider
        returns one) and returns the new access token. Returns ``None`` when
        there is no credential, no refresh token to fall back on, or the refresh
        fails — the caller then surfaces "authentication required / expired".
        """
        provider_name = self._server_providers.get(mcp_server)
        if provider_name is None:
            return None
        credential = await self._storage.get_credential(
            telegram_user_id=telegram_user_id, provider=provider_name, mcp_server=mcp_server
        )
        if credential is None:
            return None
        if _is_valid(credential):
            return credential.access_token

        # Expired (or no known expiry but we still need a token): refresh.
        if credential.refresh_token is None:
            return None
        provider = self._providers.get(credential.provider)
        if provider is None:
            return None

        lock = self._refresh_lock(telegram_user_id, mcp_server)
        async with lock:
            # Re-check under the lock — another coroutine may have refreshed.
            credential = await self._storage.get_credential(
                telegram_user_id=telegram_user_id, provider=provider_name, mcp_server=mcp_server
            )
            if credential is None:
                return None
            if _is_valid(credential):
                return credential.access_token
            if credential.refresh_token is None:
                return None
            try:
                new_token = await provider.refresh_token(refresh_token=credential.refresh_token)
            except OAuthProviderError:
                # The refresh token is (presumably) invalid — report "expired".
                # We deliberately do **not** delete the credential here; a
                # re-authorization from Telegram is the recovery path.
                logger.warning(
                    "oauth token refresh failed",
                    extra={"provider": credential.provider, "mcp_server": mcp_server, "code": "oauth_provider_error"},
                )
                return None
            updated = CredentialRecord(
                telegram_user_id=credential.telegram_user_id,
                provider=credential.provider,
                mcp_server=credential.mcp_server,
                access_token=new_token.access_token,
                # Keep the old refresh token if the provider did not rotate it.
                refresh_token=new_token.refresh_token or credential.refresh_token,
                expires_at=new_token.expires_at,
                scopes=new_token.scopes or credential.scopes,
                updated_at=utcnow(),
            )
            await self._storage.save_credential(updated)
            logger.info(
                "oauth token refreshed",
                extra={"provider": credential.provider, "mcp_server": mcp_server},
            )
            return new_token.access_token

    async def authenticated(self, *, telegram_user_id: int, mcp_server: str) -> bool:
        """Token-free "is this user connected to this MCP server?" status."""
        return await self._storage.has_credential(
            telegram_user_id=telegram_user_id, mcp_server=mcp_server
        )

    async def oauth_status(self, *, telegram_user_id: int, mcp_server: str) -> str:
        """A token-free, user-safe status string for the ``/mcp`` command.

        Returns one of ``"connected"``, ``"expired"``,
        ``"authentication_required"``, ``"not_configured"``,
        ``"provider_not_configured"``, or ``"not_oauth"``. A credential whose
        access token is past expiry **with no refresh token** is ``expired``
        (unrecoverable without a new authorization); one with a refresh token
        is still ``connected`` (it will auto-refresh on next use). No token is
        read into a log or returned — only the classification.
        """
        provider_name = self._server_providers.get(mcp_server)
        if provider_name is None:
            return "not_oauth"
        if self._callback_base_url is None:
            return "not_configured"
        if self._providers.get(provider_name) is None:
            return "provider_not_configured"
        credential = await self._storage.get_credential(
            telegram_user_id=telegram_user_id, provider=provider_name, mcp_server=mcp_server
        )
        if credential is None:
            return "authentication_required"
        if (
            credential.expires_at is not None
            and credential.expires_at < (utcnow() - _EXPIRY_SKEW)
            and credential.refresh_token is None
        ):
            return "expired"
        return "connected"

    # ------------------------------------------------------------- helpers
    def _refresh_lock(self, telegram_user_id: int, mcp_server: str) -> asyncio.Lock:
        key = (telegram_user_id, mcp_server)
        lock = self._refresh_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[key] = lock
        return lock

    async def _notify(self, telegram_user_id: int, chat_id: int, mcp_server: str, ok: bool) -> None:
        if self._notifier is None:
            return
        try:
            result = self._notifier(telegram_user_id, chat_id, mcp_server, ok)
            # ``result`` is an awaitable; awaiting it makes the hook truly async.
            if result is not None and hasattr(result, "__await__"):
                await result
        except Exception:  # a notification failure must never fail the callback
            logger.warning(
                "oauth notifier failed",
                extra={"mcp_server": mcp_server, "exception": "Exception"},
            )


# A prebuilt "invalid / expired state" outcome (reused, so it stays secret-free).
_INVALID = AuthorizationOutcome(
    status=AuthorizationStatus.INVALID,
    title="Invalid authorization",
    detail="Invalid or expired authorization request. Please start a new one from Telegram with /mcp auth.",
)


def _is_valid(credential: CredentialRecord) -> bool:
    """Whether the credential's access token is still usable.

    A credential with **no known expiry** is treated as valid (we do not speculatively
    refresh it); one with a past expiry (beyond the skew) is invalid.
    """
    if credential.expires_at is None:
        return True
    return credential.expires_at > (utcnow() - _EXPIRY_SKEW)
