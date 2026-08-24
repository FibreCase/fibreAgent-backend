"""Built-in tool: echo a message back (minimal, always-successful)."""

from __future__ import annotations

from ..base import Tool


class EchoTool(Tool):
    """Return the input message verbatim. Useful as a smoke-test / round-trip tool."""

    name = "echo"
    description = "Echo the given message back exactly as provided. Takes one argument: message."
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

    async def execute(self, arguments: dict[str, object]) -> str:
        message = arguments.get("message", "")
        return str(message)
