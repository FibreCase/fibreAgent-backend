"""Phase 4.x — the Google OAuth provider (endpoints, URL, token mapping).

The provider is the *only* Google-specific code. These tests pin its contract:
the authorization URL carries the right endpoint/params/state, the code and
refresh exchanges POST the right form body to the token endpoint, and the token
JSON is mapped with the "missing refresh token keeps the old one / missing
expiry means no known expiry" rules. All outbound HTTP is faked with
``httpx2.MockTransport`` — no network is ever touched, and the client secret is
asserted to travel only in the POST body, never in a URL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest

from fibrecase_agent_backend.mcp.auth.provider import GoogleOAuthProvider
from fibrecase_agent_backend.mcp.auth.models import OAuthProviderError

CLIENT_ID = "cid-123"
CLIENT_SECRET = "shh-secret"
SCOPES = ("https://www.googleapis.com/auth/calendar.readonly",)


def _provider(http_client=None):
    return GoogleOAuthProvider(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES,
        http_client=http_client,
    )


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------
def test_google_provider_requires_client_id():
    with pytest.raises(ValueError):
        GoogleOAuthProvider(client_id="", client_secret=CLIENT_SECRET, scopes=SCOPES)


def test_google_provider_requires_client_secret():
    with pytest.raises(ValueError):
        GoogleOAuthProvider(client_id=CLIENT_ID, client_secret="", scopes=SCOPES)


def test_google_provider_defaults_scope():
    p = GoogleOAuthProvider(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, scopes=())
    url = p.authorization_url(redirect_uri="https://ex.com/oauth/callback", state="s")
    qs = parse_qs(urlparse(url).query)
    assert qs["scope"] == ["https://www.googleapis.com/auth/calendar.readonly"]


# ---------------------------------------------------------------------------
# authorization URL
# ---------------------------------------------------------------------------
def test_authorization_url_is_pure_and_wellformed():
    p = _provider()
    url = p.authorization_url(redirect_uri="https://ex.com/oauth/callback", state="abc123")
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert parsed.path == "/o/oauth2/v2/auth"
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == [CLIENT_ID]
    assert qs["redirect_uri"] == ["https://ex.com/oauth/callback"]
    assert qs["response_type"] == ["code"]
    assert qs["state"] == ["abc123"]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["scope"] == [SCOPES[0]]
    # The client secret must **never** appear in the authorization URL.
    assert CLIENT_SECRET not in url


# ---------------------------------------------------------------------------
# code / refresh exchange (network faked)
# ---------------------------------------------------------------------------
def _token_response():
    return {
        "access_token": "AT-1",
        "refresh_token": "RT-1",
        "expires_in": 3600,
        "scope": SCOPES[0],
        "token_type": "Bearer",
    }


async def test_exchange_code_posts_grant_and_maps_tokens():
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx2.Response(200, json=_token_response())

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        p = _provider(http_client=client)
        tok = await p.exchange_code(code="CODE-1", redirect_uri="https://ex.com/oauth/callback")

    assert tok.access_token == "AT-1"
    assert tok.refresh_token == "RT-1"
    assert tok.scopes == SCOPES[0]
    assert isinstance(tok.expires_at, datetime)
    # The endpoint is Google's token endpoint, and the grant is authorization_code.
    assert captured["url"].startswith("https://oauth2.googleapis.com/token")
    body = parse_qs(captured["body"])
    assert body["code"] == ["CODE-1"]
    assert body["grant_type"] == ["authorization_code"]
    assert body["client_id"] == [CLIENT_ID]
    assert body["client_secret"] == [CLIENT_SECRET]
    assert body["redirect_uri"] == ["https://ex.com/oauth/callback"]
    # The secret travels in the body only — it is not in the request URL.
    assert CLIENT_SECRET not in captured["url"]


async def test_refresh_token_posts_grant_and_keeps_old_when_missing():
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["body"] = request.content.decode()
        # Google often omits refresh_token on a refresh; map to None (keep old).
        return httpx2.Response(200, json={"access_token": "AT-2", "expires_in": 3600})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        p = _provider(http_client=client)
        tok = await p.refresh_token(refresh_token="RT-OLD")

    assert tok.access_token == "AT-2"
    assert tok.refresh_token is None  # provider did not rotate → caller keeps old
    body = parse_qs(captured["body"])
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["RT-OLD"]
    assert body["client_secret"] == [CLIENT_SECRET]


async def test_exchange_code_http_error_raises_stable_code():
    def handler(request):
        return httpx2.Response(400, json={"error": "invalid_grant", "error_description": "secret-token-leak"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        p = _provider(http_client=client)
        with pytest.raises(OAuthProviderError) as exc:
            await p.exchange_code(code="CODE", redirect_uri="https://ex.com/oauth/callback")
    # The provider's body (which may echo a token) is never put in the message.
    assert "secret-token-leak" not in str(exc.value)
    assert "invalid_grant" not in str(exc.value)


async def test_exchange_code_malformed_response_raises():
    def handler(request):
        return httpx2.Response(200, text="not-json")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        p = _provider(http_client=client)
        with pytest.raises(OAuthProviderError):
            await p.exchange_code(code="CODE", redirect_uri="https://ex.com/oauth/callback")


def test_missing_access_token_is_malformed():
    from fibrecase_agent_backend.mcp.auth.provider import _token_response

    with pytest.raises(OAuthProviderError):
        _token_response("google", {"refresh_token": "RT"})


def test_missing_expires_in_yields_no_known_expiry():
    from fibrecase_agent_backend.mcp.auth.provider import _token_response

    tok = _token_response("google", {"access_token": "AT"})
    assert tok.access_token == "AT"
    assert tok.expires_at is None
    assert tok.refresh_token is None
