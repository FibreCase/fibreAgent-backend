"""The /mcp_status Telegram command (phase 4 — required #14).

Handler-level, with a fake in-memory :class:`McpManager`-shaped object and a real
PTB :class:`Update`/``CallbackContext``. Proves the command is read-only and
non-mutating (no connect / LLM / MCP call), shows a safe disabled state, renders
available/unavailable servers + the total as HTML, is silently ignored for an
unauthorised sender, and never leaks a URL / host / token / description.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from telegram import Chat

from fibrecase_agent_backend.telegram.bot import _COMMANDS, cmd_mcp_status


class _FakeConfig:
    def __init__(self, enable_tools=True):
        self.enable_tools = enable_tools


class _FakeManager:
    def __init__(self, status=None, total=0, length=0):
        self._status = status or []
        self.total_tools = total
        self._len = length

    def status(self):
        return self._status

    def __len__(self):
        return self._len


def _make(user_id, chat_id, text="/mcp_status", *, allowed=(1,)):
    from telegram import Message, Update, User
    from telegram.ext import CallbackContext

    user = None if user_id is None else User(id=user_id, first_name="U", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(message_id=1, date=0, chat=chat, from_user=user, text=text) if text is not None else None
    update = Update(update_id=1, message=message)
    app = type("App", (), {})()
    app.bot_data = {"allowed_user_ids": set(allowed), "config": _FakeConfig()}
    app.bot = object()
    context = CallbackContext.from_update(update, app)
    return update, chat, context, app


def _manager(app, **kw):
    m = _FakeManager(**kw)
    app.bot_data["mcp_manager"] = m
    return m


# ---------------------------------------------------------------------------
# unauthorized → silent
# ---------------------------------------------------------------------------
async def test_mcp_status_unauthorized_is_silent():
    update, chat, context, app = _make(user_id=999, chat_id=1, allowed=(1,))
    _manager(app, status=[{"name": "alpha", "available": True, "tool_count": 2}])
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp_status(update, context)
    send.assert_not_awaited()


# ---------------------------------------------------------------------------
# disabled: no manager / tools off / zero servers
# ---------------------------------------------------------------------------
async def test_mcp_status_no_manager_is_disabled():
    update, chat, context, app = _make(user_id=1, chat_id=1)
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp_status(update, context)
    assert send.await_count == 1
    assert "disabled" in send.await_args.kwargs["text"]


async def test_mcp_status_tools_disabled_is_disabled():
    update, chat, context, app = _make(user_id=1, chat_id=1)
    app.bot_data["config"] = _FakeConfig(enable_tools=False)
    _manager(app, status=[{"name": "alpha", "available": True, "tool_count": 2}], total=2, length=1)
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp_status(update, context)
    assert "disabled" in send.await_args.kwargs["text"]


async def test_mcp_status_zero_servers_is_disabled():
    update, chat, context, app = _make(user_id=1, chat_id=1)
    _manager(app, status=[], total=0, length=0)
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp_status(update, context)
    assert "disabled" in send.await_args.kwargs["text"]


# ---------------------------------------------------------------------------
# available / unavailable / total render as HTML
# ---------------------------------------------------------------------------
async def test_mcp_status_renders_servers_as_html():
    update, chat, context, app = _make(user_id=1, chat_id=1)
    _manager(
        app,
        status=[
            {"name": "alpha", "available": True, "tool_count": 2},
            {"name": "beta", "available": False, "tool_count": 0},
        ],
        total=2,
        length=2,
    )
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp_status(update, context)
    assert send.await_count == 1
    kwargs = send.await_args.kwargs
    assert kwargs.get("parse_mode") == "HTML"
    text = kwargs["text"]
    assert "alpha" in text and "beta" in text
    assert "available" in text and "unavailable" in text
    assert "Total MCP tools available" in text


async def test_mcp_status_does_not_connect_or_call_llm():
    # The command is read-only: it must not touch an LLM, a repository, or the
    # manager's network surface — only .status()/.total_tools.
    update, chat, context, app = _make(user_id=1, chat_id=1)

    class _Spy(_FakeManager):
        def start(self, *a, **k):
            raise AssertionError("must not (re)start discovery")

        def close(self, *a, **k):
            raise AssertionError("must not close")

    mgr = _Spy(status=[{"name": "alpha", "available": True, "tool_count": 1}], total=1, length=1)
    app.bot_data["mcp_manager"] = mgr
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp_status(update, context)
    assert send.await_count == 1


# ---------------------------------------------------------------------------
# long output still delivered (chunked) without loss
# ---------------------------------------------------------------------------
async def test_mcp_status_many_servers_delivered():
    update, chat, context, app = _make(user_id=1, chat_id=1)
    status = [{"name": f"srv{i:02d}", "available": True, "tool_count": i} for i in range(30)]
    total = sum(s["tool_count"] for s in status)
    _manager(app, status=status, total=total, length=len(status))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp_status(update, context)
    # Every server name is present across all chunks (nothing lost).
    joined = "".join(c.kwargs["text"] for c in send.await_args_list)
    for s in status:
        assert s["name"] in joined


# ---------------------------------------------------------------------------
# output is secret-free: no URL / host / token / description
# ---------------------------------------------------------------------------
async def test_mcp_status_output_is_secret_free():
    update, chat, context, app = _make(user_id=1, chat_id=1)
    _manager(app, status=[{"name": "alpha", "available": True, "tool_count": 2}], total=2, length=1)
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_mcp_status(update, context)
    text = send.await_args.kwargs["text"]
    # Only name / state / count — no URL, host, token, or description.
    assert "example" not in text
    assert "Bearer" not in text
    assert "token" not in text.lower()
    assert "secret" not in text.lower()
    # No raw scope / user id.
    assert "telegram:1" not in text


# ---------------------------------------------------------------------------
# the command is part of the advertised menu + /help
# ---------------------------------------------------------------------------
def test_mcp_status_is_in_command_menu():
    assert ("mcp_status", "Show remote MCP tool status") in _COMMANDS
