"""Phase 4.x — the minimal OAuth callback HTTP server.

Drives the single ``GET /oauth/callback`` route through a starlette
:class:`TestClient` against a real :class:`OAuthManager` over fakes — no
uvicorn, no port, no network. Proves the outcome mapping (success / denied /
invalid / expired), the fixed 404 for every other path, and that the query
string (which carries the ``state`` and the authorization ``code``) and any
token never reach the logs.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from fibrecase_agent_backend.mcp.auth.manager import OAuthManager
from fibrecase_agent_backend.mcp.auth.models import AuthorizationStatus
from fibrecase_agent_backend.mcp.auth.server import OAuthCallbackServer, build_oauth_callback_server

from test_oauth_manager import _FakeProvider, _FakeStorage


def _manager(**kw) -> tuple[OAuthManager, _FakeStorage]:
    storage = kw.pop("storage", None) or _FakeStorage()
    mgr = OAuthManager(
        storage=storage,
        providers={"google": _FakeProvider()},
        server_providers={"gcal": "google"},
        callback_base_url="https://ex.com",
        state_ttl_seconds=kw.pop("ttl", 600.0),
        notifier=kw.pop("notifier", None),
    )
    return mgr, storage


def _client(mgr) -> TestClient:
    return TestClient(OAuthCallbackServer(mgr, port=8090)._build_app())


async def _initiate(mgr, *, user=1, chat=100, server="gcal"):
    return await mgr.initiate(telegram_user_id=user, chat_id=chat, mcp_server=server)


# ---------------------------------------------------------------------------
# success: code + valid state → 200, credential saved, telegram notified
# ---------------------------------------------------------------------------
async def test_callback_success():
    notifications: list[tuple] = []

    async def notifier(user, chat, server, ok):
        notifications.append((user, chat, server, ok))

    mgr, storage = _manager(notifier=notifier)
    pending = await _initiate(mgr)
    import logging

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("mcp.auth")
    old = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        resp = _client(mgr).get("/oauth/callback", params={"state": pending.state, "code": "CODE-9"})
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)
    assert resp.status_code == 200
    assert "connected" in resp.text
    assert "gcal" in resp.text
    assert (1, "google", "gcal") in storage.credentials
    assert storage.credentials[(1, "google", "gcal")].access_token == "AT-new"
    assert notifications == [(1, 100, "gcal", True)]
    # The state was consumed.
    assert storage.pending == {}
    # The authorization code and the state never reach the logs.
    for rec in records:
        assert "CODE-9" not in rec.getMessage()
        assert pending.state not in rec.getMessage()
        assert "AT-new" not in rec.getMessage()


# ---------------------------------------------------------------------------
# denied: error=access_denied → fixed message, state discarded, no credential
# ---------------------------------------------------------------------------
async def test_callback_denied():
    mgr, storage = _manager()
    pending = await _initiate(mgr)
    resp = _client(mgr).get(
        "/oauth/callback",
        params={"state": pending.state, "error": "access_denied"},
    )
    assert resp.status_code == 200
    assert "not completed" in resp.text.lower() or "denied" in resp.text.lower()
    assert storage.pending == {}
    assert storage.credentials == {}


# ---------------------------------------------------------------------------
# invalid / unknown / replayed state → invalid outcome
# ---------------------------------------------------------------------------
async def test_callback_unknown_state():
    mgr, _ = _manager()
    resp = _client(mgr).get("/oauth/callback", params={"state": "never-was", "code": "C"})
    assert resp.status_code == 200
    assert "Invalid" in resp.text


async def test_callback_replay_after_success_is_invalid():
    mgr, _ = _manager()
    pending = await _initiate(mgr)
    client = _client(mgr)
    first = client.get("/oauth/callback", params={"state": pending.state, "code": "C"})
    assert "connected" in first.text
    second = client.get("/oauth/callback", params={"state": pending.state, "code": "C"})
    assert "Invalid" in second.text


async def test_callback_without_state():
    mgr, _ = _manager()
    resp = _client(mgr).get("/oauth/callback", params={"code": "C"})
    assert resp.status_code == 200
    assert "Invalid" in resp.text


# ---------------------------------------------------------------------------
# expired state → expired outcome
# ---------------------------------------------------------------------------
async def test_callback_expired_state():
    from test_oauth_manager import _pending

    mgr, storage = _manager()
    old = _pending(state="old-1", ttl=-120)
    await storage.create_pending(state="old-1", record=old)
    resp = _client(mgr).get("/oauth/callback", params={"state": "old-1", "code": "C"})
    assert resp.status_code == 200
    assert "expired" in resp.text.lower()


# ---------------------------------------------------------------------------
# provider failure → error outcome (no leak of the provider body / code)
# ---------------------------------------------------------------------------
async def test_callback_provider_error():
    mgr, storage = _manager()
    provider = next(iter(mgr._providers.values()))
    from fibrecase_agent_backend.mcp.auth.models import OAuthProviderError

    provider.exchange_error = OAuthProviderError("google token request rejected")
    pending = await _initiate(mgr)
    resp = _client(mgr).get("/oauth/callback", params={"state": pending.state, "code": "LEAKY"})
    assert resp.status_code == 200
    assert "LEAKY" not in resp.text
    assert "rejected" not in resp.text
    assert storage.credentials == {}


# ---------------------------------------------------------------------------
# every other path is a fixed 404
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["/", "/oauth", "/oauth/callback/extra", "/other", "/index.html"],
)
def test_other_paths_are_404(path):
    mgr, _ = _manager()
    resp = _client(mgr).get(path)
    assert resp.status_code == 404
    assert resp.text == "Not found"


def test_build_helper_returns_a_server():
    mgr, _ = _manager()
    server = build_oauth_callback_server(mgr, port=9999)
    assert isinstance(server, OAuthCallbackServer)
