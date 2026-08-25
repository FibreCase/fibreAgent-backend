"""Tool execution audit — the channel-/ORM-free contract (phase 3).

The *auditor* is a thin append-only log of what happened to each tool call:
that it was requested, whether it was denied / failed validation / was approved
/ timed out / completed. It is **part of the execution security boundary**, not
an afterthought: for a real ``allow``/``ask`` execution the loop *fails closed*
if it cannot write the pre-execution audit record, so a call that cannot be
audited is never executed.

This module deliberately knows nothing about the database or Telegram. It
defines the record shape (:class:`ToolAuditEvent`), the :class:`ToolAuditor`
protocol the loop depends on, and :class:`NoopAuditor` (the no-op default that
keeps ``run_tool_loop()`` convenient for pure unit tests). The real SQLite
auditor lives in :mod:`..database.audit` (the *only* place that touches the ORM
for auditing); the Telegram adapter surfaces the *user-facing* ``/tool_audit``
view through the repository.

**Never** stored here or below: tool *arguments*, tool *results*, exception
text, memory content, the full scope, a storage path, or any image/base64/secret.
Only the tool name, the stable event type / code, a nullable short call id, the
iteration, and (on a terminal event) the latency — plus the *hashed* scope,
which is a short irreversible fingerprint the auditor computes itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Stable, machine-readable result codes returned to the model as a JSON
# ``{"error": {"code", "message"}}`` when a tool call does not (or did not)
# run as expected. The *message* is a fixed, human/model-readable string that
# never echoes the arguments, the schema error, or the exception.
RESULT_OK = "ok"
RESULT_UNKNOWN_TOOL = "unknown_tool"
RESULT_TOOL_DENIED = "tool_denied"
RESULT_INVALID_ARGUMENTS = "invalid_arguments"
RESULT_APPROVAL_DENIED = "approval_denied"
RESULT_APPROVAL_EXPIRED = "approval_expired"
RESULT_TOOL_TIMEOUT = "tool_timeout"
RESULT_TOOL_EXECUTION_FAILED = "tool_execution_failed"
RESULT_AUDIT_UNAVAILABLE = "audit_unavailable"

# Event types, in the order a single tool call emits them (a subset, depending
# on the path taken). These are what the ``tool_audit_events`` table stores.
EVENT_REQUESTED = "requested"
EVENT_DENIED = "denied"
EVENT_VALIDATION_FAILED = "validation_failed"
EVENT_APPROVAL_REQUESTED = "approval_requested"
EVENT_APPROVAL_APPROVED = "approval_approved"
EVENT_APPROVAL_DENIED = "approval_denied"
EVENT_APPROVAL_EXPIRED = "approval_expired"
EVENT_STARTED = "started"
EVENT_COMPLETED = "completed"
EVENT_TIMED_OUT = "timed_out"
EVENT_FAILED = "failed"
EVENT_AUDIT_UNAVAILABLE = "audit_unavailable"


@dataclass(frozen=True)
class ToolAuditEvent:
    """One append-only, safe audit record for a single tool-call step.

    The event carries the raw, opaque ``scope`` (e.g. ``telegram:<id>``) only as
    *in-memory transport* — the concrete auditor hashes it (via ``hash_scope``)
    at the persistence boundary, so the database stores only the short,
    irreversible ``scope_hash`` and the raw scope never reaches disk. The loop
    knows nothing about hashing or the DB.

    No field here ever carries tool *arguments*, tool *results*, exception
    text, memory content, a storage path, or image/base64/secret. ``code`` is a
    stable machine-readable token (not a message); ``tool_call_id`` is the short
    OpenAI call id (never the arguments).
    """

    scope: str
    tool_name: str
    event_type: str
    code: str | None = None
    conversation_id: int | None = None
    tool_call_id: str | None = None
    iteration: int | None = None
    latency_ms: int | None = None


@runtime_checkable
class ToolAuditor(Protocol):
    """Append-only audit sink the tool loop depends on (channel/ORM-free).

    Implementations must not raise for a *write* they cannot do — instead they
    signal it via the boolean return of :meth:`record_pre` / :meth:`record` so
    the loop can fail closed. A failing implementation still must not leak the
    event's payload anywhere.
    """

    async def record_pre(self, event: ToolAuditEvent) -> bool:
        """Persist a *pre-execution* event and report whether it succeeded.

        Returns ``False`` when the record could not be written (the loop treats
        this as ``audit_unavailable`` and **does not execute** an allow/ask tool).
        Used for the ``requested`` / decision events that gate execution.
        """
        ...

    async def record(self, event: ToolAuditEvent) -> bool:
        """Persist a *terminal / informational* event.

        Returns ``False`` on a failed write; a terminal failure must be logged
        (safely) but must **not** cause the tool to be re-executed.
        """
        ...


class NoopAuditor:
    """A :class:`ToolAuditor` that records nothing and always "succeeds".

    The default for ``run_tool_loop()`` so a bare tool loop stays trivially
    unit-testable without a database. A production deployment (tools enabled)
    must inject a real auditor — see the composition root in :mod:`..main`.
    """

    async def record_pre(self, event: ToolAuditEvent) -> bool:
        return True

    async def record(self, event: ToolAuditEvent) -> bool:
        return True


# ---------------------------------------------------------------------------
# stable, non-echoing model-facing error results
# ---------------------------------------------------------------------------
def _result_json(code: str, message: str) -> str:
    return json.dumps({"error": {"code": code, "message": message}})


# Fixed, short, safe-to-show messages. None echoes arguments, the schema error
# path, or an exception — the model only ever needs to know *which* stable code
# fired so it can apologise or recover.
_MESSAGES = {
    RESULT_UNKNOWN_TOOL: "This tool is not available.",
    RESULT_TOOL_DENIED: "This tool is not permitted to run.",
    RESULT_INVALID_ARGUMENTS: "Tool arguments did not match its schema.",
    RESULT_APPROVAL_DENIED: "The tool call was not approved.",
    RESULT_APPROVAL_EXPIRED: "The tool call approval expired before it was answered.",
    RESULT_TOOL_TIMEOUT: "The tool took too long and was stopped.",
    RESULT_TOOL_EXECUTION_FAILED: "The tool failed to run.",
    RESULT_AUDIT_UNAVAILABLE: "The tool call could not be recorded and was not executed.",
}


def error_result(code: str) -> str:
    """A stable, short JSON tool result for a non-``ok`` outcome (fed to the model)."""
    return _result_json(code, _MESSAGES.get(code, "The tool call could not be completed."))
