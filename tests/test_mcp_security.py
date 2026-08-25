"""Phase 4 — an MCP tool passes the *existing* phase-3 security gate.

MCP is a Tool Provider, not a new execution path: a discovered :class:`McpTool`
is an ordinary :class:`Tool` registered into the same registry, so it must
survive every phase-3 gate step in order — policy, JSON-Schema validation
(**before** any network ``call_tool``), fail-closed pre-audit, one-time
approval for ``ask``, the per-tool timeout, and the terminal audit. These tests
drive the real :func:`run_tool_loop` with a fake LLM, a real registry holding an
:class:`McpTool` backed by a fake MCP session, and recording fakes — no network.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from fibrecase_agent_backend.agent.context import ChatMessage
from fibrecase_agent_backend.agent.tool_loop import run_tool_loop
from fibrecase_agent_backend.llm.client import LLMResult
from fibrecase_agent_backend.mcp.wrapper import McpTool
from fibrecase_agent_backend.tools import (
    ApprovalDecision,
    RESULT_APPROVAL_DENIED,
    RESULT_APPROVAL_EXPIRED,
    RESULT_AUDIT_UNAVAILABLE,
    RESULT_INVALID_ARGUMENTS,
    RESULT_OK,
    RESULT_TOOL_DENIED,
    RESULT_TOOL_TIMEOUT,
    ToolPermission,
    ToolPolicy,
    ToolRegistry,
    build_policy,
    error_result,
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _FakeRemoteSession:
    """A fake connected ``ClientSession`` whose ``call_tool`` records + replays."""

    def __init__(self, *, return_value=None, raise_exc=None):
        self._return_value = return_value
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._return_value


def _mcp_tool(session, *, remote="remote_tool", server="alpha", parameters=None, max_chars=1000):
    """An McpTool bound to a fake session (a real phase-3-gate tool)."""
    return McpTool(
        server_name=server,
        remote_name=remote,
        description="remote does a thing",
        parameters=parameters if parameters is not None else {"type": "object", "properties": {}},
        session=session,
        max_result_chars=max_chars,
    )


def _registry_with(*tools) -> ToolRegistry:
    return ToolRegistry().add(*tools)


class _ScriptedLLM:
    """Replays scripted results; records each call's messages + tools."""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[dict] = []

    async def complete(self, messages, *, tools=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.results:
            raise AssertionError("LLM asked for more than scripted")
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ctx():
    return [ChatMessage("system", "S"), ChatMessage("user", "do it")]


def _tc(name, arguments, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": arguments}}


class _RecordingAuditor:
    def __init__(self, pre_ok=True):
        self.events = []
        self._pre_ok = pre_ok

    async def record_pre(self, event):
        self.events.append(event)
        return self._pre_ok

    async def record(self, event):
        self.events.append(event)
        return True

    def types(self):
        return [e.event_type for e in self.events]

    def codes(self, event_type=None):
        return [e.code for e in self.events if event_type is None or e.event_type == event_type]


class _FakeApproval:
    def __init__(self, decision):
        self.decision = decision
        self.requests = []

    async def request_approval(self, request):
        self.requests.append(request)
        return self.decision

    async def shutdown(self):
        return None


def _policy(*, default=ToolPermission.ASK, **overrides):
    return ToolPolicy.from_items(
        {k: ToolPermission(v) for k, v in overrides.items()}, default=default
    )


def _call_tool_args(llm):
    """The arguments the LLM fed to a single tool call in its first turn."""
    tc = llm.calls[0]["messages"]
    return tc


# ===========================================================================
# required #7 — discovered MCP schema appears in the OpenAI schema, local name,
# and defaults to ask
# ===========================================================================
async def test_mcp_tool_appears_in_schema_and_defaults_ask():
    session = _FakeRemoteSession(return_value={"is_error": False, "content": []})
    tool = _mcp_tool(session, remote="get_weather")
    registry = _registry_with(tool)
    assert tool.name == "mcp_alpha__get_weather"
    assert tool.default_permission is ToolPermission.ASK

    llm = _ScriptedLLM([LLMResult(content="final")])
    await run_tool_loop(llm, _ctx(), registry)
    # The advertised schema carries the namespaced local name + remote schema.
    tools = llm.calls[0]["tools"]
    names = [t["function"]["name"] for t in tools]
    assert names == ["mcp_alpha__get_weather"]
    assert tools[0]["function"]["parameters"] == {"type": "object", "properties": {}}
    # The description never includes server instructions — only the fixed prefix.
    assert "Remote tool 'get_weather' from the configured MCP server 'alpha'" in tools[0]["function"]["description"]


# ===========================================================================
# required #8 — same remote name from two servers both callable; override
# affects only the namespaced local name
# ===========================================================================
async def test_two_servers_same_remote_name_no_collision():
    sess_a = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "A"}]})
    sess_b = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "B"}]})
    tool_a = _mcp_tool(sess_a, remote="get_weather", server="alpha")
    tool_b = _mcp_tool(sess_b, remote="get_weather", server="beta")
    registry = _registry_with(tool_a, tool_b)  # no duplicate-name error

    # Both advertised under distinct namespaced names.
    llm = _ScriptedLLM([
        LLMResult(content="", tool_calls=[_tc(tool_a.name, {})]),
        LLMResult(content=""),
    ])
    await run_tool_loop(llm, _ctx(), registry)
    tools = llm.calls[0]["tools"]
    assert [t["function"]["name"] for t in tools] == ["mcp_alpha__get_weather", "mcp_beta__get_weather"]

    # A policy override on one namespaced name does not touch the other.
    policy = _policy(mcp_alpha__get_weather="deny")
    assert policy.resolve("mcp_alpha__get_weather") is ToolPermission.DENY
    assert policy.resolve("mcp_beta__get_weather") is ToolPermission.ASK
    advertised = policy.advertised_names(registry.names())
    assert "mcp_alpha__get_weather" not in advertised
    assert "mcp_beta__get_weather" in advertised


