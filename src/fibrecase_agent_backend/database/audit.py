"""The concrete, repository-backed tool auditor (phase 3).

This is the *only* module that knows how audit events reach the database. It
wraps :meth:`ConversationRepository.add_tool_audit_event` and — at the
persistence boundary — hashes the raw scope (via :func:`..memory.hash_scope`)
so the database stores only the short, irreversible ``scope_hash``. The tool
loop (:mod:`..agent.tool_loop`) depends only on the :class:`ToolAuditor`
protocol from :mod:`..tools.audit`; it never imports this module or the ORM.

The auditor never raises: a write failure is reported via the ``False`` return
(the loop then fails closed) and logged with safe fields only.
"""

from __future__ import annotations

import logging

from ..memory import hash_scope
from ..tools.audit import ToolAuditEvent
from .repository import ConversationRepository

logger = logging.getLogger("database")


class RepositoryToolAuditor:
    """A :class:`ToolAuditor` backed by the :class:`ConversationRepository`.

    ``record_pre`` is used for the *pre-execution* gate (``requested`` /
    decision events): it returns ``False`` when the row could not be written so
    the loop does **not** execute an allow/ask tool. ``record`` is used for
    terminal / informational events: a failure here is logged but must not
    trigger a re-execution.
    """

    def __init__(self, repository: ConversationRepository) -> None:
        self._repo = repository

    def _event_dict(self, event: ToolAuditEvent) -> dict[str, object]:
        return {
            "scope_hash": hash_scope(event.scope),
            "tool_name": event.tool_name,
            "event_type": event.event_type,
            "code": event.code,
            "conversation_id": event.conversation_id,
            "tool_call_id": event.tool_call_id,
            "iteration": event.iteration,
            "latency_ms": event.latency_ms,
        }

    async def record_pre(self, event: ToolAuditEvent) -> bool:
        ok = await self._repo.add_tool_audit_event(self._event_dict(event))
        if not ok:
            logger.error(
                "pre-execution audit unavailable; tool will not run",
                extra={"scope_hash": hash_scope(event.scope), "tool": event.tool_name, "event": event.event_type},
            )
        return ok

    async def record(self, event: ToolAuditEvent) -> bool:
        ok = await self._repo.add_tool_audit_event(self._event_dict(event))
        if not ok:
            # A terminal write failure is logged (safely) but must never cause
            # the tool to be re-executed — the loop treats this as a no-op.
            logger.error(
                "terminal audit write failed (tool result unaffected)",
                extra={"scope_hash": hash_scope(event.scope), "tool": event.tool_name, "event": event.event_type},
            )
        return ok
