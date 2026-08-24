"""Tool interface.

A :class:`Tool` is the unit the Tool Runtime can invoke. Each tool exposes an
OpenAI-``function``-style identity (``name`` / ``description`` / JSON-schema
``parameters``) and an async :meth:`execute` that takes a parsed ``arguments``
mapping and returns the string result fed back to the model.

Tools are pure value + behaviour: they hold no reference to Telegram, the
database, or the LLM client. That keeps the Tool Registry / Tool Runtime
channel- and provider-agnostic, and lets a future MCP/SSH/Docker tool be a
drop-in implementation of this same interface.
"""

from __future__ import annotations

import abc
from typing import Any


class Tool(abc.ABC):
    """A single invokable capability the Agent can use via tool calling."""

    #: Stable, model-facing tool name (must be unique within a registry).
    name: str

    #: One-line description of what the tool does; shown to the model.
    description: str

    #: JSON-Schema object describing the tool's arguments.
    parameters: dict[str, Any]

    @abc.abstractmethod
    async def execute(self, arguments: dict[str, Any]) -> str:
        """Run the tool with already-parsed ``arguments``.

        Returns the result as a *string* (that is what the OpenAI tool-call
        protocol feeds back to the model). Implementations should keep the
        return value short and human/model-readable; raise a plain
        :class:`Exception` on failure and let the Tool Runtime decide how to
        surface it (it converts it into a ``{"error": ...}`` result).
        """

    def spec(self) -> dict[str, Any]:
        """The inner ``function`` block of this tool's OpenAI schema.

        Returns ``{"name", "description", "parameters"}``. The registry wraps
        this in ``{"type": "function", "function": ...}`` — see
        :meth:`ToolRegistry.to_openai_schema`.
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
