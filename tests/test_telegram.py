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

from fibrecase_agent_backend import __version__
from fibrecase_agent_backend.telegram.bot import (
    CHUNK_SIZE,
    _is_authorized,
    cmd_context,
    cmd_forget,
    cmd_help,
    cmd_memories,
    cmd_new,
    cmd_remember,
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
        # Phase 2.5 memory command state (mutable, in-memory).
        self._memories = {}
        self._mem_seq = 0
        self.remember_calls = []
        self.list_calls = []
        self.forget_calls = []
        self.forget_all_calls = []

    async def process_message(self, conv_id, text, *, memory_scope=None):
        self.calls.append((conv_id, text))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.reply

    async def conversation_status(self, conv_id):
        return {"conversation_id": conv_id, "messages": 2}

    async def context_status(self, conv_id):
        return {
            "conversation_id": conv_id,
            "cap": 50,
            "budget": 24000,
            "image_cost": 2000,
            "stored_messages": 12,
            "history_messages": 10,
            "estimated_cost": 1200,
            "system_cost": 200,
            "images_kept": 3,
            "images_in_store": 5,
        }

    async def reset(self, chat_id, user_id):
        self.reset_calls.append((chat_id, user_id))
        return 99

    # ---- memory command methods (mirrors AgentService) ----
    async def remember_memory(self, scope, content):
        self.remember_calls.append((scope, content))
        content = content.strip()
        if not content:
            from fibrecase_agent_backend.agent.service import AgentError, _user_safe_for

            raise AgentError(_user_safe_for("memory_invalid"), "memory_invalid")
        self._mem_seq += 1
        rec = _FakeMemoryRecord(self._mem_seq, content)
        self._memories[rec.id] = rec
        return rec

    async def list_memories(self, scope):
        self.list_calls.append(scope)
        return sorted(self._memories.values(), key=lambda r: r.id)

    async def forget_memory(self, scope, memory_id):
        self.forget_calls.append((scope, memory_id))
        if memory_id not in self._memories:
            from fibrecase_agent_backend.agent.service import AgentError, _user_safe_for

            raise AgentError(_user_safe_for("memory_not_found"), "memory_not_found")
        del self._memories[memory_id]

    async def forget_all_memories(self, scope):
        self.forget_all_calls.append(scope)
        n = len(self._memories)
        self._memories.clear()
        return n


class _FakeMemoryRecord:
    def __init__(self, id, content):
        from datetime import datetime, timezone

        self.id = id
        self.content = content
        self.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.updated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)

    def __eq__(self, other):
        return (
            isinstance(other, _FakeMemoryRecord)
            and self.id == other.id
            and self.content == other.content
        )

    def __hash__(self):
        return hash((self.id, self.content))


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
    assert "<b>Agent started.</b>" in send.await_args.kwargs["text"]


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
        async def process_message(self, conv_id, text, *, memory_scope=None):
            raise AgentError("模型请求超时，请稍后重试。", "timeout")

    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hello", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = _ErrorService()
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await handle_message(update, context)
    send.assert_awaited_once()
    assert "超时" in send.await_args.kwargs["text"]


async def test_cmd_context_reports_window_and_downgrade():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/context", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo(conv_id=7)
    app.bot_data["agent_service"] = _FakeService()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_context(update, context)
    sent = send.await_args.kwargs["text"]
    assert "<b>Context:</b>" in sent and "<b>Conversation:</b> 7" in sent
    # Budget + free-space arithmetic (estimated, not exact tokens).
    assert "<b>Estimated budget:</b> 24000" in sent
    assert "<b>Free:</b> ~22800 units" in sent
    assert "<b>Kept this turn:</b> 10" in sent
    # Image downgrade is surfaced when some stored images won't fit.
    assert "<b>History images kept:</b> 3 / 5 (2 downgraded to text)" in sent
    assert "Conservative estimate, not exact tokens" in sent


async def test_cmd_context_no_conversation_is_safe():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/context", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo(exists=False)
    app.bot_data["agent_service"] = _FakeService()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_context(update, context)
    send.assert_awaited_once()
    assert "No conversation yet" in send.await_args.kwargs["text"]


