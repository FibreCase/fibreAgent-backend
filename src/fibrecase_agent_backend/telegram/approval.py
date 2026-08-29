"""In-memory Telegram approval broker (phase 3 — Tool Security).

The transport-side implementation of the :class:`ToolApprovalProvider`
contract. It is the **only** module in the approval path that knows about
Telegram: it presents Approve/Deny inline buttons in the *original* chat, binds
each pending request to a ``(principal, chat)`` pair, enforces one-time
consumption and expiry, and cancels pending approvals on shutdown.

The tool loop (:mod:`..agent.tool_loop`) never imports this — it depends only on
the channel-agnostic :mod:`..tools.approval` contract. This broker is injected
into the :class:`AgentService` as the approval provider by the composition root
(:mod:`..main`).

Security properties, matching the task spec:

* **Scope + chat bound.** A pending request is resolved only by the *same*
  Telegram user (compared by an irreversible ``hash_scope`` fingerprint, so the
  raw user id is never held) **and** the *same* chat. Any other user — even an
  allow-listed one — gets a safe, existence-revealing **no** answer and can
  never approve.
* **One-time.** A request is consumed on the first valid decision; a repeat
  click, an unknown id, a stale button from a previous process, or an expired
  request all yield a safe "expired/invalid" result and never execute.
* **Bounded wait, no busy-poll.** ``request_approval`` awaits an
  ``asyncio.Future`` under ``asyncio.wait_for`` — it never blocks the event
  loop, never polls, and performs no I/O beyond the prompt send and the single
  in-place edit that finalises the card once the approval is decided or expires.
* **Secret-free UI.** The message shows only a fixed title, the tool name, a
  safe "what it does" summary (the tool's purpose), and an expiry hint. When the
  call carries arguments (already schema-validated by the loop) an "Arguments:"
  block shows them as readable JSON — the card is owner-only and the arguments
  are what makes the approve/deny judgment meaningful; an argument-free call
  omits the block. Arguments are **never** written to logs, the audit table, or
  model-facing error text (those invariants live in the loop / auditor, not
  here). When the approval is decided (or times out) the card is edited in place
  — the (emoji-labelled) Approve/Deny buttons are removed and the hint line is
  replaced by a bold, emoji-tagged status word (*Approved* / *Denied* /
  *Expired*) — so a live Approve/Deny row never lingers. The callback data
  carries only a version tag, the opaque request id, and the decision — never
  args, scope, chat id, a secret, or the tool name.

Pending approvals are **in-memory only**: a process restart drops them, so an old
button from a previous run is indistinguishable from an unknown id.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..memory import hash_scope
from ..tools.approval import ApprovalDecision, ApprovalRequest

logger = logging.getLogger("telegram.approval")

# Callback data format: "<version>:<request_id>:<decision>". Only an opaque
# version tag, the random request id, and approve/deny — nothing sensitive.
_CALLBACK_VERSION = "v1"
_DECISION_APPROVE = "a"
_DECISION_DENY = "d"

# Safe, fixed UI strings (never echo arguments, scope, chat id, or secrets).
# Sent with parse_mode=HTML, so the emphasis uses Telegram's HTML tags (<b>/<i>)
# — NOT Markdown — or the markup would be displayed literally, un-rendered.
_APPROVAL_TITLE = "<b>Approve tool call?</b>"
_APPROVAL_HINT = "<i>This approval is one-time and will expire shortly.</i>"
_APPROVAL_EXPIRED_TEXT = "This approval has expired or is no longer valid."
_APPROVAL_APPROVED_TEXT = "Approved."
_APPROVAL_DENIED_TEXT = "Denied."
# Button labels — the emoji makes each action legible at a glance.
_APPROVAL_APPROVE_LABEL = "✅ Approve"
_APPROVAL_DENY_LABEL = "❌ Deny"
# Card status lines for the *in-place* edit that finalises the card once the
# approval is decided or expires: they replace the "<i>This approval is one-time…</i>"
# hint line (with the buttons removed). A single bold, emoji-tagged word — no
# "Status:" label — fixed and secret-free (tool name is escaped; no args, scope,
# chat id, or secret).
_APPROVAL_APPROVED_STATUS = "<b>✅ Approved.</b>"
_APPROVAL_DENIED_STATUS = "<b>❌ Denied.</b>"
_APPROVAL_EXPIRED_STATUS = "<b>⏰ Expired (no decision in time).</b>"
# The card's Arguments JSON is bounded so the whole prompt stays under Telegram's
# ~4096-char per-message limit — an over-limit send would raise and fail the
# approval *closed* (the owner could never Approve it). The budget leaves room
# for the fixed card chrome (title / tool / purpose / hint) around the JSON.
# It is applied to the *escaped* text (not the raw JSON) because escaping can
# inflate a value full of ``& < >`` several-fold.
_ARGUMENTS_MAX_CHARS = 3700
# Appended (inside the code block) when the Arguments JSON was truncated —
# fixed, secret-free, and tells the owner the full arguments were shown only up
# to the cap (they remain available by re-issuing the call or via exec/edit read).
_ARGUMENTS_TRUNCATED_MARK = "\n\n[Arguments truncated to fit the message]"


def _language_class(language: str) -> str:
    """A safe ``class="language-…"`` attribute for a code block, or ``""``.

    ``language`` is a tool-declared Pygments name (e.g. ``diff`` / ``bash`` /
    ``json``) — never argument content — but it is still sanitised before it is
    interpolated into the card's HTML: only ``[A-Za-z0-9_-]`` survives (lowercased,
    length-capped), so a stray quote/space/tag cannot break the ``class``
    attribute or inject markup. Telegram's HTML supports the language class on
    ``<pre>`` only, with these Pygments identifiers.
    """
    cleaned = "".join(ch for ch in language if ch.isalnum() or ch in "-_")[:24].lower()
    return f' class="language-{cleaned}"' if cleaned else ""


def _html_escape(text: str) -> str:
    """Escape ``& < >`` so interpolated values can't form a Telegram-HTML tag.

    Same rule as :func:`telegram.markdown._esc`; ``&`` first so the ampersands
    in ``&lt;``/``&gt;`` are not re-escaped.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bound_arguments(pretty: str) -> str:
    """Escape ``pretty`` and, if the escaped result exceeds
    :data:`_ARGUMENTS_MAX_CHARS`, truncate it (and append the fixed truncation
    marker). Truncation is on the *escaped* string so a value full of
    ``& < >`` cannot inflate the card past the cap; a cut that lands inside an
    HTML entity is repaired (the partial entity is dropped) so the message is
    never left with a dangling ``&…`` that Telegram would reject.
    """
    escaped = _html_escape(pretty)
    if len(escaped) <= _ARGUMENTS_MAX_CHARS:
        return escaped
    cut = escaped[:_ARGUMENTS_MAX_CHARS]
    # Drop a trailing partial entity (a '&' that does not end a complete
    # ``&amp;``/``&lt;``/``&gt;``) so the message stays well-formed HTML.
    amp = cut.rfind("&")
    if amp != -1 and not cut[amp:].endswith(("amp;", "lt;", "gt;")):
        cut = cut[:amp]
    return cut + _ARGUMENTS_TRUNCATED_MARK


