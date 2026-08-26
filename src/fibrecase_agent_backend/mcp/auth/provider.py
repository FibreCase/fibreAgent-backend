"""Provider-agnostic OAuth 2.0 authorization-code provider interface.

The OAuth :mod:`.manager` knows **only** the :class:`OAuthProvider` contract
below — it contains no Google-specific logic. The Google-specific endpoints,
scopes, and client credentials live solely in
:class:`GoogleOAuthProvider` (and in the startup config that builds it), so a
future GitHub / Microsoft provider is a new subclass plus a new config block,
with **no** ``if provider == "google"`` branch anywhere in the codebase.

Only :mod:`httpx2` (already a transitive dependency of the MCP SDK) is used for
the outbound token requests — no new third-party dependency. A provider never
logs a token, a client secret, or a full request/response URL.
"""

from __future__ import annotations

import abc
import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx2

from .models import OAuthProviderError, TokenResponse, utcnow

logger = logging.getLogger("mcp.auth")

_TOKEN_TIMEOUT = httpx2.Timeout(30.0, connect=10.0)


class OAuthProvider(abc.ABC):
    """A minimal, provider-agnostic OAuth 2.0 authorization-code provider.

    The authorization *code* flow: the manager generates the ``state`` and the
    callback URL; the provider turns those plus its own fixed endpoints/scopes
    into an authorization URL, and later exchanges the returned code (or
    refreshes an access token) via its token endpoint.
    """

    #: Stable provider id (e.g. ``"google"``) — used to look up the provider.
    name: str = "provider"

    @abc.abstractmethod
    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        """Build the provider authorization URL for this ``state``.

        Pure (no network). The returned URL contains the ``state`` and the
        ``redirect_uri``; it is sent to the user as an OAuth login button. It is
        never logged in full by the manager (it embeds the state).
        """

    @abc.abstractmethod
    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenResponse:
        """Exchange an authorization code for a token.

        Raises :class:`OAuthProviderError` on any provider/transport failure
        (the message names only the failure kind — never a token or URL).
        """

    @abc.abstractmethod
    async def refresh_token(self, *, refresh_token: str) -> TokenResponse:
        """Refresh an access token.

        Raises :class:`OAuthProviderError` on failure. ``TokenResponse.
        refresh_token`` is ``None`` when the provider did not rotate the refresh
        token (the caller keeps the old one).
        """


async def _post_json(
    url: str,
    data: dict[str, str],
    *,
    client: httpx2.AsyncClient | None,
    provider: str,
) -> dict[str, Any]:
    """POST ``data`` (form-encoded) to the token endpoint and parse JSON.

    Shared by the concrete providers. On a non-2xx or malformed response it
    raises :class:`OAuthProviderError` with a fixed, non-echoing reason — the
    provider's body is **never** read into the message (it can echo an endpoint
    or a token).
    """
    if client is None:
        # No shared client (e.g. a unit test): open a short-lived one.
        async with httpx2.AsyncClient(timeout=_TOKEN_TIMEOUT, follow_redirects=True) as owned:
            return await _post_json(url, data, client=owned, provider=provider)
    try:
        response = await client.post(url, data=data)
    except httpx2.HTTPError as exc:
        raise OAuthProviderError(f"{provider} token request failed ({type(exc).__name__})") from None
    except Exception as exc:  # any other transport failure
        raise OAuthProviderError(f"{provider} token request failed ({type(exc).__name__})") from None
    if response.status_code >= 400:
        raise OAuthProviderError(f"{provider} token request rejected")
    try:
        payload = response.json()
    except Exception:
        raise OAuthProviderError(f"{provider} returned a malformed token response") from None
    if not isinstance(payload, dict):
        raise OAuthProviderError(f"{provider} returned a malformed token response")
    return payload


class GoogleOAuthProvider(OAuthProvider):
    """The Google OAuth 2.0 provider — the only one implemented this phase.

    Google-specific endpoints and the default scope live here; the client
    id/secret are read from config at construction and held **only in memory**
    (never logged or stored). A Google access token always carries an
    ``expires_in``, but a missing value is still tolerated (treated as no known
    expiry). The ``refresh_token`` is returned only on the first consent; a
    missing one maps to ``TokenResponse.refresh_token = None`` (keep the old).
    """

    name = "google"

    _AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
    _TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        scopes: tuple[str, ...] | list[str],
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        if not client_id:
            raise ValueError("Google OAuth client_id is required")
        if not client_secret:
            raise ValueError("Google OAuth client_secret is required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = tuple(scopes) if scopes else ("https://www.googleapis.com/auth/calendar.readonly",)
        # A shared, long-lived client (owned by the manager) or a short-lived
        # per-request client. Either way the secret travels only in the POST
        # body to Google's token endpoint — never in a header, never logged.
        self._http_client = http_client

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._scopes),
            "state": state,
            "access_type": "offline",  # request a refresh token on first consent
            "prompt": "consent",
        }
        return f"{self._AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> TokenResponse:
        payload = await _post_json(
            self._TOKEN_ENDPOINT,
            {
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            client=self._http_client,
            provider=self.name,
        )
        return _token_response(self.name, payload)

    async def refresh_token(self, *, refresh_token: str) -> TokenResponse:
        payload = await _post_json(
            self._TOKEN_ENDPOINT,
            {
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
            },
            client=self._http_client,
            provider=self.name,
        )
        return _token_response(self.name, payload)


def _token_response(provider: str, payload: dict[str, Any]) -> TokenResponse:
    """Map a provider token JSON object to :class:`TokenResponse`.

    A missing / non-string ``access_token`` is a malformed response — the
    provider's body is never echoed. A missing ``expires_in`` yields no known
    expiry; a missing ``refresh_token`` yields ``None`` (keep the old one).
    """
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthProviderError(f"{provider} returned a malformed token response")
    refresh_token = payload.get("refresh_token")
    refresh_token = refresh_token if isinstance(refresh_token, str) and refresh_token else None
    scopes = payload.get("scope")
    scopes = scopes if isinstance(scopes, str) and scopes else None
    expires_in = payload.get("expires_in")
    expires_at: datetime | None = None
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0:
        expires_at = utcnow() + timedelta(seconds=float(expires_in))
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=scopes,
    )
