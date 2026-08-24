"""Built-in tools.

``build_default_tools()`` is the single place that assembles the default tool
set. ``main`` calls it when ``ENABLE_TOOLS`` is on; add a new built-in here
(or pass your own registry for a custom set).
"""

from __future__ import annotations

from ..registry import ToolRegistry
from .echo import EchoTool
from .system_info import SystemInfoTool
from .time import GetCurrentTimeTool

__all__ = [
    "EchoTool",
    "GetCurrentTimeTool",
    "SystemInfoTool",
    "build_default_tools",
]


def build_default_tools() -> ToolRegistry:
    """Return a registry pre-loaded with the safe, read-only built-in tools."""
    return ToolRegistry().add(
        GetCurrentTimeTool(),
        EchoTool(),
        SystemInfoTool(),
    )