def _arguments_block(arguments: dict[str, Any]) -> str | None:
    """The card's ``Arguments:`` section as Telegram HTML, or ``None`` when the
    call has no arguments (an empty mapping) — in which case the whole section
    is omitted. The value is the **already schema-validated** arguments
    formatted as readable, pretty-printed JSON and wrapped in
    ``<pre class="language-json"><code>`` — the language class labels it JSON so
    the client highlights it instead of guessing — and Telegram's ``<pre>`` is
    the only tag that accepts the language class. It is escaped so no argument
    content can form a tag, and **bounded** (see :func:`_bound_arguments`) so the
    whole card stays under Telegram's single-message limit and is never
    unapprovable. ``ensure_ascii=False`` keeps non-ASCII (e.g. CJK) values
    readable instead of ``\\uXXXX``; ``default=str`` is a backstop for any
    non-JSON value (validated args are JSON-native, so this normally never fires).
    """
    if not arguments:
        return None
    pretty = json.dumps(arguments, indent=2, ensure_ascii=False, default=str)
    return (
        f"<b>Arguments:</b>\n"
        f'<pre{_language_class("json")}><code>{_bound_arguments(pretty)}</code></pre>'
    )


def _card_text(request: ApprovalRequest, footer: str, *, show_arguments: bool = True) -> str:
    """The full Telegram-HTML approval card body, single-sourced for both the
    initial prompt and the in-place finalisation.

    ``footer`` is the trailing line — the "one-time / will expire" hint on the
    prompt, or the bold, emoji-tagged status word once decided/expired. Both are
    fixed, backend-authored HTML and are inserted **verbatim** (never escaped).

    Layout: the fixed title, the tool name, the "What it does" purpose summary,
    the argument section **only when ``show_arguments`` is set**, then the footer.
    The argument section is either the tool's friendly plain-text ``detail``
    (shown under an ``Action:`` label, when the tool supplies one via
    :meth:`Tool.approval_detail`, in a code block labelled with the tool's
    ``language`` hint) or, otherwise, the generic readable-JSON ``Arguments:``
    block (labelled ``language-json``) — and is omitted for an argument-free
    call with no detail. The prompt shows it (the owner needs it to judge the
    call); the in-place finalisation passes ``show_arguments=False`` so the
    resolved card — with its buttons already removed — drops it too. Every
    interpolated *data* value (tool name, summary, the arguments/detail, the
    language hint) is HTML-escaped / sanitised and bounded so nothing can break
    the markup, inject a tag, or overflow Telegram's single-message limit.
    """
    lines = [
        _APPROVAL_TITLE,
        "",
        f"<b>Tool:</b> {_html_escape(request.tool_name)}",
        f"<b>What it does:</b> {_html_escape(request.summary)}",
    ]
    if show_arguments:
        if request.detail:
            # The tool provided a friendly, faithful plain-text view of the
            # arguments (e.g. edit's git-style diff). It is HTML-escaped and
            # bounded exactly like the JSON block, and wrapped in <pre><code> so
            # its newlines (the exact old_string/new_string) are preserved. The
            # <pre> carries a language class (from request.language, sanitised)
            # so the client highlights it correctly instead of guessing; a tool
            # that returns no language leaves the block unlabelled.
            block = (
                f"<b>Action:</b>\n"
                f"<pre{_language_class(request.language)}><code>"
                f"{_bound_arguments(request.detail)}</code></pre>"
            )
        else:
            block = _arguments_block(request.arguments)
        if block is not None:
            lines.append(block)
    lines += ["", footer]
    return "\n".join(lines)


