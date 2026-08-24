"""Built-in tool: current date and time (no parameters)."""

from __future__ import annotations

from datetime import datetime

from ..base import Tool


class GetCurrentTimeTool(Tool):
    """Return the current local date and time of the machine running the agent."""

    name = "get_current_time"
    description = "Get the current local date and time (format: YYYY-MM-DD HH:MM:SS). Takes no arguments."
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, object]) -> str:
        # arguments are ignored on purpose: this tool takes no inputs.
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
