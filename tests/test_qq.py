"""QQ (C2C) adapter: message normalisation, delivery, chunking.

The :class:`~fibrecase_agent_backend.qq.bot.QQChannel` handler is exercised
against a **fake** ``C2CMessage`` (``author.user_openid`` / ``content`` / ``id`` /
``reply``) so we never open a websocket or import a live ``botpy`` client. The
agent service and repository are lightweight fakes; no real LLM / QQ network
call happens. There is **no allow-list** (this is the owner's personal bot and a
C2C chat is one-to-one), so any sender is served — the tests cover normalisation,
delivery, chunking, error handling, and the privacy invariant (never log the raw
``user_openid`` or the message body) via ``caplog``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from fibrecase_agent_backend.agent.messages import AgentMessage
from fibrecase_agent_backend.agent.service import AgentError
from fibrecase_agent_backend.database.models import (
    QQ_CHAT_ID_BASE,
    QQ_CHAT_ID_MAX,
    qq_chat_id,
)
from fibrecase_agent_backend.qq.bot import (
    QQ_MAX_MESSAGE_CHARS,
    QQ_MSG_TYPE_MARKDOWN,
    QQ_MSG_TYPE_TEXT,
    QQChannel,
    _c2c_panel_payload,
    _ensure_c2c_panel,
    _split_for_qq,
)
from fibrecase_agent_backend.qq.commands import (
    _QQ_COMMANDS,
    build_c2c_panel_items,
    known_command_names,
)
from fibrecase_agent_backend.qq.approval import (
    QQApprovalBroker,
    QQScopedApprovalRouter,
    _approval_keyboard,
    _card_text,
    decision_from,
    request_id_from,
)
from fibrecase_agent_backend.tools.approval import ApprovalDecision, ApprovalRequest
from fibrecase_agent_backend.qq import build_qq_client


# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------
class _FakeRepo:
    """A stand-in for the conversation repository (create + get paths used)."""

    def __init__(self, conv_id=5, known=False):
        self._conv_id = conv_id
        self._known = known
        self.created = []

    async def get_or_create_conversation(self, chat_id, user_id):
        self.created.append((chat_id, user_id))
        self._known = True
        return type("C", (), {"id": self._conv_id})()

    async def get_conversation(self, chat_id):
        if self._known:
            return type("C", (), {"id": self._conv_id})()
        return None


class _FakeService:
    """A stand-in for :class:`AgentService`.

    Records ``process_message`` calls and returns a canned reply (or raises);
    the command-relevant methods return canned values that tests may override.
    """

    def __init__(self, reply="ok", exc=None, memories=None, audit_records=None, remember_record=None):
        self.reply = reply
        self.exc = exc
        self.calls = []
        # command-call recorders
        self.reset_calls = []
        self.remember_calls = []
        self.forget_calls = []
        self.forget_all_calls = []
        self.list_memories_calls = []
        self.list_tool_audit_calls = []
        self.conversation_status_calls = []
        self.context_status_calls = []
        # canned values (override per-test after construction)
        self._conversation_status = {"messages": 3}
        self._context_status = {
            "conversation_id": 5,
            "cap": 20,
            "stored_messages": 4,
            "history_messages": 3,
            "budget": 1000,
            "estimated_cost": 100,
            "system_cost": 50,
            "images_kept": 1,
            "images_in_store": 2,
        }
        self.memories = list(memories or [])
        self.audit_records = list(audit_records or [])
        self.remember_record = remember_record

    async def process_message(self, conv_id, agent_message, *, memory_scope=None, **kwargs):
        self.calls.append(
            {"conv_id": conv_id, "agent_message": agent_message, "memory_scope": memory_scope}
        )
        if self.exc is not None:
            raise self.exc
        return self.reply

    # --- command surface ------------------------------------------------------
    async def reset(self, chat_id, user_id):
        self.reset_calls.append((chat_id, user_id))
        return 0

    async def conversation_status(self, conversation_id):
        self.conversation_status_calls.append(conversation_id)
        return dict(self._conversation_status)

    async def context_status(self, conversation_id):
        self.context_status_calls.append(conversation_id)
        return dict(self._context_status)

    async def remember_memory(self, scope, content):
        self.remember_calls.append((scope, content))
        if self.remember_record is not None:
            return self.remember_record
        return type("M", (), {"id": 7, "content": content})()

    async def list_memories(self, scope):
        self.list_memories_calls.append(scope)
        return list(self.memories)

    async def forget_memory(self, scope, memory_id):
        self.forget_calls.append((scope, memory_id))
        return None

    async def forget_all_memories(self, scope):
        self.forget_all_calls.append(scope)
        return 2

    async def list_tool_audit_events(self, scope, limit=20):
        self.list_tool_audit_calls.append((scope, limit))
        return list(self.audit_records)


class _FakeAuthor:
    def __init__(self, user_openid):
        self.user_openid = user_openid


class _FakeMessage:
    """A minimal stand-in for a ``botpy`` ``C2CMessage``.

    ``reply`` records its keyword arguments verbatim (``msg_type`` / ``content``
    and/or ``markdown`` / ``msg_seq`` / ``message_reference`` …) so a test can
    assert the exact delivery calls, including the per-chunk ``msg_seq``
    increment and the first-chunk ``message_reference``. Like the real
    ``C2CMessage.reply(**kwargs)`` it forwards only what the caller passes.
    """

    def __init__(self, content, openid, msg_id=111, raise_reply=None):
        self.author = _FakeAuthor(openid)
        self.content = content
        self.id = msg_id
        self.raise_reply = raise_reply
        self.replies = []

    async def reply(self, **kwargs):
        if self.raise_reply is not None:
            raise self.raise_reply
        self.replies.append(dict(kwargs))
        return None


class _FakeConfig:
    """A stand-in for :class:`~..config.Config` exposing only the read-only
    attributes the QQ commands render (all non-secret)."""

    def __init__(
        self,
        openai_model="test-model",
        enable_tools=True,
        infra_ssh_targets=(),
        schedules=(),
        schedule_timezone=None,
    ):
        self.openai_model = openai_model
        self.enable_tools = enable_tools
        self.infra_ssh_targets = list(infra_ssh_targets)
        self.schedules = list(schedules)
        self.schedule_timezone = schedule_timezone


class _FakeMcpManager:
    """A stand-in for :class:`~..mcp.McpManager` (status() / total_tools / len)."""

    def __init__(self, entries=None, total=None):
        self._entries = list(entries or [])
        self._total = 0 if total is None else total

    def status(self):
        return list(self._entries)

    @property
    def total_tools(self):
        return self._total

    def __len__(self):
        return len(self._entries)


def _channel(service, repo, config=None, mcp_manager=None):
    return QQChannel(
        service,
        repo,
        config if config is not None else _FakeConfig(),
        mcp_manager,
    )


# ---------------------------------------------------------------------------
# the synthetic conversation id (deterministic, reserved range, disjoint from
# the schedule range)
# ---------------------------------------------------------------------------
def test_qq_chat_id_is_deterministic_and_in_reserved_range():
    a = qq_chat_id("openid-alice")
    b = qq_chat_id("openid-alice")
    assert a == b, "the same openid must always map to the same synthetic id"
    assert QQ_CHAT_ID_BASE <= a < QQ_CHAT_ID_MAX
    # Distinct openids land on (overwhelmingly) different ids.
    assert qq_chat_id("openid-alice") != qq_chat_id("openid-bob")


def test_qq_range_is_disjoint_from_schedule_range():
    from fibrecase_agent_backend.database.models import SCHEDULE_CHAT_ID_BASE

    # A QQ conversation id must never collide with a scheduled chat id, and the
    # startup ephemeral sweep (bounded to the schedule range) must never reach a
    # QQ row.
    assert QQ_CHAT_ID_MAX <= SCHEDULE_CHAT_ID_BASE


# ---------------------------------------------------------------------------
# inbound message handling
# ---------------------------------------------------------------------------
async def test_authorized_message_processes_and_replies():
    openid = "openid-alice"
    service = _FakeService(reply="你好，我在。")
    repo = _FakeRepo(conv_id=5)
    channel = _channel(service, repo)
    msg = _FakeMessage("你好", openid)

    await channel.on_c2c_message_create(msg)

    # Normalised into an AgentMessage tagged with the qq source.
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["conv_id"] == 5
    assert call["memory_scope"] == f"qq:{openid}"
    am: AgentMessage = call["agent_message"]
    assert am.source == "qq"
    assert am.text == "你好"
    # The conversation row was created keyed by the synthetic id (chat == user).
    assert repo.created == [(qq_chat_id(openid), qq_chat_id(openid))]
    # The reply was delivered as one Markdown (msg_type=2) message, seq 1, with
    # the text in the nested `markdown` field (not top-level `content`).
    assert len(msg.replies) == 1
    assert msg.replies[0]["msg_type"] == QQ_MSG_TYPE_MARKDOWN
    assert msg.replies[0]["markdown"] == {"content": "你好，我在。"}
    assert "content" not in msg.replies[0]
    assert msg.replies[0]["msg_seq"] == 1


async def test_any_openid_is_served_no_allowlist():
    # There is no allow-list on the QQ channel (personal bot, one-to-one C2C):
    # an openid that would have been "unauthorised" under the old gate is now
    # served normally.
    service = _FakeService(reply="hi there")
    repo = _FakeRepo(conv_id=9)
    channel = _channel(service, repo)
    msg = _FakeMessage("hello", openid="openid-stranger")

    await channel.on_c2c_message_create(msg)

    assert len(service.calls) == 1, "any openid is served (no allow-list)"
    sid = qq_chat_id("openid-stranger")
    assert repo.created == [(sid, sid)]
    assert msg.replies[0]["markdown"] == {"content": "hi there"}


async def test_missing_openid_is_ignored():
    service = _FakeService()
    channel = _channel(service, _FakeRepo())
    msg = _FakeMessage("hello", openid=None)

    await channel.on_c2c_message_create(msg)

    assert service.calls == []
    assert msg.replies == []


async def test_empty_content_is_a_noop():
    service = _FakeService()
    channel = _channel(service, _FakeRepo())

    for blank in ("", "   ", "\n\t"):
        msg = _FakeMessage(blank, openid="openid-alice")
        await channel.on_c2c_message_create(msg)
        assert msg.replies == [], f"blank content {blank!r} must not be replied to"

    assert service.calls == [], "whitespace-only content must not reach the service"


async def test_agent_error_surfaces_user_safe_text():
    service = _FakeService(exc=AgentError("模型请求超时，请稍后重试。", "timeout"))
    channel = _channel(service, _FakeRepo())
    msg = _FakeMessage("hello", openid="openid-alice")

    await channel.on_c2c_message_create(msg)

    assert len(msg.replies) == 1
    assert "超时" in msg.replies[0]["content"]


async def test_unexpected_error_sends_generic_notice():
    service = _FakeService(exc=RuntimeError("boom"))
    channel = _channel(service, _FakeRepo())
    msg = _FakeMessage("hello", openid="openid-alice")

    await channel.on_c2c_message_create(msg)  # must not raise

    assert len(msg.replies) == 1
    assert "意外错误" in msg.replies[0]["content"]


async def test_failed_send_is_swallowed_not_raised():
    service = _FakeService(reply="hi")
    channel = _channel(service, _FakeRepo())
    msg = _FakeMessage("hello", openid="openid-alice", raise_reply=RuntimeError("send down"))

    await channel.on_c2c_message_create(msg)  # must not raise


# ---------------------------------------------------------------------------
# multi-chunk delivery (the QQ dedup key is (msg_id, msg_seq))
# ---------------------------------------------------------------------------
async def test_long_reply_chunks_with_incrementing_msg_seq():
    service = _FakeService(reply="line\n" * 2000)  # far beyond QQ_MAX_MESSAGE_CHARS
    channel = _channel(service, _FakeRepo())
    msg = _FakeMessage("hi", openid="openid-alice")

    await channel.on_c2c_message_create(msg)

    assert len(msg.replies) >= 2, "the long reply must be chunked"
    # msg_seq increments 1, 2, 3, … so each chunk is a *distinct* dedup key;
    # every chunk is a Markdown message and replies to the same incoming message.
    for i, r in enumerate(msg.replies, start=1):
        assert r["msg_seq"] == i, "msg_seq must advance per chunk to avoid dedup"
        assert r["msg_type"] == QQ_MSG_TYPE_MARKDOWN
        assert "markdown" in r
    # Concatenating the chunks reproduces the full reply (nothing truncated).
    assert "".join(r["markdown"]["content"] for r in msg.replies) == service.reply


# ---------------------------------------------------------------------------
# the local chunker (channel-decoupled; mirrors the Telegram chunker's contract)
# ---------------------------------------------------------------------------
def test_short_message_single_chunk():
    assert _split_for_qq("hi") == ["hi"]


def test_chunks_preserve_all_content():
    text = "line\n" * 500 + "tail"
    chunks = _split_for_qq(text, limit=200)
    assert "".join(chunks) == text, "chunking must never lose or add content"
    assert all(len(c) <= 200 for c in chunks)


def test_hard_split_of_huge_line():
    text = "x" * 10_000  # one enormous line, no newlines
    chunks = _split_for_qq(text, limit=400)
    assert "".join(chunks) == text
    assert all(len(c) <= 400 for c in chunks)


def test_default_limit_under_qq_cap():
    # QQ text messages cap well under Telegram's 4096; leave CJK headroom.
    assert QQ_MAX_MESSAGE_CHARS <= 4096


# ---------------------------------------------------------------------------
# build_qq_client — the only place that knows about botpy
# ---------------------------------------------------------------------------
async def test_build_qq_client_wires_handler_and_public_intents():
    import botpy

    service = _FakeService(reply="hi")
    repo = _FakeRepo()
    client = build_qq_client(service, repo, _FakeConfig(), None)

    # A real botpy.Client, with the C2C (public_messages) intent bit set.
    assert isinstance(client, botpy.Client)
    assert client.intents == botpy.Intents(public_messages=True).value

    # The SDK's on_c2c_message_create must dispatch to our QQChannel logic.
    msg = _FakeMessage("你好", openid="openid-alice")
    await client.on_c2c_message_create(msg)
    assert len(service.calls) == 1
    assert service.calls[0]["agent_message"].text == "你好"
    assert msg.replies[0]["markdown"] == {"content": "hi"}


# ---------------------------------------------------------------------------
# privacy invariants (asserted alongside the feature, not in a separate pass)
# ---------------------------------------------------------------------------
def _all_str_fields(rec):
    """Every string value on a log record: the message plus all `extra` fields."""
    values = [rec.getMessage()]
    for key, value in rec.__dict__.items():
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, int) and key in ("conversation_id", "message_id", "text_length"):
            values.append(str(value))
    return values


async def test_turn_logs_no_openid_or_body(caplog):
    openid = "openid-alice"
    body = "这是秘密消息正文 secret-body"
    reply = "这是回复正文 secret-reply"
    service = _FakeService(reply=reply)
    channel = _channel(service, _FakeRepo(conv_id=42))

    with caplog.at_level("INFO", logger="qq"):
        await channel.on_c2c_message_create(_FakeMessage(body, openid))

    for rec in caplog.records:
        for value in _all_str_fields(rec):
            assert openid not in value, "the raw user_openid must never be logged"
            assert body not in value, "the message body must never be logged"
            assert reply not in value, "the reply body must never be logged"

    # The *safe* identifier is present instead: the synthetic conversation id
    # (not the raw openid).
    joined = [v for rec in caplog.records for v in _all_str_fields(rec)]
    assert any(str(42) in v for v in joined), "the synthetic conversation id should be logged"
    assert not any(f"qq:{openid}" in v for v in joined), "the raw memory scope is not logged"


async def test_missing_openid_logs_no_identity(caplog):
    # A message with no sender identity is ignored; it must not leak anything
    # (there is no openid to leak) and must not be processed.
    service = _FakeService()
    channel = _channel(service, _FakeRepo())

    with caplog.at_level("WARNING", logger="qq"):
        await channel.on_c2c_message_create(_FakeMessage("hello", openid=None))

    assert service.calls == []
    joined = [v for rec in caplog.records for v in _all_str_fields(rec)]
    # Only the (non-identifying) QQ message id may appear; no openid, no body.
    assert any("no sender identity" in v for v in joined)


# ---------------------------------------------------------------------------
# command + panel helpers
# ---------------------------------------------------------------------------
def _reply_text(msg):
    """The text of a fake message's first reply (markdown or plain-text)."""
    if not msg.replies:
        return None
    r = msg.replies[0]
    if "markdown" in r:
        return r["markdown"]["content"]
    return r.get("content")