async def test_cmd_context_unauthorized_noop():
    update, chat, context, app, bot = _make(user_id=999, chat_id=1, text="/context", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo(conv_id=7)
    app.bot_data["agent_service"] = _FakeService()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_context(update, context)
    send.assert_not_awaited()


async def test_cmd_status_reports_conversation():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/status", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo(conv_id=7, messages=3)
    app.bot_data["agent_service"] = _FakeService()
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_status(update, context)
    sent = send.await_args.kwargs["text"]
    assert "<b>Status:</b> OK" in sent and "test-model" in sent
    # /status reports the backend version.
    assert f"<b>Version:</b> {__version__}" in sent


async def test_cmd_new_resets_and_confirms():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/new", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_new(update, context)
    assert service.reset_calls == [(1, 1)]  # reset triggered for this chat/user
    assert "<b>New conversation started</b>" in send.await_args.kwargs["text"]


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
    for cmd in ("/start", "/new", "/status", "/help", "/remember", "/memories", "/forget", "/tool_audit"):
        assert cmd in sent


# ---------------------------------------------------------------------------
# long-term memory commands (phase 2.5)
# ---------------------------------------------------------------------------
async def test_cmd_remember_saves_and_reports_id():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/remember 我偏好中文回答。", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_remember(update, context)
    # Authenticated + calls the right service method with the opaque scope.
    assert service.remember_calls == [("telegram:1", "我偏好中文回答。")]
    send.assert_awaited_once()
    text = send.await_args.kwargs["text"]
    assert "Memory saved" in text and "我偏好中文回答。" in text
    # No LLM was involved in a command.
    assert service.calls == []


async def test_cmd_remember_empty_is_invalid_and_no_write():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/remember", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_remember(update, context)
    # Empty content → service rejects, nothing stored.
    assert service._memories == {}
    assert "empty or too long" in send.await_args.kwargs["text"].lower()


async def test_cmd_remember_unauthorized_noop():
    update, chat, context, app, bot = _make(user_id=999, chat_id=1, text="/remember x", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_remember(update, context)
    send.assert_not_awaited()
    assert service.remember_calls == []


async def test_cmd_memories_lists_own_memories():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/memories", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    service._mem_seq = 3
    service._memories = {1: _FakeMemoryRecord(1, "fact one"), 3: _FakeMemoryRecord(3, "fact three")}
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_memories(update, context)
    # Scoped to this user.
    assert service.list_calls == ["telegram:1"]
    text = send.await_args.kwargs["text"]
    assert "2 total" in text
    assert "fact one" in text and "fact three" in text
    assert "#1" in text and "#3" in text


async def test_cmd_memories_empty_state():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/memories", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_memories(update, context)
    assert "No memories saved yet" in send.await_args.kwargs["text"]


async def test_cmd_forget_id_deletes():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/forget 2", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    service._memories = {2: _FakeMemoryRecord(2, "to forget")}
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_forget(update, context)
    assert service.forget_calls == [("telegram:1", 2)]
    assert service._memories == {}
    assert "Memory deleted" in send.await_args.kwargs["text"]


async def test_cmd_forget_missing_id_safe_not_found():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/forget 999", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_forget(update, context)
    assert "Memory not found" in send.await_args.kwargs["text"]


async def test_cmd_forget_all_requires_confirmation():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/forget all", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    service._memories = {1: _FakeMemoryRecord(1, "x")}
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_forget(update, context)
    # No CONFIRM → nothing deleted, only the confirmation format is shown.
    assert service._memories == {1: _FakeMemoryRecord(1, "x")}
    assert service.forget_all_calls == []
    assert "CONFIRM" in send.await_args.kwargs["text"]


async def test_cmd_forget_all_confirm_deletes_all():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/forget all CONFIRM", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    service._memories = {1: _FakeMemoryRecord(1, "a"), 2: _FakeMemoryRecord(2, "b")}
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_forget(update, context)
    assert service.forget_all_calls == ["telegram:1"]
    assert service._memories == {}
    assert "2 deleted" in send.await_args.kwargs["text"]


async def test_cmd_forget_unauthorized_noop():
    update, chat, context, app, bot = _make(user_id=999, chat_id=1, text="/forget 1", allowed=(1,))
    service = _FakeService()
    app.bot_data["agent_service"] = service
    service._memories = {1: _FakeMemoryRecord(1, "x")}
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_forget(update, context)
    send.assert_not_awaited()
    assert service.forget_calls == [] and service._memories != {}


async def test_memory_command_logs_no_sensitive_data(repo, caplog):
    # Use the *real* repository so the safe scope-hash log line is produced.
    from fibrecase_agent_backend.agent.service import AgentService

    conv = await repo.get_or_create_conversation(1, 1)
    service = AgentService(
        repo,
        None,  # llm is unused by the /remember command path
        system_prompt="p",
    )
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/remember 我偏好中文回答。", allowed=(1,))
    app.bot_data["agent_service"] = service

    with caplog.at_level("INFO"):
        with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
            await cmd_remember(update, context)

    record = await repo.list_memories("telegram:1")
    assert len(record) == 1

    # Collect every structured log field (the `extra` dict) plus the message.
    def all_fields(rec):
        fields = {}
        for key in rec.__dict__:
            fields[key] = rec.__dict__[key]
        fields["message"] = rec.getMessage()
        return fields

    # None of the raw scope, the user id, or the memory content may appear in
    # any logged field.
    for rec in caplog.records:
        fields = all_fields(rec)
        for value in fields.values():
            if isinstance(value, str):
                assert "telegram:1" not in value
                assert "我偏好中文回答" not in value
        # The safe fields are present: a scope hash (not the raw scope), the
        # memory id, and the content length (not the content).
        extra = fields
        assert "scope_hash" in extra
        assert extra["scope_hash"] != "telegram:1"
        assert extra.get("memory_id") == record[0].id
        assert extra.get("content_length") == len("我偏好中文回答。")


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
