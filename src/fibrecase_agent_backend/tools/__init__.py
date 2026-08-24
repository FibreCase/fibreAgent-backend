"""Tool runtime support: the Tool interface, the registry, and built-ins.

This package is intentionally provider- and channel-agnostic. It knows nothing
about Telegram, the database, or the OpenAI SDK — only that a tool has a
name/description/JSON-schema and an async ``execute``. The agent's tool loop
(:mod:`..agent.tool_loop`) is what drives these through the LLM.
"""

from __future__ import annotations

from .base import Tool
from .builtin import build_default_tools
from .registry import ToolNotFoundError, ToolRegistry

__all__ = ["Tool", "ToolRegistry", "ToolNotFoundError", "build_default_tools"]
