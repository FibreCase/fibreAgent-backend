"""Tool registry: register tools, emit the OpenAI tools schema, execute by name.

The registry is the *single* dispatch point for tool execution. It looks a tool
up by name and calls it — there is deliberately **no** ``if tool_name == ...``
branching anywhere in the codebase. Adding a tool means implementing
:class:`.base.Tool` and calling :meth:`ToolRegistry.register`; nothing else
changes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import Tool

logger = logging.getLogger("tools")


class ToolNotFoundError(KeyError):
    """Raised when the model requested a tool the registry does not know."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class ToolRegistry:
    """A named set of :class:`Tool` instances plus OpenAI schema generation."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> "ToolRegistry":
        """Add ``tool`` under its :attr:`Tool.name`. Returns ``self`` for chaining."""
        if not tool.name:
            raise ValueError("tool.name must be a non-empty string")
        if tool.name in self._tools:
            # Refuse silent shadowing: a duplicate name would make the model's
            # choice ambiguous. Callers should register distinct tools.
            raise ValueError(f"duplicate tool name: {tool.name!r}")
        self._tools[tool.name] = tool
        return self

    def add(self, *tools: Tool) -> "ToolRegistry":
        """Register several tools at once; returns ``self`` for chaining."""
        for tool in tools:
            self.register(tool)
        return self

    def get(self, name: str) -> Tool:
        """Return the tool named ``name`` or raise :class:`ToolNotFoundError`."""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(name) from None

    def names(self) -> list[str]:
        """The registered tool names, in registration order."""
        return list(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def to_openai_schema(self) -> list[dict[str, Any]]:
        """Return the OpenAI ``tools`` list.

        Each entry is ``{"type": "function", "function": {name, description,
        parameters}}`` — exactly the shape the OpenAI chat-completions
        ``tools=`` parameter expects. Empty when no tools are registered.
        """
        return [
            {"type": "function", "function": tool.spec()}
            for tool in self._tools.values()
        ]

    async def execute(self, name: str, arguments: dict[str, Any] | None) -> str:
        """Look up ``name`` and run it with ``arguments``; returns a string.

        Execution errors are converted into a JSON ``{"error": ...}`` result
        rather than propagated, so a single failing tool can be reported back
        to the model (which can then apologise or recover) without aborting
        the whole tool loop. The underlying exception is logged by the caller.
        """
        tool = self.get(name)
        try:
            return await tool.execute(arguments or {})
        except Exception as exc:  # noqa: BLE001 - a tool must never kill the loop
            logger.error(
                "tool execution failed", extra={"tool": name, "error": str(exc)[:300]}
            )
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
