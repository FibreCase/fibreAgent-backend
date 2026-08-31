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
from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackContext

from fibrecase_agent_backend import __version__
from fibrecase_agent_backend.telegram.bot import (
    CHUNK_SIZE,
    _COMMANDS,
    _DraftStreamer,
    _IN_FLIGHT,
    _is_authorized,
    _send_long,
    _tail_preview,
    cmd_context,
    cmd_forget,
    cmd_help,
    cmd_memories,
    cmd_new,
    cmd_remember,
    cmd_schedule_status,
    cmd_start,
    cmd_status,
    cmd_stop,
    cmd_user_status,
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
        self.drafts: list[dict] = []  # one entry per send_message_draft call

    async def send_chat_action(self, chat_id, action):  # pragma: no cover - trivial
        self.actions += 1

    async def send_message_draft(self, chat_id, draft_id, text=None, **kwargs):
        # Records the preview update. Tests may override this on an instance to
        # raise (to exercise the fail-soft path).
        self.drafts.append({"chat_id": chat_id, "draft_id": draft_id, "text": text})
        return True


def _make(user_id, chat_id, text=None, *, allowed=(1,), chat_type="private"):
    """Build a real Update + CallbackContext (like PTB would deliver)."""
    user = None if user_id is None else User(id=user_id, first_name="U", is_bot=False)
    chat = Chat(id=chat_id, type=chat_type)
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
        self.on_text_delta = None  # set by process_message when a callback is handed in
        # Phase 2.5 memory command state (mutable, in-memory).
        self._memories = {}
        self._mem_seq = 0
        self.remember_calls = []
        self.list_calls = []
        self.forget_calls = []
        self.forget_all_calls = []

    async def process_message(self, conv_id, text, *, memory_scope=None, on_text_delta=None):
        self.calls.append((conv_id, text))
        self.on_text_delta = on_text_delta  # last callback handed in (if any)
        if self.delay:
            await asyncio.sleep(self.delay)
        if on_text_delta is not None:
            # Mimic the real service: hand the accumulated-so-far text through,
            # word by word, ending at the full reply.
            words = self.reply.split(" ")
            acc = ""
            for i, w in enumerate(words):
                acc = w if i == 0 else acc + " " + w
                await on_text_delta(acc)
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
    enable_streaming = False  # off by default in tests; streaming tests opt in

    def __init__(self, *, enable_streaming=False):
        # A per-instance override so the streaming tests can flip it on.
        self.enable_streaming = enable_streaming


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
        async def process_message(self, conv_id, text, *, memory_scope=None, on_text_delta=None):
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


async def test_cmd_user_status_reports_own_identity():
    # /user_status shows the caller's own user_id + chat_id (the values to fill
    # into a schedule's receiver.telegram). Rendered through _send_long (Markdown
    # → HTML), so assert on the raw numbers which survive the conversion.
    update, chat, context, app, bot = _make(user_id=7, chat_id=42, text="/user_status", allowed=(7,))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_user_status(update, context)
    sent = send.await_args.kwargs["text"]
    assert "7" in sent and "42" in sent
    assert "user_id" in sent and "chat_id" in sent


async def test_cmd_user_status_unauthorized_noop():
    update, chat, context, app, bot = _make(user_id=999, chat_id=42, text="/user_status", allowed=(1,))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_user_status(update, context)
    send.assert_not_awaited()


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
# Telegram Reply: the final answer quotes the user's message (and only it)
# ---------------------------------------------------------------------------
async def test_handle_message_final_answer_replies_to_user_message():
    # The model's final answer quotes the user's message (Telegram Reply), so it
    # visibly references what it is answering.
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hello", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = _FakeService(reply="hi back")
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await handle_message(update, context)
    # The user's message had message_id 1 (see _make); the reply quotes it.
    assert send.await_count == 1
    assert send.await_args.kwargs["reply_to_message_id"] == 1


async def test_command_reply_is_not_a_reply_to_user_message():
    # Command acks are not "answers" to a user's question — they must NOT carry
    # a Telegram Reply (only the final LLM answer does).
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/help", allowed=(1,))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_help(update, context)
    assert send.await_count >= 1
    for call in send.await_args_list:
        assert "reply_to_message_id" not in call.kwargs


async def test_send_long_replies_only_first_chunk():
    # A long reply is chunked: only the FIRST chunk quotes the user's message;
    # the rest follow normally, so the user's message is quoted once, not per chunk.
    chat = Chat(id=1, type="private")
    # Blank-line-separated paragraphs → several chunks (contiguous single
    # newlines would stay one atomic block and not split).
    long_text = "A paragraph here.\n\n" * 400  # ~7600 chars > CHUNK_SIZE
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await _send_long(chat, long_text, reply_to_message_id=7)
    assert send.await_count >= 2  # prove it actually chunked
    assert send.await_args_list[0].kwargs.get("reply_to_message_id") == 7  # first chunk quotes it
    for call in send.await_args_list[1:]:  # …the others do not
        assert "reply_to_message_id" not in call.kwargs


async def test_send_long_no_reply_when_id_is_none():
    # Without a reply id, no chunk carries a Telegram Reply (the old behaviour).
    chat = Chat(id=1, type="private")
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await _send_long(chat, "hi there")
    assert "reply_to_message_id" not in send.await_args.kwargs


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


# ---------------------------------------------------------------------------
# /stop — interrupt an in-flight reply (concurrent_updates: a command task can
# cancel the message handler task for the same chat)
# ---------------------------------------------------------------------------
def _turn(app, user_id, chat_id, text):
    """An (update, chat, context) for ``app`` (shares ``app.bot_data``)."""
    user = User(id=user_id, first_name="U", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(message_id=1, date=0, chat=chat, from_user=user, text=text)
    update = Update(update_id=1, message=message)
    return update, chat, CallbackContext.from_update(update, app)


async def test_stop_cancels_in_flight_and_notifies():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hello", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = _FakeService(reply="hi back", delay=0.5)
    app.bot_data["config"] = _FakeConfig()

    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        msg_task = asyncio.create_task(handle_message(update, context))
        # Let the turn get going and register itself as in-flight.
        for _ in range(200):
            if app.bot_data.get(_IN_FLIGHT, {}).get(chat.id) is msg_task:
                break
            await asyncio.sleep(0.005)
        assert app.bot_data[_IN_FLIGHT][chat.id] is msg_task  # it registered

        # /stop from the same shared app (same bot_data), same chat.
        stop_update, stop_chat, stop_context = _turn(app, 1, chat.id, "/stop")
        await cmd_stop(stop_update, stop_context)

        # Give the cancelled turn time to unwind (stop typing, post notice, clean up).
        await asyncio.sleep(0.3)
        assert msg_task.cancelled()
        assert app.bot_data.get(_IN_FLIGHT, {}).get(chat.id) is None  # cleaned up

        # The stopped turn posts the ⛔️ notice as a Telegram Reply quoting the
        # original message — never the reply that was being generated.
        texts = [c.kwargs.get("text") for c in send.await_args_list if c.kwargs]
        assert any(t and "⛔️" in t and "<b>Interrupted.</b>" in t for t in texts)
        assert "hi back" not in texts
        # It quotes the original message (message_id from _make is 1).
        stopped = [c for c in send.await_args_list if c.kwargs and c.kwargs.get("text", "").startswith("⛔️")]
        assert stopped and stopped[0].kwargs.get("reply_to_message_id") == 1


async def test_stop_when_idle_says_nothing_to_stop():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/stop", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = _FakeService()
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_stop(update, context)
    texts = [c.kwargs.get("text") for c in send.await_args_list if c.kwargs]
    assert any(t and "Nothing to stop" in t for t in texts)


async def test_stop_unauthorized_noop():
    update, chat, context, app, bot = _make(user_id=999, chat_id=1, text="/stop", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = _FakeService()
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_stop(update, context)
    send.assert_not_awaited()


async def test_stop_targets_only_its_own_chat():
    # Two private chats of the same owner, both with an in-flight turn. /stop on
    # chat A must cancel only chat A's task, not chat B's.
    app = type("App", (), {})()
    app.bot_data = {
        "allowed_user_ids": {1},
        "repository": _FakeRepo(),
        "agent_service": _FakeService(reply="hi back", delay=0.5),
        "config": _FakeConfig(),
    }
    app.bot = _StubBot()

    u1, c1, ctx1 = _turn(app, 1, 11, "one")
    u2, c2, ctx2 = _turn(app, 1, 22, "two")

    with patch.object(Chat, "send_message", new_callable=AsyncMock):
        task1 = asyncio.create_task(handle_message(u1, ctx1))
        task2 = asyncio.create_task(handle_message(u2, ctx2))
        for _ in range(200):
            in_flight = app.bot_data.get(_IN_FLIGHT, {})
            if in_flight.get(11) is task1 and in_flight.get(22) is task2:
                break
            await asyncio.sleep(0.005)

        stop_update, _, stop_context = _turn(app, 1, 11, "/stop")  # stop chat 11 only
        await cmd_stop(stop_update, stop_context)
        await asyncio.sleep(0.3)

        assert task1.cancelled()  # the targeted chat's turn was stopped
        assert not task2.cancelled()  # the other chat's turn is untouched

        # Clean up the still-running turn for chat 2.
        task2.cancel()
        try:
            await task2
        except asyncio.CancelledError:
            pass


async def test_in_flight_handle_removed_after_normal_completion():
    # The new registration must not leak on the ordinary (non-stopped) path, and
    # must not change normal reply delivery.
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hi", allowed=(1,))
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = _FakeService(reply="ok", delay=0.0)
    app.bot_data["config"] = _FakeConfig()
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await handle_message(update, context)
    assert app.bot_data.get(_IN_FLIGHT, {}).get(chat.id) is None
    assert any(c.kwargs.get("text") == "ok" for c in send.await_args_list if c.kwargs)


# ---------------------------------------------------------------------------
# /schedule_status (phase 9) — read-only status: name + cron + next fire only,
# never the prompt / chat_id / user_id; disabled when none configured.
# ---------------------------------------------------------------------------
class _ScheduleConfig:
    def __init__(self, schedules=(), schedule_timezone="UTC"):
        self.schedules = schedules
        self.schedule_timezone = schedule_timezone


def _sched(name, cron, *, chat_id=42, user_id=7, prompt="SECRET-PROMPT-BODY"):
    from fibrecase_agent_backend.config import ScheduleSpec, ScheduleTelegramReceiver

    # /schedule_status only reads name + cron (+ next fire); the receiver values
    # are a valid telegram-identity construction so the spec builds. The command
    # never shows the receiver's chat_id/user_id (or the qq openid).
    return ScheduleSpec(
        name=name, cron=cron, prompt=prompt,
        identity="telegram", telegram=ScheduleTelegramReceiver(chat_id=chat_id, user_id=user_id),
    )


def _sent_text(send):
    return [c.kwargs.get("text", "") for c in send.await_args_list if c.kwargs]


async def test_schedule_status_disabled_when_none_configured():
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/schedule_status", allowed=(1,))
    app.bot_data["config"] = _ScheduleConfig(schedules=())
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_schedule_status(update, context)
    texts = _sent_text(send)
    assert len(texts) == 1
    assert "disabled" in texts[0].lower()


async def test_schedule_status_shows_name_cron_next_fire_only():
    # Distinctive ids that do not appear in any cron field or next-fire timestamp,
    # so a leak of chat_id / user_id into the rendered text would be caught.
    specs = (
        _sched("nightly", "0 7 * * *", chat_id=81234567, user_id=98765432),
        _sched("weekly", "0 9 * * MON", chat_id=81234567, user_id=98765432),
    )
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/schedule_status", allowed=(1,))
    app.bot_data["config"] = _ScheduleConfig(schedules=specs, schedule_timezone="UTC")
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_schedule_status(update, context)
    texts = "\n".join(_sent_text(send))
    # Each schedule's name + cron + a "next:" line is shown.
    assert "nightly" in texts and "0 7 * * *" in texts
    assert "weekly" in texts and "0 9 * * MON" in texts
    assert "next:" in texts
    # …but the prompt, chat_id, or user_id are never exposed.
    assert "SECRET-PROMPT-BODY" not in texts
    assert "81234567" not in texts
    assert "98765432" not in texts


async def test_schedule_status_calendar_impossible_shows_never():
    # "0 0 31 2 *" is syntactically valid but can never fire → shown as
    # "never (untriggerable)" rather than a date.
    specs = (_sched("impossible", "0 0 31 2 *"),)
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/schedule_status", allowed=(1,))
    app.bot_data["config"] = _ScheduleConfig(schedules=specs, schedule_timezone="UTC")
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_schedule_status(update, context)
    texts = "\n".join(_sent_text(send))
    assert "impossible" in texts
    assert "never (untriggerable)" in texts


async def test_schedule_status_unauthorized_is_silent():
    update, chat, context, app, bot = _make(user_id=999, chat_id=1, text="/schedule_status", allowed=(1,))
    app.bot_data["config"] = _ScheduleConfig(schedules=(_sched("nightly", "0 7 * * *"),))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_schedule_status(update, context)
    assert send.await_count == 0  # nothing sent to an unauthorised sender


def test_schedule_status_is_in_command_menu():
    # /schedule_status is advertised in the Telegram command menu (one of the
    # read-only status commands).
    assert ("schedule_status", "Show configured schedules") in _COMMANDS


# ---------------------------------------------------------------------------
# /schedule_status does not trigger anything: no agent service, no LLM call.
# The command only reads config.schedules + the pure cron parser.
# ---------------------------------------------------------------------------
async def test_schedule_status_never_triggers_a_run():
    specs = (_sched("nightly", "0 7 * * *"),)
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="/schedule_status", allowed=(1,))
    # No agent_service in bot_data at all — if the command tried to trigger a
    # run it would raise; it must not, because it is purely read-only.
    app.bot_data["config"] = _ScheduleConfig(schedules=specs, schedule_timezone="UTC")
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_schedule_status(update, context)
    assert "nightly" in _sent_text(send)[0]


# ---------------------------------------------------------------------------
# streaming replies (Bot API 10.0 ``sendMessageDraft``)
#
# A *private* chat with ``ENABLE_STREAMING=true`` shows a live draft preview
# (``_DraftStreamer`` → ``send_message_draft``) *in parallel with* the "typing…"
# keep-alive — the typing action is the fallback that stays visible if the draft
# is rejected (the bot isn't on Telegram's streaming allowlist). Group/channel
# chats and a disabled knob always degrade to the classic typing + chunked final
# reply. The draft is *fail-soft* (a rejected draft costs nothing — the full
# reply is always sent as a normal message) and *private* (the draft body never
# reaches the logs).
# ---------------------------------------------------------------------------
def test_tail_preview_keeps_tail_beyond_limit():
    assert _tail_preview("short", limit=10) == "short"
    # Over the limit: the tail is kept, prefixed with "…\n", and the result is
    # exactly ``limit`` long.
    preview = _tail_preview("x" * 50, limit=10)
    assert preview == "…\n" + "x" * 8
    assert len(preview) == 10


async def test_draft_streamer_throttles_and_pushes_final():
    bot = _StubBot()
    streamer = _DraftStreamer(bot, chat_id=1, draft_id=7)
    # Feed a burst of accumulated-so-far text: only the first call (and the
    # explicit finalize) should reach the API, the mid-burst deltas coalesce.
    for acc in ["a", "ab", "abc"]:
        await streamer.on_delta(acc)
    await streamer.finalize("abc")

    assert len(bot.drafts) >= 2  # at least the first delta + the final
    assert all(d["draft_id"] == 7 for d in bot.drafts)
    assert all(d["chat_id"] == 1 for d in bot.drafts)
    # The final preview shows the complete answer.
    assert bot.drafts[-1]["text"] == "abc"


async def test_draft_streamer_fail_soft_swallows_telegram_error(caplog):
    from telegram.error import BadRequest

    bot = _StubBot()
    bot.send_message_draft = AsyncMock(side_effect=BadRequest("not on allowlist"))
    streamer = _DraftStreamer(bot, chat_id=1, draft_id=7)

    # Neither the delta nor the finalize raises — the failure is swallowed.
    await streamer.on_delta("The secret answer body")
    await streamer.finalize("The secret answer body")

    # The draft body must NOT appear in any log record (privacy invariant);
    # only the class of the error is logged, never the text.
    assert "The secret answer body" not in caplog.text


async def test_private_streaming_shows_draft_and_typing_fallback():
    reply = "The quick brown fox"
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hello", allowed=(1,))
    service = _FakeService(reply=reply, delay=0.1)  # long enough for typing to fire
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = service
    app.bot_data["config"] = _FakeConfig(enable_streaming=True)
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await handle_message(update, context)

    # A live draft preview was pushed (draft_id is a positive, non-zero int).
    assert len(bot.drafts) >= 1
    assert all(d["draft_id"] >= 1 for d in bot.drafts)
    # The "typing…" keep-alive runs *in parallel* as a fallback: it stays visible
    # if the draft is rejected (bot not on the streaming allowlist). So even on
    # the streaming branch a typing action fires.
    assert bot.actions >= 1
    # The full reply was *also* delivered as a normal (final) message, quoting
    # the user's message.
    assert send.await_args_list
    assert reply in _sent_text(send)


async def test_group_never_streams_uses_typing():
    reply = "hello group"
    update, chat, context, app, bot = _make(
        user_id=1, chat_id=1, text="hi", allowed=(1,), chat_type="group"
    )
    service = _FakeService(reply=reply, delay=0.1)  # long enough for typing to fire
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = service
    app.bot_data["config"] = _FakeConfig(enable_streaming=True)  # on, but group ⇒ degrade
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await handle_message(update, context)

    # Group chats degrade: no draft preview, but the classic typing keep-alive.
    assert bot.drafts == []
    assert bot.actions >= 1
    assert reply in _sent_text(send)


async def test_streaming_disabled_private_no_draft():
    reply = "plain reply"
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hello", allowed=(1,))
    service = _FakeService(reply=reply, delay=0.1)
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = service
    app.bot_data["config"] = _FakeConfig(enable_streaming=False)  # private but knob off
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await handle_message(update, context)

    assert bot.drafts == []
    assert bot.actions >= 1  # fell back to typing
    assert reply in _sent_text(send)


async def test_draft_failure_still_sends_final_message():
    from telegram.error import BadRequest

    reply = "still delivered"
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hello", allowed=(1,))
    service = _FakeService(reply=reply)
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = service
    app.bot_data["config"] = _FakeConfig(enable_streaming=True)
    # The bot isn't on the streaming allowlist: every draft is rejected. The
    # handler must swallow it (fail-soft) and still deliver the full reply.
    bot.send_message_draft = AsyncMock(side_effect=BadRequest("not on allowlist"))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await handle_message(update, context)  # must not raise

    # No draft ever landed (all rejected)…
    assert bot.drafts == []
    # …but the normal final message still went out with the full reply.
    assert reply in _sent_text(send)


async def test_streaming_final_log_does_not_leak_reply_body(caplog):
    reply = "TOP-SECRET-REPLY-BODY-XYZ"
    update, chat, context, app, bot = _make(user_id=1, chat_id=1, text="hello", allowed=(1,))
    service = _FakeService(reply=reply)
    app.bot_data["repository"] = _FakeRepo()
    app.bot_data["agent_service"] = service
    app.bot_data["config"] = _FakeConfig(enable_streaming=True)
    with patch.object(Chat, "send_message", new_callable=AsyncMock):
        with caplog.at_level("DEBUG", logger="telegram"):
            await handle_message(update, context)
    # The model reply is delivered, but it must never be written to the logs.
    assert reply not in caplog.text
