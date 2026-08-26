"""The *requesting principal* for in-flight tool/MCP calls (phase 4.x).

A user-level OAuth credential is bound to a **Telegram user**
(``telegram:<user_id>``). When the tool loop executes a tool on behalf of one
principal, the MCP transport must be able to attach *that* principal's access
token to the outgoing request — but the transport (the MCP SDK's http client)
is created once, at startup, and is shared by every conversation. The bridge is
a :data:`contextvars.ContextVar`: the tool loop sets it around each tool
execution (it is the only place that knows the requesting ``scope``), and the
per-server OAuth :class:`httpx2.Auth` hook reads it when a request goes out.

This module is deliberately **pure stdlib** — no Telegram, no OpenAI SDK, no
SQLAlchemy — so it sits at the boundary without coupling either side. The value
is the *opaque* scope string (e.g. ``"telegram:12345"``); it is never logged,
and the helpers here only ever extract the trailing user id as an ``int`` when
a caller explicitly asks for it.
"""

from __future__ import annotations

from contextvars import ContextVar

#: The opaque principal scope for the tool call currently being executed
#: (e.g. ``"telegram:12345"``), or ``None`` outside a tool execution (e.g. a
#: startup-time MCP handshake, which has no principal and must not carry a
#: user token).
active_principal: ContextVar[str | None] = ContextVar("mcp_active_principal", default=None)


def telegram_user_id_from_scope(scope: str | None) -> "int | None":
    """The numeric Telegram user id encoded in an opaque scope, or ``None``.

    The scope form is exactly ``telegram:<int user id>`` (built in one place in
    the Telegram adapter). Anything else — ``None``, a different channel prefix,
    a non-numeric id — yields ``None`` so a malformed scope can never be passed
    to a credential lookup (which would simply find nothing).
    """
    if not scope:
        return None
    prefix, sep, rest = scope.partition(":")
    if prefix != "telegram" or not sep or not rest.isdigit():
        return None
    return int(rest)
