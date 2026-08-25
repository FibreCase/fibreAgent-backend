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
  loop, never polls, and performs no I/O beyond the one transport send.
* **Secret-free UI.** The message shows only a fixed title, the tool name, the
  tool's safe ``summary`` (which by default withholds the arguments), and an
  expiry hint. The callback data carries only a version tag, the opaque request
  id, and the decision — never args, scope, chat id, a secret, or the tool name.

Pending approvals are **in-memory only**: a process restart drops them, so an old
button from a previous run is indistinguishable from an unknown id.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

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
_APPROVAL_EXPIRED_TEXT = "This approval has expired or is no longer valid."
_APPROVAL_UNKNOWN_TEXT = "This approval is no longer valid."
_APPROVAL_APPROVED_TEXT = "Approved."
_APPROVAL_DENIED_TEXT = "Denied."


def _html_escape(text: str) -> str:
    """Escape ``& < >`` so interpolated values can't form a Telegram-HTML tag.

    Same rule as :func:`telegram.markdown._esc`; ``&`` first so the ampersands
    in ``&lt;``/``&gt;`` are not re-escaped.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
        """Present an approval prompt in the original chat and await the decision.

        Resolves the transport chat from the conversation id, sends the prompt
        with Approve/Deny buttons, and awaits the one-time decision (or expiry).
        Never raises: any setup failure resolves to ``DENIED`` (fail closed — a
        tool that can't be presented for approval is not executed).
        """
        if self._application is None:
            logger.error("approval requested but no application bound; denying")
            return ApprovalDecision.DENIED

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
            await self._send_approval_message(chat_id, request.request_id, request)
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
        # "no longer valid" text as a real expiry — no existence leak.
        reply = self._callback_reply(decision)
        try:
            await query.answer(reply)
        except Exception:
            logger.warning("approval: could not answer callback", exc_info=True)
        if decision is ApprovalDecision.APPROVED:
            await self._notify(chat_id, request_id_from(data), _APPROVAL_APPROVED_TEXT)
        elif decision is ApprovalDecision.DENIED:
            await self._notify(chat_id, request_id_from(data), _APPROVAL_DENIED_TEXT)
        else:
            await self._notify(chat_id, request_id_from(data), _APPROVAL_UNKNOWN_TEXT)

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
    async def _send_approval_message(self, chat_id: int, request_id: str, request: ApprovalRequest) -> None:
        """Send the fixed, secret-free approval prompt with Approve/Deny buttons."""
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data=f"{_CALLBACK_VERSION}:{request_id}:{_DECISION_APPROVE}"),
                    InlineKeyboardButton("Deny", callback_data=f"{_CALLBACK_VERSION}:{request_id}:{_DECISION_DENY}"),
                ]
            ]
        )
        # This message is sent with parse_mode=HTML, so it must be *Telegram HTML*
        # (<b>/<i>), not Markdown (** / _) — otherwise the markers show literally.
        # The template is fixed and backend-authored; the two interpolated values
        # (tool name, summary) are escaped so a future custom summary cannot break
        # the tag markup or inject HTML.
        text = (
            f"{_APPROVAL_TITLE}\n\n"
            f"<b>Tool:</b> {_html_escape(request.tool_name)}\n"
            f"<b>Summary:</b> {_html_escape(request.summary)}\n\n"
            f"<i>This approval is one-time and will expire shortly.</i>"
        )
        await self._application.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard
        )

    async def _notify(self, chat_id: int, request_id: str, text: str) -> None:
        """Post a short follow-up to the chat (best-effort; never raises)."""
        try:
            await self._application.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.debug("approval: follow-up message failed", exc_info=True)

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
