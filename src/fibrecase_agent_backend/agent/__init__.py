"""Agent package: the channel-agnostic agent service and context building."""

from .context import ChatMessage, build_context
from .service import AgentError, AgentService

__all__ = ["ChatMessage", "build_context", "AgentError", "AgentService"]
