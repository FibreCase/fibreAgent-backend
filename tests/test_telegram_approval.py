"""The in-memory Telegram approval broker (phase 3 — required #9–#13).

Everything is faked: a fake repository (conversation→chat resolution), a fake
PTB application (records every ``send_message``), and hand-built callback
``Update`` objects. No network, no real Telegram, no real LLM. It proves the
broker presents a secret-free Approve/Deny prompt bound to one ``(user, chat)``,
resolves it one-time, refuses everyone else, expires/cancels safely, and that a
blocked conversation stays ordered while another proceeds in parallel.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from telegram import InlineKeyboardMarkup

from fibrecase_agent_backend.agent.service import AgentService
from fibrecase_agent_backend.llm.client import LLMResult
from fibrecase_agent_backend.memory import hash_scope
from fibrecase_agent_backend.telegram.approval import (
    TelegramApprovalBroker,
    _ARGUMENTS_MAX_CHARS,
    _ARGUMENTS_TRUNCATED_MARK,
    _arguments_block,
    _bound_arguments,
    _card_text,
    decision_from,
    request_id_from,
)
from fibrecase_agent_backend.tools import (
    ApprovalDecision,
    ApprovalRequest,
    ToolRegistry,
    build_default_tools,
    build_policy,
)
from fibrecase_agent_backend.tools.base import Tool

CHAT_A = 100
USER_A = 7


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _FakeMessage:
    """A sent-message stand-in carrying the id the card is edited by."""

    def __init__(self, message_id):
        self.message_id = message_id


class _FakeBot:
    def __init__(self):
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self._next_id = 1

    async def send_message(self, chat_id, text, **kwargs):
        message = _FakeMessage(self._next_id)
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "message_id": message.message_id, "text": text, **kwargs})
        return message

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kwargs})


class _FakeApp:
    def __init__(self):
        self.bot = _FakeBot()


class _FakeRepo:
    def __init__(self, chat_id: int = CHAT_A, *, exists: bool = True):
        self.chat_id = chat_id
        self.exists = exists

    async def get_conversation_by_id(self, conversation_id):
        if not self.exists:
            return None
        return type("C", (), {"telegram_chat_id": self.chat_id})()


class _FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers: list[str] = []

    async def answer(self, text):
        self.answers.append(text)


class _FakeUpdate:
    def __init__(self, data, chat_id, user_id):
        self.callback_query = _FakeQuery(data)
        self.effective_chat = type("C", (), {"id": chat_id})()
        self.effective_user = type("U", (), {"id": user_id})()


def _request(request_id="req1", *, scope=f"telegram:{USER_A}", tool="risky", seconds=60, summary=None, arguments=None, detail=None, language=None, metadata=None):
    return ApprovalRequest(
        request_id=request_id,
        conversation_id=5,
        scope=scope,
        tool_name=tool,
        summary=summary or f"{tool} does a thing.",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds),
        arguments=arguments if arguments is not None else {},
        detail=detail if detail is not None else "",
        language=language if language is not None else "",
        metadata=metadata if metadata is not None else {},
    )


_approve_label = "✅ Approve"
_deny_label = "❌ Deny"


def _callback_data(sent, button_text):
    """The callback_data of the named button on the first sent keyboard."""
    keyboard = sent["reply_markup"]
    for row in keyboard.inline_keyboard:
        for btn in row:
            if btn.text == button_text:
                return btn.callback_data
    raise AssertionError(f"button {button_text!r} not found in keyboard")


async def _await_pending(broker, request_id, *, tries=50):
    """Let the broker get to the point where it is awaiting the callback."""
    for _ in range(tries):
        await asyncio.sleep(0)
        if request_id in broker._pending:
            return
    raise AssertionError("approval never entered the pending state")


# ---------------------------------------------------------------------------
# required #9 — ask tool: Approve/Deny keyboard, approve → runs exactly once
# ---------------------------------------------------------------------------
async def test_approve_executes_exactly_once():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)

    req = _request("r1")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "r1")

    # The prompt went to the *original* chat with Approve + Deny buttons.
    sent = app.bot.sent[0]
    assert sent["chat_id"] == CHAT_A
    approve = _callback_data(sent, _approve_label)
    deny = _callback_data(sent, _deny_label)
    assert approve != deny

    # The owner (same user + chat) clicks Approve.
    await broker.handle_callback(_FakeUpdate(approve, CHAT_A, USER_A), None)
    assert await task == ApprovalDecision.APPROVED
    # The request was consumed: nothing left pending.
    assert "r1" not in broker._pending


async def test_deny_click_does_not_execute():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("r2")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "r2")
    deny = _callback_data(app.bot.sent[0], _deny_label)
    await broker.handle_callback(_FakeUpdate(deny, CHAT_A, USER_A), None)
    assert await task == ApprovalDecision.DENIED
    assert "r2" not in broker._pending


# ---------------------------------------------------------------------------
# the card is finalised in place: buttons removed + hint replaced by a status
# ---------------------------------------------------------------------------
def _edited_text(edited):
    return edited[0]["text"] if edited else ""


async def test_approved_card_is_finalised_in_place():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("ra", tool="risky")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "ra")

    prompt = app.bot.sent[0]
    # The prompt's buttons carry an emoji label…
    button_texts = [btn.text for row in prompt["reply_markup"].inline_keyboard for btn in row]
    assert _approve_label in button_texts
    assert _deny_label in button_texts
    approve = _callback_data(prompt, _approve_label)
    await broker.handle_callback(_FakeUpdate(approve, CHAT_A, USER_A), None)
    assert await task == ApprovalDecision.APPROVED

    # The original card carried the live Approve/Deny buttons and the hint…
    assert prompt["reply_markup"].inline_keyboard
    assert "<i>This approval is one-time" in prompt["text"]

    # …and after the decision the SAME message is edited once in place: the
    # buttons are gone (empty keyboard) and the hint is replaced by a status.
    assert len(app.bot.edited) == 1
    edit = app.bot.edited[0]
    assert edit["chat_id"] == CHAT_A
    assert edit["message_id"] == prompt["message_id"]  # same card, not a new message
    assert edit["reply_markup"] == InlineKeyboardMarkup([])
    assert edit["parse_mode"] == "HTML"
    text = _edited_text(app.bot.edited)
    # Bold, emoji-tagged status word — no "Status:" label, no old hint.
    assert "<b>✅ Approved.</b>" in text
    assert "Status:" not in text
    assert "<i>This approval is one-time" not in text
    # Still secret-free after the edit: tool name shown, no ids/scope.
    assert "risky" in text
    assert "telegram:" not in text and "100" not in text and "7" not in text


async def test_denied_card_is_finalised_in_place():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("rb")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "rb")
    deny = _callback_data(app.bot.sent[0], _deny_label)
    await broker.handle_callback(_FakeUpdate(deny, CHAT_A, USER_A), None)
    assert await task == ApprovalDecision.DENIED

    assert len(app.bot.edited) == 1
    assert app.bot.edited[0]["reply_markup"] == InlineKeyboardMarkup([])
    assert "<b>❌ Denied.</b>" in _edited_text(app.bot.edited)


async def test_timeout_finalises_card_as_expired():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("rc", seconds=0.05)
    task = asyncio.create_task(broker.request_approval(req))
    # No click: the wait expires on its own and the card closes as "Expired".
    assert await task == ApprovalDecision.EXPIRED

    assert len(app.bot.edited) == 1
    edit = app.bot.edited[0]
    assert edit["message_id"] == app.bot.sent[0]["message_id"]
    assert edit["reply_markup"] == InlineKeyboardMarkup([])
    text = _edited_text(app.bot.edited)
    assert "<b>⏰ Expired (no decision in time).</b>" in text
    assert "<i>This approval is one-time" not in text


async def test_decision_posts_no_follow_up_message():
    # The redesign replaces the separate follow-up with an in-place edit, so a
    # decision must NOT send a second prompt message to the chat.
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("rd")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "rd")
    approve = _callback_data(app.bot.sent[0], _approve_label)
    await broker.handle_callback(_FakeUpdate(approve, CHAT_A, USER_A), None)
    assert await task == ApprovalDecision.APPROVED

    assert len(app.bot.sent) == 1  # only the original prompt, no follow-up
    assert len(app.bot.edited) == 1  # …the card is instead edited once


async def test_finalise_edit_failure_does_not_change_decision():
    # A bot that can't edit (message too old / already edited) must not break the
    # approval: the decision is returned as-is and no exception escapes.
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))

    class _NoEditBot(_FakeBot):
        async def edit_message_text(self, *a, **k):
            raise RuntimeError("edit boom")

    app = _FakeApp()
    app.bot = _NoEditBot()
    broker.bind_application(app)
    req = _request("re")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "re")
    approve = _callback_data(app.bot.sent[0], _approve_label)
    await broker.handle_callback(_FakeUpdate(approve, CHAT_A, USER_A), None)
    assert await task == ApprovalDecision.APPROVED
    assert app.bot.edited == []  # the failed edit was swallowed


async def test_finalise_is_skipped_when_no_message_id():
    # If the send result carries no message_id (defensive), we still resolve the
    # approval — we just can't (and must not) edit a card we can't identify.
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))

    class _NoIdBot(_FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
            return None  # no .message_id

    app = _FakeApp()
    app.bot = _NoIdBot()
    broker.bind_application(app)
    req = _request("rf")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "rf")
    approve = _callback_data(app.bot.sent[0], _approve_label)
    await broker.handle_callback(_FakeUpdate(approve, CHAT_A, USER_A), None)
    assert await task == ApprovalDecision.APPROVED
    assert app.bot.edited == []


# ---------------------------------------------------------------------------
# required #10 — timeout / shutdown / repeat / unknown / expired all safe
# ---------------------------------------------------------------------------
async def test_timeout_yields_expired():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    broker.bind_application(_FakeApp())
    req = _request("r3", seconds=0.05)
    task = asyncio.create_task(broker.request_approval(req))
    # Do NOT click; the wait must expire on its own.
    assert await task == ApprovalDecision.EXPIRED
    assert "r3" not in broker._pending


async def test_shutdown_cancels_pending():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    broker.bind_application(_FakeApp())
    req = _request("r4", seconds=600)
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "r4")
    await broker.shutdown()
    assert await task == ApprovalDecision.EXPIRED
    assert broker._pending == {}


async def test_repeat_click_is_rejected():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("r5")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "r5")
    approve = _callback_data(app.bot.sent[0], _approve_label)

    # First click resolves the request.
    await broker.handle_callback(_FakeUpdate(approve, CHAT_A, USER_A), None)
    assert await task == ApprovalDecision.APPROVED

    # A *second* click on the same stale button must be a safe no-op (EXPIRED),
    # and must not re-execute anything.
    repeat = _FakeUpdate(approve, CHAT_A, USER_A)
    await broker.handle_callback(repeat, None)
    assert repeat.callback_query.answers == ["This approval has expired or is no longer valid."]
    assert broker._pending == {}


async def test_unknown_id_is_safe_noop():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    broker.bind_application(_FakeApp())
    update = _FakeUpdate("v1:does-not-exist:a", CHAT_A, USER_A)
    await broker.handle_callback(update, None)
    # The callback is answered with the safe "no longer valid" text; nothing
    # pending is touched.
    assert update.callback_query.answers == ["This approval has expired or is no longer valid."]
    assert broker._pending == {}


async def test_expired_deadline_is_rejected():
    # A request whose deadline has already passed is refused even for the owner.
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("r6", seconds=-1)  # already in the past
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "r6")
    approve = _callback_data(app.bot.sent[0], _approve_label)
    await broker.handle_callback(_FakeUpdate(approve, CHAT_A, USER_A), None)
    # The deadline check fires before the button is honoured → expired.
    assert await task == ApprovalDecision.EXPIRED


async def test_callback_data_parser_is_safe():
    assert request_id_from("v1:abc123:a") == "abc123"
    assert decision_from("v1:abc123:a") is ApprovalDecision.APPROVED
    assert decision_from("v1:abc123:d") is ApprovalDecision.DENIED
    assert decision_from("v2:abc123:a") is None  # wrong version
    assert decision_from("v1:abc123:x") is None  # bad decision
    assert request_id_from("garbage") == ""


# ---------------------------------------------------------------------------
# required #12 — delivery_chat_id routing (scheduled runs)
#
# A scheduled run's turn runs in a *synthetic* conversation whose row does not
# correspond to a real chat, so the card must go to the owner's *bound* chat.
# ``process_message(delivery_chat_id=spec.chat_id)`` threads that id into
# ``ApprovalRequest.metadata``; the broker uses it directly (no conversation
# lookup) when present, else the existing lookup path (interactive, unchanged).
# ---------------------------------------------------------------------------
class _SpyRepo:
    """A repo that both records whether the lookup path was consulted *and*
    resolves to a deliberate "wrong" chat, so a test can prove the
    metadata path bypassed it entirely."""

    def __init__(self, lookup_chat_id: int = 999_999, *, exists: bool = True):
        self.lookup_chat_id = lookup_chat_id
        self.exists = exists
        self.lookup_calls = 0

    async def get_conversation_by_id(self, conversation_id):
        self.lookup_calls += 1
        if not self.exists:
            return None
        return type("C", (), {"telegram_chat_id": self.lookup_chat_id})()


async def test_scheduled_run_card_goes_to_delivery_chat_not_synthetic():
    # metadata carries the owner's real chat; the repo would resolve to a
    # different ("synthetic-venue") chat. The card must go to the real one.
    repo = _SpyRepo(lookup_chat_id=777_777)
    broker = TelegramApprovalBroker(repo)
    app = _FakeApp()
    broker.bind_application(app)

    req = _request("sd1", metadata={"delivery_chat_id": CHAT_A})
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "sd1")

    # The prompt went to the *delivery* chat, not the repo-resolved chat.
    assert app.bot.sent[0]["chat_id"] == CHAT_A
    # …and the conversation-lookup path was never consulted for it.
    assert repo.lookup_calls == 0


async def test_scheduled_run_binding_is_owner_in_delivery_chat():
    # The principal is still bound to (hash_scope(telegram:<user_id>), chat_id),
    # where chat_id is the *delivery* chat: only the owner *in that chat* may
    # approve; a different chat is refused.
    repo = _SpyRepo()
    broker = TelegramApprovalBroker(repo)
    app = _FakeApp()
    broker.bind_application(app)

    req = _request("sd2", scope=f"telegram:{USER_A}", metadata={"delivery_chat_id": CHAT_A})
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "sd2")
    approve = _callback_data(app.bot.sent[0], _approve_label)

    # Owner in the delivery chat → approved.
    await broker.handle_callback(_FakeUpdate(approve, CHAT_A, USER_A), None)
    assert await task == ApprovalDecision.APPROVED

    # A fresh request: the same user from a *different* chat is refused (voided).
    app2 = _FakeApp()
    broker2 = TelegramApprovalBroker(_SpyRepo())
    broker2.bind_application(app2)
    req2 = _request("sd3", scope=f"telegram:{USER_A}", metadata={"delivery_chat_id": CHAT_A})
    task2 = asyncio.create_task(broker2.request_approval(req2))
    await _await_pending(broker2, "sd3")
    approve2 = _callback_data(app2.bot.sent[0], _approve_label)
    await broker2.handle_callback(_FakeUpdate(approve2, CHAT_A + 1, USER_A), None)  # wrong chat
    assert await task2 == ApprovalDecision.EXPIRED  # voided, not approved


async def test_scheduled_run_approve_denied_expire_all_fail_closed():
    # Approve → runs; Deny → does not; no-click timeout → does not. (The broker
    # only *decides*; the loop decides whether to execute, but the decision
    # returned is exactly what the loop gates on.)
    # Approve
    broker = TelegramApprovalBroker(_SpyRepo())
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("ok", metadata={"delivery_chat_id": CHAT_A})
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "ok")
    await broker.handle_callback(_FakeUpdate(_callback_data(app.bot.sent[0], _approve_label), CHAT_A, USER_A), None)
    assert await task == ApprovalDecision.APPROVED

    # Deny
    broker2 = TelegramApprovalBroker(_SpyRepo())
    app2 = _FakeApp()
    broker2.bind_application(app2)
    req2 = _request("no", metadata={"delivery_chat_id": CHAT_A})
    task2 = asyncio.create_task(broker2.request_approval(req2))
    await _await_pending(broker2, "no")
    await broker2.handle_callback(_FakeUpdate(_callback_data(app2.bot.sent[0], _deny_label), CHAT_A, USER_A), None)
    assert await task2 == ApprovalDecision.DENIED

    # Expire (no click)
    broker3 = TelegramApprovalBroker(_SpyRepo())
    app3 = _FakeApp()
    broker3.bind_application(app3)
    req3 = _request("ex", seconds=0.05, metadata={"delivery_chat_id": CHAT_A})
    task3 = asyncio.create_task(broker3.request_approval(req3))
    assert await task3 == ApprovalDecision.EXPIRED


async def test_scheduled_run_metadata_ignores_invalid_delivery_ids():
    # A delivery_chat_id that is not a positive int (bool, 0, negative, str) is
    # *not* trusted — it falls through to the (existing) conversation-lookup
    # path. A non-int/bool value is the interactive-path invariant: never a
    # silently-wrong target.
    for bad in (True, 0, -5, "42", None):
        repo = _SpyRepo(lookup_chat_id=CHAT_A, exists=True)
        broker = TelegramApprovalBroker(repo)
        app = _FakeApp()
        broker.bind_application(app)
        req = _request("inv", metadata={"delivery_chat_id": bad})
        task = asyncio.create_task(broker.request_approval(req))
        await _await_pending(broker, "inv")
        # Fell through to the lookup path (consulted the repo).
        assert repo.lookup_calls == 1
        await broker.shutdown()  # clean up the pending wait
        await task


async def test_interactive_path_unchanged_without_metadata():
    # The default (no metadata / empty metadata) uses the conversation-lookup
    # path byte-for-byte: the repo *is* consulted and its chat is used.
    repo = _SpyRepo(lookup_chat_id=CHAT_A, exists=True)
    broker = TelegramApprovalBroker(repo)
    app = _FakeApp()
    broker.bind_application(app)

    for meta in (None, {}):
        req = _request("int", metadata=meta)
        task = asyncio.create_task(broker.request_approval(req))
        await _await_pending(broker, "int")
        assert app.bot.sent[-1]["chat_id"] == CHAT_A
        assert repo.lookup_calls >= 1
        await broker.shutdown()
        await task


# ---------------------------------------------------------------------------
# required #11 — other user / other chat cannot approve; no existence leak
# ---------------------------------------------------------------------------
async def test_other_allowlisted_user_cannot_approve():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("r7")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "r7")
    approve = _callback_data(app.bot.sent[0], _approve_label)

    # A *different* (but allow-listed) user clicks in the same chat.
    other = _FakeUpdate(approve, CHAT_A, 999)
    await broker.handle_callback(other, None)
    # The owner's request is denied-by-expiry, NOT approved; and the reply does
    # not reveal that a pending request even existed for them.
    assert await task == ApprovalDecision.EXPIRED
    assert other.callback_query.answers == ["This approval has expired or is no longer valid."]
    assert "r7" not in broker._pending


async def test_other_chat_cannot_approve():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("r8")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "r8")
    approve = _callback_data(app.bot.sent[0], _approve_label)

    # Same user, but the callback arrives from a different chat id.
    other = _FakeUpdate(approve, CHAT_A + 1, USER_A)
    await broker.handle_callback(other, None)
    assert await task == ApprovalDecision.EXPIRED


# ---------------------------------------------------------------------------
# required #12 — callback data + message carry no secrets; summary hides args
# ---------------------------------------------------------------------------
async def test_prompt_and_callback_data_are_secret_free():
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("r9", scope="telegram:7")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "r9")
    sent = app.bot.sent[0]

    text = sent["text"]
    # Shows the tool name (allowed) and the "what it does" purpose summary…
    assert "risky" in text
    assert "risky does a thing." in text  # the purpose summary is shown
    # …but never the raw scope, the user id, or the chat id.
    assert "telegram:7" not in text
    assert "7" not in text  # no bare user id
    assert "100" not in text  # no chat id

    # Every callback data value is just <version>:<id>:<decision> — no scope,
    # chat id, or arguments.
    for row in sent["reply_markup"].inline_keyboard:
        for btn in row:
            cd = btn.callback_data
            assert cd.startswith("v1:")
            assert "telegram:" not in cd
            assert "100" not in cd
            assert len(cd.split(":")) == 3

    await broker.shutdown()


async def test_prompt_is_telegram_html_not_literal_markdown():
    # The prompt is sent with parse_mode=HTML, so it must use Telegram HTML tags
    # (<b>/<i>) — not Markdown (** / _) — or Telegram shows the markers literally.
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)

    req = _request("rh", tool="risky")
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "rh")
    sent = app.bot.sent[0]

    assert sent["parse_mode"] == "HTML"
    text = sent["text"]
    # Real HTML emphasis is present…
    assert "<b>Tool:</b>" in text
    assert "<b>What it does:</b>" in text
    assert "<i>This approval is one-time" in text
    assert "<b>Approve tool call?</b>" in text
    # …and no literal Markdown markers leaked into the body.
    assert "**" not in text
    assert "_This approval" not in text
    # The interpolated tool name is still shown (and would be escaped if hostile).
    assert "risky" in text

    await broker.shutdown()


def test_default_approval_summary_is_a_purpose_line():
    class Secrety(Tool):
        name = "secret_tool"
        description = "d"
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}

        async def execute(self, arguments):  # pragma: no cover
            return ""

    # The default summary is a purpose line that names the tool; it does NOT
    # embed the (validated) arguments — those are shown by the provider as a
    # separate "Arguments:" block.
    summary = Secrety().approval_summary({"password": "hunter2", "url": "https://x"})
    assert "hunter2" not in summary
    assert "secret_tool" in summary


async def test_builtin_summaries_are_complete_and_secret_free():
    # Every built-in overrides approval_summary with a purpose line that names
    # what it does (not the generic "Run the … tool." fallback).
    from fibrecase_agent_backend.tools.builtin import (
        EchoTool,
        GetCurrentTimeTool,
        SystemInfoTool,
    )

    for tool, fragment in (
        (GetCurrentTimeTool(), "current local date and time"),
        (EchoTool(), "Echo a message back"),
        (SystemInfoTool(), "host name, platform, and Python version"),
    ):
        summary = tool.approval_summary({})
        assert fragment.lower() in summary.lower()
        # Purpose lines never embed the (argument-free) tool's empty args.
        assert "hunter2" not in summary


async def test_echo_summary_does_not_echo_argument():
    from fibrecase_agent_backend.tools.builtin import EchoTool

    # Echo's argument is user input that could look like a secret; the summary
    # shows only the purpose, never the echoed value.
    summary = EchoTool().approval_summary({"message": "hunter2 / super-secret"})
    assert "hunter2" not in summary
    assert "super-secret" not in summary
    assert "Echo a message back" in summary


async def test_mcp_tool_summary_is_a_purpose_line():
    from fibrecase_agent_backend.mcp.wrapper import McpTool

    class _S:  # minimal fake session; only identity fields are exercised here
        pass

    tool = McpTool(
        server_name="alpha",
        remote_name="get_weather",
        description="Look up the current weather for a city.",
        parameters={"type": "object", "properties": {}},
        session=_S(),
        max_result_chars=1000,
    )
    summary = tool.approval_summary({"city": "secret-city"})
    # The purpose (remote description) is shown, tagged as a remote tool…
    assert "Look up the current weather for a city." in summary
    assert "(🌐Remote)" in summary
    # …but the (remote) arguments are NOT embedded in the summary — the card
    # shows them in a separate "Arguments:" block the provider renders. The
    # server/remote *names* are also not in the summary (they are the tool name,
    # shown separately on the card).
    assert "secret-city" not in summary
    assert "Arguments are not shown" not in summary


def test_arguments_block_renders_readable_json_and_omits_when_empty():
    # No arguments → the whole "Arguments:" section is omitted.
    assert _arguments_block({}) is None

    # With arguments → readable, pretty-printed JSON in a language-labelled
    # <pre><code> (labelled "json" so the client highlights it), HTML-escaped.
    block = _arguments_block({"city": "北京", "lat": 39.9, "ok": True})
    assert "<b>Arguments:</b>" in block
    assert '<pre class="language-json"><code>' in block and "</code></pre>" in block
    assert '"city": "北京"' in block
    assert '"lat": 39.9' in block
    assert '"ok": true' in block
    # Newlines are preserved (readable), not collapsed to one line.
    assert "\n" in block

    # Argument content cannot inject HTML: a value with markup is escaped.
    hostile = _arguments_block({"cmd": "</code><b>x</b>"})
    assert "</code><b>x</b>" not in hostile
    assert "&lt;/code&gt;&lt;b&gt;x&lt;/b&gt;" in hostile


def test_bound_arguments_truncates_when_over_cap():
    # Under the cap → escaped verbatim, no marker.
    small = _bound_arguments('{"a": 1}')
    assert small == '{"a": 1}'
    assert _ARGUMENTS_TRUNCATED_MARK not in small

    # Over the cap → cut to the budget + the fixed marker, never the raw length.
    big = "x" * (_ARGUMENTS_MAX_CHARS + 500)
    out = _bound_arguments(big)
    assert out.startswith(_ARGUMENTS_MAX_CHARS * "x")
    assert out.endswith(_ARGUMENTS_TRUNCATED_MARK)
    # The marker is the only thing past the cut, so the result length is bounded.
    assert len(out) == _ARGUMENTS_MAX_CHARS + len(_ARGUMENTS_TRUNCATED_MARK)


def test_bound_arguments_repairs_dangling_entity_at_cut():
    # Force the cut to land mid-entity: a run of '<' escapes to '&lt;' (4 chars).
    pad = "<" * 20000  # -> '&lt;' * 20000 in escaped form
    out = _bound_arguments(pad)
    # The marker is appended last, so inspect only the truncated content before it.
    cut = out[: -len(_ARGUMENTS_TRUNCATED_MARK)]
    # No dangling entity: after the last '&' (if any) comes a complete entity or
    # the end — never a lone '&' or partial '&l…' that would break Telegram HTML.
    tail_amp = cut.rfind("&")
    after = cut[tail_amp:] if tail_amp != -1 else ""
    assert after in ("", "&amp;", "&lt;", "&gt;")
    assert out.endswith(_ARGUMENTS_TRUNCATED_MARK)


def test_arguments_block_bounded_for_worst_case_edit_replace():
    # Two strings at the default schema cap: the card must stay under Telegram's
    # ~4096 single-message limit (so it is approvable). At this size the JSON does
    # exceed the budget, so it is truncated — bounded, never unapprovable.
    block = _arguments_block(
        {"operation": "replace", "path": "f.txt", "old_string": "a" * 2000, "new_string": "b" * 2000}
    )
    assert len(block) < 4096
    assert _ARGUMENTS_TRUNCATED_MARK in block  # the bound engaged at this size
    assert block.endswith("</code></pre>")  # still well-formed


def test_arguments_block_not_truncated_for_typical_edit():
    # A normal (small) edit shows old_string/new_string in full — no truncation.
    block = _arguments_block({"operation": "replace", "path": "f.txt", "old_string": "foo", "new_string": "bar"})
    assert _ARGUMENTS_TRUNCATED_MARK not in block
    assert '"old_string": "foo"' in block and '"new_string": "bar"' in block


def test_detail_shown_under_action_label_and_escaped():
    # A tool-supplied friendly detail view is shown under an "Action:" label, in
    # place of the JSON "Arguments:" block, wrapped in a code block.
    detail = "📄 File: f.txt\n🔁 Operation: replace (replace_all: no)\n── old_string ──\nfoo\n── new_string ──\nbar"
    req = _request(detail=detail, arguments={"operation": "replace", "path": "f.txt", "old_string": "foo", "new_string": "bar"})
    text = _card_text(req, "<i>hint</i>")
    assert "<b>Action:</b>" in text
    assert "<b>Arguments:</b>" not in text  # the JSON block is *replaced*, not added
    assert "<pre><code>" in text and "</code></pre>" in text
    # The exact old/new values are present, newlines preserved (faithful).
    assert "foo" in text and "bar" in text
    assert "── old_string ──" in text and "── new_string ──" in text


def test_detail_language_labels_the_code_block():
    # A detail that carries a language hint is wrapped in a <pre> labelled with
    # it (so the client highlights the block instead of guessing).
    req = _request(detail="-old\n+new", arguments={}, language="diff")
    text = _card_text(req, "<i>hint</i>")
    assert '<pre class="language-diff"><code>' in text
    assert "-old" in text and "+new" in text


def test_detail_without_language_is_unlabelled():
    # No language hint → the code block has no class attribute (plain <pre><code>).
    req = _request(detail="📄 File: notes.md\nOperation: read", arguments={}, language="")
    text = _card_text(req, "<i>hint</i>")
    assert "<pre><code>" in text
    assert 'class="language-' not in text


def test_detail_language_is_sanitised():
    # A tool-supplied language is a fixed vocabulary, but it is still sanitised
    # before reaching the markup: only [A-Za-z0-9_-] survives (lowercased), so a
    # hostile value cannot break the class attribute or inject a tag.
    text = _card_text(_request(detail="x", arguments={}, language='diff" class="x'), "<i>hint</i>")
    # The injected quote / nested attribute is defused: quotes are stripped, so
    # the whole thing becomes ONE class attribute — no `class="x` breakout.
    assert 'class="language-diffclassx"' in text  # a single, well-formed class
    assert 'class="x' not in text  # no injected second attribute
    # A language with no word characters at all is dropped entirely (no label).
    none_text = _card_text(_request(detail="x", arguments={}, language="<>!!"), "<i>hint</i>")
    assert 'class="language-' not in none_text


def test_detail_is_html_escaped_and_bounded():
    # Markup in the detail cannot inject a tag (escaped like the JSON block)…
    hostile = _card_text(_request(detail='a </code><b>x</b> b', arguments={}), "<i>hint</i>")
    assert "</code><b>x</b>" not in hostile
    assert "&lt;/code&gt;&lt;b&gt;x&lt;/b&gt;" in hostile
    # …and an over-cap detail is truncated to the budget + the fixed marker, so
    # the card stays approvable (the 4096 bound is shared with the JSON path).
    big = _card_text(_request(detail="x" * (_ARGUMENTS_MAX_CHARS + 500), arguments={}), "<i>hint</i>")
    assert big.count(_ARGUMENTS_TRUNCATED_MARK) == 1


def test_detail_dropped_on_finalisation():
    # The friendly detail, like the JSON block, is on the live prompt only: the
    # in-place finalisation (show_arguments=False) drops it too.
    detail = "📄 File: f.txt\n🔁 Operation: replace"
    req = _request(detail=detail, arguments={"path": "f.txt"})
    prompt = _card_text(req, "<i>hint</i>")
    final = _card_text(req, "<b>✅ Approved.</b>", show_arguments=False)
    assert "<b>Action:</b>" in prompt
    assert "<b>Action:</b>" not in final and "<b>Arguments:</b>" not in final


def test_detail_wins_over_json_when_both_present():
    # A non-empty detail replaces the JSON block even when arguments are also set.
    req = _request(detail="friendly view", arguments={"a": 1})
    text = _card_text(req, "<i>hint</i>")
    assert "<b>Action:</b>" in text
    assert "friendly view" in text
    assert '<b>Arguments:</b>' not in text
    assert '"a": 1' not in text  # the raw JSON is not shown alongside the detail


def test_detail_empty_defaults_to_json_block():
    # detail="" (the default) keeps the existing JSON behaviour for every tool
    # that does not override approval_detail.
    req = _request(arguments={"city": "北京", "days": 3})
    text = _card_text(req, "<i>hint</i>")
    assert "<b>Arguments:</b>" in text
    assert "<b>Action:</b>" not in text


def test_default_approval_detail_is_none():
    # The base Tool provides no custom detail view — the provider falls back to
    # the generic JSON block (asserted at the tool-layer contract).
    class Plain(Tool):
        name = "plain"
        description = "d"
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}

        async def execute(self, arguments):  # pragma: no cover
            return ""

    assert Plain().approval_detail({"password": "hunter2"}) is None


async def test_approval_prompt_shows_arguments_when_present():
    # End-to-end: a call with arguments shows them as a readable JSON block…
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("ra", arguments={"city": "北京", "days": 3})
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "ra")
    text = app.bot.sent[0]["text"]
    assert "<b>Arguments:</b>" in text
    assert '"city": "北京"' in text
    assert '"days": 3' in text
    await broker.shutdown()


async def test_approval_prompt_omits_arguments_when_empty():
    # …and an argument-free call omits the "Arguments:" section entirely.
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("rn", arguments={})  # no-arg tool (e.g. get_current_time)
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "rn")
    text = app.bot.sent[0]["text"]
    assert "<b>Arguments:</b>" not in text
    assert "<pre>" not in text
    assert "risky does a thing." in text  # the purpose line is still there
    await broker.shutdown()


async def test_finalised_card_drops_arguments():
    # A call *with* arguments: the live prompt shows them, but once the owner
    # decides the in-place edit removes the Arguments section too (buttons are
    # already gone, so the resolved card keeps only the purpose + status).
    broker = TelegramApprovalBroker(_FakeRepo(chat_id=CHAT_A))
    app = _FakeApp()
    broker.bind_application(app)
    req = _request("ra", tool="risky", arguments={"city": "北京", "days": 3})
    task = asyncio.create_task(broker.request_approval(req))
    await _await_pending(broker, "ra")

    # The live prompt carries the readable Arguments block…
    prompt = app.bot.sent[0]
    assert "<b>Arguments:</b>" in prompt["text"]
    assert '"city": "北京"' in prompt["text"]

    # …and approving edits the SAME card without the Arguments section.
    approve = _callback_data(prompt, _approve_label)
    await broker.handle_callback(_FakeUpdate(approve, CHAT_A, USER_A), None)
    assert await task == ApprovalDecision.APPROVED

    assert len(app.bot.edited) == 1
    edit = app.bot.edited[0]
    assert edit["message_id"] == prompt["message_id"]
    edited = _edited_text(app.bot.edited)
    assert "<b>Arguments:</b>" not in edited
    assert "北京" not in edited  # the argument value is gone with the block
    # Purpose + status survive the edit.
    assert "risky" in edited
    assert "<b>✅ Approved.</b>" in edited


# ---------------------------------------------------------------------------
# required #13 — a blocked conversation stays ordered; another proceeds
# ---------------------------------------------------------------------------
class _RiskyTool(Tool):
    name = "risky"
    description = "an ask tool"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    # default_permission is the base default ASK (deliberately not overridden).

    async def execute(self, arguments):
        return "risky ran"


class _GatedApproval:
    def __init__(self):
        self.gate = asyncio.Event()
        self.calls = []

    async def request_approval(self, request):
        self.calls.append(request)
        await self.gate.wait()
        return ApprovalDecision.APPROVED

    async def shutdown(self):  # pragma: no cover
        self.gate.set()


class _MarkerLLM:
    """Final answer once a tool result is present; else a tool-call if the user
    asked for it, else plain text. ``messages`` are :class:`ChatMessage`s."""

    def __init__(self):
        self.calls = []

    async def complete(self, messages, *, tools=None):
        roles = [m.role for m in messages]
        if "tool" in roles:
            return LLMResult(content="all done after tool")
        last_user = [m for m in messages if m.role == "user"][-1].content
        if "USE_TOOL" in str(last_user):
            return LLMResult(
                content=None,
                tool_calls=[{"id": "c1", "type": "function", "function": {"name": "risky", "arguments": "{}"}}],
            )
        return LLMResult(content="no tools here")


async def _until(predicate, *, timeout=2.0):
    """Yield the loop until ``predicate()`` is true, or ``timeout`` seconds pass."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.005)
    return True


