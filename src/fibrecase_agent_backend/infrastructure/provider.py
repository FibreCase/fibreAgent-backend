"""The phase-5.1 read-only infrastructure observation provider.

One :class:`InfraTool` is a **standard** :class:`~..tools.base.Tool` bound to
*one* fixed observation (host / disk / service status) for *one*
operator-configured SSH :class:`~..config.InfraSshTarget`. Like the MCP
``McpTool`` it is a first-class tool in every respect the registry and the
phase-3 gate care about:

* ``name`` is the stable namespaced local name
  ``infra_<target>__<observation>`` — the ``infra_`` prefix and ``__`` separator
  keep the parts unambiguous, and because the target name comes from
  ``[a-z][a-z0-9_-]{0,31}`` the local name is itself a legal ``[A-Za-z0-9_-]+``
  tool name;
* ``default_permission`` is :attr:`ToolPermission.ALLOW` — these observations are
  strictly **read-only** (fixed, argument-free commands over a host-key-pinned,
  key-only connection that can only read host / disk / service status and change
  nothing), so they run **without** a per-call approval, like the
  ``get_current_time`` / ``echo`` built-ins; an operator may still pin one ``deny``
  by its namespaced name (a local tool is not in ``MCP_PERMISSIONS_FILE``);
* ``parameters`` is the empty object
  ``{"type":"object","properties":{},"additionalProperties":false}`` — the model
  can pass **no** arguments, so it can never steer a host, path, service, or
  command (an extra property is schema-rejected before execution).

``execute()`` does exactly one thing: open a **short-lived** host-key-pinned,
key-only SSH connection, run the tool's **fixed** command template, and map the
output to a **bounded, non-echoing** normalised JSON result. It does **no**
auth/approval/param-validation/timeout/audit of its own — those live in the
phase-3 tool loop, which wraps every registered tool identically. There is no
persistent connection and no reconnect: each call that passes the gate opens its
own connection and closes it when the command finishes (even on cancel).

This module imports only :mod:`..tools` (the ``Tool`` interface),
:mod:`..config` (the frozen target type), the stdlib, and — **lazily, only when a
call actually connects** — ``asyncssh``. It knows nothing about Telegram, the
database, the OpenAI SDK, ``AgentService``, or the MCP provider. Startup never
touches the network: building a tool is pure string work, and the SSH connection
is opened only inside :meth:`InfraTool.execute` (after the gate has let the
call through).

Security invariants, all enforced here:

* the private key is passed **only** via ``client_keys=[private_key_path]``; the
  host key is pinned **only** via ``known_hosts=known_hosts_path`` (never
  ``None`` / auto-accept); the SSH agent is disabled (``agent_path=""`` makes the
  agent lookup fail cleanly so no agent key is ever tried) and password /
  keyboard-interactive auth are turned off, so the explicit key is the only auth
  path;
* the command is a **code constant** (a template) — the only interpolated values
  are the target's statically-validated ``mounts`` / ``services``, and each is
  shell-quoted before it is spliced in. No runtime input ever reaches the shell;
* stdout/stderr are **parsed, never echoed**. Every failure — connect /
  auth / host-key, a non-zero exit, stderr output, a malformed/empty/oversized
  result — maps to one of three stable, non-echoing codes: ``infra_unavailable``
  / ``infra_invalid_response`` / ``infra_result_too_large``. The target
  host, key path, known_hosts path, username, mount path, command, and any
  stdout/stderr are **never** returned to the model, logged, or written to the
  audit table (a warning carries only the tool *name*, the stable *code*, and
  the exception *class*).
"""

from __future__ import annotations

import json
import logging
import re

from ..config import InfraSshTarget
from ..tools.base import Tool
from ..tools.policy import ToolPermission

logger = logging.getLogger("infrastructure")

# ---------------------------------------------------------------------------
# Stable, non-echoing result codes (the only three a tool may return as an error)
# ---------------------------------------------------------------------------
CODE_UNAVAILABLE = "infra_unavailable"
CODE_INVALID_RESPONSE = "infra_invalid_response"
CODE_RESULT_TOO_LARGE = "infra_result_too_large"

