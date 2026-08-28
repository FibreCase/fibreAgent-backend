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

from .policy import ToolPermission


class Tool(abc.ABC):
    """A single invokable capability the Agent can use via tool calling."""

    #: Stable, model-facing tool name (must be unique within a registry).
    name: str

    #: One-line description of what the tool does; shown to the model.
    description: str

    #: JSON-Schema object describing the tool's arguments.
    parameters: dict[str, Any]

    #: The tool's *declared* default permission, before any override.
    #:
    #: The base default is :attr:`ToolPermission.ASK` — a new tool can never run
    #: bare by accident. A tool that is genuinely safe (the three read-only
    #: built-ins) declares :attr:`ToolPermission.ALLOW` explicitly so it does not
    #: annoy the owner with an approval prompt on every call. For MCP tools, a
    #: pin in ``MCP_PERMISSIONS_FILE`` still wins over this declaration; the
    #: built-ins are not in that file, so their declared default is final.
    default_permission: ToolPermission = ToolPermission.ASK

    @abc.abstractmethod
    async def execute(self, arguments: dict[str, Any]) -> str:
        """Run the tool with already-parsed ``arguments``.

        Returns the result as a *string* (that is what the OpenAI tool-call
        protocol feeds back to the model). Implementations should keep the
        return value short and human/model-readable; raise a plain
        :class:`Exception` on failure and let the Tool Runtime decide how to
        surface it (it converts it into a safe JSON ``{"error": ...}`` result).
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

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        """A safe, human-readable summary of a pending call, for approval.

        Shown on the approval card under a ``What it does:`` line. It should
        describe the tool's **purpose** so the human can judge the capability.
        The tool's **arguments** are shown separately on the card (a readable
        "Arguments:" block the approval provider renders from the
        already-validated ``arguments``) — so this summary does **not** need to
        include them and normally does not. The built-ins and the MCP ``McpTool``
        override this with a purpose line; the **default here** only names the
        tool (a tool with a richer, reviewed, secret-free purpose line should
        override it).
        """
        return f"Run the {self.name} tool."

    def approval_detail(self, arguments: dict[str, Any]) -> str | None:
        """An optional, human-friendly view of a pending call's arguments.

        Shown on the approval card **in place of** the generic readable-JSON
        "Arguments:" block, so a tool whose raw arguments are cold to read (e.g.
        ``edit``'s ``old_string`` / ``new_string``) can present them as a clear,
        structured diff. It returns **plain text** — *no* markup: the approval
        provider HTML-escapes and length-bounds it exactly like the summary, so a
        value containing markup can't inject a tag and the card can't overflow
        Telegram's message limit. The default is ``None``: the provider then
        renders the arguments as the generic pretty-JSON block (the behaviour for
        every tool that does not override this).

        Two rules, same spirit as :meth:`approval_summary`: keep it **faithful** —
        show the *real* argument values (whitespace / newlines included), never a
        lossy paraphrase — so the owner approves exactly what will run; and keep
        it **secret-free in the log** — the provider only ever puts it on the
        owner-only card, never in logs, the audit table, or model-facing text.
        """
        return None

