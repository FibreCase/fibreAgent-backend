"""Phase 5.1 — read-only infrastructure observation over SSH.

This package exposes, per operator-configured SSH :class:`~..config.InfraSshTarget`,
three fixed, argument-free, read-only :class:`~..tools.base.Tool` instances
(host / disk / service status). Each is a *local* tool (like the built-ins) that
declares :attr:`ToolPermission.ALLOW` — strictly read-only, so like
``get_current_time`` / ``echo`` it runs **without** a per-call approval — and
rides the entire phase-3 tool gate (policy → schema validation → fail-closed
pre-audit → per-call timeout → terminal audit; the one-time-approval step is
skipped because the tool is ``allow``). The model can never steer a host, path,
service, or command: the tools take no arguments and the remote command is a
code constant built only from statically-validated config.

The module deliberately **lazy-imports** ``asyncssh`` (only inside
:meth:`InfraTool.execute`, when a call actually connects) so that
with no targets — or ``ENABLE_TOOLS=false`` — the SSH library is never loaded and
no connection is ever opened. Startup performs no SSH/network probe; it only
validates the local config and credential files.

This package knows nothing about Telegram, the database, the OpenAI SDK, the
``AgentService``, or the MCP provider — only the ``Tool`` interface, the frozen
config target type, and (lazily) ``asyncssh``.
"""

from __future__ import annotations

from .provider import (
    CODE_INVALID_RESPONSE,
    CODE_RESULT_TOO_LARGE,
    CODE_UNAVAILABLE,
    InfraTool,
    build_infra_tools,
    local_tool_name,
)

__all__ = [
    "InfraTool",
    "build_infra_tools",
    "local_tool_name",
    "CODE_UNAVAILABLE",
    "CODE_INVALID_RESPONSE",
    "CODE_RESULT_TOO_LARGE",
]