# The three fixed observations, and the local-tool-name suffix for each.
_OBSERVATIONS = ("host_status", "disk_status", "service_status")
# A short human word for each observation, used in the description + approval line.
_KIND_WORD = {"host_status": "host", "disk_status": "disk", "service_status": "service"}

# Allowed systemd ``ActiveState`` values (a ``systemctl show --value`` single
# token). Anything else — empty, a stray newline, an unexpected string — is a
# parse failure, never echoed.
_SYSTEMD_STATES = frozenset(
    {"active", "inactive", "activating", "deactivating", "failed", "maintenance", "reloading", "unknown"}
)

_NONNEG_INT_RE = re.compile(r"^\d+$")
_NONNEG_FLOAT_RE = re.compile(r"^\d+(\.\d+)?$")
_PERCENT_RE = re.compile(r"^(\d{1,3})%$")


def local_tool_name(target_name: str, observation: str) -> str:
    """The stable namespaced local name for one observation on one target.

    ``infra_<target>__<observation>``. The prefix/separator make the two parts
    unambiguous and keep the local name a valid ``[A-Za-z0-9_-]+`` tool name
    (``infra_`` (6) + target (<=32) + ``__`` (2) + observation (<=14) << 128, the
    audit table's ``tool_name`` column width). The ``infra_`` prefix is disjoint
    from the MCP ``mcp_`` prefix and the built-in names, so it can never collide
    with them.
    """
    return f"infra_{target_name}__{observation}"


def _error(code: str) -> str:
    """A stable, model-facing error result — never echoes any detail.

    Always the fixed JSON shape ``{"error": "<code>"}``. No host, path, key,
    command, stdout/stderr, or exception text is ever included.
    """
    return json.dumps({"error": code}, separators=(",", ":"))


def _shquote(value: str) -> str:
    """Shell-single-quote ``value`` so it survives the remote ``sh -c`` verbatim.

    The only interpolated command values are the target's statically-validated
    ``mounts`` / ``services``. Wrapping each in single quotes (with the standard
    ``'\\''`` escape for an embedded single quote) means no shell metacharacter,
    glob, word-split, or command substitution can be injected — the value reaches
    the remote command byte-for-byte.
    """
    return "'" + value.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Fixed command templates (code constants). The remote target is Linux + systemd.
# Each emits a newline-separated, machine-parseable record format:
#   host    -> h|key=value ... records (a fixed, known key set)
#   disk    -> one d|mount=..|size_kb=..|used_kb=..|avail_kb=..|pcent=.. line per mount
#   service -> one s|service=..|state=.. line per service
# Every subcommand failure ``exit 1``s so a bad result is a non-zero exit, mapped
# to a stable code. ``2>/dev/null`` on a per-item command suppresses a stderr
# message for that item while still letting the ``[ -n ]`` check fail closed.
# ---------------------------------------------------------------------------
_HOST_COMMAND = (
    "set -u; "
    "os=$(uname -s) || exit 1; [ -n \"$os\" ] || exit 1; "
    "kernel=$(uname -r) || exit 1; [ -n \"$kernel\" ] || exit 1; "
    "arch=$(uname -m) || exit 1; [ -n \"$arch\" ] || exit 1; "
    "up=$(LC_ALL=C awk '{printf \"%d\", $1}' /proc/uptime) || exit 1; [ -n \"$up\" ] || exit 1; "
    "l1=$(LC_ALL=C awk '{print $1}' /proc/loadavg) || exit 1; [ -n \"$l1\" ] || exit 1; "
    "l5=$(LC_ALL=C awk '{print $2}' /proc/loadavg) || exit 1; [ -n \"$l5\" ] || exit 1; "
    "l15=$(LC_ALL=C awk '{print $3}' /proc/loadavg) || exit 1; [ -n \"$l15\" ] || exit 1; "
    "mt=$(LC_ALL=C awk '/^MemTotal:/{print $2}' /proc/meminfo) || exit 1; [ -n \"$mt\" ] || exit 1; "
    "ma=$(LC_ALL=C awk '/^MemAvailable:/{print $2}' /proc/meminfo) || exit 1; [ -n \"$ma\" ] || exit 1; "
    "st=$(LC_ALL=C awk '/^SwapTotal:/{print $2}' /proc/meminfo) || exit 1; [ -n \"$st\" ] || exit 1; "
    "sf=$(LC_ALL=C awk '/^SwapFree:/{print $2}' /proc/meminfo) || exit 1; [ -n \"$sf\" ] || exit 1; "
    "printf 'h|os=%s|kernel=%s|arch=%s\\n' \"$os\" \"$kernel\" \"$arch\"; "
    "printf 'h|uptime_seconds=%s|load1=%s|load5=%s|load15=%s\\n' \"$up\" \"$l1\" \"$l5\" \"$l15\"; "
    "printf 'h|mem_total_kb=%s|mem_available_kb=%s|swap_total_kb=%s|swap_free_kb=%s\\n' "
    "\"$mt\" \"$ma\" \"$st\" \"$sf\""
)

