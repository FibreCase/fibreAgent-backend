"""The tool-calling loop, gated by the phase-3 execution security boundary.

This is the piece inserted between the Agent service and the LLM client. It
drives an OpenAI-style tool-calling exchange:

    call LLM ─▶ model asks for tools?
                  ├─ no  → return the final text answer
                  └─ yes → append the assistant tool-call message,
                           run each tool **through the security gate**, append
                           ``tool`` result messages, and call the LLM again

…until the model returns a message with **no** tool calls (the final answer),
or the iteration budget is exhausted.

Phase 3 wraps every tool call in a strict, audited sequence:

    parse → registered? → policy → schema validate
          → audit ``requested`` (fail-closed gate)
          → (ask: approval wait → audit decision, fail-closed)
          → asyncio.wait_for(execute, timeout)
          → audit terminal (completed / timed_out / failed) → role=tool result

The loop depends **only** on: an LLM that accepts ``tools=``, a
:class:`~fibrecase_agent_backend.tools.registry.ToolRegistry`, a
:class:`~fibrecase_agent_backend.tools.policy.ToolPolicy`, a
:class:`~fibrecase_agent_backend.tools.audit.ToolAuditor`, and a
:class:`~fibrecase_agent_backend.tools.approval.ToolApprovalProvider`. It knows
nothing about Telegram, the database, or the OpenAI SDK — the Telegram approval
broker and the SQLite auditor are injected by the composition root.

Defaults keep the loop trivially unit-testable: ``policy=None`` means
*allow-all* (the pre-phase-3 behaviour — the three read-only built-ins run
without an approval prompt), and a missing auditor is a :class:`NoopAuditor`.
A production deployment (tools enabled) injects a real policy + auditor +
approval provider — see :mod:`..main`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from ..mcp.auth.principal import active_principal
from ..tools.approval import ApprovalDecision, ApprovalRequest, ToolApprovalProvider
from ..tools.audit import (
    EVENT_APPROVAL_APPROVED,
    EVENT_APPROVAL_DENIED,
    EVENT_APPROVAL_EXPIRED,
    EVENT_APPROVAL_REQUESTED,
    EVENT_AUDIT_UNAVAILABLE,
    EVENT_COMPLETED,
    EVENT_DENIED,
    EVENT_FAILED,
    EVENT_REQUESTED,
    EVENT_STARTED,
    EVENT_TIMED_OUT,
    EVENT_VALIDATION_FAILED,
    RESULT_APPROVAL_DENIED,
    RESULT_APPROVAL_EXPIRED,
    RESULT_AUDIT_UNAVAILABLE,
    RESULT_INVALID_ARGUMENTS,
    RESULT_OK,
    RESULT_TOOL_DENIED,
    RESULT_TOOL_EXECUTION_FAILED,
    RESULT_TOOL_TIMEOUT,
    RESULT_UNKNOWN_TOOL,
    NoopAuditor,
    ToolAuditEvent,
    ToolAuditor,
    error_result,
)
from ..tools.policy import ToolPermission, ToolPolicy
from ..tools.registry import ToolNotFoundError, ToolRegistry
from .context import ChatMessage

logger = logging.getLogger("agent.tools")

# The default no-op auditor used when the caller passes none (pure unit tests).
_NO_AUDITOR = NoopAuditor()


class ToolLoopLimitError(Exception):
    """Raised when the loop hits ``max_iterations`` without a final text answer."""

    def __init__(self, max_iterations: int) -> None:
        super().__init__(f"tool loop reached its limit of {max_iterations} iterations without a final answer")
        self.max_iterations = max_iterations


@runtime_checkable
class ToolCallingLLM(Protocol):
    """Structural type: any object that can complete a call accepting ``tools``."""

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        ...


# ---------------------------------------------------------------------------
# argument parsing (distinguished, never silently ``{}``-coerced)
# ---------------------------------------------------------------------------
# Parse outcomes, so the gate can tell a *well-formed empty* object apart from
# malformed JSON / a non-object JSON value (both are rejected, not coerced).
PARSE_OK = "ok"
PARSE_EMPTY = "empty"
PARSE_MALFORMED = "malformed"
PARSE_NON_OBJECT = "non_object"


def _parse_arguments(raw: Any) -> tuple[str, dict[str, Any] | None]:
    """Parse the model's ``function.arguments`` into ``(status, args)``.

    Returns a *status* plus, for the usable cases, a dict:

    * ``PARSE_OK``         — a valid JSON object (or a dict relay) → its dict.
    * ``PARSE_EMPTY``      — no arguments supplied (``None`` or empty string)
                             → ``None`` (the gate treats it as an empty mapping).
    * ``PARSE_MALFORMED``  — a string that is not valid JSON → ``None``.
    * ``PARSE_NON_OBJECT`` — valid JSON but not an object (array/number/…) → ``None``.

    Unlike the pre-phase-3 behaviour, a malformed or non-object payload is **not**
    silently replaced with ``{}`` — it is reported so the gate rejects it with a
    stable ``invalid_arguments`` result rather than executing a tool with bogus
    (empty) arguments.
    """
    if raw is None:
        return PARSE_EMPTY, None
    if isinstance(raw, dict):
        return PARSE_OK, raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return PARSE_EMPTY, None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("tool arguments were not valid JSON")
            return PARSE_MALFORMED, None
        return (PARSE_OK, parsed) if isinstance(parsed, dict) else (PARSE_NON_OBJECT, None)
    return PARSE_NON_OBJECT, None


def _short_id(value: str | None) -> str | None:
    """A short, safe form of a tool-call id (the DB column is length-bounded)."""
    if not value:
        return None
    return value[:128]


class _NoApprovalProvider:
    """A ``ToolApprovalProvider`` that never approves (defensive fallback).

    Only reached when a policy marks a tool ``ask`` but no provider was wired
    (a misconfiguration in production, or a non-built-in tool in a bare unit
    test). It refuses rather than block, so a call that *requires* approval can
    never execute without one.
    """

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.DENIED

    async def shutdown(self) -> None:  # pragma: no cover - nothing to cancel
        return None


async def run_tool_loop(
    llm: ToolCallingLLM,
    messages: list[ChatMessage],
    registry: ToolRegistry | None = None,
    *,
    max_iterations: int = 5,
    policy: ToolPolicy | None = None,
    approval_provider: ToolApprovalProvider | None = None,
    auditor: ToolAuditor | None = None,
    approval_timeout_seconds: float = 60.0,
    tool_timeout_seconds: float = 30.0,
    conversation_id: int | None = None,
    scope: str | None = None,
) -> Any:
    """Run the model + tools until a final text answer is produced.

    Returns the final :class:`LLMResult` (its ``.text`` is the assistant reply
    to persist and send to the user).

    Behaviour:

    * If ``registry`` is ``None`` or advertises no tools, this is exactly one
      LLM call with no ``tools`` argument — byte-for-byte the phase-one path
      (no policy / approval / audit is involved).
    * Otherwise the model is called at most ``max_iterations`` times. Each
      tool call it requests is run **through the security gate** (see module
      docstring) before its result is fed back. When the model returns a
      message with no tool calls, that is the final answer.
    * If the budget is used up with no final answer, a
      :class:`ToolLoopLimitError` is raised (logged).

    ``policy`` defaults to *allow-all* (``None``) so the loop stays unit-testable;
    ``auditor`` defaults to a :class:`NoopAuditor`; ``approval_provider`` is
    only consulted for ``ask`` tools. ``scope`` is the opaque principal used for
    audit hashing (ignored by the no-op auditor).
    """
    _auditor = auditor if auditor is not None else _NO_AUDITOR
    _approval = approval_provider if approval_provider is not None else _NoApprovalProvider()
    # When no policy is supplied, every tool is allowed (the pre-phase-3, safe
    # built-in behaviour). A supplied policy re-resolves on every call.
    _policy = policy

    if registry is None:
        return await llm.complete(messages)

    advertised = registry.names() if _policy is None else _policy.advertised_names(registry.names())
    tools = registry.to_openai_schema(advertised) or None
    if tools is None:
        # No (allowed) tools to advertise: a single completion, no loop.
        return await llm.complete(messages)

    working = list(messages)
    for iteration in range(1, max_iterations + 1):
        result = await llm.complete(working, tools=tools)

        # A message with no tool calls is the final answer.
        if not getattr(result, "tool_calls", None):
            return result

        # Record the model's tool-call turn so the results map back to it.
        working.append(
            ChatMessage(role="assistant", content=result.text, tool_calls=result.tool_calls)
        )

        # Budget exhausted with no final answer: stop before executing tools
        # whose results we could never feed back.
        if iteration == max_iterations:
            logger.error(
                "tool loop reached its limit without a final answer",
                extra={"max_iterations": max_iterations, "iteration": iteration},
            )
            raise ToolLoopLimitError(max_iterations)

        for tool_call in result.tool_calls:
            function = tool_call.get("function", {}) or {}
            name = function.get("name", "") or ""
            call_id = tool_call.get("id", "") or ""
            raw_args = function.get("arguments")

            logger.info("tool requested: %s", name, extra={"iteration": iteration})
            output = await _execute_gated(
                name=name,
                raw_args=raw_args,
                call_id=call_id,
                iteration=iteration,
                registry=registry,
                policy=_policy,
                approval=_approval,
                auditor=_auditor,
                scope=scope or "",
                conversation_id=conversation_id,
                approval_timeout_seconds=approval_timeout_seconds,
                tool_timeout_seconds=tool_timeout_seconds,
            )
            working.append(ChatMessage(role="tool", content=output, tool_call_id=call_id))

    # Unreachable: the loop either returns a final answer or raises above.
    raise ToolLoopLimitError(max_iterations)  # pragma: no cover


async def _execute_gated(
    *,
    name: str,
    raw_args: Any,
    call_id: str,
    iteration: int,
    registry: ToolRegistry,
    policy: ToolPolicy | None,
    approval: ToolApprovalProvider,
    auditor: ToolAuditor,
    scope: str,
    conversation_id: int | None,
    approval_timeout_seconds: float,
    tool_timeout_seconds: float,
) -> str:
    """Run the strict security gate for one tool call and return its tool result.

    Never raises: every failure path returns a stable, non-echoing JSON result
    (see :mod:`..tools.audit`) so the model can recover. Logs carry only the
    tool name, a stable code, and (for execution failures) the exception class.
    """
    conversation_id = conversation_id

    def _event(event_type: str, **fields: Any) -> ToolAuditEvent:
        return ToolAuditEvent(
            scope=scope,
            tool_name=name,
            event_type=event_type,
            conversation_id=conversation_id,
            tool_call_id=_short_id(call_id),
            iteration=iteration,
            **fields,
        )

    # 1. Parse (distinguish well-formed empty from malformed / non-object).
    status, args = _parse_arguments(raw_args)
    args_for_exec = args if args is not None else {}

    # 2. Registered?
    if name not in registry:
        logger.warning("tool requested but not registered", extra={"tool": name, "code": RESULT_UNKNOWN_TOOL})
        await auditor.record(_event(EVENT_REQUESTED, code=RESULT_UNKNOWN_TOOL))
        await auditor.record(_event(EVENT_DENIED, code=RESULT_UNKNOWN_TOOL))
        return error_result(RESULT_UNKNOWN_TOOL)

    tool = registry.get(name)
    permission = ToolPermission.ALLOW if policy is None else policy.resolve(name)

    # 3. Policy.
    if permission is ToolPermission.DENY:
        logger.info("tool denied by policy", extra={"tool": name, "code": RESULT_TOOL_DENIED})
        await auditor.record(_event(EVENT_REQUESTED, code=RESULT_TOOL_DENIED))
        await auditor.record(_event(EVENT_DENIED, code=RESULT_TOOL_DENIED))
        return error_result(RESULT_TOOL_DENIED)

    # 4. Schema validation (malformed / non-object / schema-mismatch).
    schema_ok = status in (PARSE_OK, PARSE_EMPTY) and registry.validate_arguments(name, args_for_exec)
    if not schema_ok:
        logger.info(
            "tool arguments invalid",
            extra={"tool": name, "code": RESULT_INVALID_ARGUMENTS, "parse": status},
        )
        await auditor.record(_event(EVENT_REQUESTED, code=RESULT_INVALID_ARGUMENTS))
        await auditor.record(_event(EVENT_VALIDATION_FAILED, code=RESULT_INVALID_ARGUMENTS))
        return error_result(RESULT_INVALID_ARGUMENTS)

    # 5. Pre-execution audit gate — fail closed if the "requested" record cannot
    #    be written. This is the *gate* event for both allow and ask.
    if not await auditor.record_pre(_event(EVENT_REQUESTED, code=RESULT_OK)):
        logger.error("pre-execution audit unavailable; not executing", extra={"tool": name, "code": RESULT_AUDIT_UNAVAILABLE})
        return error_result(RESULT_AUDIT_UNAVAILABLE)

    # 6. ask → one-time human approval (allow skips straight to execution).
    if permission is ToolPermission.ASK:
        await auditor.record(_event(EVENT_APPROVAL_REQUESTED, code=RESULT_OK))
        request = ApprovalRequest(
            request_id=secrets.token_urlsafe(16),
            conversation_id=conversation_id or 0,
            scope=scope,
            tool_name=name,
            summary=tool.approval_summary(args_for_exec),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=approval_timeout_seconds),
            arguments=args_for_exec,
            detail=tool.approval_detail(args_for_exec) or "",
        )
        try:
            decision = await approval.request_approval(request)
        except Exception:  # a broken provider must never execute the tool
            logger.error(
                "approval provider error; not executing",
                extra={"tool": name, "exception": "Exception"},
                exc_info=True,
            )
            await auditor.record(_event(EVENT_APPROVAL_DENIED, code=RESULT_APPROVAL_DENIED))
            return error_result(RESULT_APPROVAL_DENIED)

        if decision is ApprovalDecision.APPROVED:
            # The *decision* is part of the fail-closed gate too.
            if not await auditor.record_pre(_event(EVENT_APPROVAL_APPROVED, code=RESULT_OK)):
                logger.error(
                    "approval audit unavailable; not executing",
                    extra={"tool": name, "code": RESULT_AUDIT_UNAVAILABLE},
                )
                return error_result(RESULT_AUDIT_UNAVAILABLE)
        elif decision is ApprovalDecision.DENIED:
            logger.info("tool approval denied", extra={"tool": name, "code": RESULT_APPROVAL_DENIED})
            await auditor.record(_event(EVENT_APPROVAL_DENIED, code=RESULT_APPROVAL_DENIED))
            return error_result(RESULT_APPROVAL_DENIED)
        else:  # EXPIRED
            logger.info("tool approval expired", extra={"tool": name, "code": RESULT_APPROVAL_EXPIRED})
            await auditor.record(_event(EVENT_APPROVAL_EXPIRED, code=RESULT_APPROVAL_EXPIRED))
            return error_result(RESULT_APPROVAL_EXPIRED)

    # 7. Execute with a per-call timeout (cancel on timeout).
    await auditor.record(_event(EVENT_STARTED, code=RESULT_OK))
    started = time.monotonic()
    # Phase 4.x: expose the requesting principal for the duration of this one
    # tool execution. The MCP OAuth transport reads this contextvar to attach
    # the *requesting user's* access token to outgoing requests; it is unset
    # outside a tool call, so no request ever rides a stale or wrong principal.
    # This is identity propagation only — no OAuth logic lives in the loop.
    principal_token = active_principal.set(scope or None)
    try:
        output = await asyncio.wait_for(tool.execute(args_for_exec), timeout=tool_timeout_seconds)
    except asyncio.TimeoutError:
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "tool timed out",
            extra={"tool": name, "code": RESULT_TOOL_TIMEOUT, "latency_ms": latency_ms},
        )
        await auditor.record(_event(EVENT_TIMED_OUT, code=RESULT_TOOL_TIMEOUT, latency_ms=latency_ms))
        return error_result(RESULT_TOOL_TIMEOUT)
    except asyncio.CancelledError:
        # The whole turn was cancelled (e.g. shutdown): propagate so the caller
        # can unwind, but record a terminal event first on a best-effort basis.
        raise
    except Exception as exc:  # noqa: BLE001 - a tool must never kill the loop
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "tool execution failed",
            extra={"tool": name, "code": RESULT_TOOL_EXECUTION_FAILED, "exception": type(exc).__name__},
        )
        await auditor.record(_event(EVENT_FAILED, code=RESULT_TOOL_EXECUTION_FAILED, latency_ms=latency_ms))
        return error_result(RESULT_TOOL_EXECUTION_FAILED)
    finally:
        active_principal.reset(principal_token)

    latency_ms = int((time.monotonic() - started) * 1000)
    logger.info("tool completed: %s latency=%dms", name, latency_ms, extra={"iteration": iteration})
    # Terminal audit is best-effort: a failed write is logged (by the auditor)
    # but must never cause a re-execution or change the (already-produced) result.
    await auditor.record(_event(EVENT_COMPLETED, code=RESULT_OK, latency_ms=latency_ms))
    return output
