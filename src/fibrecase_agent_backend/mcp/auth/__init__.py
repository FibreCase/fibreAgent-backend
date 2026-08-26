"""User-level OAuth for MCP (phase 4.x).

A provider-agnostic OAuth 2.0 authorization-code layer that lets a Telegram
*user* connect a third-party account (first real consumer: the Google Calendar
MCP) so that *their* MCP server requests carry *their* access token. The
infrastructure here is deliberately provider-agnostic — the Google-specific
endpoints / scopes / client credentials live only in
:class:`.provider.GoogleOAuthProvider` — so a future GitHub / Microsoft provider
is a new subclass, not an ``if provider == "google"`` branch.

This subpackage stays **channel- and agent-agnostic**: it imports only the MCP
SDK's HTTP client (``httpx2``), the standard library, and (for the storage
implementation) SQLAlchemy via :mod:`..database.oauth`. It never imports
Telegram, the OpenAI SDK, or :class:`..agent.service.AgentService` — user
notification is an injected async hook, and the credential storage is an
injected interface.
"""

from __future__ import annotations

from .manager import OAuthManager
from .models import (
    AuthorizationOutcome,
    AuthorizationStatus,
    CredentialRecord,
    OAuthError,
    OAuthProviderError,
    PendingAuthorization,
    PendingAuthorizationRecord,
    TokenResponse,
)
from .oauth_auth import McpOAuthAuth
from .principal import active_principal, telegram_user_id_from_scope
from .provider import GoogleOAuthProvider, OAuthProvider
from .server import OAuthCallbackServer, build_oauth_callback_server
from .storage import OAuthStorage

__all__ = [
    "OAuthManager",
    "OAuthProvider",
    "GoogleOAuthProvider",
    "OAuthStorage",
    "OAuthError",
    "OAuthProviderError",
    "TokenResponse",
    "CredentialRecord",
    "PendingAuthorization",
    "PendingAuthorizationRecord",
    "AuthorizationOutcome",
    "AuthorizationStatus",
    "McpOAuthAuth",
    "OAuthCallbackServer",
    "build_oauth_callback_server",
    "active_principal",
    "telegram_user_id_from_scope",
]