# One line per configured mount, in the configured order. ``df -kP`` is the
# portable (POSIX) form — ``-k`` reports 1K blocks and ``-P`` forces one line
# per filesystem (no wrap) — supported by both GNU and BusyBox ``df`` (the
# GNU-only ``--noheadings`` / ``--output`` are *not*). ``-P`` lays out the
# columns in a fixed order: <filesystem> <size> <used> <avail> <pcent>
# <mounted-on>, so the four numeric fields are ``$2 $3 $4 $5``. The ``awk``
# (POSIX, no ``-v``) keeps only data lines: the header (whose ``$2`` is the
# non-numeric ``1024-blocks``) and any wrapped continuation (whose ``$2`` is a
# word, not a number) are dropped. ``--`` guards the mount point; the loop body
# then reassigns the positional parameters (``set --``) to the four columns.
_DISK_LINE = (
    "  _row=$(LC_ALL=C df -kP -- \"$_m\" 2>/dev/null | "
    "awk '{ if ($2 ~ /^[0-9]+$/) print $2\" \"$3\" \"$4\" \"$5 }') || exit 1\n"
    "  [ -n \"$_row\" ] || exit 1\n"
    "  set -- $(printf '%s' \"$_row\")\n"
    "  [ \"$#\" = 4 ] || exit 1\n"
    "  printf 'd|mount=%s|size_kb=%s|used_kb=%s|avail_kb=%s|pcent=%s\\n' "
    "\"$_m\" \"$1\" \"$2\" \"$3\" \"$4\"\n"
)


def _disk_command(mounts: tuple[str, ...]) -> str:
    quoted = " ".join(_shquote(m) for m in mounts)
    return "set -u\nfor _m in " + quoted + ";\ndo\n" + _DISK_LINE + "done\n"


# One line per configured service, in the configured order. ``systemctl show
# --value -p ActiveState`` prints the single state token; a missing/unknown unit
# yields an empty value -> the ``[ -n ]`` check exits 1 (fail-closed).
_SERVICE_LINE = (
    "  _st=$(LC_ALL=C systemctl show -p ActiveState --value -- \"$_s\" 2>/dev/null) || exit 1\n"
    "  [ -n \"$_st\" ] || exit 1\n"
    "  printf 's|service=%s|state=%s\\n' \"$_s\" \"$_st\"\n"
)


def _service_command(services: tuple[str, ...]) -> str:
    quoted = " ".join(_shquote(s) for s in services)
    return "set -u\nfor _s in " + quoted + ";\ndo\n" + _SERVICE_LINE + "done\n"


# ---------------------------------------------------------------------------
# Parsers — strict: empty output, a missing field, a duplicate field, an illegal
# number, an extra/unexpected field, or the wrong set of records is a parse
# failure (never echoed; mapped to CODE_INVALID_RESPONSE by the tool).
# ---------------------------------------------------------------------------
class _ParseError(Exception):
    """Raised on any malformed infrastructure output (never surfaced)."""