async def test_blocked_conversation_ordered_while_other_proceeds(repo):
    conv_a = await repo.get_or_create_conversation(11, 1)
    conv_b = await repo.get_or_create_conversation(22, 2)

    llm = _MarkerLLM()
    approval = _GatedApproval()
    reg = ToolRegistry().register(_RiskyTool())
    service = AgentService(
        repo,
        llm,
        system_prompt="S",
        registry=reg,
        enable_tools=True,
        max_tool_iterations=5,
        # The base default permission for a tool is ASK, so no override is
        # needed to make "risky" require approval.
        policy=build_policy({}, registry=reg),
        approval_provider=approval,
    )

    a1 = asyncio.create_task(service.process_message(conv_a.id, "USE_TOOL", memory_scope=f"telegram:{USER_A}"))
    b1 = asyncio.create_task(service.process_message(conv_b.id, "plain question"))
    a2 = asyncio.create_task(service.process_message(conv_a.id, "USE_TOOL"))

    # Let A1 reach the gated approval and B1 finish, before A2 can start
    # (A2 is queued on A's per-conversation lock).
    settled = await _until(lambda: approval.calls and b1.done() and not a1.done() and not a2.done())
    assert settled, f"expected A1 blocked + B1 done + A2 queued; approval={len(approval.calls)}"

    # A different conversation completed while A1 is blocked on the human.
    assert b1.result() == "no tools here"
    # Only A1 has reached the approval so far (A2 is still queued on A's lock).
    assert len(approval.calls) == 1

    # The owner approves; A1 finishes, then A2 (same conversation) proceeds.
    approval.gate.set()
    await asyncio.wait_for(asyncio.gather(a1, a2, b1, return_exceptions=True), timeout=5)
    assert a1.result() == "all done after tool"
    assert a2.result() == "all done after tool"
    assert len(approval.calls) == 2  # both of A's tool turns asked for approval


