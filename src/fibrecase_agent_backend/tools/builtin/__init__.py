"""Built-in tools.

``build_default_tools()`` is the single place that assembles the default tool
set. ``main`` calls it when ``ENABLE_TOOLS`` is on; add a new built-in here
(or pass your own registry for a custom set).

The three safe read-only tools (``get_current_time`` / ``echo`` /
``system_info``) are always included. Two **opt-in** capabilities are added
only when their flags are set (driven by their config knobs), so a default
deployment stays subprocess-free and touch-free: the ``exec`` shell tool
(``enable_exec``, from ``ENABLE_EXEC_TOOL``) and the ``file`` toolset
(``enable_file``, from ``ENABLE_FILE_TOOL`` — nine confined file/directory
tools: ``file_read`` / ``file_ls`` are read-only and ``allow``, the rest are
``ask``).
"""

from __future__ import annotations

from ..registry import ToolRegistry
from .echo import EchoTool
from .exec import ExecTool
from .file import build_file_tools
from .system_info import SystemInfoTool
from .time import GetCurrentTimeTool

__all__ = [
    "EchoTool",
    "ExecTool",
    "GetCurrentTimeTool",
    "SystemInfoTool",
    "build_default_tools",
    "build_file_tools",
]


def build_default_tools(
    *,
    enable_exec: bool = False,
    max_exec_output_chars: int = 8000,
    exec_workdir: str | None = None,
    exec_policy_deny_patterns: tuple[str, ...] = (),
    enable_file: bool = False,
    file_workdir: str | None = None,
    max_file_string_chars: int = 2000,
    max_file_read_chars: int = 8000,
    max_file_list_entries: int = 1000,
) -> ToolRegistry:
    """Return a registry pre-loaded with the built-in tools.

    The three safe read-only tools are always present. The two opt-in
    capabilities are added only when their ``enable_*`` flags are true; each one's
    knobs are passed straight through (``file`` last, so the three read-only
    built-ins always sort first).
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
    if enable_file:
        # ``file_workdir`` is validated (existing directory) by config when the
        # set is enabled, so this is never ``None`` on the real path.
        registry.add(
            *build_file_tools(
                workdir=file_workdir,
                max_string_chars=max_file_string_chars,
                max_read_chars=max_file_read_chars,
                max_list_entries=max_file_list_entries,
            )
        )
    return registry
