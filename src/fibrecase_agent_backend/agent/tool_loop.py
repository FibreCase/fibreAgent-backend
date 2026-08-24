"""The tool-calling loop.

This is the piece inserted between the Agent service and the LLM client in
phase two. It drives an OpenAI-style tool-calling exchange:

    call LLM ─▶ model asks for tools?
                  ├─ no  → return the final text answer
                  └─ yes → append the assistant tool-call message,
                           run each tool via the registry, append ``tool``
                           result messages, and call the LLM again

…until the model returns a message with **no** tool calls (the final answer),
or the iteration budget is exhausted.

It depends only on:

* an LLM that can call ``complete(messages, *, tools=...)`` — the OpenAI
  client, or a test fake. It does not know the OpenAI SDK or any endpoint.
* a :class:`~fibrecase_agent_backend.tools.registry.ToolRegistry` — the single
  dispatch point. There is no ``if name == ...`` branching here.
* :class:`~fibrecase_agent_backend.agent.context.ChatMessage` for message shape.

Nothing in this module touches Telegram or the database.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol, runtime_checkable

from ..tools.registry import ToolNotFoundError, ToolRegistry
from .context import ChatMessage

logger = logging.getLogger("agent.tools")


class ToolLoopLimitError(Exception):
    """Raised when the loop hits ``max_iterations`` without a final text answer."""

    def __init__(self, max_iterations: int) -> None:
        super().__init__(f"tool loop reached its limit of {max_iterations} iterations without a final answer")
        self.max_iterations = max_iterations


@runtime_checkable
class ToolCallingLLM(Protocol):
    """Structural type: any object that can complete a call accepting ``tools``.

    This keeps the loop decoupled from the concrete OpenAI client so it can be
    driven by a lightweight fake in tests (see ``FakeToolLLM``).
    """

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        ...


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Parse the model's ``function.arguments`` into a dict.

    OpenAI sends ``arguments`` as a JSON *string*; some relays send a dict.
    We accept both and degrade to ``{}`` on anything malformed rather than
    crashing the loop — a tool with no (or wrong) args will return an
    ``{"error": ...}`` result the model can recover from.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("tool arguments were not valid JSON; using empty arguments")
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def run_tool_loop(
    llm: ToolCallingLLM,
    messages: list[ChatMessage],
    registry: ToolRegistry | None = None,
    max_iterations: int = 5,
) -> Any:
    """Run the model + tools until a final text answer is produced.

    Returns the final :class:`LLMResult` (its ``.text`` is the assistant reply
    to persist and send to the user).

    Behaviour:

    * If ``registry`` is ``None`` or has no tools, this is exactly one LLM call
      with no ``tools`` argument — byte-for-byte the phase-one path.
    * Otherwise, the model is called at most ``max_iterations`` times. Each
      time it returns tool calls we execute them (in order) via the registry
      and feed the results back. When it returns a message with no tool calls,
      that message is the final answer and is returned.
    * If the budget is used up and the model still wants tools, a
      :class:`ToolLoopLimitError` is raised (logged) so the Agent service can
      translate it into a generic user-safe message.
    """
    tools: list[dict[str, Any]] | None = None
    if registry is not None:
        schema = registry.to_openai_schema()
        tools = schema or None

    if tools is None:
        # No tools available: a single completion, no loop.
        return await llm.complete(messages)

    working = list(messages)
    for iteration in range(1, max_iterations + 1):
        result = await llm.complete(working, tools=tools)

        # A message with no tool calls is the final answer.
        if not getattr(result, "tool_calls", None):
            return result

        # Record the model's tool-call turn so the results map back to it.
        working.append(
            ChatMessage(
                role="assistant",
                content=result.text,
                tool_calls=result.tool_calls,
            )
        )

        # Budget exhausted with no final answer: stop before executing tools
        # whose results we could never feed back.
        if iteration == max_iterations:
            logger.error(
                "tool loop reached its limit without a final answer",
                extra={"max_iterations": max_iterations, "iteration": iteration},
            )
            raise ToolLoopLimitError(max_iterations)

        for tool_call in result.tool_calls:
            function = tool_call.get("function", {}) or {}
            name = function.get("name", "")
            arguments = _parse_arguments(function.get("arguments"))
            logger.info("tool requested: %s", name, extra={"iteration": iteration})

            started = time.monotonic()
            # registry.execute() converts a tool's own exception into an
            # {"error": ...} result; an unknown tool name is handled here so the
            # model gets a readable error instead of a crash.
            try:
                output = await registry.execute(name, arguments)
            except ToolNotFoundError:
                logger.warning("tool requested but not registered: %s", name)
                output = json.dumps({"error": f"unknown tool: {name}"})
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info("tool completed: %s latency=%dms", name, elapsed_ms)

            working.append(
                ChatMessage(role="tool", content=output, tool_call_id=tool_call.get("id", ""))
            )

    # Unreachable: the loop either returns a final answer or raises above.
    raise ToolLoopLimitError(max_iterations)  # pragma: no cover