# ---------------------------------------------------------------------------
# approval requested but the conversation is unknown → fail closed (DENIED)
# ---------------------------------------------------------------------------
async def test_unknown_conversation_denies():
    broker = TelegramApprovalBroker(_FakeRepo(exists=False))
    broker.bind_application(_FakeApp())
    assert await broker.request_approval(_request("rx")) == ApprovalDecision.DENIED


# ---------------------------------------------------------------------------
# build_application wires the callback handler only when a broker is given
# ---------------------------------------------------------------------------
def test_build_application_wires_broker_callback_handler():
    from telegram.ext import CallbackQueryHandler

    from fibrecase_agent_backend.telegram.bot import build_application

    class _Cfg:
        telegram_bot_token = "123:abc-def"
        allowed_user_ids = frozenset({1})

    class _Svc:  # minimal; build_application only stores it
        pass

    broker = TelegramApprovalBroker(_FakeRepo())
    app = build_application(_Cfg(), _Svc(), _FakeRepo(), approval_broker=broker)
    # The broker was bound to the application…
    assert broker._application is app
    # …and a callback handler was registered in the first group.
    handlers = app.handlers[0]
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)

    # Without a broker, no callback handler is registered.
    app2 = build_application(_Cfg(), _Svc(), _FakeRepo())
    assert not any(isinstance(h, CallbackQueryHandler) for h in app2.handlers[0])
