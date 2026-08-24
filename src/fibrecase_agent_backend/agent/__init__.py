"""Agent package: the channel-agnostic agent service, context, and tool loop."""

from .context import ChatMessage, build_context
from .service import AgentError, AgentService
from .tool_loop import ToolLoopLimitError, run_tool_loop

__all__ = [
    "ChatMessage",
    "build_context",
    "AgentError",
    "AgentService",
    "ToolLoopLimitError",
    "run_tool_loop",
]
