"""Built-in tool: run a single shell command (full shell, opt-in).

This is the one *state-changing* built-in tool. It runs ``/bin/sh -c <command>``
(a full shell — pipes, redirection, ``&&`` all work) and returns the exit code
plus stdout / stderr. It is **off by default** (``ENABLE_EXEC_TOOL``) so the
default deployment stays subprocess-free, and it always declares
``ToolPermission.ASK`` — every call needs a one-time human Approve before it
runs. The full ``command`` string is shown verbatim on the approval card (as a
bash command block via :meth:`ExecTool.approval_detail`), so the owner sees
exactly what will run.

Defence in depth (in this order, all *inside* ``execute`` — the tool loop is
untouched):

1. **Static backstop** — :mod:`..exec_policy` vetoes a small set of
   catastrophic command shapes before anything is spawned, even if the owner
   just approved it (the guard against mis-approval from fatigue).
2. **Full shell via an argument vector** — ``create_subprocess_exec("/bin/sh",
   "-c", command, …)``, never ``shell=True`` and never a single concatenated
   string, so there is no second layer of shell parsing to escape into.
3. **Cancellation-safe process-group kill** — ``start_new_session=True`` puts
   ``sh -c`` and *all* its descendants in one process group; on the loop's
   ``TOOL_TIMEOUT_SECONDS`` timeout (which cancels this coroutine) or a turn
   shutdown, the whole group is ``SIGKILL``'d so no child is orphaned.
4. **Output bounding** — each of stdout / stderr is tail-truncated to
   ``max_output_chars`` with a fixed ``[N chars … truncated]`` marker. (Deliberate
   departure from the MCP/infra cap→error idiom: this is the direct result of a
   command the owner already saw and approved, so erroring on a long ``cat`` /
   ``git log`` would defeat the tool; the tail is what you inspect after a run.)

**Logging rule:** the command and its stdout / stderr are returned to the *model
only*. They are **never** logged here and **cannot** reach the audit table (which
stores only the tool name, stable code, latency, and a hashed scope). On the
spawn-failure path only a stable code is produced — the path / command are not
echoed.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any

from ..base import Tool
from ..exec_policy import check_exec_policy, compile_denylist
from ..policy import ToolPermission


# Fixed, non-echoing model-facing messages for the exec-specific result codes.
# (These are *tool* codes, not the loop-level codes in ``..audit``.)
_MESSAGES = {
    "exec_policy_deny": "The command matched a safety rule and was not run.",
    "exec_spawn_failed": "The command could not be started.",
}


def _error(code: str) -> str:
    """A stable, short JSON error result for a non-run exec outcome (fed to the
    model). Returned (not raised) so the specific code reaches the model — a
    raised exception would be flattened to ``tool_execution_failed`` by the loop.
    """
    return json.dumps({"error": {"code": code, "message": _MESSAGES[code]}})


def _to_text(value: bytes | str) -> str:
    """Decode child-process output lossily (mirrors the infra provider)."""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Best-effort, synchronous kill of the command's whole process group.

    Synchronous on purpose: on cancellation we must not ``await`` anything.
    ``start_new_session=True`` makes the child a group leader (pgid == its pid),
    so ``killpg`` reaches ``sh -c`` *and* everything it spawned. Falls back to a
    direct kill if ``killpg`` fails (e.g. an already-exited process). No-op when
    the group is already gone.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


def _bound_text(raw: bytes | str, cap: int) -> str:
    """Tail-truncate decoded output to ``cap`` chars, prefixing a fixed marker
    naming how many *earlier* chars were dropped (a number — no content echo).
    """
    text = _to_text(raw)
    if len(text) <= cap:
        return text
    dropped = len(text) - cap
    return f"[{dropped} chars of earlier output truncated]\n{text[-cap:]}"


class ExecTool(Tool):
    """Run a single shell command via ``/bin/sh -c`` (opt-in, always ``ask``)."""

    name = "exec"
    description = (
        "Run a single shell command using a full shell (/bin/sh -c), so pipes, "
        "redirection, and && are supported. Returns JSON {exit_code, stdout, "
        "stderr}; a non-zero exit code is still a successful run and is returned. "
        "Every call requires human approval before it runs. Use only when the "
        "task genuinely needs a shell command."
    )
    # State-changing: must default to ``ask`` (never ``allow``) — the owner
    # approves every call, and the command is shown verbatim on the card.
    default_permission = ToolPermission.ASK
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The full shell command line to run, e.g. 'ls -la | head'.",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        max_output_chars: int,
        workdir: str | None,
        policy_deny_patterns: tuple[str, ...] = (),
    ) -> None:
        self._max_output_chars = max_output_chars
        self._workdir = workdir  # None -> run in the process cwd
        self._denylist = compile_denylist(policy_deny_patterns)

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        # Fixed and argument-free. The full command is already shown in the
        # card's separate "Action:" block, so it is deliberately NOT echoed
        # here (secret-free convention).
        return "Run an arbitrary shell command (full shell: /bin/sh -c). Requires approval."

    def approval_detail(self, arguments: dict[str, Any]) -> str | None:
        """A human-friendly view of this call for the approval card.

        Shown in place of the generic JSON ``Arguments:`` block (see
        :meth:`Tool.approval_detail`). ``exec``'s only argument is the command,
        so this renders it as a **bash command block** — the exact command
        verbatim under a ``$`` prompt — so the owner reads precisely the shell
        line that will run rather than ``{"command": "…"}``. Plain text, no
        markup: the provider HTML-escapes and length-bounds it and wraps it in a
        code block (which keeps a multi-line command's newlines intact).
        """
        command = arguments.get("command")
        if not isinstance(command, str):
            return None
        return f"$ {command}"

    def approval_language(self, arguments: dict[str, Any]) -> str:
        # The detail view is a shell command, so label it bash for highlighting.
        # (Fixed vocabulary — never derived from the command's content.)
        return "bash"

    async def execute(self, arguments: dict[str, Any]) -> str:
        command = arguments["command"]

        # (1) Static backstop — before any spawn. Return (do not raise) so the
        # loop surfaces the specific exec_policy_deny code to the model.
        if check_exec_policy(command, self._denylist) is not None:
            return _error("exec_policy_deny")

        # (2) Spawn via an argument vector (never shell=True). start_new_session
        # gives sh -c + descendants one process group for the group kill below.
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                command,
                cwd=self._workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except Exception:
            # No /bin/sh, invalid cwd, etc. Stable code — the command / path are
            # never echoed to the model or logged.
            return _error("exec_spawn_failed")

        # (3) Capture. communicate() reads both pipes in one step (no deadlock).
        # On cancellation (the loop's timeout, or a turn shutdown) kill the whole
        # process group so no child is orphaned, then unwind.
        try:
            try:
                out, err = await proc.communicate()
            except asyncio.CancelledError:
                _kill_process_group(proc)
                raise
            finally:
                # Belt-and-suspenders: kill the group if it is still alive, even
                # on the cancel path (idempotent once the process has exited).
                if proc.returncode is None:
                    _kill_process_group(proc)
        except asyncio.CancelledError:
            raise  # propagate; the loop records/handles the timeout

        # (4) A non-zero exit is a *successful run* — return output + code so the
        # model can reason about it. Output is tail-truncated to the cap.
        exit_code = proc.returncode if proc.returncode is not None else -1
        return json.dumps(
            {
                "exit_code": exit_code,
                "stdout": _bound_text(out, self._max_output_chars),
                "stderr": _bound_text(err, self._max_output_chars),
            }
        )
