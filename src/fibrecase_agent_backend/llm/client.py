"""OpenAI-compatible LLM client.

Wraps any OpenAI Chat-Completions-compatible endpoint (OpenAI itself, a local
model server, or a third-party relay) behind the OpenAI SDK. This is the only
module that knows about the OpenAI SDK and the wire format. It deliberately
wraps the raw SDK in a small, well-typed surface:

* ``complete(messages) -> LLMResult`` for the normal non-streaming path.
* Provider failures are translated into a single :class:`LLMError` with a
  stable ``category`` so callers (the Agent) can map them to user-safe,
  generic messages without ever seeing raw exceptions, keys, or headers.

Phase one uses non-streaming requests only; the ``stream`` flag is accepted
on the interface so a streaming implementation can be added without changing
callers.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

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
    """A single chat message in an OpenAI-compatible shape."""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class LLMResult:
    """The completed (non-streaming) LLM response."""

    content: str
    usage: Any | None = field(default=None)

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
    ) -> LLMResult:
        """Run a chat completion and return the assistant text.

        ``messages`` is expected to already include the system prompt at the
        front. Raises :class:`LLMError` on any provider failure.
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

        started = time.monotonic()
        logger.info("agent requesting llm", extra={"model": self.model, "messages": len(messages)})
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
        try:
            content = response.choices[0].message.content or ""
        except (AttributeError, IndexError, KeyError) as exc:
            logger.error("llm response had no usable content")
            raise LLMError("empty_response") from exc

        if not content.strip():
            logger.error("llm returned an empty response", extra={"latency_ms": elapsed_ms})
            raise LLMError("empty_response")

        logger.info("llm response received", extra={"latency_ms": elapsed_ms, "length": len(content)})
        return LLMResult(content=content, usage=getattr(response, "usage", None))

    async def aclose(self) -> None:
        """Release the underlying HTTP client. Awaits the async SDK close."""
        try:
            await self._client.close()
        except Exception:  # pragma: no cover - defensive
            logger.warning("error closing llm client", exc_info=True)
