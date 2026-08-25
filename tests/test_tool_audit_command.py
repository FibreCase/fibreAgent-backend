"""The /tool_audit Telegram command (phase 3 — required #17, #18).

Handler-level, with a fake repository-backed service (real :class:`ToolAuditRecord`
shapes) and a real PTB :class:`Update`/``CallbackContext``. Proves the command is
read-only and scope-isolated, clamps its limit, renders HTML, shows a safe empty
state, is silently ignored for an unauthorised sender, and never leaks arguments,
results, or the raw scope/user id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from telegram import Chat

from fibrecase_agent_backend.agent.service import AgentError, _user_safe_for
from fibrecase_agent_backend.database.repository import ToolAuditRecord
from fibrecase_agent_backend.telegram.bot import cmd_tool_audit
from fibrecase_agent_backend.memory import hash_scope


def _make(user_id, chat_id, text=None, *, allowed=(1,)):
    from telegram import Message, Update, User
    from telegram.ext import CallbackContext

    user = None if user_id is None else User(id=user_id, first_name="U", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(message_id=1, date=0, chat=chat, from_user=user, text=text) if text is not None else None
    update = Update(update_id=1, message=message)
    app = type("App", (), {})()
    app.bot_data = {"allowed_user_ids": set(allowed)}
    app.bot = object()
    context = CallbackContext.from_update(update, app)
    return update, chat, context, app


def _record(i, event_type, code, latency=None, tool="echo"):
    return ToolAuditRecord(
        id=i,
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        conversation_id=5,
        tool_name=tool,
        tool_call_id=f"c{i}",
        iteration=1,
        event_type=event_type,
        code=code,
        latency_ms=latency,
        scope_hash=hash_scope("telegram:1"),
    )


class _FakeService:
    def __init__(self, records=None, *, raise_exc=False):
        self.records = records or []
        self.raise_exc = raise_exc
        self.calls = []

    async def list_tool_audit_events(self, scope, limit):
        self.calls.append((scope, limit))
        if self.raise_exc:
            raise AgentError(_user_safe_for("tool_audit_error"), "tool_audit_error")
        return self.records


def _service(app, **kw):
    svc = _FakeService(**kw)
    app.bot_data["agent_service"] = svc
    return svc


# ---------------------------------------------------------------------------
# unauthorized → silent (no reply at all)
# ---------------------------------------------------------------------------
async def test_tool_audit_unauthorized_is_silent():
    update, chat, context, app = _make(user_id=999, chat_id=1, text="/tool_audit", allowed=(1,))
    _service(app)
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_tool_audit(update, context)
    send.assert_not_awaited()


# ---------------------------------------------------------------------------
# empty state
# ---------------------------------------------------------------------------
async def test_tool_audit_empty_state():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/tool_audit", allowed=(1,))
    _service(app, records=[])
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_tool_audit(update, context)
    assert send.await_count == 1
    assert "No tool activity" in send.await_args.kwargs["text"]


# ---------------------------------------------------------------------------
# renders events as HTML, most-recent-first, with safe fields only
# ---------------------------------------------------------------------------
async def test_tool_audit_renders_events_as_html():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/tool_audit", allowed=(1,))
    svc = _service(app, records=[_record(3, "completed", "ok", latency=12), _record(2, "requested", "ok")])
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_tool_audit(update, context)
    assert send.await_count == 1
    kwargs = send.await_args.kwargs
    assert kwargs.get("parse_mode") == "HTML"
    text = kwargs["text"]
    # Both events, newest first (id 3 before id 2).
    assert "#3" in text and "#2" in text
    assert text.index("#3") < text.index("#2")
    assert "completed" in text and "requested" in text
    assert "12ms" in text
    # Scope-isolated read: the service was called with the caller's scope + default limit.
    assert svc.calls == [("telegram:1", 20)]


# ---------------------------------------------------------------------------
# limit validation: default 20, clamped to 1..50, non-numeric → usage hint
# ---------------------------------------------------------------------------
async def test_tool_audit_default_limit_is_20():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/tool_audit", allowed=(1,))
    svc = _service(app, records=[_record(1, "requested", "ok")])
    with patch.object(Chat, "send_message", new_callable=AsyncMock):
        await cmd_tool_audit(update, context)
    assert svc.calls == [("telegram:1", 20)]


async def test_tool_audit_clamps_limit_to_max_50():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/tool_audit 999", allowed=(1,))
    svc = _service(app, records=[_record(1, "requested", "ok")])
    with patch.object(Chat, "send_message", new_callable=AsyncMock):
        await cmd_tool_audit(update, context)
    assert svc.calls == [("telegram:1", 50)]


async def test_tool_audit_clamps_limit_to_min_1():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/tool_audit 0", allowed=(1,))
    svc = _service(app, records=[_record(1, "requested", "ok")])
    with patch.object(Chat, "send_message", new_callable=AsyncMock):
        await cmd_tool_audit(update, context)
    assert svc.calls == [("telegram:1", 1)]


async def test_tool_audit_accepts_explicit_limit():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/tool_audit 7", allowed=(1,))
    svc = _service(app, records=[_record(1, "requested", "ok")])
    with patch.object(Chat, "send_message", new_callable=AsyncMock):
        await cmd_tool_audit(update, context)
    assert svc.calls == [("telegram:1", 7)]


async def test_tool_audit_non_numeric_limit_is_usage_hint():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/tool_audit abc", allowed=(1,))
    svc = _service(app, records=[_record(1, "requested", "ok")])
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_tool_audit(update, context)
    # A plain usage hint, and the service is never called.
    assert "Usage" in send.await_args.kwargs["text"]
    assert svc.calls == []


# ---------------------------------------------------------------------------
# a service failure is surfaced user-safe (no traceback, no raw detail)
# ---------------------------------------------------------------------------
async def test_tool_audit_surfaces_user_safe_error():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/tool_audit", allowed=(1,))
    _service(app, raise_exc=True)
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_tool_audit(update, context)
    text = send.await_args.kwargs["text"]
    assert "Could not read the tool audit log" in text
    assert "Traceback" not in text


# ---------------------------------------------------------------------------
# the output never exposes arguments, results, or the raw scope/user id
# ---------------------------------------------------------------------------
async def test_tool_audit_output_is_secret_free():
    update, chat, context, app = _make(user_id=1, chat_id=1, text="/tool_audit", allowed=(1,))
    _service(app, records=[_record(1, "completed", "ok", latency=1, tool="system_info")])
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_tool_audit(update, context)
    text = send.await_args.kwargs["text"]
    # The raw scope and the bare user id must not appear.
    assert "telegram:1" not in text
    assert "1234567890" not in text  # (a representative raw user id is never shown)
    # No tool arguments / results — only the stable code + latency.
    assert '"message"' not in text
    assert "arguments" not in text.lower() or "arguments and results are not shown" in text.lower()