# The exact key set the host command must emit (all unique across its records).
_HOST_KEYS = frozenset(
    {"os", "kernel", "arch", "uptime_seconds", "load1", "load5", "load15",
     "mem_total_kb", "mem_available_kb", "swap_total_kb", "swap_free_kb"}
)
_DISK_KEYS = frozenset({"mount", "size_kb", "used_kb", "avail_kb", "pcent"})
_SERVICE_KEYS = frozenset({"service", "state"})


def _lines(text: str) -> list[str]:
    """The non-empty lines of ``text``; empty/whitespace-only output -> []."""
    return [ln for ln in text.splitlines() if ln.strip() != ""]


def _split_record(line: str, prefix: str) -> dict[str, str]:
    """Split one ``<prefix>|key=value|key=value...`` record into a dict.

    Raises :class:`_ParseError` on a wrong line prefix, a field with no ``=``,
    or a duplicate key. The key *set* is not checked here — the caller validates
    it against the expected keys (so an unexpected field is still caught).
    """
    if not line.startswith(prefix + "|"):
        raise _ParseError("bad record prefix")
    record: dict[str, str] = {}
    for field in line[len(prefix) + 1:].split("|"):
        if "=" not in field:
            raise _ParseError("field without separator")
        key, _, value = field.partition("=")
        if key in record:
            raise _ParseError("duplicate field")
        record[key] = value
    return record


def _nonneg_int(value: str) -> int:
    if not _NONNEG_INT_RE.match(value):
        raise _ParseError("illegal integer")
    return int(value)


def _nonneg_float(value: str) -> float:
    if not _NONNEG_FLOAT_RE.match(value):
        raise _ParseError("illegal number")
    return float(value)


def _percent(value: str) -> int:
    match = _PERCENT_RE.match(value)
    if not match:
        raise _ParseError("illegal percentage")
    return int(match.group(1))


def _parse_host(text: str) -> dict[str, object]:
    values: dict[str, str] = {}
    for line in _lines(text):
        for key, value in _split_record(line, "h").items():
            if key not in _HOST_KEYS:
                raise _ParseError("unexpected field")
            if key in values:
                raise _ParseError("duplicate field")
            values[key] = value
    if set(values) != _HOST_KEYS:
        raise _ParseError("missing field")
    return {
        "os": values["os"],
        "kernel": values["kernel"],
        "arch": values["arch"],
        "uptime_seconds": _nonneg_int(values["uptime_seconds"]),
        "load_avg": {
            "1": _nonneg_float(values["load1"]),
            "5": _nonneg_float(values["load5"]),
            "15": _nonneg_float(values["load15"]),
        },
        "memory": {
            "total_kb": _nonneg_int(values["mem_total_kb"]),
            "available_kb": _nonneg_int(values["mem_available_kb"]),
        },
        "swap": {
            "total_kb": _nonneg_int(values["swap_total_kb"]),
            "free_kb": _nonneg_int(values["swap_free_kb"]),
        },
    }


def _parse_disk(text: str, mounts: tuple[str, ...]) -> dict[str, object]:
    expected = set(mounts)
    per_mount: dict[str, dict[str, object]] = {}
    for line in _lines(text):
        record = _split_record(line, "d")
        if set(record) != _DISK_KEYS:
            raise _ParseError("bad disk record")
        mount = record["mount"]
        if mount not in expected:
            raise _ParseError("unexpected mount")
        if mount in per_mount:
            raise _ParseError("duplicate mount")
        per_mount[mount] = {
            "size_kb": _nonneg_int(record["size_kb"]),
            "used_kb": _nonneg_int(record["used_kb"]),
            "avail_kb": _nonneg_int(record["avail_kb"]),
            "pcent": _percent(record["pcent"]),
        }
    if set(per_mount) != expected:
        raise _ParseError("missing mount")
    return {m: per_mount[m] for m in mounts}  # configured order


