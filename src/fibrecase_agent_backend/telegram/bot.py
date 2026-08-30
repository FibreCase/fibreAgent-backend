"""Telegram adapter.

This is the only module that knows about Telegram. It wires Telegram events
to the channel-agnostic :class:`~..agent.service.AgentService` and handles the
concerns specific to this transport:

* user authorisation (only configured Telegram user ids may use the bot),
* the ``/start``, ``/new``, ``/stop``, ``/help``, ``/status``, ``/context``,
  memory, and ``/tool_audit`` commands,
* a "typing…" keep-alive while the model is generating,
* chunking of long model replies (the full reply is persisted *once*,
  upstream, in the Agent service),
* graceful handling of Telegram API errors (never crash the process).

The adapter never talks to the LLM directly — only to the Agent service and
the repository.

Note on this PTB build: ``CallbackContext`` does not expose the usual
``.user`` / ``.chat`` / ``.message`` shortcuts, so handlers read the sender,
chat and message from the ``Update`` object (``update.effective_user``,
``update.effective_chat``, ``update.effective_message``) and shared state from
``context.application.bot_data``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from telegram import BotCommand, Chat, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..agent.service import AgentError, AgentService, _user_safe_for
from ..automation.cron import CronError, parse_cron
from ..config import Config
from ..database.repository import ConversationRepository
from ..infrastructure import local_tool_name
from ..mcp.auth.models import OAuthError
from .markdown import to_telegram_html, to_telegram_html_chunks
from .media import MediaError, normalize_message
from .. import __version__

logger = logging.getLogger("telegram")

# Telegram hard-limits a single message to 4096 UTF-16 code units. We keep a
# small margin and, more importantly, always send the *entire* reply across
# chunks — we never truncate.
TELEGRAM_MESSAGE_LIMIT = 4096
CHUNK_SIZE = 4000
# Telegram only displays "typing…" for ~5 seconds, so refresh a bit faster.
TYPING_REFRESH_SECONDS = 4.0

# --- Streaming replies (Bot API 10.0 ``sendMessageDraft``) ---------------------
# A private chat with ``ENABLE_STREAMING=true`` shows a live "draft" preview in
# the compose box that animates as the model generates. We push updates at most
# once per interval (coalescing the burst of per-token deltas) and keep the
# preview within the same length cap we use for final delivery.
DRAFT_REFRESH_SECONDS = 0.3
DRAFT_PREVIEW_LIMIT = CHUNK_SIZE
# A process-wide, monotonically increasing supply of non-zero ``draft_id``
# values. Each streaming turn gets a fresh id; reusing one id animates, and a
# fresh id per turn keeps turns independent. (The draft is ephemeral — it
# expires ~30s after the last update — so ids are never persisted.)
_DRAFT_IDS = itertools.count(1)

# Single source of truth for the command list: rendered by ``cmd_help`` and
# advertised to Telegram's native "/" menu. ``(command, short_description)``.
_COMMANDS: list[tuple[str, str]] = [
    ("start", "Start or view the agent"),
    ("new", "Start a new conversation"),
    ("stop", "Stop the current reply"),
    ("context", "Show context budget"),
    ("remember", "Save a long-term memory"),
    ("memories", "List your memories"),
    ("forget", "Forget a memory or all"),
    ("status", "Show run status"),
    ("tool_audit", "Show tool audit log"),
    ("mcp_status", "Show remote MCP tool status"),
    ("mcp", "Show MCP servers / start OAuth login"),
    ("infra_status", "Show configured infra targets"),
    ("schedule_status", "Show configured schedules"),
    ("help", "Show this help"),
]


# ---------------------------------------------------------------------------
# text chunking
# ---------------------------------------------------------------------------
def split_into_chunks(text: str, limit: int = CHUNK_SIZE) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` characters.

    * Prefers to break on existing newlines.
    * Only hard-splits a line that is itself longer than the limit.
    * Never truncates: the concatenation of all pieces equals ``text``.

    The full model reply is split only for *delivery*; it is stored in the
    database once, whole, by the Agent service.
    """
    if text is None:
        return [""]
    text = str(text)
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        # A single line may itself exceed the limit; break it into hard pieces.
        pieces = [line[i : i + limit] for i in range(0, len(line), limit)] if len(line) > limit else [line]
        for piece in pieces:
            if not piece:
                continue
            if current and len(current) + len(piece) > limit:
                chunks.append(current)
                current = piece
            else:
                current += piece
    if current:
        chunks.append(current)
    return chunks


async def _send_long(chat: Chat, text: str, *, reply_to_message_id: int | None = None) -> None:
    """Send the model reply to ``chat``, rendering its Markdown to HTML.

    The reply is split into tag-balanced chunks (a code block never dangles
    across a 4096 split) and each is sent with ``parse_mode=HTML``. If Telegram
    rejects a chunk's HTML — the "can't parse entities" 400, which happens when
    the model emits something outside our supported subset — that one chunk is
    re-sent as **plain text** so the user still gets the content. Other chunks
    keep their formatting.

    ``reply_to_message_id`` (when given) makes the **first** chunk a Telegram
    *Reply* quoting that message, so the answer visibly references the user's
    message it is answering. Only the first chunk carries it — the remaining
    chunks follow normally, so the user's message is quoted once, not every
    chunk.
    """
    first = True
    for chunk in to_telegram_html_chunks(text, limit=CHUNK_SIZE):
        if not chunk.html:
            continue
        reply_kwargs = {"reply_to_message_id": reply_to_message_id} if (first and reply_to_message_id is not None) else {}
        first = False
        try:
            await chat.send_message(text=chunk.html, parse_mode=ParseMode.HTML, **reply_kwargs)
        except BadRequest:
            # Unparseable HTML for this chunk: deliver it verbatim instead.
            logger.warning("html parse failed for a chunk; falling back to plain text")
            for plain in split_into_chunks(chunk.text, limit=CHUNK_SIZE):
                if plain:
                    await chat.send_message(text=plain, **reply_kwargs)
                    reply_kwargs = {}
        except TelegramError:
            # Non-parse errors (FloodWait, timeouts): let the caller's
            # TelegramError handling / on_error log it, as before.
            raise


