"""Tool registry: register tools, validate args, emit the OpenAI schema, execute.

The registry is the *single* dispatch point for tool execution. It looks a tool
up by name — there is deliberately **no** ``if tool_name == ...`` branching
anywhere in the codebase. Adding a tool means implementing
:class:`.base.Tool` and calling :meth:`ToolRegistry.register`; nothing else
changes.

Phase 3 adds two responsibilities on top of dispatch:

* **Schema self-check** — :meth:`register` validates the tool's declared
  ``parameters`` as a JSON Schema *at registration time* (an invalid schema is a
  startup ``ValueError``, never a runtime surprise) and stores a
  ``Draft202012Validator`` per tool. :meth:`validate_arguments` is the
  single gate the loop calls before a tool is ever executed.
* **Safe execution** — :meth:`execute` converts a tool's own exception into a
  stable, non-echoing JSON error result (it logs only the tool name, the stable
  code, and the exception *class* — never the exception text or the arguments).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from jsonschema import Draft202012Validator

from .audit import RESULT_TOOL_EXECUTION_FAILED, error_result
from .base import Tool
from .policy import ToolPermission

logger = logging.getLogger("tools")


class ToolNotFoundError(KeyError):
    """Raised when the model requested a tool the registry does not know."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class ToolRegistry:
    """A named set of :class:`Tool` instances plus schema + OpenAI-schema generation."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._validators: dict[str, Draft202012Validator] = {}

    def register(self, tool: Tool) -> "ToolRegistry":
        """Add ``tool`` under its :attr:`Tool.name`. Returns ``self`` for chaining.

        Rejects an empty name, a duplicate name (no silent shadowing), and — new
        in phase 3 — an *invalid* JSON-Schema for ``tool.parameters`` (raised as a
        :class:`ValueError` at registration, so a broken schema is caught at
        startup, not mid-conversation).
        """
        if not tool.name:
            raise ValueError("tool.name must be a non-empty string")
        if tool.name in self._tools:
            # Refuse silent shadowing: a duplicate name would make the model's
            # choice ambiguous. Callers should register distinct tools.
            raise ValueError(f"duplicate tool name: {tool.name!r}")
        # Validate the declared parameter schema up front. ``check_schema``
        # validates the schema *against the JSON-Schema meta-schema*; the built
        # validator then validates *arguments* against it at call time.
        parameters = tool.parameters if isinstance(tool.parameters, dict) else {}
        try:
            Draft202012Validator.check_schema(parameters)
            self._validators[tool.name] = Draft202012Validator(parameters)
        except Exception as exc:  # SchemaError and any meta-schema problem
            raise ValueError(f"tool {tool.name!r} has an invalid JSON-Schema 'parameters': {type(exc).__name__}") from exc
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

    def default_permissions(self) -> dict[str, ToolPermission]:
        """Each registered tool's declared ``default_permission``."""
        return {name: getattr(tool, "default_permission", ToolPermission.ASK) for name, tool in self._tools.items()}

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def to_openai_schema(self, names: "list[str] | set[str] | frozenset[str] | None" = None) -> list[dict[str, Any]]:
        """Return the OpenAI ``tools`` list (optionally restricted to ``names``).

        Each entry is ``{"type": "function", "function": {name, description,
        parameters}}`` — exactly the shape the OpenAI chat-completions ``tools=``
        parameter expects. ``names`` (when given) is the advertised subset the
        policy has allowed; passing the policy's
        :meth:`ToolPolicy.advertised_names` is how ``deny`` tools are withheld.
        Empty when no (allowed) tools are registered.
        """
        allowed = set(names) if names is not None else None
        schema: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if allowed is not None and name not in allowed:
                continue
            schema.append({"type": "function", "function": tool.spec()})
        return schema

    def validate_arguments(self, name: str, arguments: "dict[str, Any] | None") -> bool:
        """Return ``True`` if ``arguments`` satisfies the tool's schema.

        ``None`` is treated as an empty mapping (a no-argument tool is valid).
        This is the single validation gate the loop runs before execution — it
        must never raise; any schema/instance problem is reported as ``False``.
        An unknown tool name (no validator) is ``False``.
        """
        validator = self._validators.get(name)
        if validator is None:
            return False
        return validator.is_valid(arguments if arguments is not None else {})

    async def execute(self, name: str, arguments: "dict[str, Any] | None") -> str:
        """Look up ``name`` and run it with ``arguments``; returns a string.

        Execution errors are converted into a *stable, non-echoing* JSON error
        result (``{"error": {"code", "message"}}``) rather than propagated, so a
        single failing tool is reported back to the model (which can recover)
        without aborting the loop. The log carries only the tool name, the
        stable code, and the exception **class** — never the exception text or
        the arguments. The caller (the tool loop) applies the per-tool timeout
        around :meth:`Tool.execute`; this method is the no-timeout convenience
        path and is also exercised directly by the unit tests.
        """
        tool = self.get(name)
        try:
            return await tool.execute(arguments if arguments is not None else {})
        except Exception as exc:  # noqa: BLE001 - a tool must never kill the loop
            logger.error(
                "tool execution failed",
                extra={"tool": name, "code": RESULT_TOOL_EXECUTION_FAILED, "exception": type(exc).__name__},
            )
            return error_result(RESULT_TOOL_EXECUTION_FAILED)