def _parse_service(text: str, services: tuple[str, ...]) -> dict[str, object]:
    expected = set(services)
    per_service: dict[str, str] = {}
    for line in _lines(text):
        record = _split_record(line, "s")
        if set(record) != _SERVICE_KEYS:
            raise _ParseError("bad service record")
        service = record["service"]
        if service not in expected:
            raise _ParseError("unexpected service")
        if service in per_service:
            raise _ParseError("duplicate service")
        if record["state"] not in _SYSTEMD_STATES:
            raise _ParseError("illegal state")
        per_service[service] = record["state"]
    if set(per_service) != expected:
        raise _ParseError("missing service")
    return {s: {"state": per_service[s]} for s in services}  # configured order


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------
class InfraTool(Tool):
    """A fixed, argument-free read-only tool for one observation on one target."""

    # Strictly read-only (fixed, argument-free, host-key-pinned, key-only) — like
    # the get_current_time / echo built-ins it runs without a per-call approval.
    default_permission = ToolPermission.ALLOW

    def __init__(
        self,
        *,
        target: InfraSshTarget,
        observation: str,
        connect_timeout_seconds: float,
        max_result_chars: int,
    ) -> None:
        if observation not in _OBSERVATIONS:
            raise ValueError(f"unknown infrastructure observation: {observation!r}")
        self._target = target
        self._observation = observation
        self._connect_timeout_seconds = connect_timeout_seconds
        self._max_result_chars = max_result_chars
        self._kind_word = _KIND_WORD[observation]
        self.name = local_tool_name(target.name, observation)

        # The fixed command is built once at construction (startup) from the
        # statically-validated, shell-quoted config values — no network, no I/O.
        if observation == "host_status":
            self._command = _HOST_COMMAND
        elif observation == "disk_status":
            self._command = _disk_command(target.mounts)
        else:  # service_status
            self._command = _service_command(target.services)

        # A short, non-instructional marker + the fixed purpose. The remote
        # endpoint (host/user/path) is deliberately NOT in the description — it
        # is secret-adjacent and belongs only in the process config.
        self.description = (
            f"(📡Infra) Read the {self._kind_word} status of the configured infrastructure "
            f"target '{target.name}'. Read-only; takes no arguments."
        )
        self.parameters: dict[str, object] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    def approval_summary(self, arguments: dict[str, object]) -> str:
        # No arguments (nothing to show in the card's Arguments block). A fixed,
        # purpose line naming only the target *name* — never the host, user, key
        # path, known_hosts path, mount, or service.
        return f"Read the {self._kind_word} status of infrastructure target '{self._target.name}' (read-only)."

    def _parse(self, text: str) -> dict[str, object]:
        if self._observation == "host_status":
            return _parse_host(text)
        if self._observation == "disk_status":
            return _parse_disk(text, self._target.mounts)
        return _parse_service(text, self._target.services)

    def _render(self, data: dict[str, object]) -> str:
        target_name = self._target.name
        if self._observation == "host_status":
            return json.dumps({"target": target_name, **data}, ensure_ascii=True, separators=(",", ":"))
        if self._observation == "disk_status":
            return json.dumps({"target": target_name, "mounts": data}, ensure_ascii=True, separators=(",", ":"))
        return json.dumps({"target": target_name, "services": data}, ensure_ascii=True, separators=(",", ":"))

    async def execute(self, arguments: dict[str, object]) -> str:
        """Connect once, run the fixed command, return a bounded normalised result.

        ``arguments`` is always ``{}`` (the schema forbids any property); it is
        intentionally ignored — the model has no way to influence the command.
        """
        del arguments  # no model input is ever used

        # --- connect (host-key-pinned, key-only) --------------------------------
        try:
            conn = await _connect(self._target, self._connect_timeout_seconds)
        except Exception as exc:
            # connect / auth / host-key / timeout / transport failure. Log only
            # the exception *class* and the tool name — never the exception text
            # (it may name the host or the key path) or any credential.
            _warn(self.name, CODE_UNAVAILABLE, exc)
            return _error(CODE_UNAVAILABLE)

        # --- run the fixed command, then always close the connection -----------
        proc = None
        try:
            proc = await conn.run(self._command, encoding="utf-8")
        except Exception as exc:
            # A hang here is cancelled by the loop's wait_for (CancelledError, a
            # BaseException, is *not* caught here and propagates); a genuine
            # transport/protocol error maps to a stable code.
            _warn(self.name, CODE_INVALID_RESPONSE, exc)
        finally:
            # ``close()`` is synchronous and idempotent; it runs even if ``run``
            # was cancelled, so a hung command never leaks a connection.
            conn.close()

        if proc is None:
            return _error(CODE_INVALID_RESPONSE)
        if getattr(proc, "returncode", None) != 0:
            return _error(CODE_INVALID_RESPONSE)
        if _to_text(getattr(proc, "stderr", "")).strip():
            return _error(CODE_INVALID_RESPONSE)

        # --- parse (strict) then bound -----------------------------------------
        stdout = _to_text(getattr(proc, "stdout", ""))
        try:
            data = self._parse(stdout)
        except Exception as exc:
            _warn(self.name, CODE_INVALID_RESPONSE, exc)
            return _error(CODE_INVALID_RESPONSE)

        result = self._render(data)
        if len(result) > self._max_result_chars:
            return _error(CODE_RESULT_TOO_LARGE)
        return result


