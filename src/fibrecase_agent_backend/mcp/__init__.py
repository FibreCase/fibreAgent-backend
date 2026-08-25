"""Remote MCP tool provider (phase 4 — Streamable HTTP).

A channel-, agent-, and ORM-free package that turns operator-configured remote
Model Context Protocol servers into ordinary :class:`..tools.base.Tool` objects.
At startup :class:`McpManager` connects to each configured server (Streamable
HTTP transport), initialises a session, and discovers its tools; each discovered
tool becomes an :class:`McpTool` — a standard tool that defaults to
``ask`` and whose ``execute`` forwards to the server's ``call_tool``.

The composition root (:mod:`..main`) adds the discovered tools to the *same*
:class:`..tools.registry.ToolRegistry` as the built-ins, so every MCP tool
passes through the **existing phase-3 gate** (policy → schema validation →
approval → timeout → audit) exactly like a built-in. Nothing here imports
Telegram, the OpenAI SDK, SQLAlchemy, or :class:`..agent.service.AgentService`;
the only external dependencies are the MCP SDK and its HTTP client.

The remote endpoint and bearer token come **only** from strict startup config
(:mod:`..config`) — they are never controllable by the model, chat input,
memory, or a tool argument, and are never logged (only the server *name* and a
stable code are).
"""

from __future__ import annotations

from .manager import (
    CODE_CONNECT_FAILED,
    CODE_DISCOVERY_FAILED,
    CODE_INITIALIZE_FAILED,
    CODE_INVALID_TOOL,
    McpManager,
)
from .wrapper import McpTool, is_valid_remote_tool_name, local_tool_name

__all__ = [
    "McpManager",
    "McpTool",
    "local_tool_name",
    "is_valid_remote_tool_name",
    "CODE_CONNECT_FAILED",
    "CODE_INITIALIZE_FAILED",
    "CODE_DISCOVERY_FAILED",
    "CODE_INVALID_TOOL",
]