def _reply_type(msg):
    """The ``msg_type`` of a fake message's first reply (0 plain / 2 markdown)."""
    if not msg.replies:
        return None
    return msg.replies[0]["msg_type"]


class _FakeHttp:
    """A stand-in for the ``botpy`` client's ``BotHttp`` (the panel's REST path).

    Records every ``(method, path, kwargs)`` call; the ``GET`` returns the canned
    panel list, and an optional ``raise_on`` raises on the first call to prove the
    create-or-update path swallows errors.
    """

    def __init__(self, get_response=None, raise_on=None):
        self.calls = []
        self._get = get_response if get_response is not None else {"records": [], "is_end": True}
        self._raise = raise_on

    async def request(self, route, **kwargs):
        self.calls.append((route.method, route.path, dict(kwargs)))
        if self._raise is not None:
            raise self._raise
        if route.method == "GET":
            return self._get
        return {}


# ---------------------------------------------------------------------------
# slash-command dispatch (channel-level: branch, logging, delivery, privacy)
# ---------------------------------------------------------------------------
async def test_command_new_resets_and_does_not_run_agent_turn():
    openid = "openid-alice"
    service = _FakeService()
    repo = _FakeRepo(conv_id=5, known=True)
    channel = _channel(service, repo)
    cid = qq_chat_id(openid)

    msg = _FakeMessage("/new", openid=openid)
    await channel.on_c2c_message_create(msg)

    # /new resets the conversation (chat == user == synthetic id) and does NOT run
    # an agent turn or create a conversation row (commands are not stored turns).
    assert service.reset_calls == [(cid, cid)]
    assert service.calls == []
    assert repo.created == []
    assert "已开始新会话" in _reply_text(msg)
    # A simple receipt is delivered as plain text (msg_type=0), not Markdown.
    assert _reply_type(msg) == 0