async def test_two_servers_both_callable_in_order():
    sess_a = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "A"}]})
    sess_b = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "B"}]})
    tool_a = _mcp_tool(sess_a, remote="op", server="alpha")
    tool_b = _mcp_tool(sess_b, remote="op", server="beta")
    registry = _registry_with(tool_a, tool_b)
    policy = _policy(default=ToolPermission.ALLOW)

    llm = _ScriptedLLM([
        LLMResult(content="", tool_calls=[_tc(tool_a.name, {}, cid="a"), _tc(tool_b.name, {}, cid="b")]),
        LLMResult(content="done"),
    ])
    await run_tool_loop(llm, _ctx(), registry, policy=policy)
    # Each forwarded to its own server's remote tool.
    assert sess_a.calls == [("op", {})]
    assert sess_b.calls == [("op", {})]


# ===========================================================================
# required #9 — malformed / missing / wrong / extra args rejected BEFORE the
# MCP network call (call_tool never invoked)
# ===========================================================================
_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "args",
    [
        {"city": 123},  # wrong type
        {},  # missing required
        {"city": "x", "extra": 1},  # extra property
        "[1,2]",  # non-object JSON
        "not json",  # malformed JSON
        "42",  # number
    ],
)
async def test_mcp_args_rejected_before_network(args):
    session = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "x"}]})
    tool = _mcp_tool(session, parameters=_SCHEMA)
    registry = _registry_with(tool)
    llm = _ScriptedLLM([LLMResult(content="", tool_calls=[_tc(tool.name, args)]), LLMResult(content="ok")])
    auditor = _RecordingAuditor()
    await run_tool_loop(llm, _ctx(), registry, policy=_policy(default=ToolPermission.ALLOW), auditor=auditor)
    # No network call was made.
    assert session.calls == []
    # The model got the stable invalid_arguments result; the tool was never started.
    assert RESULT_INVALID_ARGUMENTS in auditor.codes("validation_failed")
    assert "started" not in auditor.types()


