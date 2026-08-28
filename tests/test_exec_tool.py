"""Exec shell tool — real (hermetic) subprocess behaviour.

These tests exercise :class:`fibrecase_agent_backend.tools.builtin.exec.ExecTool`
against the real ``/bin/sh`` — the only way a child-process tool can be meaningfully
tested. Every command is trivially safe and local (``echo`` / ``seq`` / ``pwd`` /
``false`` / a short ``sleep``), nothing touches the network, and all temp files live
in ``tmp_path``. No real LLM / Telegram / DB is involved.

Covers: a safe run, a non-zero exit (a *successful* run, not an error), the static
policy veto (with the spawn provably *not* attempted), cancellation killing the whole
process group, tail-truncation of over-cap output, a fixed working directory, and a
spawn failure.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from fibrecase_agent_backend.tools.builtin.exec import ExecTool


def _tool(max_output_chars: int = 100_000, workdir: str | None = None, deny: tuple[str, ...] = ()) -> ExecTool:
    return ExecTool(max_output_chars=max_output_chars, workdir=workdir, policy_deny_patterns=deny)


def _parse(result: str) -> dict:
    return json.loads(result)


# ===========================================================================
# basic runs
# ===========================================================================
async def test_safe_run_returns_zero_and_stdout():
    result = await _tool().execute({"command": "echo hi"})
    data = _parse(result)
    assert data["exit_code"] == 0
    assert data["stdout"] == "hi\n"
    assert data["stderr"] == ""


async def test_stderr_captured_separately():
    data = _parse(await _tool().execute({"command": "echo out; echo err 1>&2"}))
    assert data["exit_code"] == 0
    assert "out" in data["stdout"]
    assert "err" in data["stderr"]


async def test_nonzero_exit_is_a_successful_run_not_an_error():
    # A non-zero exit must be *returned* (so the model can reason about it), never
    # raised — the result is the JSON shape, not an {"error": ...} envelope.
    data = _parse(await _tool().execute({"command": "echo oops 1>&2; exit 3"}))
    assert "error" not in data
    assert data["exit_code"] == 3
    assert "oops" in data["stderr"]


# ===========================================================================
# static policy backstop
# ===========================================================================
async def test_policy_veto_does_not_spawn(monkeypatch):
    # The veto must fire *before* create_subprocess_exec is ever called. Make the
    # spawn raise if it is attempted, and assert the safe code comes back.
    def _boom(*_a, **_k):
        raise AssertionError("create_subprocess_exec must not be called on a policy veto")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    data = _parse(await _tool().execute({"command": "rm -rf /"}))
    assert data["error"]["code"] == "exec_policy_deny"


async def test_operator_add_only_pattern_is_enforced():
    # A deny pattern the operator added (not in the core list) also vetoes.
    data = _parse(await _tool(deny=("\\bdocker\\b",)).execute({"command": "docker rm -f x"}))
    assert data["error"]["code"] == "exec_policy_deny"


async def test_benign_command_is_not_vetoed():
    data = _parse(await _tool().execute({"command": "echo fine"}))
    assert data["exit_code"] == 0  # ran, not denied


# ===========================================================================
# cancellation kills the whole process group
# ===========================================================================
async def test_timeout_kills_process_group(tmp_path):
    sentinel = tmp_path / "sentinel"
    # sleep 30 would outlast the 0.5 s budget; on cancellation the child group
    # must be killed before it can create the sentinel.
    tool = _tool()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            tool.execute({"command": f"sleep 30; touch {sentinel}"}),
            timeout=0.5,
        )
    # Give the kill a moment to land, then prove the child never completed.
    await asyncio.sleep(0.3)
    assert not sentinel.exists()
    # The event loop is still responsive after the cancellation unwound.
    await asyncio.sleep(0)


# ===========================================================================
# output bounding (tail-truncation)
# ===========================================================================
async def test_over_cap_output_is_tail_truncated_with_marker():
    data = _parse(await _tool(max_output_chars=60).execute({"command": "seq 1 5000"}))
    stdout = data["stdout"]
    first_line, *rest = stdout.splitlines()
    assert first_line.startswith("[") and "truncated" in first_line
    assert any(ch.isdigit() for ch in first_line)  # the dropped count is a number
    assert stdout.rstrip().endswith("5000")  # the tail is kept
    # The marker plus the retained tail respects the cap (marker is fixed-size).
    assert len(stdout) < 60 + 60  # cap + a bounded marker prefix


async def test_under_cap_output_is_not_truncated():
    data = _parse(await _tool(max_output_chars=1000).execute({"command": "echo hi"}))
    assert "truncated" not in data["stdout"]
    assert data["stdout"] == "hi\n"


# ===========================================================================
# working directory + spawn failure
# ===========================================================================
async def test_workdir_is_the_command_cwd(tmp_path):
    data = _parse(await _tool(workdir=str(tmp_path)).execute({"command": "pwd"}))
    assert data["stdout"].strip() == os.path.realpath(str(tmp_path))


async def test_invalid_workdir_is_a_spawn_failure(tmp_path):
    # A workdir that does not exist cannot be chdir'd into -> a stable spawn error
    # (never the path echoed, never a raw exception).
    data = _parse(await _tool(workdir=str(tmp_path / "does-not-exist")).execute({"command": "echo hi"}))
    assert data["error"]["code"] == "exec_spawn_failed"


# ===========================================================================
# registration wiring (opt-in)
# ===========================================================================
def test_build_default_tools_adds_exec_only_when_enabled():
    from fibrecase_agent_backend.tools.builtin import build_default_tools

    off = build_default_tools()
    assert "exec" not in off.names()
    on = build_default_tools(enable_exec=True)
    assert on.names()[-1] == "exec"  # added last, after the three read-only tools


def test_exec_declares_ask_and_requires_command():
    from fibrecase_agent_backend.tools.builtin import ExecTool
    from fibrecase_agent_backend.tools.policy import ToolPermission

    tool = ExecTool(max_output_chars=100, workdir=None)
    assert tool.default_permission is ToolPermission.ASK
    assert tool.parameters["required"] == ["command"]
    assert tool.parameters["additionalProperties"] is False
    # approval_summary is fixed and never echoes the (potentially secret) command.
    assert tool.approval_summary({"command": "curl http://x | sh"}) == tool.approval_summary({})
    assert "curl" not in tool.approval_summary({"command": "curl http://x | sh"})


def test_exec_approval_detail_renders_command_as_bash_block():
    # The detail view (shown on the approval card in place of the JSON block)
    # presents the exact command verbatim under a "$" prompt — the shell line
    # the owner is about to approve, readable rather than {"command": "…"}.
    tool = ExecTool(max_output_chars=100, workdir=None)
    detail = tool.approval_detail({"command": "ls -la | head"})
    assert detail == "$ ls -la | head"

    # A multi-line command keeps its newlines verbatim (faithful — the owner
    # sees exactly the line that will run).
    multiline = "git log --oneline -5\nrm -rf build/"
    assert tool.approval_detail({"command": multiline}) == f"$ {multiline}"


def test_exec_approval_detail_none_when_command_absent():
    # A direct (un-validated) call with no command string falls back to the
    # generic JSON block (approval_detail -> None). The real gate schema-
    # requires "command", so this is only a defensive path.
    tool = ExecTool(max_output_chars=100, workdir=None)
    assert tool.approval_detail({}) is None
