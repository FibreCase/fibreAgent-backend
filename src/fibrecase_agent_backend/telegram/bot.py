"""Telegram adapter.

This is the only module that knows about Telegram. It wires Telegram events
to the channel-agnostic :class:`~..agent.service.AgentService` and handles the
concerns specific to this transport:

* user authorisation (only configured Telegram user ids may use the bot),
* the ``/start``, ``/new``, ``/help`` and ``/status`` commands,
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
import logging
from typing import Callable

from telegram import BotCommand, Chat
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..agent.service import AgentError, AgentService
from ..config import Config
from ..database.repository import ConversationRepository
from .markdown import to_telegram_html_chunks
from .media import MediaError, normalize_message

logger = logging.getLogger("telegram")

# Telegram hard-limits a single message to 4096 UTF-16 code units. We keep a
# small margin and, more importantly, always send the *entire* reply across
# chunks — we never truncate.
TELEGRAM_MESSAGE_LIMIT = 4096
CHUNK_SIZE = 4000
# Telegram only displays "typing…" for ~5 seconds, so refresh a bit faster.
TYPING_REFRESH_SECONDS = 4.0

# Single source of truth for the command list: rendered by ``cmd_help`` and
# advertised to Telegram's native "/" menu. ``(command, short_description)``.
_COMMANDS: list[tuple[str, str]] = [
    ("start", "启动 / 查看当前 Agent"),
    ("new", "开始新会话（清空历史）"),
    ("status", "查看运行状态"),
    ("help", "显示本帮助"),
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


async def _send_long(chat: Chat, text: str) -> None:
    """Send the model reply to ``chat``, rendering its Markdown to HTML.

    The reply is split into tag-balanced chunks (a code block never dangles
    across a 4096 split) and each is sent with ``parse_mode=HTML``. If Telegram
    rejects a chunk's HTML — the "can't parse entities" 400, which happens when
    the model emits something outside our supported subset — that one chunk is
    re-sent as **plain text** so the user still gets the content. Other chunks
    keep their formatting.
    """
    for chunk in to_telegram_html_chunks(text, limit=CHUNK_SIZE):
        if not chunk.html:
            continue
        try:
            await chat.send_message(text=chunk.html, parse_mode=ParseMode.HTML)
        except BadRequest:
            # Unparseable HTML for this chunk: deliver it verbatim instead.
            logger.warning("html parse failed for a chunk; falling back to plain text")
            for plain in split_into_chunks(chunk.text, limit=CHUNK_SIZE):
                if plain:
                    await chat.send_message(text=plain)
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
            "Agent 已启动。\n\n"
            f"当前模型：\n{config.openai_model}\n\n"
            f"当前会话：\n{conversation.id}\n\n"
            "你可以直接发送消息。",
        )
    else:
        await _send_long(
            chat,
            f"Agent 已在运行。当前会话：{conversation.id}。",
        )


async def cmd_new(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a fresh conversation for this chat (drops all its history)."""
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    user_id = update.effective_user.id
    service: AgentService = context.application.bot_data["agent_service"]
    await service.reset(chat.id, user_id)
    await _send_long(chat, "已开始新的会话（历史已清空）。")


async def cmd_help(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the available commands (generated from ``_COMMANDS``)."""
    if not _is_authorized(update, context):
        return
    chat = update.effective_chat
    lines = ["可用命令：", ""]
    for cmd, desc in _COMMANDS:
        lines.append(f"/{cmd} — {desc}")
    lines += ["", "其它文字消息都会发给 Agent。"]
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
            "Agent Backend",
            "Status: OK",
            "",
            f"Model:\n{config.openai_model}",
            "",
            "Conversation:\n(none yet — 发送 /start 开始)",
            "",
            "Database:\nOK",
        ]
    else:
        status = await service.conversation_status(conversation.id)
        lines = [
            "Agent Backend",
            "Status: OK",
            "",
            f"Model:\n{config.openai_model}",
            "",
            f"Conversation:\n{conversation.id}",
            "",
            f"Messages:\n{status['messages']}",
            "",
            "Database:\nOK",
        ]
    # None of the above exposes keys, tokens, or file paths.
    await _send_long(chat, "\n".join(lines))


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

    try:
        reply = await _with_typing(context.bot, chat.id, service.process_message(conversation_id, agent_message))
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
        await _send_long(chat, reply)
    except TelegramError:
        logger.error("failed to send reply", extra={"conversation_id": conversation_id}, exc_info=True)


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
) -> Application:
    """Assemble the PTB :class:`Application` and register all handlers.

    Startup work (command menu, DB init) is wired by the caller via
    :func:`compose_startup_hooks`, not here — this only registers handlers.
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

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("new", cmd_new))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    # Plain text messages *and* photos (with or without a caption). Commands are
    # excluded and handled above. A photo's caption lives in message.caption, so
    # it is delivered here, not by the TEXT filter.
    application.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_message))
    # Log any unhandled update error instead of crashing.
    application.add_error_handler(on_error)
    return application
