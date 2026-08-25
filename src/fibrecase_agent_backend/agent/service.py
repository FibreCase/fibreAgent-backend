"""The Agent service — the reusable core of the backend.

The service is intentionally channel-agnostic. A Telegram message, a future
web UI, Discord, or an HTTP API all call :meth:`AgentService.process_message`
and get back a reply. It never talks to Telegram or the OpenAI SDK directly;
it depends only on the repository (persistence) and the LLM client.

Responsibilities, per message:

1. acquire the per-conversation lock (serialise one conversation, parallelise
   across conversations),
2. load history,
3. append + persist the user message,
4. build context (system prompt + recent window),
5. call the LLM — via the tool loop when tools are enabled, or a single
   completion when they are not (phase-one behaviour),
6. persist the assistant reply,
7. return the reply text.

Only the user turn and the *final* assistant turn are persisted; the
intermediate tool-call / tool-result turns of the loop are not stored, so the
conversation schema is unchanged from phase one.

Provider failures are translated into a :class:`AgentError` whose ``user_safe``
message is generic and safe to show to a user, while the structured cause is
logged for operators.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from ..database.repository import ConversationRepository, MessageRecord
from ..llm.client import LLMError, OpenAIClient
from ..llm.message_converter import agent_message_to_openai_content
from ..tools.registry import ToolRegistry
from .context import ChatMessage, build_context
from .messages import AgentMessage, TextContent
from .tool_loop import ToolLoopLimitError, run_tool_loop

logger = logging.getLogger("agent")

# Stable, user-safe replies per failure category. None of these leak
# internal details (stack traces, keys, headers, paths).
_USER_SAFE: dict[str, str] = {
    "timeout": "模型请求超时，请稍后重试。",
    "http_error": "模型服务暂时不可用。",
    "connection": "无法连接模型服务，请稍后重试。",
    "empty_response": "模型服务暂时不可用。",
    "tool_limit": "处理该请求时工具调用次数过多，请重新尝试。",
    "error": "模型服务暂时不可用。",
}
_DEFAULT_USER_SAFE = "模型服务暂时不可用。"


class AgentError(Exception):
    """A failure while processing a message, safe to surface to the user."""

    def __init__(self, user_safe: str, category: str = "error") -> None:
        super().__init__(user_safe)
        self.user_safe = user_safe
        self.category = category


def _user_safe_for(category: str) -> str:
    return _USER_SAFE.get(category, _DEFAULT_USER_SAFE)


class AgentService:
    def __init__(
        self,
        repository: ConversationRepository,
        llm: OpenAIClient,
        *,
        system_prompt: str,
        max_context_messages: int = 50,
        registry: ToolRegistry | None = None,
        enable_tools: bool = False,
        max_tool_iterations: int = 5,
    ) -> None:
        self._repo = repository
        self._llm = llm
        self._system_prompt = system_prompt
        self._max_context_messages = max_context_messages
        # Tools are opt-in: when disabled (or no registry is supplied) the
        # service behaves exactly as in phase one — a single LLM call, no tools.
        self._enable_tools = enable_tools
        self._registry = registry
        self._max_tool_iterations = max_tool_iterations
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------ locks
    def _lock_for(self, conversation_id: int) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    @asynccontextmanager
    async def conversation_lock(self, conversation_id: int):
        """Context manager that serialises work for one conversation only."""
        lock = self._lock_for(conversation_id)
        async with lock:
            yield

    # ----------------------------------------------------------------- status
    async def conversation_status(self, conversation_id: int) -> dict[str, object]:
        return {
            "conversation_id": conversation_id,
            "messages": await self._repo.count_messages(conversation_id),
        }

    async def reset(self, telegram_chat_id: int, telegram_user_id: int) -> int:
        """Reset a chat's conversation, returning the new conversation id."""
        conversation = await self._repo.reset_conversation(telegram_chat_id, telegram_user_id)
        return conversation.id

    # ------------------------------------------------------------ process
    async def process_message(self, conversation_id: int, user_message: str | AgentMessage) -> str:
        """Process one inbound message and return the assistant reply text.

        ``user_message`` is either a plain ``str`` (phase-one callers, and
        existing tests) or a channel-independent :class:`AgentMessage` carrying
        text and/or image parts. A bare string is normalised to a single-part
        text :class:`AgentMessage`, so both entry points share one code path.

        Persistence stores **text only**: an image rides along in the *current*
        request but is not written to the DB (a deliberate phase limitation —
        see the module docstring). The assistant reply is always a ``str``.
        """
        # Normalise a plain string to a single-part text message so both entry
        # points share one code path; a string with no text short-circuits.
        if isinstance(user_message, str):
            text = user_message.strip()
            if not text:
                return ""
            message = AgentMessage(contents=[TextContent(text)])
        else:
            message = user_message
            text = message.text
            if message.is_empty():
                return ""

        async with self.conversation_lock(conversation_id):
            history_records = await self._repo.get_messages(conversation_id)
            # Persist the *text* of the user turn (images are not stored).
            await self._repo.add_message(conversation_id, "user", text)

            history = [ChatMessage(role=r.role, content=r.content) for r in history_records]
            # The current turn may be multimodal (a list of OpenAI parts); the
            # persisted history is always plain strings, so only the *current*
            # message can carry an image into the wire payload.
            current_content = agent_message_to_openai_content(message)
            context = build_context(self._system_prompt, [*history, ChatMessage("user", current_content)])

            try:
                if self._enable_tools:
                    result = await run_tool_loop(
                        self._llm,
                        context,
                        self._registry,
                        max_iterations=self._max_tool_iterations,
                    )
                else:
                    # Phase-one path: one completion, no tools.
                    result = await self._llm.complete(context)
            except LLMError as exc:
                logger.error(
                    "llm failure",
                    extra={"conversation_id": conversation_id, "category": exc.category},
                )
                raise AgentError(_user_safe_for(exc.category), exc.category) from exc
            except ToolLoopLimitError as exc:
                logger.error(
                    "tool loop limit reached",
                    extra={
                        "conversation_id": conversation_id,
                        "max_iterations": self._max_tool_iterations,
                    },
                )
                raise AgentError(_user_safe_for("tool_limit"), "tool_limit") from exc

            await self._repo.add_message(conversation_id, "assistant", result.text)
            logger.info(
                "assistant reply generated",
                extra={
                    "conversation_id": conversation_id,
                    "reply_length": len(result.text),
                    "image_attached": message.has_image(),
                },
            )
            return result.text
