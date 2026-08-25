"""Telegram adapter: authorization, handler wiring, and long-message chunking.

The handlers are exercised against **real** ``telegram.Update`` /
``CallbackContext`` objects (not hand-rolled fakes) so that wrong attribute
access is caught. No real Telegram/LLM network calls happen: ``Chat.send_message``
is patched at the class level and the agent service / repository are lightweight
fakes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from telegram import Chat, Message, Update, User
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CallbackContext

from fibrecase_agent_backend.telegram.bot import (
    CHUNK_SIZE,
    _is_authorized,
    cmd_help,
    cmd_new,
    cmd_start,
    cmd_status,
    compose_startup_hooks,
    handle_message,
    split_into_chunks,
)


# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------
class _StubBot:
    def __init__(self) -> None:
        self.actions = 0

    async def send_chat_action(self, chat_id, action):  # pragma: no cover - trivial
        self.actions += 1


def _make(user_id, chat_id, text=None, *, allowed=(1,)):
    """Build a real Update + CallbackContext (like PTB would deliver)."""
    user = None if user_id is None else User(id=user_id, first_name="U", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = (
        Message(message_id=1, date=0, chat=chat, from_user=user, text=text)
        if text is not None
        else None
    )
    update = Update(update_id=1, message=message)

    bot = _StubBot()
    app = type("App", (), {})()
    app.bot_data = {"allowed_user_ids": set(allowed)}
    app.bot = bot
    context = CallbackContext.from_update(update, app)
    return update, chat, context, app, bot


class _FakeRepo:
    def __init__(self, conv_id=5, messages=0, exists=True):
        self._conv_id = conv_id
        self._messages = messages
        self._exists = exists
        self.created = []

    async def get_conversation(self, chat_id):
        return type("C", (), {"id": self._conv_id})() if self._exists else None

    async def get_or_create_conversation(self, chat_id, user_id):
        self.created.append((chat_id, user_id))
        return type("C", (), {"id": self._conv_id})()

    async def count_messages(self, conv_id):
        return self._messages


class _FakeService:
    def __init__(self, reply="ok", delay=0.0):
        self.reply = reply
        self.delay = delay
        self.calls = []
        self.reset_calls = []

    async def process_message(self, conv_id, text):
        self.calls.append((conv_id, text))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.reply

    async def conversation_status(self, conv_id):
        return {"conversation_id": conv_id, "messages": 2}

    async def reset(self, chat_id, user_id):
        self.reset_calls.append((chat_id, user_id))
        return 99


class _FakeConfig:
    openai_model = "test-model"
    max_image_size_bytes = 10_000_000


# ---------------------------------------------------------------------------
# authorization (regression: must read update.effective_user, not context.user)
# ---------------------------------------------------------------------------
def test_is_authorized_accepts_allowed_user():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hi", allowed=(1, 2))
    assert _is_authorized(update, context) is True


def test_is_authorized_rejects_unknown_user():
    update, chat, context, app, bot = _make(user_id=999, chat_id=1, text="hi", allowed=(1,))
    assert _is_authorized(update, context) is False


def test_is_authorized_rejects_unknown_sender():
    update, chat, context, app, bot = _make(user_id=None, chat_id=1, text="hi", allowed=(1,))
    assert _is_authorized(update, context) is False


# ---------------------------------------------------------------------------
# authorized handlers (real Update/CallbackContext end-to-end, no network)
# ---------------------------------------------------------------------------
async def test_unauthorized_message_sends_nothing():
    update, chat, context, app, bot = _make(user_id=999, chat_id=1, text="hi", allowed=(1,))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await handle_message(update, context)
    send.assert_not_awaited()
    assert bot.actions == 0  # no typing either


async def test_unauthorized_command_sends_nothing():
    update, chat, context, app, bot = _make(user_id=999, chat_id=1, text="/start", allowed=(1,))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_start(update, context)
    send.assert_not_awaited()


async def test_cmd_start_creates_and_replies():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/start", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo(exists=False)  # no conversation yet -> create path
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_start(update, context)
    send.assert_awaited_once()
    assert "Agent 已启动" in send.await_args.kwargs["text"]


async def test_handle_message_returns_llm_reply_and_typing():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hello", allowed=(1,))
    service = _FakeService(reply="hi back", delay=0.1)  # long enough for typing to fire
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = service
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await handle_message(update, context)
    send.assert_awaited_once()
    assert send.await_args.kwargs["text"] == "hi back"
    # The handler normalised the Telegram text into an AgentMessage and passed it on.
    conv_id, agent_message = service.calls[0]
    assert conv_id == 5
    assert agent_message.text == "hello"
    assert bot.actions >= 1  # typing keep-alive fired


async def test_handle_message_surfaces_user_safe_llm_error():
    from fibrecase_agent_backend.agent.service import AgentError

    class _ErrorService(_FakeService):
        async def process_message(self, conv_id, text):
            raise AgentError("模型请求超时，请稍后重试。", "timeout")

    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hello", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = _ErrorService()
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await handle_message(update, context)
    send.assert_awaited_once()
    assert "超时" in send.await_args.kwargs["text"]


async def test_cmd_status_reports_conversation():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/status", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo(conv_id=7, messages=3)
    app.bot_data["agent_service"] = _FakeService()
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_status(update, context)
    sent = send.await_args.kwargs["text"]
    assert "Status: OK" in sent and "test-model" in sent


async def test_cmd_new_resets_and_confirms():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/new", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_new(update, context)
    assert service.reset_calls == [(1, 1)]  # reset triggered for this chat/user
    assert "新的会话" in send.await_args.kwargs["text"]


async def test_cmd_new_unauthorized_noop():
    update, chat, context, app, bot = _make(user_id=999, chat_id=1, text="/new", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_new(update, context)
    send.assert_not_awaited()
    assert service.reset_calls == []


async def test_cmd_help_lists_commands():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/help", allowed=(1,))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_help(update, context)
    sent = send.await_args.kwargs["text"]
    for cmd in ("/start", "/new", "/status", "/help"):
        assert cmd in sent


# ---------------------------------------------------------------------------
# startup-hook composition (main.py chains the command-menu + DB-init hooks)
# ---------------------------------------------------------------------------
async def test_compose_startup_hooks_runs_all_in_order():
    calls = []

    async def h1(application):
        calls.append("h1")

    async def h2(application):
        calls.append("h2")

    chained = compose_startup_hooks(h1, None, h2)  # None must be skipped
    await chained(object())
    assert calls == ["h1", "h2"]


async def test_compose_startup_hooks_with_none_is_noop():
    chained = compose_startup_hooks()
    await chained(object())  # should not raise


# ---------------------------------------------------------------------------
# markdown rendering of model replies (parse_mode=HTML + plain fallback)
# ---------------------------------------------------------------------------
async def test_handle_message_renders_reply_as_html():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hi", allowed=(1,))
    service = _FakeService(reply="这是 **加粗** 与 `代码`")
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = service
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await handle_message(update, context)
    send.assert_awaited_once()
    assert send.await_args.kwargs["parse_mode"] == ParseMode.HTML
    assert send.await_args.kwargs["text"] == "这是 <b>加粗</b> 与 <code>代码</code>"


async def test_handle_message_falls_back_to_plain_on_bad_request():
    # Telegram rejects the HTML (400 "can't parse entities"); the chunk must be
    # re-sent as plain text so the reply is never lost.
    def _side_effect(text, **kwargs):
        if kwargs.get("parse_mode") == ParseMode.HTML:
            raise BadRequest("Can't parse entities")
        return "sent"

    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hi", allowed=(1,))
    service = _FakeService(reply="**bold** `code`")
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = service
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock, side_effect=_side_effect) as send:
        await handle_message(update, context)
    assert send.await_count == 2  # HTML attempt, then plain fallback
    second = send.await_args_list[1].kwargs
    assert second.get("parse_mode") in (None, "Text")  # not HTML
    assert second["text"] == "**bold** `code`"  # original markdown, verbatim


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------
def test_short_message_single_chunk():
    assert split_into_chunks("hi") == ["hi"]


def test_chunks_preserve_all_content():
    text = "line\n" * 500 + "tail"
    chunks = split_into_chunks(text, limit=200)
    assert "".join(chunks) == text, "chunking must never lose or add content"
    assert all(len(c) <= 200 for c in chunks)


def test_default_limit_under_telegram_cap():
    assert CHUNK_SIZE <= 4096


def test_hard_split_of_huge_line():
    text = "x" * 10_000  # one enormous line, no newlines
    chunks = split_into_chunks(text, limit=400)
    assert "".join(chunks) == text
    assert all(len(c) <= 400 for c in chunks)


def test_prefers_newline_boundaries():
    text = ("a\n" * 300)  # many short lines
    chunks = split_into_chunks(text, limit=100)
    assert "".join(chunks) == text
    for c in chunks[:-1]:
        assert c.endswith("\n"), "chunks should break on newlines where possible"