async def test_mcp_valid_args_do_reach_network():
    session = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "tokyo"}]})
    tool = _mcp_tool(session, parameters=_SCHEMA)
    registry = _registry_with(tool)
    llm = _ScriptedLLM([LLMResult(content="", tool_calls=[_tc(tool.name, json.dumps({"city": "tokyo"}))]), LLMResult(content="ok")])
    await run_tool_loop(llm, _ctx(), registry, policy=_policy(default=ToolPermission.ALLOW))
    assert session.calls == [("remote_tool", {"city": "tokyo"})]


# ===========================================================================
# required #10 — default ask lifecycle
# ===========================================================================
async def test_mcp_ask_approved_executes_exactly_once():
    session = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "hi"}]})
    tool = _mcp_tool(session)
    registry = _registry_with(tool)
    llm = _ScriptedLLM([LLMResult(content="", tool_calls=[_tc(tool.name, {})]), LLMResult(content="ok")])
    approval = _FakeApproval(ApprovalDecision.APPROVED)
    auditor = _RecordingAuditor()
    await run_tool_loop(llm, _ctx(), registry, policy=_policy(), approval_provider=approval, auditor=auditor)
    assert session.calls == [("remote_tool", {})]
    assert approval.requests and approval.requests[0].tool_name == "mcp_alpha__remote_tool"
    assert RESULT_OK in auditor.codes("completed")


@pytest.mark.parametrize("decision", [ApprovalDecision.DENIED, ApprovalDecision.EXPIRED])
async def test_mcp_ask_not_approved_zero_calls(decision):
    session = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "x"}]})
    tool = _mcp_tool(session)
    registry = _registry_with(tool)
    llm = _ScriptedLLM([LLMResult(content="", tool_calls=[_tc(tool.name, {})]), LLMResult(content="ok")])
    auditor = _RecordingAuditor()
    await run_tool_loop(
        llm, _ctx(), registry, policy=_policy(),
        approval_provider=_FakeApproval(decision), auditor=auditor,
    )
    assert session.calls == []
    code = RESULT_APPROVAL_DENIED if decision is ApprovalDecision.DENIED else RESULT_APPROVAL_EXPIRED
    assert code in [e.code for e in auditor.events]


async def test_mcp_ask_pre_audit_unavailable_fails_closed():
    session = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "x"}]})
    tool = _mcp_tool(session)
    registry = _registry_with(tool)
    llm = _ScriptedLLM([LLMResult(content="", tool_calls=[_tc(tool.name, {})]), LLMResult(content="ok")])
    auditor = _RecordingAuditor(pre_ok=False)  # pre-write fails closed
    await run_tool_loop(llm, _ctx(), registry, policy=_policy(), approval_provider=_FakeApproval(ApprovalDecision.APPROVED), auditor=auditor)
    assert session.calls == []
    # The model is told audit_unavailable (the pre-write failed, so no terminal
    # event is recorded — the code surfaces in the tool result fed back).
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.role == "tool"]
    assert RESULT_AUDIT_UNAVAILABLE in json.loads(tool_msgs[0].content)["error"]["code"]


async def test_mcp_ask_timeout_starts_then_times_out():
    # The remote call blocks past the tiny tool timeout.
    started = asyncio.Event()

    class _SlowSession:
        async def call_tool(self, name, arguments=None):
            started.set()
            await asyncio.sleep(5)
            return {"is_error": False, "content": []}

    tool = _mcp_tool(_SlowSession())
    registry = _registry_with(tool)
    llm = _ScriptedLLM([LLMResult(content="", tool_calls=[_tc(tool.name, {})]), LLMResult(content="ok")])
    auditor = _RecordingAuditor()
    await run_tool_loop(
        llm, _ctx(), registry, policy=_policy(default=ToolPermission.ALLOW),
        auditor=auditor, tool_timeout_seconds=0.05,
    )
    # The call *started* (one in-flight call) but was cancelled — no result.
    assert started.is_set()
    assert RESULT_TOOL_TIMEOUT in [e.code for e in auditor.events]


