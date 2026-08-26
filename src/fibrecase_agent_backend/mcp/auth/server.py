"""The minimal OAuth callback HTTP server (phase 4.x).

The backend is normally *outbound-only* (Telegram long polling + LLM API) and
has no inbound HTTP. OAuth is the one exception: the provider (e.g. Google)
must be able to redirect the user's browser back to **this** backend at
``GET /oauth/callback``. So this module runs the *smallest* possible HTTP
server — a single ``GET /oauth/callback`` route — and **nothing else**: it does
not expose the agent, the conversation store, or any other endpoint (an unknown
path is a fixed 404).

It runs **inside the Telegram application's event loop** (started as a task in
the composition root's ``post_init``, stopped in ``post_shutdown``), so the
callback handler — and the user notification the *manager* triggers on success
— run on the same loop as the polling bot and can drive the bot's
``send_message`` directly (no cross-loop plumbing, no thread).

The server imports only ``starlette`` + ``uvicorn`` — both already present as
transitive dependencies of the MCP SDK — and the OAuth manager. It knows
nothing about Telegram types; the manager carries the notifier.

Security: the query parameters are passed to the manager verbatim; the manager
validates and **consumes** the single-use state and never echoes a token or a
full URL. This module therefore never logs the query string, the code, the
state, or any token — only the stable outcome status.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from .manager import AuthorizationStatus, OAuthManager

logger = logging.getLogger("mcp.auth")

#: The *only* route. Anything else is refused with a fixed, detail-free reply.
_CALLBACK_PATH = "/oauth/callback"

# Fixed, secret-free browser bodies per outcome status. The manager's
# ``AuthorizationOutcome`` titles/details are user-safe (no tokens/URLs), so
# they are shown verbatim in the browser; on success the manager *additionally*
# pushes a notification to Telegram.
_OUTCOME_FOOTERS: dict[str, str] = {
    AuthorizationStatus.SUCCESS: "You can return to Telegram.",
    AuthorizationStatus.DENIED: "Start again from Telegram with /mcp auth.",
    AuthorizationStatus.EXPIRED: "Start again from Telegram with /mcp auth.",
    AuthorizationStatus.INVALID: "Start again from Telegram with /mcp auth.",
    AuthorizationStatus.ERROR: "Try again from Telegram with /mcp auth.",
}


class OAuthCallbackServer:
    """A single-route HTTP server for ``GET /oauth/callback``.

    Construct with the :class:`OAuthManager` (which owns state validation,
    code exchange, credential storage, refresh, *and* the Telegram notifier)
    and a ``port``. :meth:`start` binds the listener and serves until
    :meth:`stop`; both are idempotent and never raise, so a callback-server
    failure can never take down the Telegram bot (OAuth simply becomes
    unavailable, and MCP servers needing a user token report not-authenticated
    instead).
    """

    def __init__(self, manager: OAuthManager, *, port: int = 8090) -> None:
        self._manager = manager
        self._port = port
        self._server: Any = None
        self._task: "asyncio.Task | None" = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Bind the port and serve in the running loop. Never raises."""
        if self._task is not None:
            return
        try:
            import uvicorn

            config = uvicorn.Config(
                self._build_app(),
                host="0.0.0.0",
                port=self._port,
                log_level="warning",  # access logs would carry the callback query
                access_log=False,
            )
            self._server = uvicorn.Server(config)
            self._task = asyncio.create_task(self._server.serve(), name="oauth-callback-server")
            await asyncio.sleep(0)  # let a bind failure surface as a dead task
            logger.info("oauth callback server listening", extra={"port": self._port})
        except Exception as exc:  # defensive: never take down the bot
            logger.error(
                "oauth callback server failed to start",
                extra={"exception": type(exc).__name__},
            )
            self._server = None
            self._task = None

    async def stop(self) -> None:
        """Shut the listener down. Idempotent, never raises."""
        if self._server is not None:
            self._server.should_exit = True
        task = self._task
        self._task = None
        self._server = None
        if task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=5.0)
        logger.info("oauth callback server stopped")

    # --------------------------------------------------------------- routes
    def _build_app(self) -> Starlette:
        async def callback(request: Request) -> Response:
            outcome = await self._manager.complete_authorization(dict(request.query_params))
            text = f"{outcome.title}\n\n{outcome.detail}"
            footer = _OUTCOME_FOOTERS.get(outcome.status, "")
            if footer and footer not in text:
                text = f"{text}\n\n{footer}"
            # Log only the stable status — never the query (code/state).
            logger.info("oauth callback completed", extra={"status": outcome.status})
            return PlainTextResponse(text, status_code=200)

        async def not_found(request: Request) -> Response:
            return PlainTextResponse("Not found", status_code=404)

        return Starlette(
            routes=[
                Route(_CALLBACK_PATH, callback, methods=["GET"]),
                Route("/{path:path}", not_found),
            ]
        )


def build_oauth_callback_server(manager: OAuthManager, *, port: int) -> OAuthCallbackServer:
    """The composition-root entry point: a callback server for one manager."""
    return OAuthCallbackServer(manager, port=port)