async def test_command_help_lists_all_commands():
    service = _FakeService()
    channel = _channel(service, _FakeRepo())

    msg = _FakeMessage("/help", openid="openid-alice")
    await channel.on_c2c_message_create(msg)

    assert service.calls == [], "/help must not run an agent turn"
    text = _reply_text(msg)
    assert "可用命令" in text
    for name, _desc in _QQ_COMMANDS:
        assert f"/{name}" in text
    assert len(_QQ_COMMANDS) == 13, "the QQ command set is the 13 core + mcp_status + user_status"
    # A structured display is delivered as Markdown (msg_type=2).
    assert _reply_type(msg) == QQ_MSG_TYPE_MARKDOWN


async def test_command_user_status_returns_own_openid_and_is_not_logged(caplog):
    # /user_status returns the *caller's own* user_openid (derived from the
    # memory_scope) in the reply — and, being user-facing, the openid is **not**
    # logged. It also must not run an agent turn.
    openid = "openid-alice"
    service = _FakeService()
    channel = _channel(service, _FakeRepo(conv_id=5))

    with caplog.at_level("INFO", logger="qq"):
        msg = _FakeMessage("/user_status", openid=openid)
        await channel.on_c2c_message_create(msg)

    assert service.calls == [], "/user_status must not run an agent turn"
    text = _reply_text(msg)
    assert openid in text  # the caller's own openid is shown to the caller
    assert _reply_type(msg) == QQ_MSG_TYPE_MARKDOWN
    # The openid is user-facing (in the reply) but must never reach the logs.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert openid not in logged


async def test_unknown_slash_falls_through_to_agent_turn():
    service = _FakeService(reply="hi")
    channel = _channel(service, _FakeRepo(conv_id=5))

    msg = _FakeMessage("/foo bar", openid="openid-alice")
    await channel.on_c2c_message_create(msg)

    # An unrecognised ``/…`` is not a command — it reaches the agent as a turn.
    assert len(service.calls) == 1
    am = service.calls[0]["agent_message"]
    assert am.text == "/foo bar"
    assert am.source == "qq"


async def test_command_remember_uses_scope_and_leaks_nothing(caplog):
    openid = "openid-alice"
    service = _FakeService()
    channel = _channel(service, _FakeRepo(conv_id=5))

    with caplog.at_level("INFO", logger="qq"):
        msg = _FakeMessage("/remember my secret note", openid=openid, msg_id=12)
        await channel.on_c2c_message_create(msg)

    assert service.remember_calls == [(f"qq:{openid}", "my secret note")]
    assert "记忆已保存" in _reply_text(msg)
    # A simple receipt is delivered as plain text (msg_type=0).
    assert _reply_type(msg) == QQ_MSG_TYPE_TEXT
    # The command *name* is logged; the *content* and the raw openid are not.
    joined = [v for rec in caplog.records for v in _all_str_fields(rec)]
    assert any("remember" in v for v in joined)
    assert not any("my secret note" in v for v in joined)
    assert not any(openid in v for v in joined)


