"""In-memory QQ approval broker (phase 10 — Tool Security, QQ channel).

The QQ transport-side implementation of the channel-agnostic
:class:`~..tools.approval.ToolApprovalProvider` contract. It is the QQ mirror of
:mod:`..telegram.approval` — the same security posture, delivered over QQ's
button-interaction API instead of Telegram inline keyboards:

* presents an approval card (Markdown message + an inline ``keyboard`` of
  Approve / Deny **callback buttons**, ``action.type == 1``) to the QQ user who
  is running the turn;
* resolves the one-time decision from the ``INTERACTION_CREATE`` event that a
  button click raises (handled by :meth:`handle_interaction`, wired into the
  ``botpy`` client by :mod:`.bot`), which **acks within the 3-second window**
  via ``PUT /interactions/{id}`` (``client.api.on_interaction_result``) so the
  client stops spinning;
* cancels pending approvals on shutdown.

This module is the **only** place in the QQ approval path that knows about QQ's
interaction primitives. The tool loop (:mod:`..agent.tool_loop`) never imports
it — it depends only on the channel-agnostic :mod:`..tools.approval` contract.
The composition root (:mod:`..main`) injects this broker (wrapped in a
scope-routing provider) into the shared :class:`AgentService`.

Security properties, matching the Telegram broker:

* **Principal-bound.** A QQ C2C chat is one-to-one, so the clicker's
  ``user_openid`` *is* both the principal and the chat. A pending request is
  resolved only by the *same* openid that the turn ran under (compared by an
  irreversible :func:`~..memory.hash_scope` fingerprint, so the raw openid is
  never held on the pending record) — any other openid gets a safe
  "no longer valid" result and can never approve.
* **One-time.** A request is consumed on the first valid decision; a repeat
  click, an unknown id, a stale button, or an expired request all yield a safe
  expired result and never execute.
* **Bounded wait, no busy-poll.** :meth:`request_approval` awaits an
  ``asyncio.Future`` under ``asyncio.wait_for`` — it never blocks the event
  loop and never polls.
* **Fail-closed.** No bound client, an unresolvable openid, or a card-send
  failure all resolve to ``DENIED`` — a tool that can't be presented for
  approval is not executed.
* **Secret-free.** The card shows only a fixed title, the tool name, the tool's
  purpose summary, and (when the call has arguments, already schema-validated
  by the loop) an ``Action:``/``Arguments:`` block. Nothing sensitive is ever
  logged — :meth:`handle_interaction` and :meth:`request_approval` log only the
  scope hash, the tool name, and the decision (never the raw openid).

The target openid is derived from ``request.scope`` (``qq:<openid>``) — the same
opaque principal the tool loop threads through — so no per-turn context or
repository lookup is needed to route the card to the right user.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from ..memory import hash_scope
from ..tools.approval import ApprovalDecision, ApprovalRequest

logger = logging.getLogger("qq.approval")

# QQ C2C message type for a Markdown message (0 = plain text, 2 = Markdown).
# The approval card is Markdown so the ``Action:``/``Arguments:`` block renders
# in a highlighted code fence. Kept local so this module has no import edge on
# :mod:`.bot` (a channel's approval layer stays decoupled from its transport).
QQ_MSG_TYPE_MARKDOWN = 2

# Button-callback data format: "<version>:<request_id>:<decision>". Only an
# opaque version tag, the random request id, and approve/deny — nothing
# sensitive. Mirrors the Telegram broker's callback-data format.
_CALLBACK_VERSION = "v1"
_DECISION_APPROVE = "a"
_DECISION_DENY = "d"

# QQ button-interaction (INTERACTION_CREATE) type for an inline-keyboard button
# click — the only interaction type this broker resolves. Other interaction
# types (feedback, clear-session, …) are ignored.
_INTERACTION_TYPE_BUTTON = 11

# ``PUT /interactions/{id}``` ack codes (the client uses these to stop the
# button spinner / show feedback). 0 = success (a valid, consumed click); 1 =
# operation failed (unknown / stale / foreign / malformed / already consumed).
_ACK_SUCCESS = 0
_ACK_FAILED = 1

# The memory-scope prefix the QQ channel uses for its turns (``qq:<openid>``).
# It names the channel in the channel-agnostic scope string the tool loop threads
# through; both :meth:`QQApprovalBroker.request_approval` (to recover the target
# openid) and :class:`QQScopedApprovalRouter` (to decide whether a request is a
# QQ one) key off this single constant.
QQ_SCOPE_PREFIX = "qq:"

# Safe, fixed UI strings (never echo arguments, scope, or secrets).
_APPROVAL_TITLE = "### 🔐 需要批准一个工具调用"
_APPROVAL_HINT = "_此批准为一次性，且即将过期。_"
# Button labels — the emoji makes each action legible at a glance. ``style``:
# 1 = blue outline (approve), 2 = white-on-dark (deny); ``visited_label`` is the
# text shown once the button is clicked (client-side), so a live Approve/Deny
# pair visibly collapses to "已批准/已拒绝" on press.
_APPROVAL_APPROVE_LABEL = "✅ 批准"
_APPROVAL_DENY_LABEL = "❌ 拒绝"
_APPROVAL_APPROVE_VISITED = "已批准"
_APPROVAL_DENY_VISITED = "已拒绝"
# The card's argument block is bounded so the whole prompt stays under QQ's
# per-message limit — an over-limit send would raise and fail the approval
# *closed* (the user could never approve it).
_ARGUMENTS_MAX_CHARS = 3500


def _code_block(content: str, language: str = "") -> str:
    """Wrap ``content`` in a fenced Markdown code block.

    ``language`` is a tool-declared fixed hint (e.g. ``bash`` / ``diff`` /
    ``json``) — never argument content — used only as the fence's language tag
    so the client can highlight the block. A literal `` ``` `` inside the
    content is replaced (with ``'''``) so it cannot prematurely close the fence.
    """
    body = content.replace("```", "'''")
    return f"```{language}\n{body}\n```"


def _bound(text: str) -> str:
    """Truncate ``text`` to :data:`_ARGUMENTS_MAX_CHARS` (with a fixed marker)."""
    if len(text) <= _ARGUMENTS_MAX_CHARS:
        return text
    return text[:_ARGUMENTS_MAX_CHARS] + "\n\n[已截断]"


def _arguments_block(arguments: dict[str, Any]) -> str | None:
    """The card's ``Arguments:`` section as QQ Markdown, or ``None`` when the
    call has no arguments. The value is the already schema-validated arguments
    pretty-printed into a ``json`` code fence, bounded so the card never
    overflows the message limit. ``ensure_ascii=False`` keeps CJK values
    readable; ``default=str`` is a backstop for any non-JSON value."""
    if not arguments:
        return None
    pretty = json.dumps(arguments, indent=2, ensure_ascii=False, default=str)
    return "**参数：**\n" + _code_block(_bound(pretty), "json")


def _card_text(request: ApprovalRequest) -> str:
    """The full QQ-Markdown approval card body.

    Fixed title, tool name, the "What it does" purpose summary, the argument
    section (the tool's friendly ``detail`` under an ``Action:`` label when
    supplied, else the generic pretty-JSON ``Arguments:`` block; omitted for an
    argument-free call), then the one-time/expiry hint. The tool name and
    summary are tool-authored (not user input) and are inserted as-is.
    """
    lines = [
        _APPROVAL_TITLE,
        "",
        f"**工具：** {request.tool_name}",
        f"**用途：** {request.summary}",
    ]
    if request.detail:
        lines.append("**操作：**\n" + _code_block(_bound(request.detail), request.language))
    else:
        block = _arguments_block(request.arguments)
        if block is not None:
            lines.append(block)
    lines += ["", _APPROVAL_HINT]
    return "\n".join(lines)


def _approval_keyboard(request_id: str) -> dict:
    """The ``keyboard`` payload for the approval card: one row of two callback
    buttons (``action.type == 1``), each carrying the opaque
    ``<version>:<request_id>:<decision>`` in its ``data`` field. ``permission``
    is ``type 2`` (everyone) — the one-to-one C2C binding (only the turn's own
    openid resolves it) is enforced in :meth:`_resolve`, not by the button.
    """
    return {
        "content": {
            "rows": [
                {
                    "buttons": [
                        {
                            "id": "allow",
                            "render_data": {
                                "label": _APPROVAL_APPROVE_LABEL,
                                "visited_label": _APPROVAL_APPROVE_VISITED,
                                "style": 1,
                            },
                            "action": {
                                "type": 1,
                                "permission": {"type": 2, "specify_role_ids": [], "specify_user_ids": []},
                                "data": f"{_CALLBACK_VERSION}:{request_id}:{_DECISION_APPROVE}",
                            },
                        },
                        {
                            "id": "deny",
                            "render_data": {
                                "label": _APPROVAL_DENY_LABEL,
                                "visited_label": _APPROVAL_DENY_VISITED,
                                "style": 2,
                            },
                            "action": {
                                "type": 1,
                                "permission": {"type": 2, "specify_role_ids": [], "specify_user_ids": []},
                                "data": f"{_CALLBACK_VERSION}:{request_id}:{_DECISION_DENY}",
                            },
                        },
                    ]
                }
            ]
        }
    }


class _Pending:
    """Internal, in-memory state for one outstanding QQ approval."""

    __slots__ = ("openid", "principal_hash", "tool_name", "summary", "expires_at", "future")

    def __init__(self, openid: str, principal_hash: str, tool_name: str,
                 summary: str, expires_at: datetime, future: asyncio.Future) -> None:
        self.openid = openid
        self.principal_hash = principal_hash
        self.tool_name = tool_name
        self.summary = summary
        self.expires_at = expires_at
        self.future = future


class QQApprovalBroker:
    """App-lifetime, in-memory :class:`ToolApprovalProvider` for QQ."""

    def __init__(self) -> None:
        self._client = None  # bound in build_qq_client (the botpy Client)
        self._pending: dict[str, _Pending] = {}

    # -- lifecycle / wiring ------------------------------------------------
    def bind_client(self, client) -> None:
        """Attach the ``botpy`` client (source of ``client.api``) once built.

        Before this the broker cannot deliver a card and every approval fails
        closed (DENIED) — so a turn that requests approval before the client is
        bound simply cannot run an ``ask`` tool.
        """
        self._client = client

    async def shutdown(self) -> None:
        """Cancel all pending approvals so any waiting caller resolves (expired)."""
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_result(ApprovalDecision.EXPIRED)
        self._pending.clear()

    # -- the ToolApprovalProvider contract ---------------------------------
    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """Present an approval card to the turn's QQ user and await the decision.

        Recovers the target openid from ``request.scope`` (``qq:<openid>``),
        sends the card with Approve/Deny buttons, and awaits the one-time
        decision (or expiry). Never raises: any setup failure resolves to
        ``DENIED`` (fail closed). A cancellation of the waiting turn (e.g. a
        ``/stop``) propagates after the pending entry is cleaned up.
        """
        if self._client is None:
            logger.error("qq approval requested but no client bound; denying")
            return ApprovalDecision.DENIED

        openid = request.scope.removeprefix(QQ_SCOPE_PREFIX)
        if not openid:
            logger.error("qq approval with unresolvable openid; denying")
            return ApprovalDecision.DENIED
        principal_hash = hash_scope(request.scope)

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request.request_id] = _Pending(
            openid=openid,
            principal_hash=principal_hash,
            tool_name=request.tool_name,
            summary=request.summary,
            expires_at=request.expires_at,
            future=future,
        )

        # Present the card. A send failure means the user never sees it —
        # clean up and fail closed.
        try:
            await self._send_approval_message(openid, request)
        except Exception:
            self._pending.pop(request.request_id, None)
            if not future.done():
                future.set_result(ApprovalDecision.DENIED)
            logger.error("qq approval: could not send card; denying", exc_info=True)
            return ApprovalDecision.DENIED

        timeout = max(0.0, (request.expires_at - datetime.now(timezone.utc)).total_seconds())
        try:
            decision = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            decision = ApprovalDecision.EXPIRED
        finally:
            # The request is over (decided, expired, or the turn cancelled): it
            # is consumed and can never be resolved again. Popping here also
            # covers a CancelledError raised out of ``wait_for`` (a /stop while
            # awaiting), which then propagates after this.
            self._pending.pop(request.request_id, None)

        logger.info(
            "qq tool approval resolved",
            extra={"scope_hash": principal_hash, "tool": request.tool_name, "decision": decision.value},
        )
        return decision

    # -- the interaction side ----------------------------------------------
    async def handle_interaction(self, interaction) -> None:
        """Resolve (or safely reject) a button click, then ack it.

        ``interaction`` is a ``botpy`` ``Interaction``: ``interaction.id`` is the
        ``interaction_id`` for the ack, ``interaction.user_openid`` the clicker,
        and ``interaction.data`` its ``{type, resolved:{button_id, button_data,
        message_id}}``. Only message-button callbacks (``type == 11``) are ours;
        any other interaction type is ignored (the QQ platform does not require
        an ack for it). The resolve is synchronous and completes well inside the
        3-second ack window; the ack itself is a single network call. The ack
        failure is logged by class only and never raised — a failed ack leaves
        the client spinning briefly but does not change the decision.
        """
        data = getattr(interaction, "data", None)
        if getattr(data, "type", None) != _INTERACTION_TYPE_BUTTON:
            return  # not a button click — nothing to resolve, no ack needed

        resolved = getattr(data, "resolved", None)
        button_data = getattr(resolved, "button_data", None) or ""
        clicker_openid = getattr(interaction, "user_openid", None)
        principal_hash = hash_scope(f"{QQ_SCOPE_PREFIX}{clicker_openid}") if clicker_openid else None

        decision, ack_code = self._resolve(button_data, principal_hash)
        if self._client is not None:
            try:
                await self._client.api.on_interaction_result(interaction.id, ack_code)
            except Exception:
                logger.warning("qq approval: could not ack interaction", exc_info=True)

    def _resolve(self, button_data: str, principal_hash: str | None):
        """Bind-check and consume a button click.

        Returns a ``(decision, ack_code)`` pair. ``APPROVED`` / ``DENIED`` with
        ``ack_code 0`` only when the request exists, is not consumed, is from
        the exact bound openid, is not expired, and pressed a valid button.
        Any *found* request that fails those checks is **voided** (its wait
        resolved as ``EXPIRED`` and removed) and reported as ``EXPIRED`` with
        ``ack_code 1`` — a foreign click, a stale button, a repeat click, or a
        lapsed deadline all void it so the waiting caller unblocks immediately
        and the tool is never executed. An unknown / already-consumed id also
        yields the safe ``EXPIRED`` / ``ack_code 1`` with nothing to unblock.
        """
        request_id = request_id_from(button_data)
        pending = self._pending.get(request_id)
        if pending is None:
            return ApprovalDecision.EXPIRED, _ACK_FAILED  # unknown / stale / consumed

        valid = (
            principal_hash is not None
            and principal_hash == pending.principal_hash
            and datetime.now(timezone.utc) < pending.expires_at
            and decision_from(button_data) is not None
        )
        if not valid:
            self._void(request_id, pending)
            return ApprovalDecision.EXPIRED, _ACK_FAILED

        requested = decision_from(button_data)
        # One-time consumption: set the future (if still waiting) and remove.
        if not pending.future.done():
            pending.future.set_result(requested)
        self._pending.pop(request_id, None)
        return requested, _ACK_SUCCESS

    def _void(self, request_id: str, pending: "_Pending") -> None:
        """Resolve a found-but-invalid request as ``EXPIRED`` and drop it.

        Unblocks the waiting caller immediately (never leaving it to the full
        timeout) and removes the one-time request so it cannot be re-used.
        """
        if not pending.future.done():
            pending.future.set_result(ApprovalDecision.EXPIRED)
        self._pending.pop(request_id, None)

    # -- helpers ------------------------------------------------------------
    async def _send_approval_message(self, openid: str, request: ApprovalRequest) -> None:
        """Send the fixed, secret-free approval card with Approve/Deny buttons.

        A Markdown C2C message carrying the inline ``keyboard``. Sent as an
        *active* message (no ``msg_id`` / ``msg_seq``) so it never collides with
        the turn's own reply chunks (which reuse ``msg_id`` + a ``msg_seq``
        sequence) and is not subject to the passive-reply 5-minute window — at
        the personal-bot scale of one approval per ``ask`` tool call the active
        quota is ample. A send failure raises; the caller fails closed.
        """
        await self._client.api.post_c2c_message(
            openid=openid,
            msg_type=QQ_MSG_TYPE_MARKDOWN,
            markdown={"content": _card_text(request)},
            keyboard=_approval_keyboard(request.request_id),
        )


class QQScopedApprovalRouter:
    """A channel- :class:`ToolApprovalProvider` that routes by the scope prefix.

    The single ``approval_provider`` the composition root injects into the one
    shared :class:`AgentService`. :meth:`request_approval` forwards the request
    to the QQ broker when ``request.scope`` starts with :data:`QQ_SCOPE_PREFIX`
    (a QQ turn — the only turns that run with a ``qq:…`` scope), and to the
    Telegram broker otherwise (every Telegram turn and scheduled run, which use
    a ``telegram:…`` scope). :meth:`shutdown` drains both.

    The Telegram path is delegated verbatim, so its behaviour (delivery-chat
    resolution, in-place card finalisation, callback binding) is unchanged —
    this router only *adds* the QQ branch. It is deliberately kept here (a
    channel-agnostic, botpy-free module) rather than in :mod:`..main` so its
    routing is unit-testable without the composition root.
    """

    def __init__(self, telegram_broker, qq_broker) -> None:
        self._telegram = telegram_broker
        self._qq = qq_broker

    def _provider(self, request: ApprovalRequest):
        if request.scope.startswith(QQ_SCOPE_PREFIX):
            return self._qq
        return self._telegram

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return await self._provider(request).request_approval(request)

    async def shutdown(self) -> None:
        # Drain both. Each ``shutdown`` never raises; order is immaterial (they
        # hold independent pending sets).
        if self._telegram is not None:
            await self._telegram.shutdown()
        if self._qq is not None:
            await self._qq.shutdown()


# ---------------------------------------------------------------------------
# button-data parsing (safe: version + request id + decision only)
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
