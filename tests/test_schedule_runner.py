"""Phase 9 — the Telegram schedule runner ``_run_schedule`` (required #5, #6, #11, #13).

This pins the *composition-root* half of the phase-9 loop (``main.py::_run_schedule``)
— the half the pure scheduler tests cannot reach because the scheduler is
channel-agnostic and only knows an injected ``runner``. Here the runner is the real
``AgentBackend._run_schedule`` bound to a **real** in-memory repository (the
``repo`` fixture) and **faked** ``service`` + ``bot``. The invariants:

* **Dedicated, fresh venue.** Each run prepares a synthetic conversation in the
  reserved range (``schedule_chat_id(spec.name)``) via ``reset_conversation`` —
  the venue has an *empty* history at ``process_message`` time, and a second run
  starts fresh again. The id is name-derived (stable across runs), not arbitrary.
* **One turn.** ``process_message`` is called **exactly once** with the fixed
  prompt, the owner's ``memory_scope=f"telegram:{user_id}"`` (so long-term memory
  retrieval still happens — the scope is what triggers it) and
  ``delivery_chat_id=spec.chat_id`` (so an approval card goes to the real chat,
  not the synthetic venue). No tools, no extra completions.
* **Formatted notification.** On success exactly one notification is delivered to
  ``spec.chat_id`` containing the task *name* + the *result* — **never** the
  prompt. An empty reply sends nothing. A long result is chunked.
* **Failure notice.** An ``AgentError`` sends a fixed, safe notice (name +
  ``user_safe``) — never the prompt or an exception/stack body. A generic
  exception sends *no* notification (fault isolation is the scheduler's job); it
  only logs.
* **Always-clean venue.** ``finally`` deletes the dedicated conversation on every
  path (success / AgentError / other-exception / send-error), leaving no trace; a
  failed Telegram send is swallowed (logged by name) and the cleanup still runs.
* **Privacy.** Across a run, neither the prompt nor the reply appears in the
  Telegram text or the logs.

Nothing here talks to the real LLM or Telegram: the LLM is behind the fake
``service`` and Telegram behind the fake ``bot``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from telegram.error import TelegramError

from fibrecase_agent_backend.agent.service import AgentError, _user_safe_for
from fibrecase_agent_backend.config import ScheduleQQReceiver, ScheduleTelegramReceiver
from fibrecase_agent_backend.database.models import (
    SCHEDULE_CHAT_ID_BASE,
    SCHEDULE_CHAT_ID_MAX,
    qq_chat_id,
    schedule_chat_id,
)
from fibrecase_agent_backend.main import AgentBackend

PROMPT = "TOP-SECRET-7AM-CHECK-PROMPT"
REPLY = "the result: all services are up (REPLY-BODY)"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
@dataclass
class _FakeService:
    reply: str = REPLY
    error: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def process_message(self, conv_id, text, *, memory_scope=None, delivery_chat_id=None):
        self.calls.append(
            {
                "conv_id": conv_id,
                "text": text,
                "memory_scope": memory_scope,
                "delivery_chat_id": delivery_chat_id,
            }
        )
        if self.error is not None:
            raise self.error
        return self.reply


class _RecordingBot:
    def __init__(self, error: BaseException | None = None):
        self.sent: list[dict[str, Any]] = []
        self.error = error

    async def send_message(self, chat_id, text, **kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})


class _FakeQQClient:
    """A stand-in for the ``botpy`` client's ``.api`` — records ``post_c2c_message``.

    ``deliver_qq_markdown`` calls ``client.api.post_c2c_message(openid=...,
    msg_type=..., markdown={"content": ...})``; this records each call so a test
    can assert the openid target and the (chunked) content.
    """

    def __init__(self, error: BaseException | None = None):
        self.sent: list[dict[str, Any]] = []
        self.error = error

        api = self

        class _Api:
            async def post_c2c_message(self, **kwargs):
                if api.error is not None:
                    raise api.error
                api.sent.append(kwargs)

        self.api = _Api()


class _App:
    def __init__(self, bot: _RecordingBot) -> None:
        self.bot = bot


def _make_backend(repo, service: _FakeService, bot: _RecordingBot, qq_client: _FakeQQClient | None = None):
    """An ``AgentBackend`` with just the attributes ``_run_schedule`` uses.

    Built via ``__new__`` so the (heavy) ``__init__`` — which would open the LLM
    client, build the PTB application, discover MCP, etc. — is never run. Also
    spies the venue ``reset_conversation`` / ``delete_conversation`` calls so a
    test can assert *which* ``telegram_chat_id`` (the reserved-range synthetic
    id) and principal are prepared and torn down, delegating to the real methods.
    ``qq_client`` (the botpy stand-in) is set on ``backend._qq_client`` — ``None``
    models a Telegram-only deployment (a qq receiver is then skipped + warned).
    """
    backend = AgentBackend.__new__(AgentBackend)
    backend.service = service
    backend.application = _App(bot)
    backend._qq_client = qq_client

    reset_calls: list[tuple[int, int]] = []
    delete_calls: list[int] = []
    real_reset = repo.reset_conversation
    real_delete = repo.delete_conversation

    async def spy_reset(telegram_chat_id, user_id):
        reset_calls.append((telegram_chat_id, user_id))
        return await real_reset(telegram_chat_id, user_id)

    async def spy_delete(conversation_id):
        delete_calls.append(conversation_id)
        return await real_delete(conversation_id)

    repo.reset_conversation = spy_reset
    repo.delete_conversation = spy_delete
    backend.repository = repo
    backend.reset_calls = reset_calls
    backend.delete_calls = delete_calls
    return backend


_UNSET = object()


def _spec(name: str = "nightly", *, identity: str = "telegram",
          telegram=_UNSET, qq=None) -> Any:
    """Build a schedule spec. Defaults to a ``telegram``-identity, telegram-only
    receiver (chat 42 / user 7) — the shape the pre-multi-channel tests assume.

    Pass ``qq=ScheduleQQReceiver(user_openid=...)`` to add a QQ receiver, and/or
    ``identity="qq"`` + ``telegram=None`` for a QQ-identity run.
    """
    from fibrecase_agent_backend.config import ScheduleSpec

    if telegram is _UNSET:
        telegram = ScheduleTelegramReceiver(chat_id=42, user_id=7)
    return ScheduleSpec(
        name=name, cron="0 7 * * *", prompt=PROMPT,
        identity=identity, telegram=telegram, qq=qq,
    )


def _all_text(bot: _RecordingBot) -> str:
    return "\n".join(m["text"] for m in bot.sent)


# ---------------------------------------------------------------------------
# required #5 — the dedicated-venue + single-turn + notification contract
# ---------------------------------------------------------------------------
async def test_success_run_resets_reserved_venue_and_notifies(repo):
    service = _FakeService(reply=REPLY)
    bot = _RecordingBot()
    backend = _make_backend(repo, service, bot)

    await backend._run_schedule(_spec("nightly"))

    # Exactly one turn, with the fixed prompt and the routing scope/chat.
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["text"] == PROMPT
    assert call["memory_scope"] == "telegram:7"  # owner scope → long-term memory retrieval
    assert call["delivery_chat_id"] == 42  # approval card goes to the real chat

    # The venue is prepared with the reserved-range, name-derived id for this
    # schedule (reset_conversation's telegram_chat_id argument), and the owner's
    # user_id.
    expected_id = schedule_chat_id("nightly")
    assert backend.reset_calls == [(expected_id, 7)]
    assert SCHEDULE_CHAT_ID_BASE < expected_id < SCHEDULE_CHAT_ID_MAX

    # Exactly one notification, to the real chat, carrying name + result, not the prompt.
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 42
    text = bot.sent[0]["text"]
    assert "nightly" in text
    assert "REPLY-BODY" in text
    assert PROMPT not in text

    # The dedicated venue is cleaned up: no conversation row for the synthetic id.
    remaining = await repo.get_conversation(expected_id)
    assert remaining is None
    # And the teardown targeted the run's conversation id exactly once.
    assert len(backend.delete_calls) == 1


async def test_same_name_always_uses_same_reserved_id(repo):
    # The venue id is deterministic in the name (not a per-run random value), so
    # a schedule's row can be self-healed / swept by name-derived id.
    a, b = schedule_chat_id("nightly"), schedule_chat_id("nightly")
    assert a == b
    # Distinct names produce distinct ids (32-bit space; these two differ).
    assert schedule_chat_id("nightly") != schedule_chat_id("weekly")


# ---------------------------------------------------------------------------
# required #6 — a fresh venue each run; the venue is empty at process time
# ---------------------------------------------------------------------------
async def test_venue_is_fresh_each_run(repo):
    # A fake service that, at process time, peeks the venue's stored messages via
    # the *real* repo and records what history it sees.
    seen_history: list[int] = []

    async def process_message(conv_id, text, *, memory_scope=None, delivery_chat_id=None):
        messages = await repo.get_messages(conv_id)
        seen_history.append(len(messages))
        return REPLY

    service = _FakeService()
    service.process_message = process_message
    bot = _RecordingBot()
    backend = _make_backend(repo, service, bot)

    # Two consecutive runs: each venue must be empty at process time.
    await backend._run_schedule(_spec("nightly"))
    await backend._run_schedule(_spec("nightly"))

    assert seen_history == [0, 0]  # fresh every run — no carry-over history


async def test_previous_run_left_no_venue_for_next_run(repo):
    service = _FakeService(reply=REPLY)
    bot = _RecordingBot()
    backend = _make_backend(repo, service, bot)

    await backend._run_schedule(_spec("nightly"))
    # After run 1, the synthetic venue is gone, so run 2's reset_conversation
    # has nothing to delete (it creates a brand-new empty conversation).
    assert await repo.get_conversation(schedule_chat_id("nightly")) is None


# ---------------------------------------------------------------------------
# required #5 — empty reply: no notification, venue still cleaned up
# ---------------------------------------------------------------------------
async def test_empty_reply_sends_no_notification_but_cleans_venue(repo):
    service = _FakeService(reply="")
    bot = _RecordingBot()
    backend = _make_backend(repo, service, bot)

    await backend._run_schedule(_spec("nightly"))

    assert bot.sent == []  # nothing to report
    assert len(service.calls) == 1  # the turn still ran
    assert await repo.get_conversation(schedule_chat_id("nightly")) is None


# ---------------------------------------------------------------------------
# required #5 — a long result is chunked (tag-balanced), all to the real chat
# ---------------------------------------------------------------------------
async def test_long_result_is_chunked_to_real_chat(repo):
    long_reply = ("line-of-result\n" * 600) + "END-MARKER"  # ~7800 chars > CHUNK_SIZE (4000)
    service = _FakeService(reply=long_reply)
    bot = _RecordingBot()
    backend = _make_backend(repo, service, bot)

    await backend._run_schedule(_spec("nightly"))

    # The result exceeds CHUNK_SIZE (4000) → more than one send, all to chat 42.
    assert len(bot.sent) >= 2
    assert all(m["chat_id"] == 42 for m in bot.sent)
    joined = _all_text(bot)
    assert "END-MARKER" in joined
    assert PROMPT not in joined


# ---------------------------------------------------------------------------
# required #5 — AgentError → fixed safe notice (name + user_safe, never prompt)
# ---------------------------------------------------------------------------
async def test_agent_error_sends_safe_notice_not_prompt(repo):
    from fibrecase_agent_backend.agent.service import AgentError

    err = AgentError(_user_safe_for("llm_error"), "llm_error")
    service = _FakeService(error=err)
    bot = _RecordingBot()
    backend = _make_backend(repo, service, bot)

    await backend._run_schedule(_spec("nightly"))

    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 42
    text = bot.sent[0]["text"]
    assert "nightly" in text
    assert err.user_safe in text
    assert PROMPT not in text  # the prompt is never in the failure notice
    assert await repo.get_conversation(schedule_chat_id("nightly")) is None


# ---------------------------------------------------------------------------
# required #5 — a generic (non-AgentError) exception: no notification, but the
# venue is still cleaned up and the runner never raises (fault isolation is the
# scheduler's; here we only pin that the runner does not leak the venue).
# ---------------------------------------------------------------------------
async def test_generic_exception_cleans_venue_and_does_not_raise(repo):
    service = _FakeService(error=RuntimeError("boom"))
    bot = _RecordingBot()
    backend = _make_backend(repo, service, bot)

    await backend._run_schedule(_spec("nightly"))  # must not raise

    assert bot.sent == []  # no notification for a non-AgentError failure
    assert await repo.get_conversation(schedule_chat_id("nightly")) is None


# ---------------------------------------------------------------------------
# required #5 — a failed Telegram send is swallowed; the venue is still cleaned up
# ---------------------------------------------------------------------------
async def test_failed_send_is_swallowed_and_venue_cleaned(repo, caplog):
    service = _FakeService(reply=REPLY)
    bot = _RecordingBot(error=TelegramError("flooded"))
    backend = _make_backend(repo, service, bot)

    await backend._run_schedule(_spec("nightly"))  # must not raise

    # The send failed, but the venue was still cleaned up.
    assert await repo.get_conversation(schedule_chat_id("nightly")) is None
    # Logged by name (via ``extra``) + class only — never the prompt or the reply body.
    names = [getattr(r, "schedule", None) for r in caplog.records]
    assert "nightly" in names
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert PROMPT not in logged
    assert "REPLY-BODY" not in logged


# ---------------------------------------------------------------------------
# required #11 — privacy: neither prompt nor reply in Telegram text or logs
# ---------------------------------------------------------------------------
async def test_prompt_and_reply_never_in_telegram_or_logs(repo, caplog):
    service = _FakeService(reply=REPLY)
    bot = _RecordingBot()
    backend = _make_backend(repo, service, bot)

    await backend._run_schedule(_spec("nightly"))

    tele = _all_text(bot)
    assert PROMPT not in tele  # prompt never reaches Telegram
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert PROMPT not in logged  # prompt never reaches the logs
    # (The reply *is* delivered — that is the whole point — but it must not leak
    # into the logs as an outcome/debug string.)
    assert "REPLY-BODY" not in logged


# ---------------------------------------------------------------------------
# required #13 — ENABLE_TOOLS=false: a scheduled run still works as a single
# turn (the runner does not branch on enable_tools; tools run only if the model
# calls them inside the single process_message turn).
# ---------------------------------------------------------------------------
async def test_scheduled_run_is_a_single_completion_without_tools(repo):
    # The runner makes exactly one process_message call and one notification —
    # there is no tool loop, no extra LLM turn, no second completion. This holds
    # whether or not tools are enabled (that knob is irrelevant to the runner).
    service = _FakeService(reply=REPLY)
    bot = _RecordingBot()
    backend = _make_backend(repo, service, bot)

    await backend._run_schedule(_spec("nightly"))

    assert len(service.calls) == 1
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 42
    assert await repo.get_conversation(schedule_chat_id("nightly")) is None


# ---------------------------------------------------------------------------
# multi-channel (QQ) delivery
# ---------------------------------------------------------------------------
OPENID = "QQ-OPENID-XYZ-123"


async def test_qq_only_run_uses_qq_scope_and_delivers_to_qq(repo):
    # identity=qq, only a qq receiver: the run executes under the qq scope, the
    # row principal is the openid's synthetic chat id, delivery is a proactive
    # C2C send to the openid (no Telegram send), and the venue is cleaned up.
    service = _FakeService(reply=REPLY)
    bot = _RecordingBot()
    qq = _FakeQQClient()
    backend = _make_backend(repo, service, bot, qq_client=qq)

    await backend._run_schedule(_spec(identity="qq", telegram=None, qq=ScheduleQQReceiver(user_openid=OPENID)))

    call = service.calls[0]
    assert len(service.calls) == 1
    assert call["text"] == PROMPT
    assert call["memory_scope"] == f"qq:{OPENID}"
    assert call["delivery_chat_id"] is None  # qq run: the broker routes by scope, not a chat

    # The dedicated row's principal is the qq synthetic chat id (not a real chat).
    assert backend.reset_calls == [(schedule_chat_id("nightly"), qq_chat_id(OPENID))]

    # Delivered to QQ only (one C2C send to the openid), never to Telegram.
    assert len(qq.sent) == 1
    assert qq.sent[0]["openid"] == OPENID
    assert qq.sent[0]["msg_type"] == 2
    body = qq.sent[0]["markdown"]["content"]
    assert "nightly" in body and "REPLY-BODY" in body and PROMPT not in body
    assert bot.sent == []

    assert await repo.get_conversation(schedule_chat_id("nightly")) is None


async def test_both_receivers_deliver_to_both_channels(repo):
    # identity=qq, both receivers present: the agent runs once, the result is
    # delivered to BOTH the Telegram chat and the QQ openid.
    service = _FakeService(reply=REPLY)
    bot = _RecordingBot()
    qq = _FakeQQClient()
    backend = _make_backend(repo, service, bot, qq_client=qq)

    await backend._run_schedule(
        _spec(identity="qq", telegram=ScheduleTelegramReceiver(chat_id=42, user_id=7),
              qq=ScheduleQQReceiver(user_openid=OPENID))
    )

    assert len(service.calls) == 1
    assert service.calls[0]["memory_scope"] == f"qq:{OPENID}"

    # Both channels got the notification.
    assert len(bot.sent) == 1 and bot.sent[0]["chat_id"] == 42 and "REPLY-BODY" in bot.sent[0]["text"]
    assert len(qq.sent) == 1 and qq.sent[0]["openid"] == OPENID and "REPLY-BODY" in qq.sent[0]["markdown"]["content"]
    assert await repo.get_conversation(schedule_chat_id("nightly")) is None


async def test_qq_receiver_skipped_when_channel_not_running(repo, caplog):
    # A qq receiver but no qq client (Telegram-only deployment): the run still
    # completes, the Telegram delivery proceeds, the QQ send is skipped + warned
    # (never an exception), and the openid is not logged.
    service = _FakeService(reply=REPLY)
    bot = _RecordingBot()
    backend = _make_backend(repo, service, bot, qq_client=None)

    await backend._run_schedule(
        _spec(identity="telegram", telegram=ScheduleTelegramReceiver(chat_id=42, user_id=7),
              qq=ScheduleQQReceiver(user_openid=OPENID))
    )

    # Telegram delivered normally.
    assert len(bot.sent) == 1 and bot.sent[0]["chat_id"] == 42
    # A warning was logged (by name), and the openid never appears in a log line.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "QQ delivery skipped" in logged
    assert OPENID not in logged
    assert await repo.get_conversation(schedule_chat_id("nightly")) is None


async def test_qq_send_failure_is_swallowed_and_other_channel_still_delivers(repo, caplog):
    # A failing QQ send must not block the Telegram delivery (best-effort per
    # receiver) and must not leak the openid into the logs.
    service = _FakeService(reply=REPLY)
    bot = _RecordingBot()
    qq = _FakeQQClient(error=RuntimeError("qq flood"))
    backend = _make_backend(repo, service, bot, qq_client=qq)

    await backend._run_schedule(
        _spec(identity="telegram", telegram=ScheduleTelegramReceiver(chat_id=42, user_id=7),
              qq=ScheduleQQReceiver(user_openid=OPENID))
    )

    # The Telegram receiver still got the notification despite the QQ failure.
    assert len(bot.sent) == 1 and bot.sent[0]["chat_id"] == 42
    assert qq.sent == []  # the qq send raised
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "notification failed (qq)" in logged
    assert OPENID not in logged
    assert await repo.get_conversation(schedule_chat_id("nightly")) is None


async def test_qq_agent_error_notices_both_channels(repo):
    # An AgentError during the run sends the fixed safe notice to every
    # configured receiver (telegram + qq), never the prompt.
    err = AgentError(_user_safe_for("llm_error"), "llm_error")
    service = _FakeService(error=err)
    bot = _RecordingBot()
    qq = _FakeQQClient()
    backend = _make_backend(repo, service, bot, qq_client=qq)

    await backend._run_schedule(
        _spec(identity="qq", telegram=ScheduleTelegramReceiver(chat_id=42, user_id=7),
              qq=ScheduleQQReceiver(user_openid=OPENID))
    )

    assert "nightly" in bot.sent[0]["text"] and err.user_safe in bot.sent[0]["text"] and PROMPT not in bot.sent[0]["text"]
    assert "nightly" in qq.sent[0]["markdown"]["content"] and PROMPT not in qq.sent[0]["markdown"]["content"]
    assert await repo.get_conversation(schedule_chat_id("nightly")) is None


# ---------------------------------------------------------------------------
# reserved-range sanity: out-of-range real ids are *not* treated as venues here
# (the sweep is exercised in test_database.py; this pins the derivation bound).
# ---------------------------------------------------------------------------
def test_schedule_id_is_strictly_inside_reserved_range():
    for name in ("a", "nightly", "x" * 32):
        cid = schedule_chat_id(name)
        assert SCHEDULE_CHAT_ID_BASE < cid < SCHEDULE_CHAT_ID_MAX


def test_real_chat_ids_are_outside_reserved_range():
    # Normal Telegram chat ids (even large positive ones) never collide with the
    # reserved range, so a real conversation can never be mistaken for a venue.
    for real in (1, 10**12, 10**15, 10**17):
        assert not (SCHEDULE_CHAT_ID_BASE < real < SCHEDULE_CHAT_ID_MAX)
