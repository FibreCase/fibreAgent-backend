"""The phase-3 execution gate in the tool loop (required #3–#8).

Everything is fake: a scripted LLM, a fake approval provider, an in-memory
recording auditor, and no real tools that touch the network. It proves the strict
order parse → registered? → policy → schema validate → audit → (approval) →
timeout-wrapped execute → audit, and that every failure returns a stable,
non-echoing JSON result instead of executing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fibrecase_agent_backend.agent.context import ChatMessage
from fibrecase_agent_backend.agent.tool_loop import run_tool_loop
from fibrecase_agent_backend.llm.client import LLMResult
from fibrecase_agent_backend.tools import (
    ApprovalDecision,
    ToolPermission,
    ToolRegistry,
    build_default_tools,
    build_policy,
)
from fibrecase_agent_backend.tools.audit import ToolAuditEvent
from fibrecase_agent_backend.tools.base import Tool


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _ScriptedLLM:
    def __init__(self, results: list[Any]):
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, *, tools=None):
        self.calls.append({"tools": tools, "messages": [m.to_dict() for m in messages]})
        if not self.results:
            raise AssertionError("ScriptedLLM exhausted")
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _tc(name: str, arguments: Any, cid: str = "c1") -> dict[str, Any]:
    """A tool-call dict. ``arguments`` is passed *verbatim* (already-JSON-string
    or a raw value) so we can exercise malformed / non-object payloads."""
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _ctx() -> list[ChatMessage]:
    return [ChatMessage("system", "S"), ChatMessage("user", "do it")]


class _RecordingAuditor:
    """Captures audit events and lets us force the pre-write to fail closed."""

    def __init__(self, *, pre_ok: bool = True):
        self.pre_ok = pre_ok
        self.events: list[ToolAuditEvent] = []

    async def record_pre(self, event):
        self.events.append(event)
        return self.pre_ok

    async def record(self, event):
        self.events.append(event)
        return True

    @property
    def types(self) -> list[str]:
        return [e.event_type for e in self.events]

    def codes(self, event_type: str) -> list[str]:
        return [e.code for e in self.events if e.event_type == event_type]


class _FakeApproval:
    def __init__(self, decision: ApprovalDecision = ApprovalDecision.APPROVED):
        self.decision = decision
        self.requests = []
        self.shutdown_called = False

    async def request_approval(self, request):
        self.requests.append(request)
        return self.decision

    async def shutdown(self):
        self.shutdown_called = True


# ---------------------------------------------------------------------------
# required #3 — deny tool is withheld from the schema and never executed
# ---------------------------------------------------------------------------
async def test_deny_tool_is_not_advertised():
    reg = build_default_tools()
    policy = build_policy({"echo": ToolPermission.DENY}, registry=reg)
    advertised = policy.advertised_names(set(reg.names()))
    assert "echo" not in advertised
    assert set(advertised) == {"get_current_time", "system_info"}
    # And the schema the loop would send matches the advertised set.
    schema = reg.to_openai_schema(advertised)
    names = [s["function"]["name"] for s in schema]
    assert "echo" not in names


async def test_deny_tool_requested_is_refused_and_audited():
    reg = build_default_tools()
    policy = build_policy({"echo": ToolPermission.DENY}, registry=reg)
    auditor = _RecordingAuditor()
    approval = _FakeApproval()
    # The model insists on a denied tool anyway.
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("echo", {"message": "x"})]),
        LLMResult(content="refused"),
    ])

    result = await run_tool_loop(
        llm, _ctx(), reg, policy=policy, approval_provider=approval, auditor=auditor
    )
    assert result.text == "refused"
    # The tool was NOT executed (no started/completed) and no approval was asked.
    assert "started" not in auditor.types
    assert "completed" not in auditor.types
    assert approval.requests == []
    # The gate recorded requested + denied with the tool_denied code.
    assert "requested" in auditor.types and "denied" in auditor.types
    assert "tool_denied" in auditor.codes("denied")
    # The model got a stable, non-echoing error result.
    tool_msg = llm.calls[1]["messages"][-1]
    assert json.loads(tool_msg["content"])["error"]["code"] == "tool_denied"


# ---------------------------------------------------------------------------
# required #4 — malformed / wrong-type / extra-property args are rejected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "args",
    [
        "{not json",                       # malformed JSON
        json.dumps(["a", "b"]),            # non-object (array)
        json.dumps("just a string"),       # non-object (string)
        json.dumps(42),                    # non-object (number)
        json.dumps({}),                    # missing required 'message'
        json.dumps({"message": 123}),      # wrong type
        json.dumps({"message": "a", "extra": 1}),  # extra property (additionalProperties:false)
    ],
)
async def test_invalid_arguments_rejected_not_executed(args):
    reg = build_default_tools()
    auditor = _RecordingAuditor()
    approval = _FakeApproval()
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("echo", args)]),
        LLMResult(content="cannot"),
    ])

    result = await run_tool_loop(llm, _ctx(), reg, policy=None, approval_provider=approval, auditor=auditor)
    assert result.text == "cannot"
    # Never executed, never asked for approval.
    assert "started" not in auditor.types
    assert approval.requests == []
    # Recorded as requested + validation_failed with the stable code.
    assert "validation_failed" in auditor.types
    assert "invalid_arguments" in auditor.codes("validation_failed")
    payload = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert payload["error"]["code"] == "invalid_arguments"
    # The message is the fixed, stable string — no jsonschema path, no raw args.
    assert payload["error"]["message"] == "Tool arguments did not match its schema."
    assert "path" not in payload["error"]


# ---------------------------------------------------------------------------
# required #5 — an invalid tool schema is rejected at register() time
# ---------------------------------------------------------------------------
def test_invalid_schema_rejected_at_register():
    class Bad(Tool):
        name = "bad"
        description = "d"
        # "type" must be a string; this is an invalid JSON-Schema.
        parameters = {"type": "object", "properties": {"x": {"type": 5}}}

        async def execute(self, arguments):  # pragma: no cover
            return ""

    with pytest.raises(ValueError):
        ToolRegistry().register(Bad())


# ---------------------------------------------------------------------------
# required #7 — a slow tool is cancelled by wait_for → tool_timeout
# ---------------------------------------------------------------------------
class _SlowTool(Tool):
    name = "slow"
    description = "slow"
    default_permission = ToolPermission.ALLOW
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self):
        self.started = False

    async def execute(self, arguments):
        self.started = True
        import asyncio

        await asyncio.sleep(10)  # will be cancelled by the timeout
        return "never"


async def test_slow_tool_times_out_and_loop_continues():
    import asyncio

    reg = ToolRegistry().register(_SlowTool())
    auditor = _RecordingAuditor()
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("slow", {})]) ,
        LLMResult(content="it timed out, sorry"),
    ])

    result = await run_tool_loop(
        llm, _ctx(), reg, policy=None, auditor=auditor, tool_timeout_seconds=0.05
    )
    # The loop recovered and produced the model's final answer.
    assert result.text == "it timed out, sorry"
    assert len(llm.calls) == 2
    # It was started but timed out (not completed), with a latency recorded.
    assert "started" in auditor.types
    assert "timed_out" in auditor.types
    assert "completed" not in auditor.types
    # The model-facing result carries the stable tool_timeout code.
    payload = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert payload["error"]["code"] == "tool_timeout"


async def test_multiple_calls_in_one_turn_keep_order_under_timeout():
    # A fast allow tool and a slow tool in one turn: order of results preserved,
    # the slow one times out, the loop still finishes.
    reg = build_default_tools().register(_SlowTool())
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[
            _tc("echo", {"message": "A"}, cid="fast"),
            _tc("slow", {}, cid="slow1"),
        ]),
        LLMResult(content="done"),
    ])
    result = await run_tool_loop(llm, _ctx(), reg, policy=None, tool_timeout_seconds=0.05)
    assert result.text == "done"
    second = llm.calls[1]["messages"]
    # Two tool results, in model order.
    assert second[-2]["tool_call_id"] == "fast"
    assert second[-2]["content"] == "A"
    assert second[-1]["tool_call_id"] == "slow1"
    assert json.loads(second[-1]["content"])["error"]["code"] == "tool_timeout"


# ---------------------------------------------------------------------------
# approval lifecycle at the loop level (supports required #9/#10)
# ---------------------------------------------------------------------------
async def test_ask_tool_approved_executes_exactly_once():
    reg = build_default_tools()
    policy = build_policy({"echo": ToolPermission.ASK}, registry=reg)
    auditor = _RecordingAuditor()
    approval = _FakeApproval(ApprovalDecision.APPROVED)
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("echo", {"message": "hi"})]),
        LLMResult(content="approved ok"),
    ])
    result = await run_tool_loop(
        llm, _ctx(), reg, policy=policy, approval_provider=approval, auditor=auditor
    )
    assert result.text == "approved ok"
    # Approval was requested exactly once and then the tool ran to completion.
    assert len(approval.requests) == 1
    assert "started" in auditor.types and "completed" in auditor.types
    assert "approval_requested" in auditor.types and "approval_approved" in auditor.types
    assert llm.calls[1]["messages"][-1]["content"] == "hi"


async def test_ask_tool_denied_not_executed():
    reg = build_default_tools()
    policy = build_policy({"echo": ToolPermission.ASK}, registry=reg)
    auditor = _RecordingAuditor()
    approval = _FakeApproval(ApprovalDecision.DENIED)
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("echo", {"message": "hi"})]),
        LLMResult(content="denied"),
    ])
    result = await run_tool_loop(
        llm, _ctx(), reg, policy=policy, approval_provider=approval, auditor=auditor
    )
    assert result.text == "denied"
    assert "started" not in auditor.types
    assert "approval_denied" in auditor.types
    assert json.loads(llm.calls[1]["messages"][-1]["content"])["error"]["code"] == "approval_denied"


async def test_ask_tool_expired_not_executed():
    reg = build_default_tools()
    policy = build_policy({"echo": ToolPermission.ASK}, registry=reg)
    auditor = _RecordingAuditor()
    approval = _FakeApproval(ApprovalDecision.EXPIRED)
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("echo", {"message": "hi"})]),
        LLMResult(content="expired"),
    ])
    result = await run_tool_loop(
        llm, _ctx(), reg, policy=policy, approval_provider=approval, auditor=auditor
    )
    assert result.text == "expired"
    assert "started" not in auditor.types
    assert "approval_expired" in auditor.types
    assert json.loads(llm.calls[1]["messages"][-1]["content"])["error"]["code"] == "approval_expired"


# ---------------------------------------------------------------------------
# fail-closed: a pre-execution audit write failure means the tool does not run
# ---------------------------------------------------------------------------
async def test_pre_audit_failure_fails_closed():
    reg = build_default_tools()
    auditor = _RecordingAuditor(pre_ok=False)  # the requested-record can't be written
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("echo", {"message": "hi"})]),
        LLMResult(content="no go"),
    ])
    result = await run_tool_loop(llm, _ctx(), reg, policy=None, auditor=auditor)
    assert result.text == "no go"
    # Never started, and the model is told the call could not be audited.
    assert "started" not in auditor.types
    assert json.loads(llm.calls[1]["messages"][-1]["content"])["error"]["code"] == "audit_unavailable"


# ---------------------------------------------------------------------------
# terminal audit write failure must NOT re-execute the tool
# ---------------------------------------------------------------------------
class _TerminalFailingAuditor:
    """Pre-write succeeds; terminal writes all fail. Counts executions via the tool."""

    def __init__(self, tool):
        self.tool = tool
        self.events = []

    async def record_pre(self, event):
        self.events.append(event)
        return True

    async def record(self, event):
        self.events.append(event)
        return False


async def test_terminal_audit_failure_does_not_reexecute():
    class Counting(Tool):
        name = "count"
        description = "d"
        default_permission = ToolPermission.ALLOW
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}

        def __init__(self):
            self.runs = 0

        async def execute(self, arguments):
            self.runs += 1
            return "ran"

    tool = Counting()
    reg = ToolRegistry().register(tool)
    auditor = _TerminalFailingAuditor(tool)
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("count", {})]),
        LLMResult(content="done"),
    ])
    result = await run_tool_loop(llm, _ctx(), reg, policy=None, auditor=auditor)
    assert result.text == "done"
    # Executed exactly once despite the terminal audit write failing.
    assert tool.runs == 1
    assert llm.calls[1]["messages"][-1]["content"] == "ran"


# ---------------------------------------------------------------------------
# required #6 / #19 — logs and results carry no exception text / args
# ---------------------------------------------------------------------------
class _SecretTool(Tool):
    name = "secret"
    description = "d"
    default_permission = ToolPermission.ALLOW
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, arguments):
        raise ValueError("super secret kaboom payload")


async def test_execution_failure_log_and_result_hide_exception_text(caplog):
    import logging

    reg = ToolRegistry().register(_SecretTool())
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("secret", {})]),
        LLMResult(content="it failed"),
    ])
    with caplog.at_level(logging.INFO, logger="agent.tools"):
        result = await run_tool_loop(llm, _ctx(), reg, policy=None)
    # The model got a stable, non-echoing error result.
    assert result.text == "it failed"
    payload = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert payload["error"]["code"] == "tool_execution_failed"
    assert "super secret" not in payload["error"]["message"]
    # The logs carry the exception *class* but never the exception *text*.
    # ``extra`` keys are merged onto the LogRecord, so read them off the record.
    joined = "|".join(
        [r.getMessage() for r in caplog.records]
        + [str(r.__dict__[k]) for r in caplog.records for k in ("tool", "code", "exception") if k in r.__dict__]
    )
    assert "super secret kaboom payload" not in joined
    assert "ValueError" in joined  # the class is fine; the message is not


async def test_unknown_tool_result_is_stable(caplog):
    import logging

    reg = build_default_tools()
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("does_not_exist", {"password": "hunter2"})]),
        LLMResult(content="no such tool"),
    ])
    with caplog.at_level(logging.INFO, logger="agent.tools"):
        result = await run_tool_loop(llm, _ctx(), reg, policy=None)
    assert result.text == "no such tool"
    payload = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert payload["error"]["code"] == "unknown_tool"
    # The (deliberately) bogus argument the model supplied never leaks to logs.
    joined = "".join(str(r.getMessage()) for r in caplog.records)
    assert "hunter2" not in joined


# ---------------------------------------------------------------------------
# required #8 — ENABLE_TOOLS=false stays a single call, no approval/audit
# ---------------------------------------------------------------------------
async def test_tools_disabled_is_single_call_no_gate():
    # With a None registry the loop is exactly one LLM call with tools=None —
    # this is what the service does when ENABLE_TOOLS=false.
    auditor = _RecordingAuditor()
    approval = _FakeApproval()
    llm = _ScriptedLLM([LLMResult(content="just chat")])
    result = await run_tool_loop(llm, _ctx(), None, max_iterations=5, auditor=auditor, approval_provider=approval)
    assert result.text == "just chat"
    assert len(llm.calls) == 1
    assert llm.calls[0]["tools"] is None
    assert auditor.events == []
    assert approval.requests == []
