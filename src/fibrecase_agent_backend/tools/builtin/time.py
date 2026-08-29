"""Built-in tool: current date and time (no parameters)."""

from __future__ import annotations

from datetime import datetime

from ..base import Tool
from ..policy import ToolPermission


class GetCurrentTimeTool(Tool):
    """Return the current local date and time of the machine running the agent."""

    name = "get_current_time"
    description = (
        "Get the current local date and time, including its timezone offset. "
        "Returns an ISO-8601 local timestamp (format: YYYY-MM-DD HH:MM:SS+HH:MM). "
        "Takes no arguments."
    )
    # Safe, read-only: no arguments, no side effects — allow without approval.
    default_permission = ToolPermission.ALLOW
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def approval_summary(self, arguments: dict[str, object]) -> str:
        # No arguments, no side effects: a fixed purpose line fully describes it.
        return "Get the current local date and time, with its timezone offset, of the machine running the agent."

    async def execute(self, arguments: dict[str, object]) -> str:
        # arguments are ignored on purpose: this tool takes no inputs.
        # ``astimezone()`` attaches the machine's local timezone so the returned
        # value carries a UTC offset (e.g. "+08:00") — an offset-less local
        # timestamp is ambiguous without it.
        return datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")
