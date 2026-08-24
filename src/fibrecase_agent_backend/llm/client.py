"""OpenAI-compatible LLM client.

Wraps any OpenAI Chat-Completions-compatible endpoint (OpenAI itself, a local
model server, or a third-party relay) behind the OpenAI SDK. This is the only
module that knows about the OpenAI SDK and the wire format. It deliberately
wraps the raw SDK in a small, well-typed surface:

* ``complete(messages) -> LLMResult`` for the normal non-streaming path.
* Provider failures are translated into a single :class:`LLMError` with a
  stable ``category`` so callers (the Agent) can map them to user-safe,
  generic messages without ever seeing raw exceptions, keys, or headers.

``complete`` accepts an optional ``tools=`` argument (a list of OpenAI tool
schemas) and surfaces the model's ``tool_calls`` on the returned
:class:`LLMResult`. Driving those calls to completion is *not* the client's
job — that loop lives in :mod:`..agent.tool_loop`. This module still knows
only the OpenAI protocol, never which tools exist or how to run them.

Requests are non-streaming; the ``stream`` flag is accepted on the interface
so a streaming implementation can be added without changing callers.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
)

logger = logging.getLogger("llm")


@dataclass(frozen=True)
class ChatMessage:
    """A single chat message in an OpenAI-compatible shape.

    ``tool_calls`` and ``tool_call_id`` are only populated on assistant / tool
    messages during a tool-calling loop; they stay ``None`` on every
    phase-one (chat-only) message, so ``to_dict()`` output is unchanged there.
    """

    role: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls is not None:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        return d


def _normalize_tool_call(tc: Any) -> dict[str, Any]:
    """Coerce one tool-call into the canonical OpenAI dict shape.

    Accepts either the SDK's ``ChatCompletionMessageToolCall`` object (with
    ``.id`` / ``.function.name`` / ``.function.arguments``) or an already-plain
    dict (as produced by test fakes). ``function`` always ends up as
    ``{"name", "arguments"}`` with an ``arguments`` key present.
    """
    if isinstance(tc, Mapping):
        func = tc.get("function") or {}
        return {
            "id": tc.get("id", ""),
            "type": tc.get("type", "function"),
            "function": {
                "name": func.get("name", ""),
                "arguments": func.get("arguments", "") or "",
            },
        }
    # SDK object form.
    function = getattr(tc, "function", None)
    name = getattr(function, "name", "") or "" if function is not None else ""
    arguments = getattr(function, "arguments", "") or "" if function is not None else ""
    return {
        "id": getattr(tc, "id", "") or "",
        "type": getattr(tc, "type", "function") or "function",
        "function": {"name": name, "arguments": arguments},
    }


@dataclass
class LLMResult:
    """The completed (non-streaming) LLM response.

    ``tool_calls`` is populated only when the model requested tool calls (its
    ``content`` is then typically ``None``); it is ``None`` for a normal text
    answer.
    """

    content: str
    usage: Any | None = field(default=None)
    tool_calls: list[dict[str, Any]] | None = field(default=None)

    @property
    def text(self) -> str:
        return self.content or ""


class LLMError(Exception):
    """Provider failure, translated into a stable category for upstream use."""

    def __init__(self, category: str, message: str = "") -> None:
        super().__init__(message or category)
        self.category = category  # one of: timeout, connection, http_error, error, empty_response


def _safe(exc: BaseException, limit: int = 300) -> str:
    """A short, non-sensitive one-line description of an exception.

    We log the message but never request headers (which would carry the
    ``Authorization`` bearer token).
    """
    return str(exc)[:limit]


class OpenAIClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        """Run a chat completion and return the assistant text (or tool calls).

        ``messages`` is expected to already include the system prompt at the
        front. When ``tools`` is provided (a list of OpenAI tool schemas) it is
        passed through to the provider as ``tools=``; the model may then reply
        with ``tool_calls`` instead of (or before) a text answer. Raises
        :class:`LLMError` on any provider failure.
        """
        if stream:
            # Accepted on the interface for the future; not implemented yet.
            raise NotImplementedError("streaming is not implemented in phase one")

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "stream": False,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            # Only send the argument when there is something to send; a
            # provider that predates tools may reject an empty ``tools=[]``.
            kwargs["tools"] = tools

        started = time.monotonic()
        logger.info(
            "agent requesting llm",
            extra={
                "model": self.model,
                "messages": len(messages),
                "tools": len(tools) if tools else 0,
            },
        )
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except APITimeoutError as exc:
            logger.error("llm request timed out: %s", _safe(exc))
            raise LLMError("timeout") from exc
        except APIStatusError as exc:
            # Log the status code only; the body may contain provider-side
            # details we do not want to persist verbatim.
            logger.error("llm http error status=%s", getattr(exc, "status_code", None))
            raise LLMError("http_error", f"status={getattr(exc, 'status_code', None)}") from exc
        except APIConnectionError as exc:
            logger.error("llm connection error: %s", _safe(exc))
            raise LLMError("connection") from exc
        except OpenAIError as exc:
            logger.error("llm request failed: %s", _safe(exc))
            raise LLMError("error", _safe(exc)) from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        choice = getattr(response, "choices", [None])[0]
        message = getattr(choice, "message", None) if choice is not None else None
        raw_tool_calls = getattr(message, "tool_calls", None) if message is not None else None
        tool_calls = (
            [_normalize_tool_call(tc) for tc in raw_tool_calls]
            if raw_tool_calls
            else None
        )

        try:
            content = (getattr(message, "content", None) or "") if message is not None else ""
        except (AttributeError, IndexError, KeyError) as exc:
            # No usable content — but a tool-call turn legitimately has no
            # text, so only treat a truly empty response as an error.
            if not tool_calls:
                logger.error("llm response had no usable content")
                raise LLMError("empty_response") from exc
            content = ""

        if not content.strip() and not tool_calls:
            logger.error("llm returned an empty response", extra={"latency_ms": elapsed_ms})
            raise LLMError("empty_response")

        logger.info(
            "llm response received",
            extra={
                "latency_ms": elapsed_ms,
                "length": len(content),
                "tool_calls": len(tool_calls) if tool_calls else 0,
            },
        )
        return LLMResult(content=content, usage=getattr(response, "usage", None), tool_calls=tool_calls)

    async def aclose(self) -> None:
        """Release the underlying HTTP client. Awaits the async SDK close."""
        try:
            await self._client.close()
        except Exception:  # pragma: no cover - defensive
            logger.warning("error closing llm client", exc_info=True)