class _Pending:
    """Internal, in-memory state for one outstanding approval."""

    __slots__ = ("request_id", "chat_id", "principal_hash", "tool_name", "summary",
                 "expires_at", "future")

    def __init__(self, chat_id: int, principal_hash: str, tool_name: str,
                 summary: str, expires_at: datetime, future: asyncio.Future) -> None:
        self.chat_id = chat_id
        self.principal_hash = principal_hash
        self.tool_name = tool_name
        self.summary = summary
        self.expires_at = expires_at
        self.future = future


class TelegramApprovalBroker:
    """App-lifetime, in-memory :class:`ToolApprovalProvider` for Telegram."""

    def __init__(self, repository) -> None:
        self._repo = repository
        self._application = None  # bound in build_application (owns the bot)
        self._pending: dict[str, _Pending] = {}

    # -- lifecycle / wiring ------------------------------------------------
    def bind_application(self, application) -> None:
        """Attach the PTB application (source of the bot) once it is built."""
        self._application = application

    def build_callback_handler(self) -> CallbackQueryHandler:
        """The single ``CallbackQueryHandler`` wired into the application."""
        return CallbackQueryHandler(self.handle_callback, pattern=r"^v1:")

    async def shutdown(self) -> None:
        """Cancel all pending approvals so any waiting caller resolves (expired)."""
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_result(ApprovalDecision.EXPIRED)
        self._pending.clear()

    # -- the ToolApprovalProvider contract ---------------------------------
    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """Present an approval prompt in the delivery chat and await the decision.

        Resolves the transport chat to deliver the prompt to, sends the prompt
        with Approve/Deny buttons, and awaits the one-time decision (or expiry).
        Never raises: any setup failure resolves to ``DENIED`` (fail closed — a
        tool that can't be presented for approval is not executed).

        The delivery chat is resolved in one of two ways:

        * **``metadata["delivery_chat_id"]`` is set** (a scheduled run) — the turn
          ran in a synthetic conversation whose row does not correspond to a real
          chat, so the card must go to the owner's *bound* chat. That id is used
          directly (no conversation lookup); the principal binding below is
          unchanged, so only the bound user in that chat can approve.
        * **otherwise** (every interactive caller) — resolve the conversation's
          ``telegram_chat_id`` from the conversation id, exactly as before.
        """
        if self._application is None:
            logger.error("approval requested but no application bound; denying")
            return ApprovalDecision.DENIED

        delivery_chat_id = request.metadata.get("delivery_chat_id")
        if isinstance(delivery_chat_id, int) and not isinstance(delivery_chat_id, bool) and delivery_chat_id > 0:
            # Scheduled run: deliver to the owner's bound (real) chat directly.
            chat_id = delivery_chat_id
        else:
            try:
                conversation = await self._repo.get_conversation_by_id(request.conversation_id)
            except Exception:
                logger.error("approval: could not resolve conversation; denying", exc_info=True)
                return ApprovalDecision.DENIED
            if conversation is None:
                logger.error("approval: unknown conversation; denying")
                return ApprovalDecision.DENIED
            chat_id = conversation.telegram_chat_id
        principal_hash = hash_scope(request.scope)

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request.request_id] = _Pending(
            chat_id=chat_id,
            principal_hash=principal_hash,
            tool_name=request.tool_name,
            summary=request.summary,
            expires_at=request.expires_at,
            future=future,
        )

        # Present the prompt. A send failure means the human never sees it —
        # clean up and fail closed.
        try:
            message_id = await self._send_approval_message(chat_id, request.request_id, request)
        except Exception:
            self._pending.pop(request.request_id, None)
            if not future.done():
                future.set_result(ApprovalDecision.DENIED)
            logger.error("approval: could not send prompt; denying", exc_info=True)
            return ApprovalDecision.DENIED

        timeout = max(0.0, (request.expires_at - datetime.now(timezone.utc)).total_seconds())
        try:
            decision = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            decision = ApprovalDecision.EXPIRED
        finally:
            # The request is over (decided, expired, or cancelled): it is
            # consumed and can never be resolved again.
            self._pending.pop(request.request_id, None)

        # Finalise the card in place: strip the Approve/Deny buttons and replace
        # the "one-time / will expire" hint with the outcome. Every resolution
        # path (valid click, void, shutdown, timeout) funnels through this
        # ``wait_for``, so this is the single point that closes the card. It is
        # best-effort — a failed edit never changes the decision returned above.
        if message_id is not None:
            if decision is ApprovalDecision.APPROVED:
                status = _APPROVAL_APPROVED_STATUS
            elif decision is ApprovalDecision.DENIED:
                status = _APPROVAL_DENIED_STATUS
            else:
                status = _APPROVAL_EXPIRED_STATUS
            await self._finalize_message(chat_id, message_id, request, status)

        logger.info(
            "tool approval resolved",
            extra={"scope_hash": principal_hash, "tool": request.tool_name, "decision": decision.value},
        )
        return decision

    # -- the callback side --------------------------------------------------
    async def handle_callback(self, update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Resolve (or safely reject) an Approve/Deny inline-button callback."""
        query = update.callback_query
        data = query.data or ""
        chat_id = update.effective_chat.id if update.effective_chat else None
        user = update.effective_user
        principal_hash = hash_scope(f"telegram:{user.id}") if user is not None else None

        decision = self._resolve(data, principal_hash, chat_id)
        # Always answer the callback so the button spinner stops, with a safe,
        # non-revealing message. A foreign/stale/expired click gets the same
        # "no longer valid" text as a real expiry — no existence leak. The card
        # itself is finalised in place by the waiting ``request_approval`` (see
        # ``_finalize_message``) — no separate follow-up message is posted.
        reply = self._callback_reply(decision)
        try:
            await query.answer(reply)
        except Exception:
            logger.warning("approval: could not answer callback", exc_info=True)

    def _resolve(self, data: str, principal_hash: str | None, chat_id: int | None):
        """Bind-check and consume a callback; return the decision (or a safe error).

        Returns ``APPROVED`` / ``DENIED`` only when the request exists, has not
        been consumed, and the callback is from the exact bound ``(principal,
        chat)``, is not expired, and pressed a valid button. Any *found* request
        that fails one of those checks is **voided** (its wait resolved as
        ``EXPIRED`` and it is removed) and reported as ``EXPIRED`` — a foreign
        click, a stale button, a repeat click, or a lapsed deadline all void it
        so the waiting caller unblocks immediately and the tool is never executed.
        A callback for an id that was never seen (unknown / already consumed)
        also yields the safe ``EXPIRED`` with nothing to unblock.
        """
        request_id = request_id_from(data)
        pending = self._pending.get(request_id)
        if pending is None:
            return ApprovalDecision.EXPIRED  # unknown / stale / already consumed

        valid = (
            principal_hash is not None
            and principal_hash == pending.principal_hash
            and chat_id is not None
            and chat_id == pending.chat_id
            and datetime.now(timezone.utc) < pending.expires_at
            and decision_from(data) is not None
        )
        if not valid:
            self._void(request_id, pending)
            return ApprovalDecision.EXPIRED

        requested = decision_from(data)
        # One-time consumption: set the future (if still waiting) and remove.
        if not pending.future.done():
            pending.future.set_result(requested)
        self._pending.pop(request_id, None)
        return requested

    def _void(self, request_id: str, pending: "_Pending") -> None:
        """Resolve a found-but-invalid request as ``EXPIRED`` and drop it.

        Unblocks the waiting caller immediately (never leaving it to the full
        timeout) and removes the one-time request so it cannot be re-used.
        """
        if not pending.future.done():
            pending.future.set_result(ApprovalDecision.EXPIRED)
        self._pending.pop(request_id, None)

    # -- helpers ------------------------------------------------------------
    async def _send_approval_message(self, chat_id: int, request_id: str, request: ApprovalRequest) -> int | None:
        """Send the fixed, secret-free approval prompt with Approve/Deny buttons.

        Returns the sent message's ``message_id`` (so the card can be edited in
        place later), or ``None`` if the send result does not carry one (the
        prompt still went out; in-place finalisation is then skipped).
        """
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(_APPROVAL_APPROVE_LABEL, callback_data=f"{_CALLBACK_VERSION}:{request_id}:{_DECISION_APPROVE}"),
                    InlineKeyboardButton(_APPROVAL_DENY_LABEL, callback_data=f"{_CALLBACK_VERSION}:{request_id}:{_DECISION_DENY}"),
                ]
            ]
        )
        # This message is sent with parse_mode=HTML, so it must be *Telegram HTML*
        # (<b>/<i>/<pre>), not Markdown (** / _) — otherwise the markers show
        # literally. The template is fixed and backend-authored; every interpolated
        # value (tool name, summary, arguments) is escaped inside _card_text so a
        # custom summary or argument content cannot break the markup or inject HTML.
        text = _card_text(request, _APPROVAL_HINT)
        message = await self._application.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard
        )
        # ``message`` is a telegram.Message in production; tests use a fake whose
        # ``message_id`` we read defensively so a missing attribute is not fatal.
        return getattr(message, "message_id", None)

    async def _finalize_message(self, chat_id: int, message_id: int, request: ApprovalRequest, status: str) -> None:
        """Edit the approval card in place once the approval is decided/expired.

        Removes the Approve/Deny buttons and replaces the "one-time / will
        expire" hint line with a short, bold, emoji-tagged status word
        (Approved / Denied / Expired), so no live buttons linger after the moment
        they could no longer be pressed. Best-effort: a failed edit never raises
        and never changes the decision.

        ``status`` is already Telegram-HTML (a fixed, bold, emoji-tagged
        constant) — it is inserted verbatim, **not** escaped, so its ``<b>`` tags
        render. The card keeps the title, tool name, and purpose summary
        (escaped inside :func:`_card_text`) but **drops the Arguments section**
        (``show_arguments=False``), since the buttons are already removed and the
        arguments have served their purpose.

        Passing ``reply_markup=InlineKeyboardMarkup([])`` is what removes the
        keyboard: the empty markup serialises to ``{}`` on the wire (the Bot API
        "remove the inline keyboard" signal), whereas ``None`` would be dropped
        by PTB entirely and leave the old buttons in place.
        """
        try:
            # The resolved card keeps only the outcome: the buttons are gone and
            # the arguments have already served their purpose, so drop the
            # Arguments section too (show_arguments=False).
            text = _card_text(request, status, show_arguments=False)
            await self._application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([]),
            )
        except Exception:
            logger.debug("approval: could not finalise approval card", exc_info=True)

    def _callback_reply(self, decision) -> str:
        if decision is ApprovalDecision.APPROVED:
            return _APPROVAL_APPROVED_TEXT
        if decision is ApprovalDecision.DENIED:
            return _APPROVAL_DENIED_TEXT
        return _APPROVAL_EXPIRED_TEXT


# ---------------------------------------------------------------------------
# callback-data parsing (safe: version + request id + decision only)
# ---------------------------------------------------------------------------
def request_id_from(data: str) -> str:
    """The opaque request id out of ``v1:<id>:<d>`` (empty if malformed)."""
    parts = data.split(":")
    return parts[1] if len(parts) >= 3 else ""


def decision_from(data: str):
    """The requested :class:`ApprovalDecision` from ``v1:<id>:<d>``, else ``None``."""
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != _CALLBACK_VERSION:
        return None
    if parts[2] == _DECISION_APPROVE:
        return ApprovalDecision.APPROVED
    if parts[2] == _DECISION_DENY:
        return ApprovalDecision.DENIED
    return None
