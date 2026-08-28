"""Phase 5.1 — read-only infrastructure observation: provider + gate (required #1,#3–#10).

Everything is fake: the ``asyncssh`` module is injected as a stub (no real connection,
network, or subprocess) and/or ``infrastructure.provider._connect`` is replaced with a
recording stub. The gate tests drive the real ``run_tool_loop`` with a scripted LLM +
a fake approval provider + an in-memory recording auditor, and assert that SSH is
**never** opened on any gate failure and is opened **exactly once** on approval.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from typing import Any

import pytest

from fibrecase_agent_backend.agent.context import ChatMessage
from fibrecase_agent_backend.agent.tool_loop import run_tool_loop
from fibrecase_agent_backend.config import InfraSshTarget
from fibrecase_agent_backend.infrastructure import (
    CODE_INVALID_RESPONSE,
    CODE_RESULT_TOO_LARGE,
    CODE_UNAVAILABLE,
    build_infra_tools,
    local_tool_name,
)
from fibrecase_agent_backend.infrastructure import provider as infra_provider
from fibrecase_agent_backend.llm.client import LLMResult
from fibrecase_agent_backend.tools import (
    ApprovalDecision,
    ToolPermission,
    ToolRegistry,
    build_default_tools,
    build_policy,
)
from fibrecase_agent_backend.tools.audit import ToolAuditEvent


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
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _ctx() -> list[ChatMessage]:
    return [ChatMessage("system", "S"), ChatMessage("user", "do it")]


class _RecordingAuditor:
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

    def tools(self) -> list[str]:
        return [e.tool_name for e in self.events]


class _FakeApproval:
    def __init__(self, decision: ApprovalDecision = ApprovalDecision.APPROVED):
        self.decision = decision
        self.requests = []

    async def request_approval(self, request):
        self.requests.append(request)
        return self.decision

    async def shutdown(self):
        pass


def _target(**over) -> InfraSshTarget:
    d = dict(
        name="nas",
        host="secret-nas-host",
        port=22,
        username="probe",
        private_key_path="/run/secrets/nas_key",
        known_hosts_path="/run/secrets/nas_known_hosts",
        mounts=("/volume1", "/data"),
        services=("ssh.service",),
    )
    d.update(over)
    return InfraSshTarget(**d)


_HOST_STDOUT = (
    "h|os=Linux|kernel=6.6.0|arch=x86_64\n"
    "h|uptime_seconds=123|load1=0.1|load5=0.2|load15=0.3\n"
    "h|mem_total_kb=8000000|mem_available_kb=4000000|swap_total_kb=0|swap_free_kb=0\n"
)


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeConn:
    def __init__(self, proc, run_delay=0.0):
        self._proc = proc
        self._run_delay = run_delay
        self.run_commands: list[str] = []
        self.closed = False

    async def run(self, cmd, *, encoding=None):
        self.run_commands.append(cmd)
        if self._run_delay:
            await asyncio.sleep(self._run_delay)
        return self._proc

    def close(self):
        self.closed = True


class _ConnectStub:
    """Replaces ``infra_provider._connect`` for execute-level tests.

    Callable as ``stub(target, connect_timeout_seconds)`` (the provider's own
    signature). Records each call; returns a scripted conn, raises a scripted
    error, or (``delay``) simulates a slow handshake. ``hang_command`` makes the
    returned conn's ``run()`` block (simulating a hung command).
    """

    def __init__(self, *, delay=0.0, error=None, procedures=None, hang_command=0.0):
        self.delay = delay
        self.error = error
        self.procedures = list(procedures or [])
        self.hang_command = hang_command
        self.calls = 0
        self.targets: list[InfraSshTarget] = []
        self.timeouts: list[float] = []
        self.conns: list[_FakeConn] = []

    async def __call__(self, target, connect_timeout_seconds):
        self.calls += 1
        self.targets.append(target)
        self.timeouts.append(connect_timeout_seconds)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        proc = self.procedures.pop(0) if self.procedures else _FakeProc()
        conn = _FakeConn(proc, run_delay=self.hang_command)
        self.conns.append(conn)
        return conn


def _install_fake_asyncssh(monkeypatch, connect) -> None:
    """Inject a stub ``asyncssh`` module whose ``connect`` is ``connect``.

    Lets us exercise the *real* ``infra_provider._connect`` (which does
    ``import asyncssh`` lazily) without ever touching the real library.
    """
    mod = types.ModuleType("asyncssh")
    mod.connect = connect
    monkeypatch.setitem(sys.modules, "asyncssh", mod)


# ---------------------------------------------------------------------------
# required #1 — empty targets / tools disabled → no asyncssh import, no tools
# ---------------------------------------------------------------------------
def test_no_targets_yields_no_tools_and_no_asyncssh_import():
    tools = build_infra_tools((), connect_timeout_seconds=10.0, max_result_chars=8000)
    assert tools == []
    assert "asyncssh" not in sys.modules


def test_construction_does_not_import_asyncssh():
    tools = build_infra_tools((_target(),), connect_timeout_seconds=10.0, max_result_chars=8000)
    assert len(tools) == 3
    assert "asyncssh" not in sys.modules


def test_composition_root_guards_infra_build():
    # The composition root builds the infra tools **only** when tools are enabled
    # *and* at least one target is configured — otherwise no tool objects and no
    # (lazy) asyncssh load. This mirrors the MCP manager guard.
    import fibrecase_agent_backend.main as main

    src = open(main.__file__).read()
    assert "config.enable_tools and config.infra_ssh_targets" in src


# ---------------------------------------------------------------------------
# required #3 — exactly 3 namespaced no-arg ALLOW (read-only) tools per target
# ---------------------------------------------------------------------------
def test_three_allow_noarg_tools_per_target():
    tools = build_infra_tools((_target(),), connect_timeout_seconds=10.0, max_result_chars=8000)
    assert [t.name for t in tools] == [
        "infra_nas__host_status",
        "infra_nas__disk_status",
        "infra_nas__service_status",
    ]
    for t in tools:
        # Strictly read-only (fixed, argument-free) → runs without approval.
        assert t.default_permission == ToolPermission.ALLOW
        assert t.parameters == {"type": "object", "properties": {}, "additionalProperties": False}


def test_two_targets_no_collision_with_builtins():
    targets = (_target(name="nas"), _target(name="pi"))
    reg = build_default_tools()
    reg.add(*build_infra_tools(targets, connect_timeout_seconds=10.0, max_result_chars=8000))
    names = set(reg.names())
    assert {"get_current_time", "echo", "system_info"} <= names
    assert {
        "infra_nas__host_status", "infra_nas__disk_status", "infra_nas__service_status",
        "infra_pi__host_status", "infra_pi__disk_status", "infra_pi__service_status",
    } <= names
    assert len(names) == 3 + 6


def test_deny_override_withholds_infra_tool_from_schema():
    reg = build_default_tools()
    reg.add(*build_infra_tools((_target(name="nas"),), connect_timeout_seconds=10.0, max_result_chars=8000))
    policy = build_policy({"infra_nas__host_status": ToolPermission.DENY}, registry=reg)
    advertised = policy.advertised_names(set(reg.names()))
    assert "infra_nas__host_status" not in advertised
    assert "infra_nas__disk_status" in advertised
    schema = reg.to_openai_schema(advertised)
    assert "infra_nas__host_status" not in [s["function"]["name"] for s in schema]


def test_approval_summary_never_echoes_endpoint():
    tool = build_infra_tools((_target(),), connect_timeout_seconds=10.0, max_result_chars=8000)[0]
    summary = tool.approval_summary({})
    assert "nas" in summary  # the target *name* is allowed
    for secret in ("secret-nas-host", "probe", "/run/secrets/nas_key", "/volume1"):
        assert secret not in summary
    assert "secret-nas-host" not in tool.description


# ---------------------------------------------------------------------------
# required #4 — each tool runs its fixed command; strict parse; no model args
# ---------------------------------------------------------------------------
def test_host_command_is_fixed_constant():
    tools = build_infra_tools((_target(),), connect_timeout_seconds=10.0, max_result_chars=8000)
    cmd = next(t for t in tools if t.name.endswith("__host_status"))._command
    for secret in ("secret-nas-host", "probe", "/volume1", "ssh.service"):
        assert secret not in cmd
    assert "/proc/uptime" in cmd and "/proc/meminfo" in cmd


def test_disk_command_quotes_configured_mounts():
    tools = build_infra_tools((_target(mounts=("/volume1", "/data o'x")),), connect_timeout_seconds=10.0, max_result_chars=8000)
    cmd = next(t for t in tools if t.name.endswith("__disk_status"))._command
    assert "'/volume1'" in cmd
    assert "'/data o'\\''x'" in cmd  # embedded single quote escaped
    assert "for _m in" in cmd


def test_service_command_quotes_configured_services():
    tools = build_infra_tools((_target(services=("nginx.service",)),), connect_timeout_seconds=10.0, max_result_chars=8000)
    cmd = next(t for t in tools if t.name.endswith("__service_status"))._command
    assert "'nginx.service'" in cmd
    assert "systemctl show -p ActiveState --value" in cmd


async def test_host_output_parses_to_stable_json(monkeypatch):
    stub = _ConnectStub(procedures=[_FakeProc(0, _HOST_STDOUT)])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    tool = build_infra_tools((_target(),), connect_timeout_seconds=10.0, max_result_chars=8000)[0]
    data = json.loads(await tool.execute({}))
    assert data["target"] == "nas"
    assert data["os"] == "Linux"
    assert data["uptime_seconds"] == 123
    assert data["load_avg"]["5"] == 0.2
    assert data["memory"]["total_kb"] == 8000000
    for secret in ("secret-nas-host", "probe", "/run/secrets"):
        assert secret not in json.dumps(data)


async def test_disk_output_parses_preserving_configured_order(monkeypatch):
    stdout = (
        "d|mount=/volume1|size_kb=1000|used_kb=100|avail_kb=900|pcent=10%\n"
        "d|mount=/data|size_kb=2000|used_kb=500|avail_kb=1500|pcent=25%\n"
    )
    stub = _ConnectStub(procedures=[_FakeProc(0, stdout)])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    tool = next(
        t for t in build_infra_tools((_target(mounts=("/volume1", "/data")),), connect_timeout_seconds=10.0, max_result_chars=8000)
        if t.name.endswith("__disk_status")
    )
    data = json.loads(await tool.execute({}))
    assert list(data["mounts"]) == ["/volume1", "/data"]
    assert data["mounts"]["/volume1"]["pcent"] == 10
    assert data["mounts"]["/data"]["avail_kb"] == 1500


async def test_service_output_parses(monkeypatch):
    stub = _ConnectStub(procedures=[_FakeProc(0, "s|service=ssh.service|state=active\n")])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    tool = next(
        t for t in build_infra_tools((_target(services=("ssh.service",)),), connect_timeout_seconds=10.0, max_result_chars=8000)
        if t.name.endswith("__service_status")
    )
    assert json.loads(await tool.execute({}))["services"] == {"ssh.service": {"state": "active"}}


# --- required #6 — malformed / empty / stderr / non-zero / oversize → safe code
async def test_connect_failure_maps_to_unavailable(monkeypatch, caplog):
    stub = _ConnectStub(error=OSError("cannot reach secret-nas-host key /run/secrets/nas_key"))
    monkeypatch.setattr(infra_provider, "_connect", stub)
    tool = build_infra_tools((_target(),), connect_timeout_seconds=10.0, max_result_chars=8000)[0]
    assert json.loads(await tool.execute({})) == {"error": CODE_UNAVAILABLE}
    joined = "|".join(
        [r.getMessage() for r in caplog.records]
        + [str(r.__dict__[k]) for r in caplog.records for k in ("tool", "code", "exception") if k in r.__dict__]
    )
    assert "OSError" in joined  # class is fine
    assert "cannot reach" not in joined  # ...the text is not
    assert "secret-nas-host" not in joined
    assert "/run/secrets/nas_key" not in joined


@pytest.mark.parametrize(
    "proc",
    [
        _FakeProc(0, ""),                                   # empty stdout
        _FakeProc(0, "garbage that is not our format"),     # malformed
        _FakeProc(1, "h|os=Linux"),                          # non-zero exit
        _FakeProc(0, "h|os=Linux", "boom secret stderr"),   # stderr present
    ],
)
async def test_bad_host_output_maps_to_invalid_response(monkeypatch, proc):
    stub = _ConnectStub(procedures=[proc])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    tool = build_infra_tools((_target(),), connect_timeout_seconds=10.0, max_result_chars=8000)[0]
    assert json.loads(await tool.execute({})) == {"error": CODE_INVALID_RESPONSE}


@pytest.mark.parametrize(
    "stdout",
    [
        "h|os=Linux|kernel=1|arch=x86_64\n",  # missing fields
        "h|os=Linux|kernel=1|arch=x86_64|h|uptime_seconds=1|load1=1|load5=1|load15=1\n",  # dup field
        "h|os=Linux|kernel=1|arch=x86_64|bogus=1|h|uptime_seconds=1|load1=1|load5=1|load15=1\nh|mem_total_kb=1|mem_available_kb=1|swap_total_kb=0|swap_free_kb=0\n",  # unexpected field
    ],
)
async def test_partial_host_output_maps_to_invalid_response(monkeypatch, stdout):
    stub = _ConnectStub(procedures=[_FakeProc(0, stdout)])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    tool = build_infra_tools((_target(),), connect_timeout_seconds=10.0, max_result_chars=8000)[0]
    assert json.loads(await tool.execute({})) == {"error": CODE_INVALID_RESPONSE}


async def test_oversize_result_maps_to_result_too_large(monkeypatch):
    stub = _ConnectStub(procedures=[_FakeProc(0, _HOST_STDOUT)])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    tool = build_infra_tools((_target(),), connect_timeout_seconds=10.0, max_result_chars=5)[0]
    assert json.loads(await tool.execute({})) == {"error": CODE_RESULT_TOO_LARGE}


# --- required #5 — connect receives explicit known_hosts + client_keys, never None
async def test_connect_uses_pinned_key_and_known_hosts_never_none(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_connect(host, port, **kwargs):
        seen["host"] = host
        seen["port"] = port
        seen.update(kwargs)
        return object()

    _install_fake_asyncssh(monkeypatch, fake_connect)
    await infra_provider._connect(_target(), 7.0)
    assert seen["host"] == "secret-nas-host"
    assert seen["port"] == 22
    assert seen["username"] == "probe"
    assert seen["client_keys"] == ["/run/secrets/nas_key"]
    assert seen["known_hosts"] == "/run/secrets/nas_known_hosts"  # pinned, never None
    assert seen["known_hosts"] is not None and seen["client_keys"] is not None
    assert seen["agent_path"] == ""
    assert seen["password_auth"] is False
    assert seen["kbdint_auth"] is False
    assert seen["public_key_auth"] is True
    assert seen["connect_timeout"] == 7.0


async def test_connection_is_closed_after_run(monkeypatch):
    stub = _ConnectStub(procedures=[_FakeProc(0, _HOST_STDOUT)])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    tool = build_infra_tools((_target(),), connect_timeout_seconds=7.0, max_result_chars=8000)[0]
    await tool.execute({})
    assert stub.conns[0].closed is True


async def test_model_arguments_are_ignored(monkeypatch):
    # Even if (impossibly) arguments were passed, the fixed command is unchanged
    # and no argument value reaches the remote command.
    stub = _ConnectStub(procedures=[_FakeProc(0, _HOST_STDOUT)])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    tool = build_infra_tools((_target(),), connect_timeout_seconds=10.0, max_result_chars=8000)[0]
    await tool.execute({"host": "evil", "command": "rm -rf /"})
    assert stub.conns[0].run_commands[0] == tool._command


# ---------------------------------------------------------------------------
# required #7 — gate: SSH never opened on any gate failure; exactly once on approve
# ---------------------------------------------------------------------------
def _infra_registry():
    reg = build_default_tools()
    reg.add(*build_infra_tools((_target(name="nas"),), connect_timeout_seconds=10.0, max_result_chars=8000))
    return reg


async def test_deny_infra_tool_never_connects(monkeypatch):
    stub = _ConnectStub()
    monkeypatch.setattr(infra_provider, "_connect", stub)
    reg = _infra_registry()
    policy = build_policy({"infra_nas__host_status": ToolPermission.DENY}, registry=reg)
    auditor = _RecordingAuditor()
    approval = _FakeApproval()
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("infra_nas__host_status", {})]),
        LLMResult(content="refused"),
    ])
    result = await run_tool_loop(llm, _ctx(), reg, policy=policy, approval_provider=approval, auditor=auditor)
    assert result.text == "refused"
    assert stub.calls == 0  # no SSH connection at all
    assert approval.requests == []
    assert json.loads(llm.calls[1]["messages"][-1]["content"])["error"]["code"] == "tool_denied"


@pytest.mark.parametrize(
    "args",
    ["{not json", json.dumps({"mount": "/volume1"}), json.dumps([1])],
)
async def test_invalid_infra_args_never_connect(monkeypatch, args):
    stub = _ConnectStub()
    monkeypatch.setattr(infra_provider, "_connect", stub)
    reg = _infra_registry()
    auditor = _RecordingAuditor()
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("infra_nas__host_status", args)]),
        LLMResult(content="nope"),
    ])
    await run_tool_loop(llm, _ctx(), reg, policy=None, auditor=auditor)
    assert stub.calls == 0
    assert "validation_failed" in auditor.types


async def test_pre_audit_failure_infra_never_connects(monkeypatch):
    stub = _ConnectStub()
    monkeypatch.setattr(infra_provider, "_connect", stub)
    reg = _infra_registry()
    auditor = _RecordingAuditor(pre_ok=False)
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("infra_nas__host_status", {})]),
        LLMResult(content="blocked"),
    ])
    result = await run_tool_loop(llm, _ctx(), reg, policy=None, auditor=auditor)
    assert result.text == "blocked"
    assert stub.calls == 0
    assert "started" not in auditor.types
    assert json.loads(llm.calls[1]["messages"][-1]["content"])["error"]["code"] == "audit_unavailable"


async def test_ask_infra_denied_never_connects(monkeypatch):
    stub = _ConnectStub()
    monkeypatch.setattr(infra_provider, "_connect", stub)
    reg = _infra_registry()
    # The default is now ALLOW; force `ask` to exercise the approval gate.
    policy = build_policy({"infra_nas__host_status": ToolPermission.ASK}, registry=reg)
    auditor = _RecordingAuditor()
    approval = _FakeApproval(ApprovalDecision.DENIED)
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("infra_nas__host_status", {})]),
        LLMResult(content="denied"),
    ])
    await run_tool_loop(llm, _ctx(), reg, policy=policy, approval_provider=approval, auditor=auditor)
    assert stub.calls == 0
    assert len(approval.requests) == 1
    assert "approval_denied" in auditor.types


async def test_ask_infra_expired_never_connects(monkeypatch):
    stub = _ConnectStub()
    monkeypatch.setattr(infra_provider, "_connect", stub)
    reg = _infra_registry()
    policy = build_policy({"infra_nas__host_status": ToolPermission.ASK}, registry=reg)
    auditor = _RecordingAuditor()
    approval = _FakeApproval(ApprovalDecision.EXPIRED)
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("infra_nas__host_status", {})]),
        LLMResult(content="expired"),
    ])
    await run_tool_loop(llm, _ctx(), reg, policy=policy, approval_provider=approval, auditor=auditor)
    assert stub.calls == 0
    assert "approval_expired" in auditor.types


async def test_ask_infra_approved_connects_exactly_once(monkeypatch):
    stub = _ConnectStub(procedures=[_FakeProc(0, _HOST_STDOUT)])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    reg = _infra_registry()
    policy = build_policy({"infra_nas__host_status": ToolPermission.ASK}, registry=reg)
    auditor = _RecordingAuditor()
    approval = _FakeApproval(ApprovalDecision.APPROVED)
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("infra_nas__host_status", {})]),
        LLMResult(content="here it is"),
    ])
    result = await run_tool_loop(llm, _ctx(), reg, policy=policy, approval_provider=approval, auditor=auditor)
    assert result.text == "here it is"
    assert stub.calls == 1  # exactly one connection for exactly one approved call
    assert stub.conns[0].closed is True
    assert "started" in auditor.types and "completed" in auditor.types
    assert "approval_requested" in auditor.types and "approval_approved" in auditor.types


# ---------------------------------------------------------------------------
# required #8 — connect delay and command delay each time out; loop continues
# ---------------------------------------------------------------------------
async def test_slow_connect_times_out(monkeypatch):
    stub = _ConnectStub(delay=5.0, procedures=[_FakeProc(0, _HOST_STDOUT)])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    reg = ToolRegistry().register(
        build_infra_tools((_target(name="nas"),), connect_timeout_seconds=10.0, max_result_chars=8000)[0]
    )
    auditor = _RecordingAuditor()
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("infra_nas__host_status", {})]),
        LLMResult(content="timed out"),
    ])
    result = await run_tool_loop(llm, _ctx(), reg, policy=None, auditor=auditor, tool_timeout_seconds=0.05)
    assert result.text == "timed out"
    assert "started" in auditor.types and "timed_out" in auditor.types
    payload = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert payload["error"]["code"] == "tool_timeout"


async def test_slow_command_times_out_and_conn_closed(monkeypatch):
    stub = _ConnectStub(hang_command=5.0, procedures=[_FakeProc(0, _HOST_STDOUT)])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    reg = ToolRegistry().register(
        build_infra_tools((_target(name="nas"),), connect_timeout_seconds=10.0, max_result_chars=8000)[0]
    )
    auditor = _RecordingAuditor()
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("infra_nas__host_status", {})]),
        LLMResult(content="command hung"),
    ])
    result = await run_tool_loop(llm, _ctx(), reg, policy=None, auditor=auditor, tool_timeout_seconds=0.05)
    assert result.text == "command hung"
    assert "timed_out" in auditor.types
    assert stub.conns[0].closed is True  # closed even though the command was cancelled
    payload = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert payload["error"]["code"] == "tool_timeout"


# ---------------------------------------------------------------------------
# required #9 — multi-call order preserved
# ---------------------------------------------------------------------------
async def test_multi_call_order_preserved(monkeypatch):
    stub = _ConnectStub(procedures=[_FakeProc(0, _HOST_STDOUT)])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    reg = _infra_registry()
    policy = build_policy({}, registry=reg)
    approval = _FakeApproval(ApprovalDecision.APPROVED)
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[
            _tc("infra_nas__host_status", {}, cid="h"),
            _tc("infra_nas__service_status", {"bogus": 1}, cid="s"),  # invalid → not run
        ]),
        LLMResult(content="done"),
    ])
    result = await run_tool_loop(llm, _ctx(), reg, policy=policy, approval_provider=approval)
    assert result.text == "done"
    second = llm.calls[1]["messages"]
    assert second[-2]["tool_call_id"] == "h"
    assert json.loads(second[-2]["content"])["target"] == "nas"
    assert second[-1]["tool_call_id"] == "s"
    assert json.loads(second[-1]["content"])["error"]["code"] == "invalid_arguments"
    assert stub.calls == 1  # only the schema-valid host tool connected


# ---------------------------------------------------------------------------
# required #10 — audit events + logs carry only local tool name / stable metadata
# ---------------------------------------------------------------------------
async def test_audit_and_logs_are_secret_free(monkeypatch, caplog):
    stub = _ConnectStub(procedures=[_FakeProc(0, _HOST_STDOUT)])
    monkeypatch.setattr(infra_provider, "_connect", stub)
    reg = _infra_registry()
    # Force `ask` so the approval request (asserted below) is actually made.
    policy = build_policy({"infra_nas__host_status": ToolPermission.ASK}, registry=reg)
    approval = _FakeApproval(ApprovalDecision.APPROVED)
    auditor = _RecordingAuditor()
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("infra_nas__host_status", {})]),
        LLMResult(content="ok"),
    ])
    with caplog.at_level(logging.INFO, logger="agent.tools"):
        await run_tool_loop(llm, _ctx(), reg, policy=policy, approval_provider=approval, auditor=auditor, scope="telegram:42")

    # Every audit event names only the local (namespaced) tool.
    assert all(n == "infra_nas__host_status" for n in auditor.tools())
    # The approval request carries the tool name, not the endpoint.
    assert approval.requests[0].tool_name == "infra_nas__host_status"

    # Secrets never leak into the logs.
    joined = "|".join(
        [r.getMessage() for r in caplog.records]
        + [str(r.__dict__[k]) for r in caplog.records for k in ("tool", "code", "exception") if k in r.__dict__]
    )
    for s in ("secret-nas-host", "probe", "/run/secrets/nas_key", "/run/secrets/nas_known_hosts", "telegram:42"):
        assert s not in joined

    # The model-facing result is bounded JSON with the target *name* only.
    tool_msg = llm.calls[1]["messages"][-1]
    data = json.loads(tool_msg["content"])
    assert data["target"] == "nas"
    for s in ("secret-nas-host", "probe", "/run/secrets"):
        assert s not in tool_msg["content"]


# ---------------------------------------------------------------------------
# local_tool_name shape
# ---------------------------------------------------------------------------
def test_local_tool_name_shape():
    assert local_tool_name("nas", "host_status") == "infra_nas__host_status"
    assert local_tool_name("pi", "disk_status") == "infra_pi__disk_status"
    assert not local_tool_name("nas", "host_status").startswith("mcp_")
