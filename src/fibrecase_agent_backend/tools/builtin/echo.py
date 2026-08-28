"""Built-in tool: echo a message back (minimal, always-successful)."""

from __future__ import annotations

from ..base import Tool
from ..policy import ToolPermission


class EchoTool(Tool):
    """Return the input message verbatim. Useful as a smoke-test / round-trip tool."""

    name = "echo"
    description = "Echo the given message back exactly as provided. Takes one argument: message."
    # Safe, read-only (returns exactly what it was given) — allow without approval.
    default_permission = ToolPermission.ALLOW
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The text to echo back.",
            },
        },
        "required": ["message"],
        "additionalProperties": False,
    }

    def approval_summary(self, arguments: dict[str, object]) -> str:
        # A fixed purpose line. The echo argument is the user's own input and
        # could be anything (incl. text that looks like secrets), so it is NOT
        # shown here — only what the tool does.
        return "Echo a message back to the conversation, verbatim."

    async def execute(self, arguments: dict[str, object]) -> str:
        message = arguments.get("message", "")
        return str(message)
