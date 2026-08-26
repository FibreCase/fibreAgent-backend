"""Phase 4.x — the /mcp command (status view + ``auth <server>`` login flow).

Handler-level, with a real PTB :class:`Update`/``CallbackContext`` and fake
in-memory manager-shaped objects. Proves: bare ``/mcp`` is read-only, renders
per-user OAuth state (connected / authentication required / not configured) for
OAuth servers plus plain availability for the rest, degrades safely when a
status lookup fails, shows a disabled state, is silent for an unauthorised
sender, and leaks no token/URL/state; ``/mcp auth <server>`` (the *same* command
with an ``auth`` argument — there is no separate ``mcp_auth`` command) sends
the inline **URL button** (never a bare URL for the user to copy), requires the
server argument, reports "not configured" / the stable user-safe error
otherwise, and never crashes on an initiate failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from telegram.ext import CallbackContext

from fibrecase_agent_backend.config import McpServer
from fibrecase_agent_backend.mcp.auth.models import OAuthError, PendingAuthorization
from fibrecase_agent_backend.telegram.bot import _COMMANDS, cmd_mcp


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _FakeConfig:
    def __init__(self, enable_tools=True, mcp_servers=()):
        self.enable_tools = enable_tools
        self.mcp_servers = list(mcp_servers)


class _FakeManager:
    def __init__(self, status=()):
        self._status = list(status)

    def status(self):
        return self._status

    def __len__(self):
        return len(self._status)


class _FakeOAuthManager:
    """A manager-shaped fake: per-user status + a scripted initiate."""

    def __init__(self, *, states=None, init_result=None, init_error=None):
        self._states = states or {}
        self._init_result = init_result
        self._init_error = init_error
        self.initiate_calls: list[dict] = []

    async def oauth_status(self, *, telegram_user_id, mcp_server):
        return self._states.get((telegram_user_id, mcp_server), "authentication_required")

    async def initiate(self, *, telegram_user_id, chat_id, mcp_server):
        self.initiate_calls.append(
            {"telegram_user_id": telegram_user_id, "chat_id": chat_id, "mcp_server": mcp_server}
        )
        if self._init_error is not None:
            raise self._init_error
        return self._init_result


def _make(user_id, chat_id, text="/mcp", *, allowed=(1,)):
    user = None if user_id is None else User(id=user_id, first_name="U", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(message_id=1, date=0, chat=chat, from_user=user, text=text) if text is not None else None
    update = Update(update_id=1, message=message)
    app = type("App", (), {})()
    app.bot_data = {"allowed_user_ids": set(allowed), "config": _FakeConfig()}
    app.bot = object()
    context = CallbackContext.from_update(update, app)
    return update, chat, context, app


def _pending():
    return PendingAuthorization(
        state="st-abc",
        telegram_user_id=1,
        chat_id=7,
        provider="google",
        mcp_server="gcal",
        authorization_url="https://accounts.google.com/o/oauth2/v2/auth?state=st-abc",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        expires_in_seconds=600,
    )


OAUTH_SERVER = McpServer(name="gcal", url="https://g.example/mcp", auth_type="oauth", auth_provider="google")
PLAIN_SERVER = McpServer(name="alpha", url="https://a.example/mcp")


# ---------------------------------------------------------------------------
# /mcp (bare) — the status view
# ---------------------------------------------------------------------------
async def test_mcp_unauthorized_is_silent():
    update, chat, context, app = _make(user_id=999, chat_id=1, allowed=(1,))
    app.bot_data["config"] = _FakeConfig(mcp_servers=[PLAIN_SERVER])
    app.bot_data["mcp_manager"] = _FakeManager([{"name": "alpha", "available": True, "tool_count": 2}])
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    send.assert_not_awaited()


async def test_mcp_no_manager_is_disabled():
    update, chat, context, app = _make(user_id=1, chat_id=1)
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    assert send.await_count == 1
    assert "disabled" in send.await_args.kwargs["text"]


async def test_mcp_renders_per_user_oauth_states():
    update, chat, context, app = _make(user_id=1, chat_id=1)
    app.bot_data["config"] = _FakeConfig(mcp_servers=[OAUTH_SERVER, PLAIN_SERVER])
    app.bot_data["mcp_manager"] = _FakeManager(
        [
            {"name": "gcal", "available": True, "tool_count": 1},
            {"name": "alpha", "available": True, "tool_count": 2},
        ]
    )
    app.bot_data["oauth_manager"] = _FakeOAuthManager(states={(1, "gcal"): "connected"})
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    assert send.await_count == 1
    kwargs = send.await_args.kwargs
    assert kwargs.get("parse_mode") == "HTML"
    text = kwargs["text"]
    # The caller's own connection state for the OAuth server…
    assert "gcal" in text and "connected" in text
    # …and plain availability for the non-OAuth one.
    assert "alpha" in text and "available" in text


async def test_mcp_authentication_required_points_at_command():
    update, chat, context, app = _make(user_id=1, chat_id=1)
    app.bot_data["config"] = _FakeConfig(mcp_servers=[OAUTH_SERVER])
    app.bot_data["mcp_manager"] = _FakeManager([{"name": "gcal", "available": True, "tool_count": 1}])
    app.bot_data["oauth_manager"] = _FakeOAuthManager()  # default: authentication_required
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    text = send.await_args.kwargs["text"]
    assert "authentication required" in text
    assert "/mcp auth gcal" in text


async def test_mcp_unavailable_server_shows_unavailable():
    update, chat, context, app = _make(user_id=1, chat_id=1)
    app.bot_data["config"] = _FakeConfig(mcp_servers=[OAUTH_SERVER])
    app.bot_data["mcp_manager"] = _FakeManager([{"name": "gcal", "available": False, "tool_count": 0}])
    app.bot_data["oauth_manager"] = _FakeOAuthManager(states={(1, "gcal"): "connected"})
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    text = send.await_args.kwargs["text"]
    assert "unavailable" in text
    # An unavailable server shows no per-user OAuth state at all.
    assert "connected" not in text


async def test_mcp_status_lookup_failure_degrades_to_required():
    class _BoomOAuth:
        async def oauth_status(self, **_kw):
            raise RuntimeError("db down")

    update, chat, context, app = _make(user_id=1, chat_id=1)
    app.bot_data["config"] = _FakeConfig(mcp_servers=[OAUTH_SERVER])
    app.bot_data["mcp_manager"] = _FakeManager([{"name": "gcal", "available": True, "tool_count": 1}])
    app.bot_data["oauth_manager"] = _BoomOAuth()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    assert "authentication required" in send.await_args.kwargs["text"]


async def test_mcp_does_not_show_another_users_state():
    # Both users are allow-listed; only user 1 is connected. User 2 must see
    # "required" for *their own* state — never user 1's "connected".
    update, chat, context, app = _make(user_id=2, chat_id=1, allowed=(1, 2))
    app.bot_data["config"] = _FakeConfig(mcp_servers=[OAUTH_SERVER])
    app.bot_data["mcp_manager"] = _FakeManager([{"name": "gcal", "available": True, "tool_count": 1}])
    app.bot_data["oauth_manager"] = _FakeOAuthManager(states={(1, "gcal"): "connected"})
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    text = send.await_args.kwargs["text"]
    assert "connected" not in text
    assert "authentication required" in text


async def test_mcp_output_is_secret_free():
    update, chat, context, app = _make(user_id=1, chat_id=1)
    app.bot_data["config"] = _FakeConfig(mcp_servers=[OAUTH_SERVER])
    app.bot_data["mcp_manager"] = _FakeManager([{"name": "gcal", "available": True, "tool_count": 1}])
    app.bot_data["oauth_manager"] = _FakeOAuthManager()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    text = send.await_args.kwargs["text"]
    assert "example" not in text  # no URL/host
    assert "Bearer" not in text
    assert "token" not in text.lower()
    assert "secret" not in text.lower()
    assert "telegram:1" not in text


# ---------------------------------------------------------------------------
# /mcp auth <server> — the login flow (same command, an ``auth`` argument)
# ---------------------------------------------------------------------------
async def test_mcp_auth_unauthorized_is_silent():
    update, chat, context, app = _make(user_id=999, chat_id=1, text="/mcp auth gcal", allowed=(1,))
    app.bot_data["oauth_manager"] = _FakeOAuthManager(init_result=_pending())
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    send.assert_not_awaited()


async def test_mcp_auth_without_server_is_usage():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/mcp auth")
    app.bot_data["oauth_manager"] = _FakeOAuthManager(init_result=_pending())
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    assert send.await_count == 1
    assert "Usage" in send.await_args.kwargs["text"]


async def test_mcp_auth_not_configured():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/mcp auth gcal")
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    assert "not configured" in send.await_args.kwargs["text"].lower()


async def test_mcp_auth_non_oauth_server_gets_stable_safe_message():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/mcp auth alpha")
    app.bot_data["oauth_manager"] = _FakeOAuthManager(init_error=OAuthError(
        "mcp_server_not_oauth", "'alpha' does not require (or support) OAuth authentication."
    ))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    text = send.await_args.kwargs["text"]
    assert "alpha" in text
    assert "OAuth" in text


async def test_mcp_auth_happy_path_sends_url_button():
    update, chat, context, app = _make(user_id=1, chat_id=7, text="/mcp auth gcal")
    oam = _FakeOAuthManager(init_result=_pending())
    app.bot_data["oauth_manager"] = oam
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)

    # The flow started for this user + chat + server (credential binds to the user).
    assert oam.initiate_calls == [{"telegram_user_id": 1, "chat_id": 7, "mcp_server": "gcal"}]
    # Two sends: the HTML prompt with the button, then the expiry note.
    assert send.await_count == 2
    prompt = send.await_args_list[0].kwargs
    assert prompt.get("parse_mode") == "HTML"
    assert "gcal" in prompt["text"]
    markup = prompt["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    button = markup.inline_keyboard[0][0]
    assert isinstance(button, InlineKeyboardButton)
    assert button.url == "https://accounts.google.com/o/oauth2/v2/auth?state=st-abc"
    # The user never has to copy a URL: the plain body does not contain it.
    assert "accounts.google.com" not in prompt["text"]
    note = send.await_args_list[1].kwargs["text"]
    assert "expires" in note.lower()


async def test_mcp_auth_initiate_crash_is_user_safe():
    update, chat, context, app = _make(user_id=1, chat_id=7, text="/mcp auth gcal")
    app.bot_data["oauth_manager"] = _FakeOAuthManager(init_error=RuntimeError("db exploded: SECRET"))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    text = send.await_args.kwargs["text"]
    assert "try again" in text.lower()
    assert "SECRET" not in text


async def test_bare_mcp_with_other_arguments_still_shows_status():
    # A stray argument that is not ``auth`` must not start a flow.
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/mcp gcal")
    app.bot_data["config"] = _FakeConfig(mcp_servers=[PLAIN_SERVER])
    app.bot_data["mcp_manager"] = _FakeManager([{"name": "alpha", "available": True, "tool_count": 2}])
    oam = _FakeOAuthManager()
    app.bot_data["oauth_manager"] = oam
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp(update, context)
    assert "alpha" in send.await_args.kwargs["text"]
    assert oam.initiate_calls == []


# ---------------------------------------------------------------------------
# the commands are in the advertised menu
# ---------------------------------------------------------------------------
def test_mcp_commands_are_in_menu():
    names = {name for name, _desc in _COMMANDS}
    assert "mcp" in names
    assert "mcp_status" in names
    # ``mcp_auth`` is not a command — it is an argument to ``/mcp``.
    assert "mcp_auth" not in names
