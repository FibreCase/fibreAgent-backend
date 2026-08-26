"""Phase 4.x — the provider-agnostic OAuth manager.

Drives the full authorization-code flow against a fake provider + fake storage
(no network, no ORM): initiate (server/provider/callback validation + state
generation), the callback (success / denied / invalid / expired / replay /
provider error — the state is *consumed* before the code is exchanged),
``valid_access_token`` (valid / expired+refresh / rotation persisted / keep-old
refresh / refresh failure keeps the credential / no credential), and the
token-free ``oauth_status`` classifications. Also proves the manager never
imports Telegram: notification is an injected async hook.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from fibrecase_agent_backend.mcp.auth.manager import OAuthManager, utcnow
from fibrecase_agent_backend.mcp.auth.models import (
    AuthorizationStatus,
    CredentialRecord,
    OAuthError,
    OAuthProviderError,
    PendingAuthorizationRecord,
    TokenResponse,
)
from fibrecase_agent_backend.mcp.auth.provider import OAuthProvider
from fibrecase_agent_backend.mcp.auth.storage import OAuthStorage


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _FakeProvider(OAuthProvider):
    name = "google"

    def __init__(self):
        self.exchange_calls: list[tuple[str, str]] = []
        self.refresh_calls: list[str] = []
        self.exchange_result = TokenResponse(
            access_token="AT-new", refresh_token="RT-new", expires_at=utcnow() + timedelta(hours=1), scopes="s"
        )
        self.exchange_error: Exception | None = None
        self.refresh_result = TokenResponse(
            access_token="AT-refreshed", refresh_token=None, expires_at=utcnow() + timedelta(hours=1), scopes="s"
        )
        self.refresh_error: Exception | None = None
        self.refresh_delay = 0.0

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        return f"https://provider.example/auth?redirect_uri={redirect_uri}&state={state}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenResponse:
        self.exchange_calls.append((code, redirect_uri))
        if self.exchange_error:
            raise self.exchange_error
        return self.exchange_result

    async def refresh_token(self, *, refresh_token: str) -> TokenResponse:
        self.refresh_calls.append(refresh_token)
        if self.refresh_delay:
            await asyncio.sleep(self.refresh_delay)
        if self.refresh_error:
            raise self.refresh_error
        return self.refresh_result


class _FakeStorage(OAuthStorage):
    def __init__(self):
        self.credentials: dict[tuple[int, str, str], CredentialRecord] = {}
        self.pending: dict[str, PendingAuthorizationRecord] = {}

    async def save_credential(self, record):
        self.credentials[(record.telegram_user_id, record.provider, record.mcp_server)] = record

    async def get_credential(self, *, telegram_user_id, provider, mcp_server):
        return self.credentials.get((telegram_user_id, provider, mcp_server))

    async def create_pending(self, *, state, record):
        self.pending[state] = record

    async def consume_pending(self, *, state):
        return self.pending.pop(state, None)

    async def delete_pending(self, *, state):
        self.pending.pop(state, None)

    async def has_credential(self, *, telegram_user_id, mcp_server):
        return any(u == telegram_user_id and s == mcp_server for (u, _p, s) in self.credentials)


def _credential(*, user=1, at="AT", rt="RT", expires_in=3600.0) -> CredentialRecord:
    return CredentialRecord(
        telegram_user_id=user,
        provider="google",
        mcp_server="gcal",
        access_token=at,
        refresh_token=rt,
        expires_at=utcnow() + timedelta(seconds=expires_in) if expires_in is not None else None,
        scopes="s",
        updated_at=utcnow(),
    )


def _pending(*, state="st-1", user=1, chat=100, ttl=600.0) -> PendingAuthorizationRecord:
    return PendingAuthorizationRecord(
        state=state,
        telegram_user_id=user,
        chat_id=chat,
        provider="google",
        mcp_server="gcal",
        expires_at=utcnow() + timedelta(seconds=ttl),
    )


def _manager(*, callback="https://ex.com", providers=None, servers=None, storage=None, notifier=None, ttl=600.0):
    storage = storage or _FakeStorage()
    providers = {"google": _FakeProvider()} if providers is None else providers
    servers = {"gcal": "google"} if servers is None else servers
    return OAuthManager(
        storage=storage,
        providers=providers,
        server_providers=servers,
        callback_base_url=callback,
        state_ttl_seconds=ttl,
        notifier=notifier,
    ), storage


# ---------------------------------------------------------------------------
# initiate
# ---------------------------------------------------------------------------
async def test_initiate_happy_path():
    mgr, storage = _manager()
    pending = await mgr.initiate(telegram_user_id=1, chat_id=100, mcp_server="gcal")
    assert pending.state and len(pending.state) >= 32
    assert pending.telegram_user_id == 1
    assert pending.chat_id == 100
    assert pending.expires_in_seconds == 600
    # The state is persisted, bound to the user + chat + server.
    row = storage.pending[pending.state]
    assert row.telegram_user_id == 1 and row.mcp_server == "gcal"
    # The authorization URL carries the state and the public callback URL.
    qs = parse_qs(urlparse(pending.authorization_url).query)
    assert qs["state"] == [pending.state]
    assert qs["redirect_uri"] == ["https://ex.com/oauth/callback"]


async def test_initiate_unknown_server_is_stable_error():
    mgr, _ = _manager()
    with pytest.raises(OAuthError) as exc:
        await mgr.initiate(telegram_user_id=1, chat_id=1, mcp_server="nope")
    assert exc.value.code == "mcp_server_not_oauth"
    assert "nope" in exc.value.user_safe


async def test_initiate_provider_missing_is_stable_error():
    mgr, _ = _manager(providers={})
    with pytest.raises(OAuthError) as exc:
        await mgr.initiate(telegram_user_id=1, chat_id=1, mcp_server="gcal")
    assert exc.value.code == "oauth_provider_not_configured"


async def test_initiate_without_callback_base_is_stable_error():
    mgr, _ = _manager(callback=None)
    with pytest.raises(OAuthError) as exc:
        await mgr.initiate(telegram_user_id=1, chat_id=1, mcp_server="gcal")
    assert exc.value.code == "oauth_callback_not_configured"


# ---------------------------------------------------------------------------
# complete_authorization
# ---------------------------------------------------------------------------
async def test_callback_success_saves_credential_and_notifies():
    notifications: list[tuple] = []

    async def notifier(user, chat, server, ok):
        notifications.append((user, chat, server, ok))

    mgr, storage = _manager(notifier=notifier)
    pending = await mgr.initiate(telegram_user_id=1, chat_id=100, mcp_server="gcal")
    outcome = await mgr.complete_authorization({"state": pending.state, "code": "CODE-1"})

    assert outcome.status == AuthorizationStatus.SUCCESS
    assert "gcal" in outcome.title
    # The code was exchanged against the public callback URL.
    provider = next(iter(mgr._providers.values()))
    assert provider.exchange_calls == [("CODE-1", "https://ex.com/oauth/callback")]
    # The credential is bound to the user, not the chat.
    cred = storage.credentials[(1, "google", "gcal")]
    assert cred.access_token == "AT-new" and cred.refresh_token == "RT-new"
    assert notifications == [(1, 100, "gcal", True)]
    # The state was consumed.
    assert storage.pending == {}


async def test_callback_denied_is_fixed_and_deletes_state():
    mgr, storage = _manager()
    pending = await mgr.initiate(telegram_user_id=1, chat_id=100, mcp_server="gcal")
    outcome = await mgr.complete_authorization(
        {"state": pending.state, "error": "access_denied", "error_description": "secretish"}
    )
    assert outcome.status == AuthorizationStatus.DENIED
    assert "secretish" not in outcome.detail
    assert storage.pending == {}
    assert storage.credentials == {}


async def test_callback_without_state_is_invalid():
    mgr, storage = _manager()
    outcome = await mgr.complete_authorization({"code": "CODE"})
    assert outcome.status == AuthorizationStatus.INVALID


async def test_callback_state_without_code_is_invalid_and_deleted():
    mgr, storage = _manager()
    await mgr.initiate(telegram_user_id=1, chat_id=100, mcp_server="gcal")
    state = next(iter(storage.pending))
    outcome = await mgr.complete_authorization({"state": state})
    assert outcome.status == AuthorizationStatus.INVALID
    assert storage.pending == {}


async def test_callback_unknown_state_is_invalid():
    mgr, _ = _manager()
    outcome = await mgr.complete_authorization({"state": "never-was", "code": "CODE"})
    assert outcome.status == AuthorizationStatus.INVALID


async def test_callback_expired_state_is_expired():
    mgr, storage = _manager()
    old = _pending(state="old-1", ttl=-120)  # already past its expiry
    await storage.create_pending(state="old-1", record=old)
    outcome = await mgr.complete_authorization({"state": "old-1", "code": "CODE"})
    assert outcome.status == AuthorizationStatus.EXPIRED


async def test_callback_replay_after_success_is_invalid():
    mgr, storage = _manager()
    pending = await mgr.initiate(telegram_user_id=1, chat_id=100, mcp_server="gcal")
    first = await mgr.complete_authorization({"state": pending.state, "code": "C1"})
    assert first.status == AuthorizationStatus.SUCCESS
    second = await mgr.complete_authorization({"state": pending.state, "code": "C1"})
    assert second.status == AuthorizationStatus.INVALID


async def test_callback_binding_comes_from_pending_not_forged_query():
    """The spec's "wrong user / wrong provider / wrong server" binding check.

    A real provider redirect carries only ``code`` + ``state``. The target user,
    provider, and MCP server come from the *stored pending record* — forged
    query parameters cannot redirect the credential to another user/server.
    """
    mgr, storage = _manager()
    pending = await mgr.initiate(telegram_user_id=1, chat_id=100, mcp_server="gcal")
    outcome = await mgr.complete_authorization(
        {
            "state": pending.state,
            "code": "C",
            "telegram_user_id": "999",  # forged: a different user
            "mcp_server": "other",  # forged: a different server
            "provider": "evil",  # forged: a different provider
        }
    )
    assert outcome.status == AuthorizationStatus.SUCCESS
    # Bound to the pending record's triple — never the forged values.
    assert (1, "google", "gcal") in storage.credentials
    assert (999, "evil", "other") not in storage.credentials


async def test_callback_provider_exchange_failure_is_error_without_leak():
    mgr, storage = _manager()
    pending = await mgr.initiate(telegram_user_id=1, chat_id=100, mcp_server="gcal")
    provider = next(iter(mgr._providers.values()))
    provider.exchange_error = OAuthProviderError("google token request rejected")
    outcome = await mgr.complete_authorization({"state": pending.state, "code": "LEAKY-CODE"})
    assert outcome.status == AuthorizationStatus.ERROR
    assert "LEAKY-CODE" not in outcome.detail
    assert "rejected" not in outcome.detail
    assert storage.credentials == {}


async def test_callback_provider_missing_at_callback_is_error():
    storage = _FakeStorage()
    await storage.create_pending(state="st-1", record=_pending(state="st-1"))
    mgr = OAuthManager(
        storage=storage, providers={}, server_providers={"gcal": "google"}, callback_base_url="https://ex.com"
    )
    outcome = await mgr.complete_authorization({"state": "st-1", "code": "C"})
    assert outcome.status == AuthorizationStatus.ERROR


async def test_callback_notifier_failure_never_breaks_outcome():
    async def boom(user, chat, server, ok):
        raise RuntimeError("telegram down")

    mgr, storage = _manager(notifier=boom)
    pending = await mgr.initiate(telegram_user_id=1, chat_id=100, mcp_server="gcal")
    outcome = await mgr.complete_authorization({"state": pending.state, "code": "C"})
    assert outcome.status == AuthorizationStatus.SUCCESS
    assert (1, "google", "gcal") in storage.credentials


# ---------------------------------------------------------------------------
# valid_access_token
# ---------------------------------------------------------------------------
async def test_valid_access_token_returns_stored_when_valid():
    mgr, storage = _manager()
    await storage.save_credential(_credential(at="AT-valid"))
    provider = next(iter(mgr._providers.values()))
    assert await mgr.valid_access_token(telegram_user_id=1, mcp_server="gcal") == "AT-valid"
    assert provider.refresh_calls == []


async def test_valid_access_token_no_credential_is_none():
    mgr, _ = _manager()
    assert await mgr.valid_access_token(telegram_user_id=1, mcp_server="gcal") is None
    # Unknown server → None, even with a credential for another server.
    mgr2, storage2 = _manager()
    await storage2.save_credential(CredentialRecord(
        telegram_user_id=1, provider="google", mcp_server="other",
        access_token="AT", refresh_token="RT", expires_at=None, scopes=None, updated_at=utcnow(),
    ))
    assert await mgr2.valid_access_token(telegram_user_id=1, mcp_server="gcal") is None


async def test_valid_access_token_refreshes_when_expired():
    mgr, storage = _manager()
    await storage.save_credential(_credential(at="AT-old", rt="RT-old", expires_in=-120))
    tok = await mgr.valid_access_token(telegram_user_id=1, mcp_server="gcal")
    assert tok == "AT-refreshed"
    provider = next(iter(mgr._providers.values()))
    assert provider.refresh_calls == ["RT-old"]
    # The rotated credential is persisted.
    cred = storage.credentials[(1, "google", "gcal")]
    assert cred.access_token == "AT-refreshed"


async def test_valid_access_token_keeps_old_refresh_when_not_rotated():
    mgr, storage = _manager()
    await storage.save_credential(_credential(at="AT-old", rt="RT-old", expires_in=-120))
    await mgr.valid_access_token(telegram_user_id=1, mcp_server="gcal")
    # The fake provider returns refresh_token=None → the old RT is kept.
    assert storage.credentials[(1, "google", "gcal")].refresh_token == "RT-old"


async def test_valid_access_token_uses_rotated_refresh_when_returned():
    mgr, storage = _manager()
    await storage.save_credential(_credential(at="AT-old", rt="RT-old", expires_in=-120))
    provider = next(iter(mgr._providers.values()))
    provider.refresh_result = TokenResponse(
        access_token="AT-r2", refresh_token="RT-rotated", expires_at=utcnow() + timedelta(hours=1), scopes=None
    )
    await mgr.valid_access_token(telegram_user_id=1, mcp_server="gcal")
    assert storage.credentials[(1, "google", "gcal")].refresh_token == "RT-rotated"


async def test_valid_access_token_refresh_failure_keeps_credential():
    mgr, storage = _manager()
    await storage.save_credential(_credential(at="AT-old", rt="RT-old", expires_in=-120))
    provider = next(iter(mgr._providers.values()))
    provider.refresh_error = OAuthProviderError("google token request rejected")
    assert await mgr.valid_access_token(telegram_user_id=1, mcp_server="gcal") is None
    # The credential is NOT deleted — re-authorization is the recovery path.
    cred = storage.credentials.get((1, "google", "gcal"))
    assert cred is not None and cred.refresh_token == "RT-old"


async def test_valid_access_token_expired_without_refresh_is_none():
    mgr, storage = _manager()
    await storage.save_credential(_credential(at="AT-old", rt=None, expires_in=-120))
    assert await mgr.valid_access_token(telegram_user_id=1, mcp_server="gcal") is None


async def test_valid_access_token_no_known_expiry_is_valid():
    mgr, storage = _manager()
    await storage.save_credential(_credential(at="AT-eternal", rt=None, expires_in=None))
    assert await mgr.valid_access_token(telegram_user_id=1, mcp_server="gcal") == "AT-eternal"


async def test_concurrent_refresh_refreshes_once():
    mgr, storage = _manager()
    await storage.save_credential(_credential(at="AT-old", rt="RT-old", expires_in=-120))
    provider = next(iter(mgr._providers.values()))
    provider.refresh_delay = 0.05
    results = await asyncio.gather(
        mgr.valid_access_token(telegram_user_id=1, mcp_server="gcal"),
        mgr.valid_access_token(telegram_user_id=1, mcp_server="gcal"),
    )
    assert results == ["AT-refreshed", "AT-refreshed"]
    assert len(provider.refresh_calls) == 1


# ---------------------------------------------------------------------------
# authenticated / oauth_status
# ---------------------------------------------------------------------------
async def test_authenticated_is_token_free_boolean():
    mgr, storage = _manager()
    assert await mgr.authenticated(telegram_user_id=1, mcp_server="gcal") is False
    await storage.save_credential(_credential(at="AT"))
    assert await mgr.authenticated(telegram_user_id=1, mcp_server="gcal") is True
    assert await mgr.authenticated(telegram_user_id=2, mcp_server="gcal") is False


async def test_oauth_status_all_states():
    mgr, storage = _manager()
    assert await mgr.oauth_status(telegram_user_id=1, mcp_server="nope") == "not_oauth"

    mgr_nc, storage_nc = _manager(callback=None)
    assert await mgr_nc.oauth_status(telegram_user_id=1, mcp_server="gcal") == "not_configured"

    mgr_pnc = OAuthManager(
        storage=_FakeStorage(), providers={}, server_providers={"gcal": "google"}, callback_base_url="https://ex.com"
    )
    assert await mgr_pnc.oauth_status(telegram_user_id=1, mcp_server="gcal") == "provider_not_configured"

    assert await mgr.oauth_status(telegram_user_id=1, mcp_server="gcal") == "authentication_required"

    # A past-expiry credential **with** a refresh token is still "connected"
    # (it will auto-refresh on next use).
    await storage.save_credential(_credential(at="AT", rt="RT", expires_in=-120))
    assert await mgr.oauth_status(telegram_user_id=1, mcp_server="gcal") == "connected"
    # …but past expiry with **no** refresh token is unrecoverable → "expired".
    await storage.save_credential(_credential(at="AT", rt=None, expires_in=-120))
    assert await mgr.oauth_status(telegram_user_id=1, mcp_server="gcal") == "expired"
    # A fresh credential is connected; a foreign user is not.
    await storage.save_credential(_credential(at="AT", rt="RT", expires_in=3600))
    assert await mgr.oauth_status(telegram_user_id=1, mcp_server="gcal") == "connected"
    assert await mgr.oauth_status(telegram_user_id=2, mcp_server="gcal") == "authentication_required"
