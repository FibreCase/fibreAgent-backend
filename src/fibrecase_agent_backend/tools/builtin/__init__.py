"""Built-in tools.

``build_default_tools()`` is the single place that assembles the default tool
set. ``main`` calls it when ``ENABLE_TOOLS`` is on; add a new built-in here
(or pass your own registry for a custom set).

The three safe read-only tools (``get_current_time`` / ``echo`` / ``system_info``)
are always included. The ``exec`` shell tool is **opt-in** — it is added only
when ``enable_exec=True`` (driven by ``ENABLE_EXEC_TOOL``), so a default
deployment stays subprocess-free.
"""

from __future__ import annotations

from ..registry import ToolRegistry
from .echo import EchoTool
from .exec import ExecTool
from .system_info import SystemInfoTool
from .time import GetCurrentTimeTool

__all__ = [
    "EchoTool",
    "ExecTool",
    "GetCurrentTimeTool",
    "SystemInfoTool",
    "build_default_tools",
]


def build_default_tools(
    *,
    enable_exec: bool = False,
    max_exec_output_chars: int = 8000,
    exec_workdir: str | None = None,
    exec_policy_deny_patterns: tuple[str, ...] = (),
) -> ToolRegistry:
    """Return a registry pre-loaded with the built-in tools.

    The three safe read-only tools are always present. The ``exec`` shell tool
    is added only when ``enable_exec`` is true; its three knobs (output cap,
    fixed working directory, add-only deny patterns) are passed straight through
    to :class:`ExecTool`.
    """
    registry = ToolRegistry().add(
        GetCurrentTimeTool(),
        EchoTool(),
        SystemInfoTool(),
    )
    if enable_exec:
        registry.add(
            ExecTool(
                max_output_chars=max_exec_output_chars,
                workdir=exec_workdir,
                policy_deny_patterns=exec_policy_deny_patterns,
            )
        )
    return registry