# ===========================================================================
# required #11 — allow override runs directly (still audited + timed), deny
# withheld
# ===========================================================================
async def test_mcp_allow_override_runs_without_approval():
    session = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "hi"}]})
    tool = _mcp_tool(session)
    registry = _registry_with(tool)
    llm = _ScriptedLLM([LLMResult(content="", tool_calls=[_tc(tool.name, {})]), LLMResult(content="ok")])
    approval = _FakeApproval(ApprovalDecision.DENIED)  # would deny if consulted
    auditor = _RecordingAuditor()
    policy = _policy(**{tool.name: "allow"})
    await run_tool_loop(llm, _ctx(), registry, policy=policy, approval_provider=approval, auditor=auditor)
    assert session.calls == [("remote_tool", {})]  # ran despite DENIED default provider
    assert approval.requests == []  # never consulted
    assert RESULT_OK in auditor.codes("completed")


async def test_mcp_deny_not_advertised_and_refused():
    session = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "x"}]})
    tool = _mcp_tool(session)
    other = _mcp_tool(_FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "y"}]}), remote="ok", server="alpha")
    registry = _registry_with(tool, other)
    policy = _policy(**{tool.name: "deny"})
    llm = _ScriptedLLM([LLMResult(content="", tool_calls=[_tc(tool.name, {})]), LLMResult(content="ok")])
    auditor = _RecordingAuditor()
    await run_tool_loop(llm, _ctx(), registry, policy=policy, auditor=auditor)
    # Withheld from the schema (the other allowed tool is still advertised).
    names = [t["function"]["name"] for t in llm.calls[0]["tools"]]
    assert tool.name not in names
    assert other.name in names
    # Refused + audited if the model calls it anyway.
    assert session.calls == []
    assert RESULT_TOOL_DENIED in [e.code for e in auditor.events]


# ===========================================================================
# required #12 — result mapping: multi-block merge; error/size/transport
# ===========================================================================
async def test_mcp_multi_text_blocks_merged_in_order():
    session = _FakeRemoteSession(return_value={
        "is_error": False,
        "content": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}],
    })
    tool = _mcp_tool(session)
    registry = _registry_with(tool)
    llm = _ScriptedLLM([LLMResult(content="", tool_calls=[_tc(tool.name, {})]), LLMResult(content="ok")])
    await run_tool_loop(llm, _ctx(), registry, policy=_policy(default=ToolPermission.ALLOW))
    # The merged text is what the next LLM turn sees as the tool message.
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.role == "tool"]
    assert tool_msgs and tool_msgs[0].content == "line one\nline two"


@pytest.mark.parametrize(
    "return_value, expected_code",
    [
        ({"is_error": True, "content": [{"type": "text", "text": "boom"}]}, "mcp_tool_error"),
        ({"is_error": False, "content": [{"type": "image", "data": "x"}]}, "mcp_unsupported_result"),
        ({"is_error": False, "content": []}, "mcp_unsupported_result"),
        ({"is_error": False, "content": [{"type": "text", "text": "x" * 50}]}, "mcp_result_too_large"),
    ],
)
async def test_mcp_result_shapes_mapped_safely(return_value, expected_code):
    session = _FakeRemoteSession(return_value=return_value)
    tool = _mcp_tool(session, max_chars=10)
    out = await tool.execute({})
    assert out.startswith(f"[{expected_code}]")
    # No remote body echoed back.
    if expected_code != "mcp_result_too_large":
        assert "boom" not in out


async def test_mcp_transport_exception_maps_to_unavailable():
    session = _FakeRemoteSession(raise_exc=Exception("connection reset to evil.host:9999"))
    tool = _mcp_tool(session)
    out = await tool.execute({})
    assert out.startswith("[mcp_unavailable]")
    # The endpoint / exception body is never echoed.
    assert "evil.host" not in out
    assert "connection reset" not in out


