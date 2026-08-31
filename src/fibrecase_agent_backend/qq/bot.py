"""QQ (multi-channel, phase 10) adapter — the C2C plain-text channel.

This package is the *only* place that knows about the ``botpy`` SDK, mirroring
how :mod:`..telegram` is the only place that knows about python-telegram-bot.
Everything it does with the agent core is channel-agnostic: it normalises an
incoming QQ C2C (private-chat) message into an :class:`~..agent.messages.AgentMessage`,
calls :meth:`..agent.service.AgentService.process_message` (the same
channel-agnostic entry point the Telegram adapter uses), and delivers the reply
back over the QQ websocket.

On top of plain send/receive this channel also offers:

* **Slash-commands** — the same core command surface as the Telegram bot
  (``/new`` / ``/stop`` / ``/help`` / ``/status`` / ``/context`` / memory /
  ``/tool_audit`` / read-only ``/mcp_status`` / ``/infra_status`` /
  ``/schedule_status``), implemented in :mod:`.commands` and dispatched here
  off a leading ``/`` in the message text.
* **Reply-quoting** — a normal answer references the user's message it is
  answering, via QQ's ``message_reference`` (the first chunk only, mirroring
  Telegram's quote-once).
* **A native command panel** — a best-effort, idempotent create-or-update of
  the QQ 指令面板 (``POST /v2/panels``) fired from ``on_ready``.
* **Tool approval** — ``ask``-permission tools are approvable on QQ. A turn that
  reaches one presents an approval card (a Markdown message with Approve / Deny
  **callback buttons**) to the user running the turn; the click raises an
  ``INTERACTION_CREATE`` event, handled here by :func:`build_qq_client`'s
  ``on_interaction_create`` (which acks within the 3-second window and delegates
  the decision to the :class:`~.approval.QQApprovalBroker`).

Scope of this slice is deliberately minimal: **plain-text C2C** (no images, no
streaming draft). The channel is a thin transport over the existing
``AgentService`` → tool loop → LLM core, so tool calling, the tool-security
gate, context budgeting, and long-term memory all work unchanged for QQ — now
including one-time ``ask`` approval.

There is **no allow-list** (unlike the Telegram adapter's
``allowed_user_ids``): this is the owner's *personal* bot and a C2C chat is a
one-to-one private chat, so any ``user_openid`` that can reach the app is
served — access is bounded by the owner holding the app id + a QQ account.

Privacy: like every other layer, this adapter never logs a message *body*, a
tool argument/result, or the raw QQ ``user_openid`` (a user identity). It logs
only the (safe) synthetic conversation id, the QQ *message id*, a text
length, and (for a command) the command *name* — never the openid itself.
"""

from __future__ import annotations

import asyncio
import logging

from ..agent.messages import AgentMessage, TextContent
from ..agent.service import AgentError
from ..database.models import qq_chat_id
from . import commands

logger = logging.getLogger("qq")

# QQ C2C text replies are chunked to this many characters per send. The QQ
# platform caps a single text message well below Telegram's 4096; 4000 leaves
# headroom for multibyte (CJK) content. The full reply is still stored whole by
# the agent service — this split is for *delivery* only (mirrors the Telegram
# adapter's ``split_into_chunks`` / ``CHUNK_SIZE``).
QQ_MAX_MESSAGE_CHARS = 4000

# QQ C2C message type for a Markdown message. 0 is plain text; 2 is Markdown
# (rendered by the client). A Markdown message carries its text in the nested
# ``markdown`` field (``markdown.content``), *not* the top-level ``content`` —
# see the SDK's ``demo_at_reply_markdown.py``. ``MarkdownPayload`` is a
# ``TypedDict`` (a plain dict at runtime), so we build it as ``{"content": …}``
# rather than importing the SDK type — that keeps :class:`QQChannel` (and its
# unit tests) free of ``botpy``.
QQ_MSG_TYPE_MARKDOWN = 2

# QQ C2C message type for a plain-text message (used for short, fixed error
# notices, which are never Markdown).
QQ_MSG_TYPE_TEXT = 0


