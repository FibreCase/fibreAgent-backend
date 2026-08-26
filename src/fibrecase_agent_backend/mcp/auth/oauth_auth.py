"""Per-request user-level OAuth token injection for MCP (phase 4.x).

The *minimal* MCP-client integration point the phase asks for: one
:class:`httpx2.Auth` object, built once per OAuth-configured MCP server and
passed to the server's http client (``auth=``). On **every** outgoing request
it:

1. reads the requesting principal from the :data:`.principal.active_principal`
   contextvar (set by the tool loop around each tool execution — the only
   place that knows *which* Telegram user is making the call),
2. resolves the numeric ``telegram_user_id`` from the opaque scope, and
3. asks the :class:`.manager.OAuthManager` for a **valid** access token for
   that (user, server) — auto-refreshing an expired one and persisting any
   rotated refresh token.

The token is attached as the request's ``Authorization: Bearer`` header. It is
never attached to a request made **outside** a tool execution (e.g. the
startup handshake — no principal, so no user token may ride on it), for a
principal that cannot be parsed, or for a user with no usable credential
(server then rejects the request, which the wrapper maps to its stable code).

The hook never logs a token, the ``Authorization`` header, the endpoint, or
the user id — only a stable code and the exception *class* on a failure. A
token-resolution failure must never break the request: the request goes out
without the user token and the server's rejection is the stable fallback.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx2

from .manager import OAuthManager
from .principal import active_principal, telegram_user_id_from_scope

logger = logging.getLogger("mcp.auth")


class McpOAuthAuth(httpx2.Auth):
    """Attach the *requesting user's* OAuth access token to each MCP request.

    ``manager`` is the phase-4.x :class:`OAuthManager` (it owns the credential
    storage and the provider's refresh logic); ``mcp_server`` is the configured
    server name this client talks to. Construct one per OAuth server; it holds
    no secrets of its own (the client id/secret live inside the provider the
    manager was given, in memory only).
    """

    def __init__(self, *, manager: OAuthManager, mcp_server: str) -> None:
        self._manager = manager
        self._mcp_server = mcp_server

    async def async_auth_flow(self, request: httpx2.Request) -> Any:
        """Resolve the user token (before dispatch) and attach it, then yield the
        request out. The client sends it and returns the response; there is
        nothing further to do with it, so the flow ends there.
        """
        token = await self._resolve_token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request

    async def _resolve_token(self) -> str | None:
        """The requesting user's valid access token, or ``None`` (no header)."""
        user_id = telegram_user_id_from_scope(active_principal.get())
        if user_id is None:
            # No principal (startup handshake) or a non-Telegram scope: never
            # attach a user token.
            return None
        try:
            return await self._manager.valid_access_token(
                telegram_user_id=user_id, mcp_server=self._mcp_server
            )
        except Exception:  # a credential-store/refresh failure must not break the request
            # Log only the stable code + exception class — never the user id,
            # the server URL, or any token.
            logger.warning(
                "mcp oauth token resolution failed",
                extra={"server": self._mcp_server, "code": "mcp_oauth_unavailable", "exception": "Exception"},
            )
            return None
