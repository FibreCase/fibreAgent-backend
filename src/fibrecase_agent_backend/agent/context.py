"""Conversation context building.

Deliberately simple: the system prompt plus the most recent N *messages*
(N is a message count, not a token budget). Phase one has no token counting,
but the single ``build_context`` function is the one place to change when a
token-based strategy is introduced later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChatMessage:
    """A single chat message in an OpenAI-compatible shape.

    ``tool_calls`` / ``tool_call_id`` are only populated on the assistant and
    tool turns created by the tool loop (:mod:`.tool_loop`); they stay ``None``
    for every phase-one (chat-only) message, so ``to_dict()`` output — and the
    messages persisted to the database — are unchanged there.
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


def build_context(
    system_prompt: str,
    history: list[ChatMessage],
    max_messages: int = 50,
) -> list[ChatMessage]:
    """Return the messages to send to the model.

    Layout: ``[system, ...recent history...``]``.

    * ``system_prompt`` is always pinned to the front.
    * ``history`` is the stored messages *without* a system turn (we rebuild
      the system prompt fresh each turn rather than trusting stored rows).
    * At most ``max_messages`` history messages are kept, taken from the
      most recent end, preserving chronological order.

    ``max_messages`` is a message count, not a token count.
    """
    recent = history[-max_messages:] if max_messages > 0 else []
    return [ChatMessage(role="system", content=system_prompt), *recent]