def _split_for_qq(text: str, limit: int = QQ_MAX_MESSAGE_CHARS) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` characters.

    Same contract as the Telegram adapter's chunker: prefer breaking on existing
    newlines, hard-split an over-long line, and never truncate (the pieces
    concatenate back to ``text``). Kept local to this package rather than
    imported from :mod:`..telegram` so the two channel adapters stay decoupled
    (a channel must not import another channel).
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


class QQChannel:
    """The QQ C2C adapter logic, independent of the ``botpy`` client object.

    Holds the channel-agnostic :class:`AgentService`, the conversation
    repository, the startup :class:`~..config.Config`, and the
    :class:`~..mcp.McpManager` (the last two are what the read-only config
    commands — ``/status`` / ``/infra_status`` / ``/schedule_status`` /
    ``/mcp_status`` — render from). ``on_c2c_message_create`` is the single
    inbound hook the ``botpy`` client dispatches to (see :func:`build_qq_client`).
    Keeping the logic here — and out of the ``botpy.Client`` subclass — means it
    can be unit-tested by constructing a ``QQChannel`` and feeding it a *fake*
    message, without a live websocket or an SDK client.

    There is **no allow-list**: this is the owner's *personal* bot, and a QQ
    C2C chat is a one-to-one private chat, so any ``user_openid`` that can DM (or
    @) the app is served. Access is bounded by the fact that only the owner has
    the bot's app id + a QQ account that can be added to it — the same
    trust posture as every other personal deployment of this backend.

    A QQ C2C chat is identified by a string ``user_openid``. The conversation
    row is keyed by the deterministic synthetic :func:`qq_chat_id` (a reserved
    ``int`` range distinct from the schedule range, so the startup schedule sweep
    can never touch a QQ conversation); the memory scope is the opaque string
    ``qq:<openid>`` (the memory layer is channel-agnostic and needs no change).

    ``_in_flight`` is a small QQ-local registry mapping a conversation id to the
    :class:`asyncio.Task` currently generating a reply for it, so a ``/stop``
    (a separate inbound message, hence a separate task) can cancel it — the
    QQ counterpart of the Telegram layer's in-flight registry.
    """

    def __init__(self, service, repository, config, mcp_manager) -> None:
        self._service = service
        self._repo = repository
        self._config = config
        self._mcp_manager = mcp_manager
        self._in_flight: dict[int, asyncio.Task] = {}

    async def on_c2c_message_create(self, message) -> None:
        """Handle one incoming QQ C2C (private-chat) message.

        ``message`` is a ``botpy`` ``C2CMessage``: ``message.author.user_openid``
        is the sender's openid (string), ``message.content`` its text, and
        ``message.reply(**kwargs)`` posts back to that user. There is no
        authorization gate (see the class docstring) — any sender is served. A
        message with **no** sender identity is malformed and is ignored (logged
        by class only; there is no openid to hash), since a conversation cannot
        be keyed without one.

        A leading ``/`` selects a slash-command (handled and answered, then the
        message is *not* stored as a conversation turn — matching Telegram); any
        other text is a normal agent turn.
        """
        openid = getattr(getattr(message, "author", None), "user_openid", None)
        if not openid:
            logger.warning("qq message with no sender identity; ignored", extra={"message_id": message.id})
            return

        text = (message.content or "").strip()
        if not text:
            return

        # Key the persistent conversation by the deterministic synthetic int for
        # this openid (one row per QQ user, reused across restarts). The same id
        # is stored as both chat id and user id — there is no separate numeric QQ
        # identity to distinguish the two.
        conversation_id = qq_chat_id(openid)
        memory_scope = f"qq:{openid}"

        # --- Slash-command branch -------------------------------------------
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            command = parts[0][1:]  # strip the leading "/"
            args = parts[1].strip() if len(parts) > 1 else ""
            if command in commands.known_command_names():
                # Log the command *name* only — never its arguments (they can
                # carry memory content or other text).
                logger.info(
                    "qq command received",
                    extra={"conversation_id": conversation_id, "command": command, "message_id": message.id},
                )
                reply = await commands.dispatch(
                    command,
                    args,
                    service=self._service,
                    repo=self._repo,
                    config=self._config,
                    mcp_manager=self._mcp_manager,
                    conversation_id=conversation_id,
                    memory_scope=memory_scope,
                    in_flight=self._in_flight,
                )
                if reply:
                    try:
                        # Command acks do not quote; the reply's own ``markdown``
                        # flag picks the delivery type (plain ``msg_type=0`` for a
                        # simple receipt, Markdown ``msg_type=2`` for a display).
                        await self._send_long(message, reply.text, markdown=reply.markdown)
                    except Exception:
                        logger.error(
                            "qq failed to send command reply",
                            extra={"conversation_id": conversation_id},
                            exc_info=True,
                        )
                return
            # An *unknown* ``/…`` is not a command: fall through and treat it as a
            # normal agent turn (matching Telegram, where unmatched ``/…`` text
            # reaches the message handler rather than being swallowed).

        # --- Normal agent turn ----------------------------------------------
        # One row per QQ user, reused across restarts (get_or_create). The same
        # id is stored as both chat id and user id.
        conversation = await self._repo.get_or_create_conversation(conversation_id, conversation_id)
        cid = conversation.id

        logger.info(
            "qq message received",
            extra={"conversation_id": cid, "message_id": message.id, "text_length": len(text)},
        )

        # Register this turn as the conversation's in-flight reply so a ``/stop``
        # (arriving as its own message / task) can cancel it. Removed in the
        # ``finally`` below on completion *and* cancellation, so a finished or
        # stopped turn never lingers as a stale, cancellable handle.
        self._in_flight[conversation_id] = asyncio.current_task()
        try:
            try:
                # No ``delivery_chat_id`` (that carrier is a *real Telegram* chat
                # id, used by scheduled runs to route a card to the owner's bound
                # chat) and no ``on_text_delta`` (QQ has no draft preview).
                # Approval, however, **does** work on QQ: when the shared service
                # reaches an ``ask`` tool, it routes to the QQ broker by this turn's
                # ``qq:<openid>`` scope, which sends a button card to this same
                # openid and resolves from the ``INTERACTION_CREATE`` the click
                # raises (wired in :func:`build_qq_client`). ``allow`` tools run
                # freely; ``ask`` tools are approvable; ``deny`` is rejected.
                agent_message = AgentMessage(contents=[TextContent(text)], source="qq")
                reply = await self._service.process_message(cid, agent_message, memory_scope=memory_scope)
            except AgentError as exc:
                logger.info(
                    "qq llm error surfaced to user",
                    extra={"conversation_id": cid, "category": exc.category},
                )
                await self._safe_reply(message, exc.user_safe)
                return
            except Exception:
                # Log by class only (exc_info carries the type, not the body); never
                # the openid or the message text.
                logger.exception("qq unexpected error handling message", extra={"conversation_id": cid})
                await self._safe_reply(message, "出现了一个意外错误，请稍后重试。")
                return

            if not reply:
                return
            try:
                # The final answer quotes the user's message (``message_reference``)
                # so it visibly references what it is answering. Only the final
                # answer carries the reference — command acks, error notices, and
                # intermediate sends do not.
                await self._send_long(message, reply, quote_id=message.id)
            except Exception:
                logger.error("qq failed to send reply", extra={"conversation_id": cid}, exc_info=True)
        except asyncio.CancelledError:
            # ``/stop`` (or a shutdown) cancelled this turn while it was generating
            # or running a tool. ``process_message``'s per-conversation lock is an
            # async context manager, so it already released on this unwind — the
            # next message can proceed. Send a short notice quoting the user's
            # interrupted message, then re-raise so the task is observed as
            # cancelled. The notice is best-effort — a failed send never changes
            # the cancellation outcome.
            logger.info("turn cancelled by /stop", extra={"conversation_id": cid})
            await self._safe_reply(message, "⛔️ 已停止。", quote_id=message.id)
            raise
        finally:
            self._in_flight.pop(conversation_id, None)

    async def _send_long(self, message, text: str, *, quote_id: int | None = None,
                         markdown: bool = True) -> None:
        """Deliver ``text`` to the user, chunked, as a C2C reply.

        ``markdown`` picks the delivery *type* (shared by the agent's final
        answer and by slash-command replies): ``True`` sends each chunk as a
        ``msg_type=2`` Markdown message so the QQ client renders it — the text
        rides the nested ``markdown`` field (``markdown.content``), **not** the
        top-level ``content``; ``False`` sends each chunk as a ``msg_type=0``
        plain-text message, the text in the top-level ``content`` field (a
        simple one-line command receipt carries no Markdown markers).

        The agent's final answer is always ``markdown=True``: its text goes in
        **verbatim** — there is **no** Markdown→anything conversion or escaping
        pass (unlike the Telegram adapter's ``telegram/markdown.py`` HTML
        conversion). QQ's renderer handles the Markdown the model emits; whatever
        it does not recognise simply shows as-is, and nothing is rewritten or
        dropped on the way out.

        Each chunk is a separate reply to the *same* incoming message
        (``msg_id = message.id``) with an incrementing ``msg_seq`` (1, 2, 3, …). The
        QQ API dedups on ``(msg_id, msg_seq)`` — re-sending the same pair fails — so
        the sequence number must advance per chunk or only the first chunk would land.

        When ``quote_id`` is given, the **first** chunk also carries a
        ``message_reference`` (the visible quote) pointing at that message; later
        chunks do not, so the referenced message is quoted once, not every chunk —
        mirroring the Telegram adapter's quote-once. ``message_reference`` is
        distinct from the ``msg_id`` passive-reply thread: the former is the visible
        quote, the latter is how QQ knows which message we are answering.
        """
        for i, chunk in enumerate(_split_for_qq(text), start=1):
            if not chunk:
                continue
            kwargs: dict = {"msg_seq": i}
            if markdown:
                kwargs["msg_type"] = QQ_MSG_TYPE_MARKDOWN
                kwargs["markdown"] = {"content": chunk}
            else:
                kwargs["msg_type"] = QQ_MSG_TYPE_TEXT
                kwargs["content"] = chunk
            if i == 1 and quote_id is not None:
                kwargs["message_reference"] = {
                    "message_id": str(quote_id),
                    "ignore_get_message_error": True,
                }
            await message.reply(**kwargs)

    async def _safe_reply(self, message, text: str, *, quote_id: int | None = None) -> None:
        """Send one short plain-text notice, never raising on a send failure.

        Used for the fixed, content-free error notices (never Markdown). When
        ``quote_id`` is given the notice quotes that message (the ``/stop``
        "已停止" notice quotes the interrupted message).
        """
        try:
            kwargs: dict = {"msg_type": QQ_MSG_TYPE_TEXT, "content": text}
            if quote_id is not None:
                kwargs["message_reference"] = {
                    "message_id": str(quote_id),
                    "ignore_get_message_error": True,
                }
            await message.reply(**kwargs)
        except Exception:
            # A failed error-notice send is logged by class only; the openid is
            # never part of the log line.
            logger.error("qq failed to send error notice", exc_info=True)


# A fixed, developer-facing marker identifying *our* C2C command panel in the
# QQ panel list. It is not user-facing (``remark`` is invisible to users) and
# makes the create-or-update idempotent across restarts: on startup we look for
# an existing panel whose ``remark`` matches and update it in place rather than
# create a duplicate (the panel API is not idempotent — a blind re-POST would
# stack up to 20 identical panels).
_PANEL_REMARK = "fibrecase-c2c"


def _c2c_panel_payload() -> dict:
    """Build the full body for ``POST /v2/panels`` (create the C2C command panel).

    Pure (no I/O): a c2c-scoped, ``target_type=all`` panel whose items are the
    dispatchable slash-commands (filtered by :func:`.commands.build_c2c_panel_items`
    to the panel's 14-char name cap and 20-item cap), tagged with
    :data:`_PANEL_REMARK` so :func:`_ensure_c2c_panel` can find it on later
    restarts.
    """
    return {
        "scope": "c2c",
        "target_type": "all",
        "panel": {
            "items": commands.build_c2c_panel_items(),
            "remark": _PANEL_REMARK,
        },
    }


async def _ensure_c2c_panel(http, payload: dict) -> None:
    """Best-effort create-or-update the C2C command panel.

    ``http`` is the ``botpy`` client's public ``BotHttp`` (exposing
    ``await http.request(route, **kwargs)`` — the same primitive ``botpy``'s own
    API layer uses). Idempotent across restarts via the ``remark`` marker:

    1. ``GET /v2/panels?scope=c2c`` → scan the records for one whose
       ``panel.remark`` equals :data:`_PANEL_REMARK`.
    2. If found → ``PUT /v2/panels/{panel_id}`` with our items + remark (and the
       record's current ``version``, for optimistic locking).
    3. If not → ``POST /v2/panels`` with the full create payload.

    **Never raises.** A panel hiccup (network, a 4xx like the "operation in
    progress" 40030009, malformed data) is logged by class only and swallowed —
    it must never break startup or message handling. No secret, openid, or
    message body is logged (the only values touched are the command names and the
    opaque ``panel_id``).
    """
    # Lazy import: keep botpy out of the module top level so ``import
    # fibrecase_agent_backend.qq`` (and a Telegram-only deployment) never pays
    # for it — the same rule as :func:`build_qq_client`.
    from botpy.http import Route

    remark = payload["panel"]["remark"]
    items = payload["panel"]["items"]
    try:
        existing = await http.request(Route("GET", "/v2/panels"), params={"scope": "c2c", "limit": 50})
        target_id: str | None = None
        target_version: int | None = None
        for rec in ((existing or {}).get("records") or []):
            if (rec.get("panel") or {}).get("remark") == remark:
                target_id = rec.get("panel_id")
                target_version = rec.get("version")
                break
        if target_id:
            panel: dict = {"items": items, "remark": remark}
            if target_version is not None:
                panel["version"] = target_version
            await http.request(Route("PUT", "/v2/panels/{panel_id}", panel_id=target_id), json={"panel": panel})
            logger.info("qq command panel updated")
        else:
            await http.request(Route("POST", "/v2/panels"), json=payload)
            logger.info("qq command panel created")
    except Exception:
        # Log by class only — the panel API errors are stable, secret-free strings
        # (and any URL in a transport error carries no token, which rides the
        # Authorization header). Never let a panel failure escape.
        logger.exception("qq command panel sync failed")


# ---------------------------------------------------------------------------
# Global custom menu (v2_menu) — the C2C "⋮" menu that appears next to the input
# box for every C2C user. Unlike the C2C *command panel* (``/v2/panels``), this is
# a **global, owner-configured** resource (no per-user remark/marker): a single
# ``PUT /v2/menu`` **replaces** the whole menu and is therefore naturally
# idempotent across restarts (no create-or-update dance, no remark to match).
#
# We add two ``send_message`` items — the only item type that fits a personal
# bot (``link`` needs a https URL and ``switch`` needs a search endpoint we don't
# have):
#   * "对话指令" → "/help": clicking it fills ``/help`` into the input box and,
#     once sent, dispatches the ``/help`` command (the quick-command list).
#   * "工具能力" → "你会使用哪些工具？": sent as a normal agent turn; the model
#     answers from the tools it was given (a plain conversational prompt, not a
#     command, so it runs the full tool loop and the reply is a quoted Markdown
#     answer).
# Both are fixed, secret-free, and content we fully control — the menu carries no
# openid, no command argument, and no message body beyond these two literals.
# ---------------------------------------------------------------------------
_GLOBAL_MENU_ITEMS: tuple[dict, ...] = (
    {"type": "send_message", "name": "对话指令", "send_message": "/help"},
    {"type": "send_message", "name": "工具能力", "send_message": "你会使用哪些工具？"},
)


def _global_menu_payload() -> dict:
    """Build the full body for ``PUT /v2/menu`` (replace the global C2C menu).

    Pure (no I/O, no botpy): a menu whose items are the two fixed
    ``send_message`` entries in :data:`_GLOBAL_MENU_ITEMS`. ``PUT /v2/menu``
    replaces the entire menu with this body, so sending the same payload on
    every startup is idempotent by construction.
    """
    return {"menu": {"items": list(_GLOBAL_MENU_ITEMS)}}


async def _ensure_global_menu(http) -> None:
    """Best-effort replace the global C2C custom menu (``PUT /v2/menu``).

    ``http`` is the ``botpy`` client's public ``BotHttp`` (the same
    ``await http.request(route, **kwargs)`` primitive the panel path uses).
    Sends the fixed two-item menu and logs only a success line (the response is
    a bare ``{"version": N}`` revision counter — not logged).

    **Never raises.** A menu hiccup (network, a 4xx) is logged by class only and
    swallowed — it must never break startup or message handling, exactly like the
    command panel. No secret, openid, or message body is logged.
    """
    # Lazy import: keep botpy out of the module top level (same rule as the panel
    # path and :func:`build_qq_client`).
    from botpy.http import Route

    try:
        await http.request(Route("PUT", "/v2/menu"), json=_global_menu_payload())
        logger.info("qq global menu updated")
    except Exception:
        # Log by class only — never let a menu failure escape.
        logger.exception("qq global menu sync failed")


def build_qq_client(service, repository, config, mcp_manager, approval_broker=None):
    """Construct the ``botpy`` client for the QQ channel.

    This is the *only* function in the codebase that imports ``botpy``. It is
    called by the composition root **only** when the QQ channel is configured
    (an app id plus a client secret in the environment) and **inside**
    ``_post_init`` (on the running PTB event loop) — the SDK's ``Client`` grabs
    the running loop at construction, and its ``start()`` is driven as a task on
    that same loop (see :meth:`AgentBackend._post_init`).

    ``config`` and ``mcp_manager`` are threaded into the :class:`QQChannel` for
    the read-only config commands (``/status`` / ``/infra_status`` /
    ``/schedule_status`` / ``/mcp_status``); they are not secrets and are safe to
    hold.

    ``approval_broker`` (a :class:`~.approval.QQApprovalProvider`) is the
    QQ-side approval transport, injected by the composition root only when
    tools are enabled. It is bound to this client (so it can send the approval
    card) and its :meth:`~.approval.QQApprovalBroker.handle_interaction` is
    wired to the client's ``on_interaction_create`` handler — the entry point
    for message-button clicks.

    Returns a ``botpy.Client`` subclass instance whose ``on_c2c_message_create``
    is wired to a fresh :class:`QQChannel`, whose ``on_ready`` best-effort
    creates-or-updates the native command panel (fired after login, when the
    token is valid), and (when ``approval_broker`` is given) whose
    ``on_interaction_create`` routes button clicks to the broker. ``Intents``
    enables ``public_messages`` (bit 1<<25, the C2C ``c2c_message_create`` event)
    and, when approval is on, ``interaction`` (bit 1<<26, the button-click
    ``INTERACTION_CREATE`` event). There is no allow-list to thread through —
    see :class:`QQChannel`.
    """
    # Lazy import: ``botpy`` (and its ``aiohttp`` dependency) is loaded only when
    # the QQ channel is actually turned on, so a Telegram-only deployment never
    # pays for it — the same "optional provider imports its own SDK" rule the
    # ``infrastructure`` package applies to ``asyncssh``.
    import botpy

    channel = QQChannel(service, repository, config, mcp_manager)
    panel_payload = _c2c_panel_payload()

    class _QQClient(botpy.Client):
        async def on_c2c_message_create(self, message):
            await channel.on_c2c_message_create(message)

        async def on_ready(self):
            # ``on_ready`` is dispatched by botpy (by name, no args) once the
            # websocket login completes, so the token is valid here. Both are
            # best-effort: each swallows its own failure (a panel/menu hiccup
            # must never break startup or message handling).
            await _ensure_c2c_panel(self.http, panel_payload)
            await _ensure_global_menu(self.http)

        async def on_interaction_create(self, interaction):
            # A message-button click (or another interaction). The broker
            # acks it (within the 3-second window) and resolves the pending
            # approval, or ignores it if it is not a button click. Only wired
            # when approval is on (see below).
            if approval_broker is not None:
                await approval_broker.handle_interaction(interaction)

    intents = botpy.Intents(public_messages=True, interaction=approval_broker is not None)
    client = _QQClient(
        intents=intents,
        bot_log=True,
        ext_handlers=False,
    )
    # Bind the client to the broker so it can send approval cards via
    # ``client.api.post_c2c_message``. Done after construction (the client owns
    # its own ``api``) and only when approval is on.
    if approval_broker is not None:
        approval_broker.bind_client(client)
    return client