# ===========================================================================
# required #13 — multi-call / multi-round / iteration limit coexist with MCP
# ===========================================================================
async def test_mcp_multi_call_preserves_order():
    sess1 = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "1"}]})
    sess2 = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "2"}]})
    t1 = _mcp_tool(sess1, remote="a", server="s")
    t2 = _mcp_tool(sess2, remote="b", server="s")
    registry = _registry_with(t1, t2)
    llm = _ScriptedLLM([
        LLMResult(content="", tool_calls=[_tc(t1.name, {}, cid="x"), _tc(t2.name, {}, cid="y")]),
        LLMResult(content="final"),
    ])
    await run_tool_loop(llm, _ctx(), registry, policy=_policy(default=ToolPermission.ALLOW))
    assert sess1.calls == [("a", {})]
    assert sess2.calls == [("b", {})]
    assert llm.calls[1]["messages"][-2].tool_call_id == "x"


async def test_mcp_multi_round():
    session = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "r1"}]})
    tool = _mcp_tool(session)
    registry = _registry_with(tool)
    llm = _ScriptedLLM([
        LLMResult(content="", tool_calls=[_tc(tool.name, {}, cid="a")]),
        LLMResult(content="", tool_calls=[_tc(tool.name, {}, cid="b")]),
        LLMResult(content="final"),
    ])
    await run_tool_loop(llm, _ctx(), registry, policy=_policy(default=ToolPermission.ALLOW))
    assert len(session.calls) == 2  # two separate rounds
    assert len(llm.calls) == 3


async def test_mcp_iteration_limit_with_mcp_tool():
    from fibrecase_agent_backend.agent.tool_loop import ToolLoopLimitError

    session = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "x"}]})
    tool = _mcp_tool(session)
    registry = _registry_with(tool)
    llm = _ScriptedLLM([
        LLMResult(content="", tool_calls=[_tc(tool.name, {}, cid="a")]),
        LLMResult(content="", tool_calls=[_tc(tool.name, {}, cid="b")]),
        LLMResult(content="", tool_calls=[_tc(tool.name, {}, cid="c")]),
    ])
    with pytest.raises(ToolLoopLimitError):
        await run_tool_loop(llm, _ctx(), registry, policy=_policy(default=ToolPermission.ALLOW), max_iterations=3)


# ===========================================================================
# required #15 — privacy: no endpoint/token/args/result/instructions in
# logs, audit, or the approval prompt
# ===========================================================================
async def test_mcp_approval_summary_never_echoes_args(caplog):
    session = _FakeRemoteSession(return_value={"is_error": False, "content": [{"type": "text", "text": "x"}]})
    tool = _mcp_tool(session)
    registry = _registry_with(tool)
    llm = _ScriptedLLM([LLMResult(content="", tool_calls=[_tc(tool.name, {"city": "secret-city"})]), LLMResult(content="ok")])
    approval = _FakeApproval(ApprovalDecision.APPROVED)
    with caplog.at_level(logging.INFO, logger="agent.tools"):
        await run_tool_loop(llm, _ctx(), registry, policy=_policy(), approval_provider=approval, auditor=_RecordingAuditor())
    # The approval prompt shows the tool + "arguments withheld", never the args.
    assert "secret-city" not in approval.requests[0].summary
    # Logs never carry the arguments either.
    assert "secret-city" not in caplog.text


async def test_mcp_logs_carry_only_name_and_code(caplog):
    session = _FakeRemoteSession(return_value={"is_error": True, "content": [{"type": "text", "text": "remote-error-body"}]})
    tool = _mcp_tool(session)
    with caplog.at_level(logging.INFO, logger="mcp"):
        await tool.execute({})
    # The wrapper logs the exception class / code, never the remote body.
    assert "remote-error-body" not in caplog.text