async def test_command_memories_lists_with_timestamp_and_scope():
    openid = "openid-alice"
    m = type(
        "M",
        (),
        {"id": 3, "content": "remember me", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    )()
    service = _FakeService(memories=[m])
    channel = _channel(service, _FakeRepo(conv_id=5))

    msg = _FakeMessage("/memories", openid=openid)
    await channel.on_c2c_message_create(msg)

    assert service.list_memories_calls == [f"qq:{openid}"]
    text = _reply_text(msg)
    assert "#3" in text and "remember me" in text and "2026-01-01" in text
    # The memory *list* is a structured display → Markdown (msg_type=2).
    assert _reply_type(msg) == QQ_MSG_TYPE_MARKDOWN


async def test_command_forget_single():
    openid = "openid-alice"
    service = _FakeService()
    channel = _channel(service, _FakeRepo(conv_id=5))

    msg = _FakeMessage("/forget 3", openid=openid)
    await channel.on_c2c_message_create(msg)

    assert service.forget_calls == [(f"qq:{openid}", 3)]
    assert "记忆已删除" in _reply_text(msg)
    # A simple receipt is delivered as plain text (msg_type=0).
    assert _reply_type(msg) == QQ_MSG_TYPE_TEXT


async def test_command_forget_all_requires_confirm_token():
    openid = "openid-alice"
    service = _FakeService()
    channel = _channel(service, _FakeRepo(conv_id=5))

    msg = _FakeMessage("/forget all", openid=openid)
    await channel.on_c2c_message_create(msg)

    assert service.forget_all_calls == [], "/forget all without CONFIRM deletes nothing"
    assert "CONFIRM" in _reply_text(msg)


async def test_command_forget_all_confirm_deletes():
    openid = "openid-alice"
    service = _FakeService()
    channel = _channel(service, _FakeRepo(conv_id=5))

    msg = _FakeMessage("/forget all CONFIRM", openid=openid)
    await channel.on_c2c_message_create(msg)

    assert service.forget_all_calls == [f"qq:{openid}"]
    assert "已清除全部记忆" in _reply_text(msg)


async def test_command_status_known_conversation():
    service = _FakeService()
    repo = _FakeRepo(conv_id=5, known=True)
    channel = _channel(service, repo, config=_FakeConfig(openai_model="gpt-x"))

    msg = _FakeMessage("/status", openid="openid-alice")
    await channel.on_c2c_message_create(msg)

    assert service.conversation_status_calls == [5]
    text = _reply_text(msg)
    assert "gpt-x" in text and "5" in text and "消息数" in text
    # A structured display is delivered as Markdown (msg_type=2).
    assert _reply_type(msg) == QQ_MSG_TYPE_MARKDOWN


async def test_command_context_none_yet():
    service = _FakeService()
    channel = _channel(service, _FakeRepo(known=False))

    msg = _FakeMessage("/context", openid="openid-alice")
    await channel.on_c2c_message_create(msg)

    assert service.context_status_calls == []
    assert "还没有会话" in _reply_text(msg)


async def test_command_tool_audit_clamps_limit():
    openid = "openid-alice"
    service = _FakeService()
    channel = _channel(service, _FakeRepo(conv_id=5))

    msg = _FakeMessage("/tool_audit 999", openid=openid)
    await channel.on_c2c_message_create(msg)

    # 999 clamps to the max (50); the scope is the qq:<openid> principal.
    assert service.list_tool_audit_calls == [(f"qq:{openid}", 50)]


async def test_command_mcp_status_enabled():
    manager = _FakeMcpManager(entries=[{"name": "gcal", "available": True, "tool_count": 3}], total=3)
    channel = _channel(_FakeService(), _FakeRepo(conv_id=5), mcp_manager=manager)

    msg = _FakeMessage("/mcp_status", openid="openid-alice")
    await channel.on_c2c_message_create(msg)

    text = _reply_text(msg)
    assert "gcal" in text and "可用" in text and "3 个工具" in text
    assert "可用 MCP 工具总数" in text and "3" in text
    # The server list is a structured display → Markdown (msg_type=2).
    assert _reply_type(msg) == QQ_MSG_TYPE_MARKDOWN


async def test_command_mcp_status_disabled_when_no_manager():
    channel = _channel(_FakeService(), _FakeRepo(conv_id=5), mcp_manager=None)

    msg = _FakeMessage("/mcp_status", openid="openid-alice")
    await channel.on_c2c_message_create(msg)

    assert _reply_text(msg) == "MCP：未启用"
    assert _reply_type(msg) == QQ_MSG_TYPE_TEXT  # a disabled notice is plain text


async def test_command_infra_status_renders_tool_names_and_no_host():
    target = type("T", (), {"name": "prod"})()
    config = _FakeConfig(infra_ssh_targets=[target])
    channel = _channel(_FakeService(), _FakeRepo(conv_id=5), config=config)

    msg = _FakeMessage("/infra_status", openid="openid-alice")
    await channel.on_c2c_message_create(msg)

    text = _reply_text(msg)
    assert "infra_prod__host_status" in text
    assert "infra_prod__disk_status" in text
    assert "infra_prod__service_status" in text
    # Only the target *name* and local tool names — never a host/path/command.
    assert "prod" in text
    # The target list is a structured display → Markdown (msg_type=2).
    assert _reply_type(msg) == QQ_MSG_TYPE_MARKDOWN


async def test_command_infra_status_disabled():
    channel = _channel(_FakeService(), _FakeRepo(conv_id=5), config=_FakeConfig(infra_ssh_targets=[]))

    msg = _FakeMessage("/infra_status", openid="openid-alice")
    await channel.on_c2c_message_create(msg)

    assert _reply_text(msg) == "基础设施观测：未启用"
    assert _reply_type(msg) == QQ_MSG_TYPE_TEXT  # a disabled notice is plain text


async def test_command_schedule_status_renders_next_fire_and_leaks_no_prompt():
    spec = type(
        "S", (), {"name": "daily", "cron": "*/5 * * * *", "prompt": "secret-prompt", "chat_id": 1, "user_id": 2}
    )()
    config = _FakeConfig(schedules=[spec], schedule_timezone="UTC")
    channel = _channel(_FakeService(), _FakeRepo(conv_id=5), config=config)

    msg = _FakeMessage("/schedule_status", openid="openid-alice")
    await channel.on_c2c_message_create(msg)

    text = _reply_text(msg)
    assert "daily" in text and "*/5 * * * *" in text and "下次触发" in text
    # The reply shows name + cron + next-fire only — never the prompt/chat/user id.
    assert "secret-prompt" not in text
    # The schedule list is a structured display → Markdown (msg_type=2).
    assert _reply_type(msg) == QQ_MSG_TYPE_MARKDOWN


async def test_command_schedule_status_disabled():
    channel = _channel(_FakeService(), _FakeRepo(conv_id=5), config=_FakeConfig(schedules=[]))

    msg = _FakeMessage("/schedule_status", openid="openid-alice")
    await channel.on_c2c_message_create(msg)

    assert _reply_text(msg) == "定时任务：未启用（未配置）"
    assert _reply_type(msg) == QQ_MSG_TYPE_TEXT  # a disabled notice is plain text


# ---------------------------------------------------------------------------
# /stop (a QQ-local in-flight registry; the turn runs as its own asyncio.Task)
# ---------------------------------------------------------------------------
async def test_stop_with_nothing_running_replies_notice():
    service = _FakeService()
    channel = _channel(service, _FakeRepo())

    msg = _FakeMessage("/stop", openid="openid-alice", msg_id=1)
    await channel.on_c2c_message_create(msg)

    assert service.calls == []
    assert "没有正在进行的回复" in _reply_text(msg)
    # A simple receipt is delivered as plain text (msg_type=0).
    assert _reply_type(msg) == QQ_MSG_TYPE_TEXT


async def test_stop_cancels_in_flight_turn():
    started = asyncio.Event()
    block = asyncio.Event()
    service = _FakeService(reply="the long answer")

    async def slow_process(conv_id, agent_message, *, memory_scope=None, **kwargs):
        service.calls.append(
            {"conv_id": conv_id, "agent_message": agent_message, "memory_scope": memory_scope}
        )
        started.set()
        await block.wait()  # blocks (a tool / generation in progress) until cancelled
        return service.reply

    service.process_message = slow_process

    repo = _FakeRepo(conv_id=5)
    channel = _channel(service, repo)
    openid = "openid-alice"
    cid = qq_chat_id(openid)

    turn = _FakeMessage("a very long question", openid, msg_id=77)
    turn_task = asyncio.create_task(channel.on_c2c_message_create(turn))

    await asyncio.wait_for(started.wait(), timeout=2)
    assert cid in channel._in_flight, "the turn must register itself as in-flight"

    # A /stop from a *separate* message (hence a separate task) cancels it.
    stop_msg = _FakeMessage("/stop", openid, msg_id=78)
    await channel.on_c2c_message_create(stop_msg)

    with pytest.raises(asyncio.CancelledError):
        await turn_task

    # The /stop itself sent nothing — the cancelled turn posts its own notice.
    assert stop_msg.replies == []
    # The cancelled turn's notice is plain-text, quoting the interrupted message.
    assert turn.replies, "the cancelled turn must post a stop notice"
    notice = turn.replies[-1]
    assert notice["msg_type"] == 0
    assert "已停止" in notice["content"]
    assert notice["message_reference"] == {"message_id": "77", "ignore_get_message_error": True}
    # The in-flight handle is cleaned up once the cancelled turn unwinds.
    assert cid not in channel._in_flight


# ---------------------------------------------------------------------------
# reply-quoting (message_reference on the first chunk of a normal answer only)
# ---------------------------------------------------------------------------
async def test_single_answer_quotes_user_message():
    service = _FakeService(reply="你好")
    channel = _channel(service, _FakeRepo(conv_id=5))

    msg = _FakeMessage("hi", openid="openid-alice", msg_id=99)
    await channel.on_c2c_message_create(msg)

    assert len(msg.replies) == 1
    assert msg.replies[0]["message_reference"] == {"message_id": "99", "ignore_get_message_error": True}


async def test_long_answer_quotes_first_chunk_only():
    service = _FakeService(reply="line\n" * 2000)  # multi-chunk reply
    channel = _channel(service, _FakeRepo(conv_id=5))

    msg = _FakeMessage("hi", openid="openid-alice", msg_id=4242)
    await channel.on_c2c_message_create(msg)

    assert len(msg.replies) >= 2
    assert msg.replies[0]["message_reference"] == {
        "message_id": "4242", "ignore_get_message_error": True}
    for r in msg.replies[1:]:
        assert "message_reference" not in r, "only the first chunk quotes"


async def test_command_ack_does_not_quote():
    channel = _channel(_FakeService(), _FakeRepo(conv_id=5))

    msg = _FakeMessage("/help", openid="openid-alice", msg_id=50)
    await channel.on_c2c_message_create(msg)

    assert msg.replies, "/help must reply"
    for r in msg.replies:
        assert "message_reference" not in r, "command acks must not quote the user message"


# ---------------------------------------------------------------------------
# the native command panel (pure item builder + the create-or-update REST path)
# ---------------------------------------------------------------------------
def test_build_c2c_panel_items_drops_long_names_and_caps():
    items = build_c2c_panel_items()
    names = [i["name"] for i in items]
    assert all(
        i["type"] == "command" and i["name"].startswith("/") and i["desc"] for i in items
    )
    # /schedule_status (16 chars incl. the slash) is dropped by the 14-char cap.
    assert "/schedule_status" not in names
    # Every command whose "/name" fits the cap is present, in order, and each
    # panel description is the command's (Chinese) description — the same value
    # the ``/help`` reply shows, so the two surfaces never drift.
    for name, desc in _QQ_COMMANDS:
        if len(f"/{name}") <= 14:
            item = next(i for i in items if i["name"] == f"/{name}")
            assert item["desc"] == desc
    assert len(items) <= 20


def test_c2c_panel_payload_shape_and_privacy():
    p = _c2c_panel_payload()
    assert p["scope"] == "c2c"
    assert p["target_type"] == "all"
    assert p["panel"]["remark"] == "fibrecase-c2c"
    assert len(p["panel"]["items"]) == len(build_c2c_panel_items())
    # The panel carries only command names + the opaque remark — no openid/body.
    assert "openid" not in repr(p)


def test_known_command_names_matches_command_table():
    assert known_command_names() == {name for name, _desc in _QQ_COMMANDS}


async def test_ensure_panel_creates_when_absent():
    http = _FakeHttp(get_response={"records": [], "is_end": True})
    await _ensure_c2c_panel(http, _c2c_panel_payload())

    assert [c[0] for c in http.calls] == ["GET", "POST"]
    assert http.calls[1][1] == "/v2/panels"
    body = http.calls[1][2]["json"]
    assert body["scope"] == "c2c" and body["panel"]["remark"] == "fibrecase-c2c"


async def test_ensure_panel_updates_when_marker_found():
    get = {
        "records": [
            {
                "panel_id": "p_abc",
                "version": 4,
                "panel": {"remark": "fibrecase-c2c", "items": []},
            }
        ],
        "is_end": True,
    }
    http = _FakeHttp(get_response=get)
    await _ensure_c2c_panel(http, _c2c_panel_payload())

    assert [c[0] for c in http.calls] == ["GET", "PUT"]
    assert http.calls[1][1] == "/v2/panels/{panel_id}"
    panel = http.calls[1][2]["json"]["panel"]
    assert panel["remark"] == "fibrecase-c2c"
    assert panel["version"] == 4, "the record's version is sent for optimistic locking"


async def test_ensure_panel_ignores_other_remarks():
    # A panel with a *different* remark is not ours — we must create, not update.
    get = {"records": [{"panel_id": "p_x", "version": 1, "panel": {"remark": "someone-else"}}], "is_end": True}
    http = _FakeHttp(get_response=get)
    await _ensure_c2c_panel(http, _c2c_panel_payload())
    assert [c[0] for c in http.calls] == ["GET", "POST"]


async def test_ensure_panel_swallows_errors():
    http = _FakeHttp(raise_on=RuntimeError("boom"))
    await _ensure_c2c_panel(http, _c2c_panel_payload())  # must not raise
    assert [c[0] for c in http.calls] == ["GET"]  # it died on the first call


async def test_on_ready_wires_panel():
    client = build_qq_client(_FakeService(), _FakeRepo(), _FakeConfig(), None)
    http = _FakeHttp(get_response={"records": [], "is_end": True})
    client.http = http

    await client.on_ready()

    # Panel (create-or-update) then the global menu (a plain replace).
    assert [c[0] for c in http.calls] == ["GET", "POST", "PUT"]


# ---------------------------------------------------------------------------
# global custom menu (v2_menu) — the C2C "⋮" menu with two send_message items
# ---------------------------------------------------------------------------
def test_global_menu_payload_shape_and_privacy():
    from fibrecase_agent_backend.qq.bot import _global_menu_payload

    p = _global_menu_payload()
    items = p["menu"]["items"]
    assert len(items) == 2
    # The two fixed send_message items: a "/help" shortcut (dispatches the
    # command list) and a plain question sent as a normal agent turn.
    assert items[0] == {"type": "send_message", "name": "对话指令", "send_message": "/help"}
    assert items[1] == {"type": "send_message", "name": "工具能力", "send_message": "你会使用哪些工具？"}
    # The menu is fixed and content-free: no openid, no command argument, no body
    # beyond the two literals we fully control.
    blob = str(p)
    assert "openid" not in blob


async def test_ensure_global_menu_puts_fixed_payload():
    from fibrecase_agent_backend.qq.bot import _ensure_global_menu, _global_menu_payload

    http = _FakeHttp()
    await _ensure_global_menu(http)

    assert len(http.calls) == 1
    method, path, kwargs = http.calls[0]
    assert method == "PUT"
    assert path == "/v2/menu"
    # The body is the fixed two-item payload — PUT /v2/menu replaces the whole menu.
    assert kwargs["json"] == _global_menu_payload()


async def test_ensure_global_menu_swallows_errors():
    from fibrecase_agent_backend.qq.bot import _ensure_global_menu

    http = _FakeHttp(raise_on=RuntimeError("boom"))
    await _ensure_global_menu(http)  # must not raise
    assert [c[0] for c in http.calls] == ["PUT"]


# ---------------------------------------------------------------------------
# shutdown teardown (main.py::_qq_shutdown_tasks) — cancel the QQ tasks the SDK
# spawns on the shared PTB loop, leaving unrelated in-flight work untouched.
#
# This is the regression for the Ctrl+C "Task was destroyed but it is pending"
# + "RuntimeError: coroutine ignored GeneratorExit" noise: botpy's
# Client.close() only closes the HTTP client, so the connection-runner /
# websocket / heartbeat coroutines it spawned on our loop must be cancelled
# explicitly by the backend, keyed off the pre-start task baseline.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# shutdown teardown (main.py::_qq_shutdown_tasks) — cancel the QQ tasks the SDK
# spawns on the shared PTB loop, leaving unrelated in-flight work untouched.
#
# This is the regression for the Ctrl+C "Task was destroyed but it is pending"
# + "RuntimeError: coroutine ignored GeneratorExit" noise: botpy's
# Client.close() only closes the HTTP client, so the connection-runner /
# websocket / heartbeat coroutines it spawned on our loop must be cancelled
# explicitly by the backend, keyed off the pre-start task *id* baseline.
# ---------------------------------------------------------------------------
def _bare_backend():
    # A minimal object carrying only the attribute the teardown reads; the method
    # is the real bound one, so we don't build a full AgentBackend (which would
    # construct an engine/LLM/registry).
    from fibrecase_agent_backend.main import AgentBackend

    return AgentBackend.__new__(AgentBackend)


def _id_baseline(exclude=()):
    """The production baseline shape: a frozenset of task *ids* (not tasks)."""
    return frozenset(id(t) for t in asyncio.all_tasks() if t not in exclude)


async def test_qq_shutdown_tasks_cancels_qq_tasks_not_unrelated():
    backend = _bare_backend()

    # The "unrelated" task that was already pending *before* the QQ subsystem
    # started (e.g. an in-progress Telegram approval callback or scheduled run).
    unrelated_started = asyncio.Event()

    async def _unrelated():
        unrelated_started.set()
        await asyncio.sleep(30)

    unrelated = asyncio.create_task(_unrelated())
    await unrelated_started.wait()

    # Snapshot the baseline now (task ids): the current test task + ``unrelated``
    # are all "pre-QQ". Everything created after this is attributed to QQ.
    backend._qq_pending_before = _id_baseline()

    # The QQ outer task plus the SDK's own background tasks (connection-runner +
    # heartbeat), all created *after* the baseline — mirroring ``_post_init``.
    async def _qq_loop():
        while True:
            await asyncio.sleep(30)

    qq_task = asyncio.create_task(_qq_loop())
    inner_a = asyncio.create_task(_qq_loop())
    inner_b = asyncio.create_task(_qq_loop())
    await asyncio.sleep(0)  # let the post-baseline tasks start

    await backend._qq_shutdown_tasks()

    # The outer QQ task and both SDK-spawned inner tasks are cancelled.
    assert qq_task.cancelled(), "the outer QQ task must be cancelled"
    assert inner_a.cancelled() and inner_b.cancelled(), "the SDK's inner tasks must be cancelled"
    # The unrelated task (pending before the QQ baseline) is left running.
    assert not unrelated.cancelled(), "unrelated in-flight work must not be cancelled"
    unrelated.cancel()
    with pytest.raises(asyncio.CancelledError):
        await unrelated


async def test_qq_shutdown_tasks_with_no_qq_tasks_is_noop():
    backend = _bare_backend()

    # QQ never started: the baseline covers the whole task set, so the teardown's
    # diff is empty — it cancels nothing and does not hang. An in-flight
    # unrelated task must survive.
    async def _unrelated():
        await asyncio.sleep(30)

    unrelated = asyncio.create_task(_unrelated())
    await asyncio.sleep(0)
    backend._qq_pending_before = _id_baseline()  # everything is "pre-QQ"

    await backend._qq_shutdown_tasks()  # must not raise or hang

    assert not unrelated.cancelled(), "with no QQ tasks, nothing unrelated may be cancelled"
    unrelated.cancel()
    with pytest.raises(asyncio.CancelledError):
        await unrelated


async def test_qq_shutdown_tasks_collects_a_raising_task_without_raising():
    backend = _bare_backend()

    # A QQ task that ends in an *exception* must be collected safely by the
    # teardown (``gather(..., return_exceptions=True)``) — it must not raise out
    # of the shutdown and must not drag down unrelated in-flight work. This
    # guards the invariant that a misbehaving QQ teardown can never abort the
    # LLM/DB close.
    async def _explode():
        raise RuntimeError("boom")

    bad = asyncio.create_task(_explode())
    await asyncio.sleep(0)

    async def _unrelated():
        await asyncio.sleep(30)

    unrelated = asyncio.create_task(_unrelated())
    await asyncio.sleep(0)
    # Baseline = every task except ``bad``: so the teardown's diff targets only
    # ``bad`` (the QQ task), leaving ``unrelated`` and the current task untouched.
    backend._qq_pending_before = _id_baseline(exclude=(bad,))

    await backend._qq_shutdown_tasks()  # must not raise

    assert bad.done(), "the raising QQ task is collected (not left pending)"
    assert not unrelated.cancelled(), "unrelated work must not be cancelled"
    unrelated.cancel()
    with pytest.raises(asyncio.CancelledError):
        await unrelated


# ---------------------------------------------------------------------------
# tool approval (QQ button-interaction transport — the QQApprovalBroker)
# ---------------------------------------------------------------------------
class _FakeApi:
    """A stand-in for the ``botpy`` client's ``api`` (the approval send + ack).

    ``post_c2c_message`` records its kwargs (openid / msg_type / markdown /
    keyboard) and raises if ``raise_send`` is set (to prove the fail-closed
    path). ``on_interaction_result`` records ``(interaction_id, code)``.
    """

    def __init__(self, raise_send=None):
        self.sent = []
        self.acks = []
        self.raise_send = raise_send

    async def post_c2c_message(self, **kwargs):
        if self.raise_send is not None:
            raise self.raise_send
        self.sent.append(dict(kwargs))
        return {"id": "ROBOT1.0_fake"}

    async def on_interaction_result(self, interaction_id, code):
        self.acks.append((interaction_id, code))


class _FakeClient:
    """A minimal stand-in for the ``botpy`` Client (just holds ``.api``)."""

    def __init__(self, api):
        self.api = api


def _make_request(scope="qq:openid-alice", tool_name="exec", summary="run a shell command",
                  arguments=None, detail="", language=""):
    return ApprovalRequest(
        request_id="req123",
        conversation_id=5,
        scope=scope,
        tool_name=tool_name,
        summary=summary,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        arguments=dict(arguments or {}),
        detail=detail,
        language=language,
    )


def _fake_interaction(interaction_id="int-1", openid="openid-alice", button_data="v1:req123:a", itype=11):
    resolved = type("R", (), {"button_id": "allow", "button_data": button_data,
                              "message_id": "ROBOT1.0_fake", "user_id": None, "feature_id": None})()
    data = type("D", (), {"type": itype, "resolved": resolved})()
    return type("I", (), {"id": interaction_id, "user_openid": openid, "data": data,
                          "type": itype, "scene": "c2c"})()


async def test_qq_approval_approve_resolves_and_acks_success(caplog):
    api = _FakeApi()
    broker = QQApprovalBroker()
    broker.bind_client(_FakeClient(api))
    openid = "openid-alice"

    with caplog.at_level("INFO", logger="qq.approval"):
        decision_task = asyncio.create_task(broker.request_approval(_make_request(scope=f"qq:{openid}")))
        await asyncio.sleep(0)  # let request_approval register the pending + send the card
        assert len(api.sent) == 1, "the approval card must be sent to the turn's openid"
        assert api.sent[0]["openid"] == openid
        assert api.sent[0]["msg_type"] == 2

        await broker.handle_interaction(_fake_interaction(interaction_id="int-1", openid=openid,
                                                           button_data="v1:req123:a"))
        decision = await asyncio.wait_for(decision_task, timeout=2)

    assert decision is ApprovalDecision.APPROVED
    # The card carries an Approve + Deny callback-button row with the request id
    # embedded in the opaque button data (never an openid or the tool name).
    kb = api.sent[0]["keyboard"]["content"]["rows"][0]["buttons"]
    labels = {b["render_data"]["label"]: b["action"] for b in kb}
    assert labels["✅ 批准"]["type"] == 1 and labels["✅ 批准"]["data"] == "v1:req123:a"
    assert labels["❌ 拒绝"]["type"] == 1 and labels["❌ 拒绝"]["data"] == "v1:req123:d"
    # The click was acked as success (code 0) so the client stops spinning.
    assert api.acks == [("int-1", 0)]
    # Privacy: the resolved log line carries a scope *hash*, the tool name, and
    # the decision — never the raw openid.
    joined = [v for rec in caplog.records for v in _all_str_fields(rec)]
    assert not any(openid in v for v in joined), "the raw openid must never be logged"
    assert any("exec" in v for v in joined), "the tool name is a safe identifier and is logged"


async def test_qq_approval_deny_resolves():
    api = _FakeApi()
    broker = QQApprovalBroker()
    broker.bind_client(_FakeClient(api))

    decision_task = asyncio.create_task(broker.request_approval(_make_request()))
    await asyncio.sleep(0)

    await broker.handle_interaction(_fake_interaction(openid="openid-alice", button_data="v1:req123:d"))
    decision = await asyncio.wait_for(decision_task, timeout=2)

    assert decision is ApprovalDecision.DENIED
    assert api.acks == [("int-1", 0)]


async def test_qq_approval_foreign_openid_is_rejected_and_voids():
    # A click from a *different* openid than the one running the turn must not
    # approve — it voids the pending request (→ EXPIRED) and acks failure (code 1).
    api = _FakeApi()
    broker = QQApprovalBroker()
    broker.bind_client(_FakeClient(api))

    decision_task = asyncio.create_task(broker.request_approval(_make_request(scope="qq:openid-alice")))
    await asyncio.sleep(0)

    await broker.handle_interaction(_fake_interaction(openid="openid-EVIL", button_data="v1:req123:a"))
    decision = await asyncio.wait_for(decision_task, timeout=2)

    assert decision is ApprovalDecision.EXPIRED
    assert api.acks == [("int-1", 1)]


async def test_qq_approval_unknown_request_acks_failure():
    api = _FakeApi()
    broker = QQApprovalBroker()
    broker.bind_client(_FakeClient(api))

    # No pending request with this id (stale button / already consumed).
    await broker.handle_interaction(_fake_interaction(openid="openid-alice", button_data="v1:ghost:a"))

    assert api.acks == [("int-1", 1)]


async def test_qq_approval_non_button_interaction_is_ignored():
    api = _FakeApi()
    broker = QQApprovalBroker()
    broker.bind_client(_FakeClient(api))

    # A non-button interaction type (e.g. message feedback, type 13) is not ours:
    # no ack, no resolution.
    await broker.handle_interaction(_fake_interaction(openid="openid-alice", itype=13))

    assert api.acks == [], "a non-button interaction must not be acked"
    assert api.sent == []


async def test_qq_approval_without_client_fails_closed():
    broker = QQApprovalBroker()  # no client bound

    decision = await broker.request_approval(_make_request())

    assert decision is ApprovalDecision.DENIED


async def test_qq_approval_send_failure_fails_closed():
    broker = QQApprovalBroker()
    broker.bind_client(_FakeClient(_FakeApi(raise_send=RuntimeError("send down"))))

    decision = await broker.request_approval(_make_request())  # must not raise

    assert decision is ApprovalDecision.DENIED


async def test_qq_approval_expires_when_no_decision():
    broker = QQApprovalBroker()
    broker.bind_client(_FakeClient(_FakeApi()))

    # expires_at in the past → the bounded wait times out immediately → EXPIRED.
    request = _make_request()
    request = ApprovalRequest(
        request_id=request.request_id, conversation_id=request.conversation_id,
        scope=request.scope, tool_name=request.tool_name, summary=request.summary,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        arguments=request.arguments,
    )
    decision = await asyncio.wait_for(broker.request_approval(request), timeout=2)

    assert decision is ApprovalDecision.EXPIRED


async def test_qq_approval_shutdown_resolves_pending_expired():
    api = _FakeApi()
    broker = QQApprovalBroker()
    broker.bind_client(_FakeClient(api))

    decision_task = asyncio.create_task(broker.request_approval(_make_request()))
    await asyncio.sleep(0)  # card sent, future pending
    assert not decision_task.done()

    await broker.shutdown()
    decision = await asyncio.wait_for(decision_task, timeout=2)

    assert decision is ApprovalDecision.EXPIRED


async def test_qq_approval_one_time_repeat_click_fails():
    api = _FakeApi()
    broker = QQApprovalBroker()
    broker.bind_client(_FakeClient(api))

    decision_task = asyncio.create_task(broker.request_approval(_make_request()))
    await asyncio.sleep(0)

    # First (valid) click consumes the request and acks success.
    await broker.handle_interaction(_fake_interaction(openid="openid-alice", button_data="v1:req123:a"))
    decision = await asyncio.wait_for(decision_task, timeout=2)
    assert decision is ApprovalDecision.APPROVED
    # A repeat click on the same (now consumed) id acks failure.
    await broker.handle_interaction(_fake_interaction(openid="openid-alice", button_data="v1:req123:a"))
    assert api.acks == [("int-1", 0), ("int-1", 1)]


async def test_qq_approval_button_data_ack_code_swallowed():
    # A failed ack (client.api raising) must not propagate out of the handler.
    class _BrokenApi(_FakeApi):
        async def on_interaction_result(self, interaction_id, code):
            raise RuntimeError("ack down")

    broker = QQApprovalBroker()
    broker.bind_client(_FakeClient(_BrokenApi()))

    decision_task = asyncio.create_task(broker.request_approval(_make_request()))
    await asyncio.sleep(0)
    await broker.handle_interaction(_fake_interaction(openid="openid-alice", button_data="v1:req123:a"))
    decision = await asyncio.wait_for(decision_task, timeout=2)  # must not raise
    assert decision is ApprovalDecision.APPROVED


# --- the approval card (secret-free Markdown) ------------------------------
def test_qq_approval_card_shows_tool_and_summary_not_openid():
    text = _card_text(_make_request(scope="qq:openid-alice", tool_name="exec",
                                    summary="run a shell command"))
    assert "exec" in text and "run a shell command" in text
    assert "openid-alice" not in text, "the card must never carry the raw openid"
    assert "需要批准" in text or "工具" in text


def test_qq_approval_card_detail_replaces_arguments_json():
    # A tool that supplies a friendly detail (e.g. exec's bash block) renders it
    # under an Action: label with the language tag — not the generic JSON block.
    text = _card_text(_make_request(detail="$ ls -la", language="bash", arguments={"command": "ls -la"}))
    assert "```bash" in text and "ls -la" in text
    assert "**操作：**" in text
    assert "**参数：**" not in text, "the detail block replaces the generic Arguments block"


def test_qq_approval_card_arguments_json_when_no_detail():
    text = _card_text(_make_request(arguments={"command": "ls -la"}))
    assert "**参数：**" in text and "```json" in text and "ls -la" in text


def test_qq_approval_keyboard_binds_request_id_only():
    kb = _approval_keyboard("reqXYZ")
    buttons = kb["content"]["rows"][0]["buttons"]
    datas = {b["action"]["data"] for b in buttons}
    assert datas == {"v1:reqXYZ:a", "v1:reqXYZ:d"}
    # The button data carries the version + request id + decision only.
    assert not any("openid" in d for d in datas)


def test_qq_approval_button_data_parsing():
    assert request_id_from("v1:req123:a") == "req123"
    assert decision_from("v1:req123:a") is ApprovalDecision.APPROVED
    assert decision_from("v1:req123:d") is ApprovalDecision.DENIED
    assert decision_from("v2:req123:a") is None, "a wrong version is not a valid decision"
    assert decision_from("garbage") is None


# --- the scope-routing provider (main.py's single approval_provider) --------
class _RecordingBroker:
    """A fake approval provider that records calls and returns a canned decision."""

    def __init__(self, tag, decision):
        self.tag = tag
        self.decision = decision
        self.calls = []

    async def request_approval(self, request):
        self.calls.append(request)
        return self.decision

    async def shutdown(self):
        pass


async def test_routing_provider_dispatches_by_scope_prefix():
    tele = _RecordingBroker("telegram", ApprovalDecision.DENIED)
    qq = _RecordingBroker("qq", ApprovalDecision.APPROVED)
    router = QQScopedApprovalRouter(tele, qq)

    assert await router.request_approval(_make_request(scope="qq:openid-alice")) is ApprovalDecision.APPROVED
    assert await router.request_approval(_make_request(scope="telegram:42")) is ApprovalDecision.DENIED
    # Each broker got exactly its own requests.
    assert len(qq.calls) == 1 and qq.calls[0].scope.startswith("qq:")
    assert len(tele.calls) == 1 and tele.calls[0].scope.startswith("telegram:")


async def test_routing_provider_shutdown_drains_both():
    tele = _RecordingBroker("telegram", ApprovalDecision.DENIED)
    qq = _RecordingBroker("qq", ApprovalDecision.APPROVED)

    class _ShutdownRecorder(_RecordingBroker):
        def __init__(self, tag):
            super().__init__(tag, ApprovalDecision.DENIED)
            self.shutdown_calls = 0

        async def shutdown(self):
            self.shutdown_calls += 1

    t = _ShutdownRecorder("t")
    q = _ShutdownRecorder("q")
    router = QQScopedApprovalRouter(t, q)
    await router.shutdown()
    assert t.shutdown_calls == 1 and q.shutdown_calls == 1


# --- build_qq_client wires the interaction handler + intent ----------------
async def test_build_qq_client_wires_interaction_handler():
    import botpy

    broker = QQApprovalBroker()
    client = build_qq_client(
        _FakeService(), _FakeRepo(), _FakeConfig(), None, approval_broker=broker
    )
    # The interaction intent bit (1<<26) must now be set alongside public_messages.
    assert client.intents == botpy.Intents(public_messages=True, interaction=True).value
    # The client was bound to the broker (so it can send cards).
    assert broker._client is client

    # A button click routed through on_interaction_create reaches the broker
    # (here an unknown id → ack failure, no exception).
    broker.bind_client(client)  # re-bind to the real client's api
    class _CaptureApi:
        async def on_interaction_result(self, interaction_id, code):
            self.ack = (interaction_id, code)
    capture = _CaptureApi()
    client.api = capture
    await client.on_interaction_create(_fake_interaction(openid="openid-alice", button_data="v1:ghost:a"))
    assert capture.ack == ("int-1", 1)


async def test_build_qq_client_no_approval_leaves_interaction_intent_off():
    import botpy

    client = build_qq_client(_FakeService(), _FakeRepo(), _FakeConfig(), None)
    # Without an approval broker, the interaction intent is off (no button events
    # are requested) and on_interaction_create is a harmless no-op.
    assert client.intents == botpy.Intents(public_messages=True).value
    await client.on_interaction_create(_fake_interaction())  # must not raise