async def _safe_reply(chat: Chat, text: str) -> None:
    """Send a single short message, never raising on a Telegram error."""
    try:
        await chat.send_message(text=text)
    except TelegramError:
        logger.error("telegram failed to send reply", extra={"chat_id": chat.id}, exc_info=True)


async def deliver_markdown(bot, chat_id: int, markdown_text: str) -> None:
    """Send ``markdown_text`` (rendered Markdown→HTML) to a raw ``chat_id`` via ``bot``.

    The shared delivery primitive for **proactive, non-reply** sends — the phase-9
    scheduled-run notification (and any future out-of-band message) uses it, since
    there is no inbound :class:`Chat` / message to quote. It mirrors
    :func:`_send_long`'s behaviour exactly: the text is split into tag-balanced
    chunks and each is sent with ``parse_mode=HTML``; a chunk whose HTML Telegram
    rejects (the "can't parse entities" 400) is re-sent as **plain text** so the
    recipient still gets the content. The only differences from ``_send_long`` are
    that the target is a bare ``chat_id`` (not a :class:`Chat`) and no
    ``reply_to_message_id`` is ever set (there is nothing to reply to).

    This is the **caller's** responsibility to swallow ``TelegramError`` (the
    interactive path routes its own error handling); a caller that must not crash
    (the schedule runner) wraps the call in its own try/except.
    """
    for chunk in to_telegram_html_chunks(markdown_text, limit=CHUNK_SIZE):
        if not chunk.html:
            continue
        try:
            await bot.send_message(chat_id=chat_id, text=chunk.html, parse_mode=ParseMode.HTML)
        except BadRequest:
            # Unparseable HTML for this chunk: deliver it verbatim instead.
            logger.warning("html parse failed for a delivery chunk; falling back to plain text")
            for plain in split_into_chunks(chunk.text, limit=CHUNK_SIZE):
                if plain:
                    await bot.send_message(chat_id=chat_id, text=plain)
        except TelegramError:
            # Non-parse errors (FloodWait, timeouts): let the caller handle them.
            raise



# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------
def _is_authorized(update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if the sender is in the configured allow-list.

    Unknown users are logged (without revealing anything to them) and ignored.
    We deliberately do not reply to unauthorised users so we do not confirm
    the bot's existence or leak details.
    """
    allowed = context.application.bot_data.get("allowed_user_ids") or set()
    user = update.effective_user
    user_id = user.id if user is not None else None
    if user is None or user_id not in allowed:
        logger.warning(
            "unauthorized telegram user attempted access",
            extra={"telegram_user_id": user_id},
        )
        return False
    return True


# ---------------------------------------------------------------------------
# streaming draft preview (Bot API 10.0 ``sendMessageDraft``)
# ---------------------------------------------------------------------------
def _tail_preview(text: str, limit: int = DRAFT_PREVIEW_LIMIT) -> str:
    """Return ``text`` if it fits the preview cap, else its tail.

    A draft is a *live preview* — only its tail is visible in the compose box.
    When the accumulated reply outgrows the cap we keep the most recent part
    and prefix it with a "…" so the user sees the newest tokens, not an old
    opening. (The *final* message is always delivered whole via ``_send_long``.)
    """
    if len(text) <= limit:
        return text
    return "…\n" + text[-(limit - 2):]


class _DraftStreamer:
    """Coalesces the Agent service's ``on_text_delta`` stream into throttled,
    fail-soft ``sendMessageDraft`` updates for one chat, then finalises.

    The Agent service hands us **accumulated-so-far** text on every token
    (many calls per second). Pushing each to Telegram verbatim would hammer
    the API and, on a bot that isn't on the streaming allowlist, fail every
    call. So we:

    * keep the latest text and *throttle* pushes to at most one per
      ``interval`` seconds (a trailing push guarantees the last chunk lands), and
    * **fail soft**: a draft update that Telegram rejects (the most common
      cause — the bot not being on the streaming allowlist — a
      ``TelegramError``) is logged without a traceback and swallowed. The
      turn keeps running and the full reply is always sent as a normal
      message by the caller, so a draft failure costs the user nothing.

    Privacy: a push logs only the chat id and *class* of any error — never the
    draft body (message content must not reach the logs/audit, per the
    privacy invariant).
    """

    def __init__(self, bot, chat_id: int, draft_id: int, *, interval: float = DRAFT_REFRESH_SECONDS) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._draft_id = draft_id
        self._interval = interval
        self._pending: str | None = None
        self._last_push = 0.0

    async def on_delta(self, text: str) -> None:
        """Accumulated-so-far callback — the ``on_text_delta`` we hand down.

        Stashes the latest text and pushes when due. Must **not** swallow
        ``CancelledError``: a ``/stop`` mid-generation cancels the turn, and
        that must propagate so the caller's cleanup runs.
        """
        self._pending = text
        now = time.monotonic()
        if now - self._last_push >= self._interval:
            await self._flush(now)

    async def finalize(self, final_text: str) -> None:
        """Send one last draft update with the *complete* reply, so the preview
        ends showing the full answer just before the real message is sent."""
        self._pending = final_text
        await self._flush(time.monotonic())

    async def _flush(self, now: float) -> None:
        if self._pending is None:
            return
        self._last_push = now
        try:
            await self._bot.send_message_draft(
                self._chat_id,
                draft_id=self._draft_id,
                text=_tail_preview(self._pending),
            )
        except TelegramError:
            # Fail soft: e.g. the bot isn't on the streaming allowlist. Log the
            # class only — never the draft body (privacy invariant).
            logger.debug(
                "draft update not delivered; continuing to normal send",
                extra={"chat_id": self._chat_id},
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# typing keep-alive
# ---------------------------------------------------------------------------
async def _typing_loop(bot, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except TelegramError:
            logger.debug("failed to send typing action", exc_info=True)
        # Sleep up to the refresh interval, waking early if we are stopped.
        try:
            await asyncio.wait_for(stop.wait(), timeout=TYPING_REFRESH_SECONDS)
        except asyncio.TimeoutError:
            continue


# bot_data key for the in-flight reply task, one per chat. A chat may have at
# most one turn running at a time (its per-conversation lock serialises them),
# so a single slot keyed by chat id is exactly the handle ``/stop`` cancels.
_IN_FLIGHT = "in_flight"


def _take_in_flight(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> asyncio.Task | None:
    """Pop and return the in-flight reply task for ``chat_id``, or ``None``.

    The task is consumed (removed from the registry) as it is taken so that a
    ``/stop`` racing with the turn finishing never cancels an already-settled
    task (cancelling a done task is a no-op, but removing it here keeps the
    registry honest). A missing entry simply means the chat is idle.
    """
    in_flight = context.application.bot_data.get(_IN_FLIGHT)
    if not in_flight:
        return None
    return in_flight.pop(chat_id, None)


async def _with_typing(bot, chat_id: int, coro):
    """Run ``coro`` while keeping the "typing…" indicator alive."""
    stop = asyncio.Event()
    task = asyncio.create_task(_typing_loop(bot, chat_id, stop))
    try:
        return await coro
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if not task.done():
                task.cancel()


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------
async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    user_id = update.effective_user.id
    repo: ConversationRepository = context.application.bot_data["repository"]
    config: Config = context.application.bot_data["config"]

    conversation = await repo.get_conversation(chat.id)
    if conversation is None:
        conversation = await repo.get_or_create_conversation(chat.id, user_id)
        await _send_long(
            chat,
            "**Agent started.**\n\n"
            f"**Model:** {config.openai_model}\n"
            f"**Conversation:** {conversation.id}\n\n"
            "You can send messages directly.",
        )
    else:
        await _send_long(
            chat,
            f"**Agent is already running.** Current conversation: {conversation.id}.",
        )


async def cmd_new(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a fresh conversation for this chat (drops all its history)."""
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    user_id = update.effective_user.id
    service: AgentService = context.application.bot_data["agent_service"]
    await service.reset(chat.id, user_id)
    await _send_long(chat, "**New conversation started** (history cleared).")


async def cmd_stop(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop — interrupt the chat's in-flight reply.

    Cancels the ``handle_message`` task that is currently generating a reply or
    running a tool for *this chat* (if one is running). Because updates are
    handled concurrently (``concurrent_updates``), this handler runs as an
    independent task and can cancel the other. Cancelling unwinds the turn: the
    per-conversation lock releases (so a later message proceeds), the typing
    keep-alive stops, and the in-flight handle is removed. The stopped turn
    itself posts a short **Telegram Reply** to the interrupted message ("⛔️
    **Interrupted.**"). When nothing is running for this chat, it replies
    "Nothing to stop." Unauthorised senders are ignored silently, exactly like
    every other command.

    This only stops a *generation* — it does not drop the conversation or
    memory (that is ``/new``), and it never touches another chat's turn.
    """
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    task = _take_in_flight(context, chat.id)
    if task is None or task.done():
        await _send_long(chat, "**Nothing to stop.**")
        return
    task.cancel()
    # Do not await the cancelled task here: it is unwinding in its own task and
    # posts its own "⛔️ **Interrupted.**" notice. Awaiting it could deadlock on
    # the very per-conversation lock it is releasing.


async def cmd_help(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the available commands (generated from ``_COMMANDS``)."""
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    lines = ["**Available commands:**", ""]
    for cmd, desc in _COMMANDS:
        lines.append(f"/{cmd} — {desc}")
    lines += ["", "Any other text message is sent to the agent."]
    await _send_long(chat, "\n".join(lines))


async def cmd_status(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    service: AgentService = context.application.bot_data["agent_service"]
    repo: ConversationRepository = context.application.bot_data["repository"]
    config: Config = context.application.bot_data["config"]

    conversation = await repo.get_conversation(chat.id)
    if conversation is None:
        lines = [
            "**Agent Backend:**",
            "**Status:** OK",
            "",
            f"**Version:** {__version__}",
            f"**Model:** {config.openai_model}",
            "**Conversation:** (none yet — send /start)",
            "**Database:** OK",
        ]
    else:
        status = await service.conversation_status(conversation.id)
        lines = [
            "**Agent Backend:**",
            "**Status:** OK",
            "",
            f"**Version:** {__version__}",
            f"**Model:** {config.openai_model}",
            f"**Conversation:** {conversation.id}",
            f"**Messages:** {status['messages']}",
            "**Database:** OK",
        ]
    # None of the above exposes keys, tokens, or file paths.
    await _send_long(chat, "\n".join(lines))


async def cmd_context(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the context window: how much of the stored history (and its images)
    would fit both the message cap and the estimated-token budget.

    A read-only preview. It never reads an attachment blob (planning is
    metadata-only) and the reply exposes only counts and the conservative
    estimated costs — no message text, captions, digests, paths, or secrets.
    """
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    service: AgentService = context.application.bot_data["agent_service"]
    repo: ConversationRepository = context.application.bot_data["repository"]

    conversation = await repo.get_conversation(chat.id)
    if conversation is None:
        await _send_long(chat, "**No conversation yet** — send /start first.")
        return

    s = await service.context_status(conversation.id)
    free_tokens = s["budget"] - s["estimated_cost"]
    free_messages = s["cap"] - (s["history_messages"] + 1)
    images_downgraded = s["images_in_store"] - s["images_kept"]
    lines = [
        "**Context:**",
        f"**Conversation:** {s['conversation_id']}",
        "",
        f"**Message cap:** {s['cap']}",
        f"**Stored:** {s['stored_messages']} messages",
        f"**Kept this turn:** {s['history_messages']} (+1 current)",
        f"**Room left:** ~{free_messages} messages",
        "",
        f"**Estimated budget:** {s['budget']} units",
        f"**Used:** ~{s['estimated_cost']} units (system {s['system_cost']})",
        f"**Free:** ~{free_tokens} units",
        "",
        f"**History images kept:** {s['images_kept']} / {s['images_in_store']}"
        + (f" ({images_downgraded} downgraded to text)" if images_downgraded > 0 else ""),
        "",
        "(Conservative estimate, not exact tokens.)",
    ]
    # None of the above exposes message text, keys, tokens, digests, or paths.
    await _send_long(chat, "\n".join(lines))


# ---------------------------------------------------------------------------
# long-term memory commands (phase 2.5)
# ---------------------------------------------------------------------------
def _memory_scope(update) -> str:
    """The opaque, channel-agnostic principal scope for long-term memory.

    Built here — and *only* here — from the Telegram identity. The agent
    service, memory package, and database treat it as an opaque string and never
    see a Telegram ``User`` / ``chat_id`` / ``file_id``.
    """
    return f"telegram:{update.effective_user.id}"


def _command_body(text: str | None) -> str:
    """The argument text after a ``/command`` (its leading token removed)."""
    if not text:
        return ""
    parts = text.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def cmd_remember(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/remember <content> — save one explicit long-term memory."""
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    service: AgentService = context.application.bot_data["agent_service"]
    scope = _memory_scope(update)
    content = _command_body(update.effective_message.text)

    try:
        record = await service.remember_memory(scope, content)
    except AgentError as exc:
        await _safe_reply(chat, exc.user_safe)
        return
    except Exception:  # never crash on an unexpected handler error
        logger.error("remember command failed", exc_info=True)
        await _safe_reply(chat, _user_safe_for("memory_error"))
        return
    await _send_long(chat, f"**Memory saved.**\n**ID:** {record.id}\n\n{record.content}")


async def cmd_memories(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/memories — list the caller's own memories (chunked for long lists)."""
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    service: AgentService = context.application.bot_data["agent_service"]
    scope = _memory_scope(update)

    try:
        records = await service.list_memories(scope)
    except AgentError as exc:
        await _safe_reply(chat, exc.user_safe)
        return
    except Exception:
        logger.error("memories command failed", exc_info=True)
        await _safe_reply(chat, _user_safe_for("memory_error"))
        return

    if not records:
        await _send_long(chat, "**No memories saved yet.** Use /remember <text> to save one.")
        return

    lines = [f"**Your memories:** ({len(records)} total)", ""]
    for r in records:
        lines.append(f"**#{r.id}** (saved {_utc_stamp(r.created_at)})")
        lines.append(r.content)
        lines.append("")
    await _send_long(chat, "\n".join(lines).rstrip())


async def cmd_forget(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/forget <id> — delete one memory; /forget all CONFIRM — delete all.

    ``/forget all`` without the exact ``CONFIRM`` token only shows the
    confirmation format and changes nothing. A foreign/missing id is reported
    exactly as a missing one (no existence leak).
    """
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    service: AgentService = context.application.bot_data["agent_service"]
    scope = _memory_scope(update)
    tokens = _command_body(update.effective_message.text).split()

    if not tokens:
        await _safe_reply(chat, "Usage: /forget <id> or /forget all CONFIRM")
        return

    try:
        if tokens[0].lower() == "all":
            if len(tokens) >= 2 and tokens[1] == "CONFIRM":
                removed = await service.forget_all_memories(scope)
                await _send_long(chat, f"**All memories cleared.** ({removed} deleted)")
            else:
                await _safe_reply(chat, _user_safe_for("memory_clear_confirmation"))
            return

        try:
            memory_id = int(tokens[0])
        except ValueError:
            raise AgentError("Usage: /forget <id> or /forget all CONFIRM", "memory_invalid")
        await service.forget_memory(scope, memory_id)
        await _send_long(chat, f"**Memory deleted.** (ID: {memory_id})")
    except AgentError as exc:
        await _safe_reply(chat, exc.user_safe)
        return
    except Exception:
        logger.error("forget command failed", exc_info=True)
        await _safe_reply(chat, _user_safe_for("memory_error"))
        return


def _utc_stamp(dt) -> str:
    """A short UTC timestamp for display (time only — never memory content)."""
    try:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:  # pragma: no cover - defensive (dt is always tz-aware)
        return str(dt)


# ---------------------------------------------------------------------------
# tool audit command (phase 3)
# ---------------------------------------------------------------------------
_TOOL_AUDIT_MAX_LIMIT = 50


async def cmd_tool_audit(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tool_audit [limit] — show the caller's own recent tool-audit events.

    Read-only and scope-isolated: it reads only the *current* principal's audit
    rows (by irreversible scope hash) and shows, per event, a timestamp, the
    event id, the tool name, the event type, a stable result code, and (where
    recorded) the latency. It never shows tool arguments, tool results, exception
    text, the raw scope/user id, or any secret. An unauthorised sender is ignored
    silently, exactly like every other command.
    """
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    service: AgentService = context.application.bot_data["agent_service"]
    scope = _memory_scope(update)

    # Optional ``[limit]``: default 20, clamped to 1..50; a non-numeric arg is a
    # usage hint (not an error, not a crash).
    arg = _command_body(update.effective_message.text).strip()
    limit = 20
    if arg:
        try:
            limit = max(1, min(_TOOL_AUDIT_MAX_LIMIT, int(arg)))
        except ValueError:
            await _safe_reply(chat, "Usage: /tool_audit [limit]  (limit is 1-50)")
            return

    try:
        records = await service.list_tool_audit_events(scope, limit)
    except AgentError as exc:
        await _safe_reply(chat, exc.user_safe)
        return
    except Exception:  # never crash on an unexpected handler error
        logger.error("tool_audit command failed", exc_info=True)
        await _safe_reply(chat, _user_safe_for("tool_audit_error"))
        return

    if not records:
        await _send_long(chat, "**No tool activity yet.** Tool calls will appear here as they run.")
        return

    lines = [f"**Tool audit:** (last {len(records)} events, most recent first)", ""]
    for r in records:
        line = f"**#{r.id}** {_utc_stamp(r.created_at)} — {r.tool_name} / {r.event_type}"
        if r.code:
            line += f" / {r.code}"
        if r.latency_ms is not None:
            line += f" / {r.latency_ms}ms"
        lines.append(line)
    lines += ["", "(Codes are stable, human-readable status tags — arguments and results are not shown.)"]
    # None of the above exposes tool args, results, exception text, raw scope,
    # the user id, or any secret.
    await _send_long(chat, "\n".join(lines))


# ---------------------------------------------------------------------------
# remote MCP status command (phase 4)
# ---------------------------------------------------------------------------
async def cmd_mcp_status(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mcp_status — show which configured remote MCP servers are up and how many
    tools each exposes.

    Read-only and non-mutating: it reads the in-memory :class:`~..mcp.McpManager`
    state set at startup. It does **not** connect, refresh, re-discover, or call
    the LLM or any MCP server. The reply shows only each server's *name*, an
    ``available``/``unavailable`` flag, and its discovered-tool count, plus the
    total — it never exposes a URL, host, header, token, tool description or
    schema, server instructions, or a failure detail. An unauthorised sender is
    ignored silently, exactly like every other command.
    """
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    config: Config = context.application.bot_data["config"]
    manager = context.application.bot_data.get("mcp_manager")

    # No manager (no servers configured, or tools disabled) → MCP is disabled.
    if manager is None or not getattr(config, "enable_tools", True) or len(manager) == 0:
        await _send_long(chat, "**MCP:** disabled")
        return

    lines = ["**Remote MCP servers:**", ""]
    for entry in manager.status():
        state = "available" if entry["available"] else "unavailable"
        lines.append(f"**{entry['name']}** — {state} ({entry['tool_count']} tools)")
    lines += ["", f"**Total MCP tools available:** {manager.total_tools}"]
    # None of the above exposes a URL, host, header, token, description, schema,
    # server instructions, or any failure detail.
    await _send_long(chat, "\n".join(lines))


# ---------------------------------------------------------------------------
# read-only infrastructure observation status (phase 5.1)
# ---------------------------------------------------------------------------
async def cmd_infra_status(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/infra_status — show which read-only infrastructure targets are configured.

    Read-only and non-mutating: it renders only the **configuration** set from
    the in-memory :class:`~..config.Config` at startup — each target's *name* and
    its three fixed, argument-free, read-only tool names (which run without a
    per-call approval). It does **not** connect over SSH, refresh anything, probe
    reachability, or call the LLM or any target. The reply shows **only** the
    target name and the local tool names; it never exposes a host, port, username,
    key path, known_hosts path, mount path, service name, or any command — and it
    draws **no** conclusion about whether a target is reachable (that is unknown
    until a tool is run). An unauthorised sender is ignored silently, exactly
    like every other command.
    """
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    config: Config = context.application.bot_data["config"]

    # No targets configured, or tools disabled → the provider is off.
    if not getattr(config, "enable_tools", True) or not config.infra_ssh_targets:
        await _send_long(chat, "**Infrastructure:** disabled")
        return

    lines = ["**Infrastructure observation targets (read-only):**", ""]
    for target in config.infra_ssh_targets:
        tool_names = ", ".join(
            f"`{local_tool_name(target.name, obs)}`"
            for obs in ("host_status", "disk_status", "service_status")
        )
        lines.append(f"**{target.name}** — configured (3 tools, read-only): {tool_names}")
    lines += [
        "",
        f"**Total configured tools:** {len(config.infra_ssh_targets) * 3}",
        "",
        "(Configured only — this shows nothing about reachability; a status is "
        "read only when the corresponding tool is actually called.)",
    ]
    # None of the above exposes a host, port, username, key path, known_hosts
    # path, mount path, service name, or command — only the target name and the
    # (operator-named) local tool names.
    await _send_long(chat, "\n".join(lines))


# ---------------------------------------------------------------------------
# read-only schedule status (phase 9)
# ---------------------------------------------------------------------------
async def cmd_schedule_status(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/schedule_status — show the configured cron schedules and their next fire time.

    Read-only and non-mutating: it renders the startup-configured
    :class:`~..config.ScheduleSpec` list (name + cron expression) and, for each,
    the next fire time computed by the pure :func:`..automation.cron.parse_cron`
    cron parser in ``SCHEDULE_TIMEZONE``. It does **not** trigger a run, call the
    LLM, or touch any conversation. The reply shows **only** each schedule's
    *name*, its *cron* expression, and its *next fire time* — never its ``prompt``,
    ``chat_id``, or ``user_id`` (those are startup config and must not be exposed
    through a command). A schedule whose cron has no fire time in the search
    window (calendar-impossible) is shown as "never (untriggerable)". An
    unauthorised sender is ignored silently, exactly like every other command.
    """
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    config: Config = context.application.bot_data["config"]

    if not config.schedules:
        await _send_long(chat, "**Schedules:** disabled (none configured)")
        return

    tz = ZoneInfo(config.schedule_timezone) if config.schedule_timezone else datetime.now().astimezone().tzinfo
    now = datetime.now(tz)
    lines = ["**Scheduled tasks:**", ""]
    for spec in config.schedules:
        try:
            nxt = parse_cron(spec.cron).next_fire(now, tz)
        except CronError:
            # Cannot happen — the cron was validated at startup — but stay safe.
            nxt = None
        if nxt is None:
            fire = "never (untriggerable)"
        else:
            fire = nxt.strftime("%Y-%m-%d %H:%M %Z")
        lines.append(f"**{spec.name}** — `{spec.cron}` → next: {fire}")
    lines += ["", f"(Times in {config.schedule_timezone or 'local tz'}. Read-only — this does not trigger anything.)"]
    # Name + cron + next-fire only; never prompt / chat_id / user_id.
    await _send_long(chat, "\n".join(lines))


# ---------------------------------------------------------------------------
# MCP status + user-level OAuth (phase 4.x)
# ---------------------------------------------------------------------------
async def cmd_mcp(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mcp — show the caller's MCP status, or start an OAuth login.

    With **no argument** it is a read-only status view: for each configured
    server it reports availability and, for a server that uses **user-level
    OAuth**, *this user's* connection state (connected / authentication required
    / expired / not configured) — it never reads or shows a token, a URL, a
    header, or another user's state.

    With **``auth <server>``** it starts the user-level OAuth flow for that one
    server: the credential binds to **this Telegram user** (never to a
    conversation, so ``/new`` never affects it) and the reply is an inline URL
    button (the user is never asked to copy a URL). The link embeds a single-use,
    expiring ``state``. Any failure is a fixed, user-safe message — never a
    token, a full callback URL, or a stack trace.

    An unauthorised sender is ignored silently, like every other command.
    """
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    bot_data = context.application.bot_data
    body = _command_body(update.effective_message.text).strip()
    tokens = body.split()
    # ``/mcp auth <server>`` → start the login flow (the only OAuth entry point;
    # Telegram treats ``/mcp auth ...`` as the single command ``/mcp`` with args).
    if tokens and tokens[0].lower() == "auth":
        server_name = " ".join(tokens[1:]).strip()
        await _start_mcp_auth(chat, update.effective_user.id, server_name, bot_data.get("oauth_manager"))
        return
    await _show_mcp_status(
        chat,
        update.effective_user.id,
        bot_data["config"],
        bot_data.get("mcp_manager"),
        bot_data.get("oauth_manager"),
    )


async def _show_mcp_status(chat, user_id, config, manager, oauth_manager) -> None:
    """The read-only ``/mcp`` status view (availability + per-user OAuth state)."""
    if manager is None or not getattr(config, "enable_tools", True) or len(manager) == 0:
        await _send_long(chat, "**MCP:** disabled")
        return

    status_by_name = {entry["name"]: entry for entry in manager.status()}
    lines = ["**MCP servers:**", ""]
    for spec in config.mcp_servers:
        entry = status_by_name.get(spec.name)
        if entry is None or not entry["available"]:
            lines.append(f"✗ **{spec.name}** — unavailable")
            continue
        if spec.auth_type == "oauth" and oauth_manager is not None:
            try:
                state = await oauth_manager.oauth_status(telegram_user_id=user_id, mcp_server=spec.name)
            except Exception:  # a status lookup failure must never crash the reply
                logger.warning("mcp oauth status lookup failed", extra={"server": spec.name})
                state = "authentication_required"
            _oauth_lines = {
                "connected": f"✓ **{spec.name}** — connected (your {spec.auth_provider} account)",
                "authentication_required": f"✗ **{spec.name}** — authentication required — /mcp auth {spec.name}",
                "expired": f"✗ **{spec.name}** — authorization expired — /mcp auth {spec.name}",
                "not_configured": f"✗ **{spec.name}** — OAuth not configured",
                "provider_not_configured": f"✗ **{spec.name}** — OAuth provider not configured",
                "not_oauth": f"✓ **{spec.name}** — available",
            }
            lines.append(_oauth_lines.get(state, f"✓ **{spec.name}** — available"))
        else:
            lines.append(f"✓ **{spec.name}** — available")
    await _send_long(chat, "\n".join(lines))


async def _start_mcp_auth(chat, user_id, server_name, oauth_manager) -> None:
    """The ``/mcp auth <server>`` flow: an inline **URL button**, bound to the
    caller's Telegram user. Any failure is a fixed, user-safe message — never a
    token, a full callback URL, or a stack trace."""
    if not server_name:
        await _safe_reply(chat, "Usage: /mcp auth <server>")
        return
    if oauth_manager is None:
        await _safe_reply(
            chat,
            "OAuth is not configured on this server (set OAUTH_CALLBACK_BASE_URL and the provider credentials).",
        )
        return

    try:
        pending = await oauth_manager.initiate(
            telegram_user_id=user_id, chat_id=chat.id, mcp_server=server_name
        )
    except OAuthError as exc:
        # exc.user_safe is a fixed, secret-free message naming only the code.
        await _safe_reply(chat, exc.user_safe)
        return
    except Exception:  # never crash the handler; a DB failure is user-safe too
        logger.error("mcp auth initiate failed", extra={"server": server_name})
        await _safe_reply(chat, "Could not start the authorization. Please try again.")
        return

    minutes = max(1, int(pending.expires_in_seconds // 60))
    body = (
        f"**{server_name}** requires authorization.\n\n"
        "Please sign in with your account:\n"
    )
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="🔐 Sign in", url=pending.authorization_url)]]
    )
    note = f"The authorization link expires in ~{minutes} minutes."
    try:
        # body is Markdown; render it to HTML before the parse_mode=HTML send
        # (this call carries the inline URL button, so it can't use _send_long).
        await chat.send_message(
            text=to_telegram_html(body), parse_mode=ParseMode.HTML, reply_markup=markup
        )
        await _safe_reply(chat, note)
    except TelegramError:
        logger.error("failed to send oauth login prompt", extra={"server": server_name})


async def handle_message(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    message = update.effective_message
    # Accept either a plain text message or a photo (with or without a caption).
    # Photo captions carry their text in ``message.caption``, not ``message.text``.
    has_text = bool((message.text or "").strip()) if message is not None else False
    has_photo = bool(message.photo) if message is not None else False
    if message is None or not (has_text or has_photo):
        return
    user_id = update.effective_user.id

    service: AgentService = context.application.bot_data["agent_service"]
    repo: ConversationRepository = context.application.bot_data["repository"]
    config: Config = context.application.bot_data["config"]

    conversation = await repo.get_or_create_conversation(chat.id, user_id)
    conversation_id = conversation.id

    # Normalize the Telegram message into a channel-independent AgentMessage.
    # This is where a photo is downloaded (bytes held in memory) and size/MIME
    # are validated; a failure here is user-safe and never crashes the backend.
    try:
        agent_message = await normalize_message(message, config.max_image_size_bytes)
    except MediaError as exc:
        logger.warning(
            "media could not be processed",
            extra={"conversation_id": conversation_id, "category": exc.category},
        )
        await _safe_reply(chat, exc.user_safe)
        return

    logger.info(
        "received message",
        extra={
            "conversation_id": conversation_id,
            "message_id": message.message_id,
            "has_image": agent_message.has_image(),
            "text_length": len(agent_message.text),
        },
    )

    # Register this turn as the chat's in-flight reply so ``/stop`` can cancel
    # it while the model is generating or a tool is running. The per-conversation
    # lock serialises a chat, so there is at most one such task per chat. The
    # handle is removed in the ``finally`` below (completion *and* cancellation),
    # so a finished or stopped turn never lingers as a stale, cancellable handle.
    bot_data = context.application.bot_data
    bot_data.setdefault(_IN_FLIGHT, {})[chat.id] = asyncio.current_task()
    try:
        try:
            # Streaming replies: a *private* chat with ``ENABLE_STREAMING=true``
            # shows a live "draft" preview that animates as the model generates.
            # Group / channel chats (and a disabled knob) always degrade to the
            # classic "typing…" keep-alive + chunked final reply. *Both* branches
            # keep the "typing…" indicator alive (see the streaming branch
            # below): the draft is the *preferred* indicator, but the typing
            # action is the fallback that stays visible when the draft can't be
            # shown (the bot is not on Telegram's streaming allowlist).
            streaming = bool(config.enable_streaming) and chat.type == ChatType.PRIVATE
            if streaming:
                streamer = _DraftStreamer(context.bot, chat.id, next(_DRAFT_IDS))
                # Run the "typing…" keep-alive in parallel with the draft. The
                # draft is rejected (fail-soft, inside ``_DraftStreamer``) unless
                # the bot is on Telegram's streaming allowlist — without this
                # fallback there'd be *no* "the bot is working" feedback at all
                # for such a deployment. When the draft *can* be shown the typing
                # action is at worst a harmless duplicate (and, if Telegram
                # rejects a typing action while a draft is up, ``_typing_loop``
                # already swallows that error).
                reply = await _with_typing(
                    context.bot,
                    chat.id,
                    service.process_message(
                        conversation_id,
                        agent_message,
                        memory_scope=_memory_scope(update),
                        on_text_delta=streamer.on_delta,
                    ),
                )
                # Show the complete answer in the preview, then send the real
                # message. A draft failure here (e.g. bot not allowlisted) is
                # fail-soft inside ``finalize`` and never suppresses the send.
                await streamer.finalize(reply)
            else:
                reply = await _with_typing(
                    context.bot,
                    chat.id,
                    service.process_message(conversation_id, agent_message, memory_scope=_memory_scope(update)),
                )
        except AgentError as exc:
            logger.info(
                "llm error surfaced to user",
                extra={"conversation_id": conversation_id, "category": exc.category},
            )
            await _safe_reply(chat, exc.user_safe)
            return
        except Exception:
            logger.exception("unexpected error handling message", extra={"conversation_id": conversation_id})
            await _safe_reply(chat, "出现了一个意外错误，请稍后重试。")
            return

        if not reply:
            return
        try:
            # The final answer quotes the user's message (Telegram Reply) so it
            # visibly references what it is answering. Only this last reply carries
            # the reference — command acks, the typing keep-alive, and intermediate
            # sends do not.
            await _send_long(chat, reply, reply_to_message_id=message.message_id)
        except TelegramError:
            logger.error("failed to send reply", extra={"conversation_id": conversation_id}, exc_info=True)
    except asyncio.CancelledError:
        # ``/stop`` (or a shutdown) cancelled this turn while it was generating or
        # running a tool. ``process_message``'s per-conversation lock is an async
        # context manager, so it already released on this unwind — the next message
        # can proceed. Send a short, bold notice as a **Telegram Reply quoting the
        # user's interrupted message** (like the final answer quotes its question),
        # then re-raise so the task is observed as cancelled: PTB never treats a
        # cancelled handler as an error, and (in the non-streaming branch) the
        # typing keep-alive in ``_with_typing``'s ``finally`` stops before this
        # point; in the streaming branch the draft is simply left to expire. The
        # notice is best-effort — a failed send never changes the cancellation outcome.
        logger.info("turn cancelled by /stop", extra={"conversation_id": conversation_id})
        try:
            await chat.send_message(
                text=to_telegram_html("⛔️ **Interrupted.**"),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message.message_id,
            )
        except TelegramError:
            logger.error(
                "failed to send stop notice", extra={"conversation_id": conversation_id}, exc_info=True
            )
        raise
    finally:
        in_flight = bot_data.get(_IN_FLIGHT)
        if in_flight is not None:
            in_flight.pop(chat.id, None)


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------
async def on_error(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all so an unexpected failure logs cleanly instead of crashing.

    Telegram API errors (FloodWait, timeouts, …) are a warning; anything else
    is logged with its traceback. In neither case do we leak details to the
    user or kill the process.
    """
    err = context.error
    if isinstance(err, TelegramError):
        logger.warning("telegram API error: %s", getattr(err, "message", repr(err)))
    else:
        logger.error("unhandled exception processing update", exc_info=err)


# ---------------------------------------------------------------------------
# startup hooks
# ---------------------------------------------------------------------------
def compose_startup_hooks(*hooks) -> Callable:
    """Chain zero or more ``async (application) -> None`` hooks into one.

    Lets the Telegram adapter advertise its command menu and the composition
    root initialise the database without each overwriting the other's
    ``post_init``. ``None`` hooks are skipped, so callers can pass optional
    hooks positionally.
    """
    async def chained(application) -> None:
        for hook in hooks:
            if hook is not None:
                await hook(application)
    return chained


async def register_command_menu(application) -> None:
    """Advertise our commands in Telegram's native "/" menu (best-effort).

    Runs in ``post_init`` (inside the running event loop) because it is a
    network call. A failure here must not stop the bot from starting.
    """
    try:
        await application.bot.set_my_commands([BotCommand(c, d) for c, d in _COMMANDS])
    except TelegramError:
        logger.warning("failed to set bot command menu", exc_info=True)


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
def build_application(
    config: Config,
    service: AgentService,
    repository: ConversationRepository,
    approval_broker=None,
    mcp_manager=None,
    oauth_manager=None,
) -> Application:
    """Assemble the PTB :class:`Application` and register all handlers.

    Startup work (command menu, DB init, MCP discovery) is wired by the caller
    via :func:`compose_startup_hooks`, not here — this only registers handlers.

    ``approval_broker`` (an in-memory :class:`~..telegram.approval
    .TelegramApprovalBroker`, optional) supplies the phase-3 Approve/Deny
    callback handler. When ``None`` (tools disabled, or a bare unit test) no
    callback handler is registered and no broker is bound — the approval path is
    simply absent.

    ``mcp_manager`` (the phase-4 :class:`~..mcp.McpManager`, optional) is
    exposed in ``bot_data`` so the read-only ``/mcp_status`` command can report
    each server's availability and discovered-tool count. When ``None`` (no
    servers configured, or tools disabled) ``/mcp_status`` reports MCP as
    disabled. It is **never** used to connect or refresh — the manager is
    started/closed by the composition root's lifecycle hooks only.

    ``oauth_manager`` (the phase-4.x :class:`~..mcp.auth.OAuthManager`,
    optional) is exposed in ``bot_data`` for the ``/mcp`` status view and the
    ``/mcp auth <server>`` login flow. When ``None`` (OAuth not configured)
    ``/mcp auth`` reports "not configured" and ``/mcp`` shows OAuth servers as
    unavailable — no OAuth URL is ever generated.
    """
    application = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .concurrent_updates(True)  # different chats may be handled concurrently
        .build()
    )
    application.bot_data["agent_service"] = service
    application.bot_data["repository"] = repository
    application.bot_data["config"] = config
    application.bot_data["allowed_user_ids"] = set(config.allowed_user_ids)
    application.bot_data["mcp_manager"] = mcp_manager
    application.bot_data["oauth_manager"] = oauth_manager

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("new", cmd_new))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CommandHandler("context", cmd_context))
    application.add_handler(CommandHandler("remember", cmd_remember))
    application.add_handler(CommandHandler("memories", cmd_memories))
    application.add_handler(CommandHandler("forget", cmd_forget))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("tool_audit", cmd_tool_audit))
    application.add_handler(CommandHandler("mcp_status", cmd_mcp_status))
    # Phase 5.1: read-only view of the configured infrastructure observation
    # targets (config metadata only — no SSH / LLM / reachability).
    application.add_handler(CommandHandler("infra_status", cmd_infra_status))
    # Phase 9: read-only view of the configured cron schedules + next fire time
    # (config metadata only — no LLM, no trigger, no prompt/chat_id/user_id).
    application.add_handler(CommandHandler("schedule_status", cmd_schedule_status))
    # Phase 4.x: MCP status + user-level OAuth login. ``/mcp auth <server>`` is
    # the single ``/mcp`` command with an ``auth`` argument (Telegram treats it
    # as one command), so it is dispatched inside ``cmd_mcp`` — there is no
    # separate ``mcp_auth`` command (that would never match ``/mcp auth …``).
    application.add_handler(CommandHandler("mcp", cmd_mcp))
    # Phase 3: the Approve/Deny inline-button callback (bound to the exact
    # (principal, chat) that requested the approval; all other clicks are no-ops).
    if approval_broker is not None:
        approval_broker.bind_application(application)
        application.bot_data["approval_broker"] = approval_broker
        application.add_handler(approval_broker.build_callback_handler())
    # Plain text messages *and* photos (with or without a caption). Commands are
    # excluded and handled above. A photo's caption lives in message.caption, so
    # it is delivered here, not by the TEXT filter.
    application.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_message))
    # Log any unhandled update error instead of crashing.
    application.add_error_handler(on_error)
    return application