def _warn(tool_name: str, code: str, exc: BaseException) -> None:
    """Log only the tool name, the stable code, and the exception *class*.

    The exception message is never logged — an asyncssh error can embed the host
    or key path, and a parse failure can embed a fragment of remote output.
    ``exc_info`` is deliberately *not* passed (a traceback would carry the text).
    """
    logger.warning(
        "infra tool failed",
        extra={"tool": tool_name, "code": code, "exception": type(exc).__name__},
    )


def _to_text(value: object) -> str:
    """Normalise a captured stream (str or bytes) to text without raising."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return ""


async def _connect(target: InfraSshTarget, connect_timeout_seconds: float):
    """Open one short-lived, host-key-pinned, key-only SSH connection.

    ``asyncssh`` is imported **lazily** so that with no targets (or tools
    disabled) the library is never loaded and no SSH machinery is initialised.

    The auth surface is deliberately locked down to the explicit private key:
    ``client_keys=[private_key_path]`` is the only credential, ``known_hosts``
    is the explicit pinned file (never ``None`` — no auto-accept), the SSH agent
    is disabled (``agent_path=""`` makes the agent lookup fail cleanly so no
    agent key is ever tried), and password / keyboard-interactive auth are off.
    Any failure (connect, auth, host-key mismatch, timeout) is raised to the
    caller, which maps it to :data:`CODE_UNAVAILABLE`.
    """
    import asyncssh  # lazy — see module docstring

    return await asyncssh.connect(
        target.host,
        target.port,
        username=target.username,
        client_keys=[target.private_key_path],
        known_hosts=target.known_hosts_path,
        agent_path="",
        public_key_auth=True,
        password_auth=False,
        kbdint_auth=False,
        connect_timeout=connect_timeout_seconds,
    )


def build_infra_tools(
    targets: tuple[InfraSshTarget, ...],
    *,
    connect_timeout_seconds: float,
    max_result_chars: int,
) -> list[InfraTool]:
    """The three fixed, argument-free tools for each target, in a stable order.

    Pure construction (no network / I/O) — safe to call at startup. The returned
    tools are registered into the *same* registry the built-ins and MCP tools use
    (after them), so each rides the entire phase-3 gate. A name collision is
    caught at registration time by the composition root.
    """
    tools: list[InfraTool] = []
    for target in targets:
        for observation in _OBSERVATIONS:
            tools.append(
                InfraTool(
                    target=target,
                    observation=observation,
                    connect_timeout_seconds=connect_timeout_seconds,
                    max_result_chars=max_result_chars,
                )
            )
    return tools
