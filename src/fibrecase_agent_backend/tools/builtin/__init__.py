"""Built-in tools.

``build_default_tools()`` is the single place that assembles the default tool
set. ``main`` calls it when ``ENABLE_TOOLS`` is on; add a new built-in here
(or pass your own registry for a custom set).

The three safe read-only tools (``get_current_time`` / ``echo`` / ``system_info``)
are always included. Two state-changing tools are **opt-in** and added only when
their flags are set (driven by their config knobs), so a default deployment stays
subprocess-free and touch-free: the ``exec`` shell tool (``enable_exec``, from
``ENABLE_EXEC_TOOL``) and the ``edit`` file tool (``enable_edit``, from
``ENABLE_EDIT_TOOL``).
"""

from __future__ import annotations

from ..registry import ToolRegistry
from .echo import EchoTool
from .edit import EditTool
from .exec import ExecTool
from .system_info import SystemInfoTool
from .time import GetCurrentTimeTool

__all__ = [
    "EchoTool",
    "EditTool",
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
    enable_edit: bool = False,
    edit_workdir: str | None = None,
    max_edit_string_chars: int = 2000,
    max_edit_read_chars: int = 8000,
) -> ToolRegistry:
    """Return a registry pre-loaded with the built-in tools.

    The three safe read-only tools are always present. The two state-changing
    tools are added only when their ``enable_*`` flags are true; each tool's knobs
    are passed straight through to its constructor (``edit`` last).
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
    if enable_edit:
        registry.add(
            EditTool(
                workdir=edit_workdir,
                max_string_chars=max_edit_string_chars,
                max_read_chars=max_edit_read_chars,
            )
        )
    return registry
