"""The Agent service — the reusable core of the backend.

The service is intentionally channel-agnostic. A Telegram message, a future
web UI, Discord, or an HTTP API all call :meth:`AgentService.process_message`
and get back a reply. It never talks to Telegram or the OpenAI SDK directly;
it depends only on the repository (persistence), the LLM client, and (phase 2.2)
the attachment store (persistent image blobs).

Responsibilities, per message:

1. acquire the per-conversation lock (serialise one conversation, parallelise
   across conversations),
2. load history (with attachment metadata),
3. append + persist the user message, and persist any image blobs it carries
   (phase 2.2 — images now survive a restart),
4. build context (system prompt + recent window), re-attaching in-window
   history images from the store in their original order,
5. call the LLM — via the tool loop when tools are enabled, or a single
   completion when they are not (phase-one behaviour),
6. persist the assistant reply,
7. return the reply text.

Only the user turn and the *final* assistant turn are persisted; the
intermediate tool-call / tool-result turns of the loop are not stored, so the
conversation schema is unchanged from phase one. **Text** is the only thing
stored in ``messages.content``; image **bytes** live in the content-addressed
attachment store, referenced by metadata rows.

Provider and storage failures are translated into a :class:`AgentError` whose
``user_safe`` message is generic and safe to show to a user, while the
structured cause is logged for operators (never with bytes, base64, paths, or
secrets).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from ..attachments import AttachmentStore, AttachmentStoreError
from ..database.repository import (
    ConversationRepository,
    MessageWithAttachments,
)
from ..llm.client import LLMError, OpenAIClient
from ..llm.message_converter import agent_message_to_openai_content
from ..tools.registry import ToolRegistry
from .context import ChatMessage, build_context
from .messages import AgentMessage, ImageContent, TextContent
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
    "attachment_error": "图片附件保存失败，请重新发送。",
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
        attachment_store: AttachmentStore | None = None,
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
        # Phase 2.2: when set, image attachments are persisted to disk and
        # re-attached into history. When None (tests / explicit opt-out) images
        # are still sent in the current turn but are not persisted — the exact
        # phase-2.1.x behaviour.
        self._attachment_store = attachment_store
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
        """Reset a chat's conversation, returning the new conversation id.

        Also reclaims image blobs that were referenced *only* by the dropped
        conversation. Blobs shared with another conversation are kept (a dedup'd
        blob must never be deleted while any attachment still points at it).
        """
        # Snapshot the digests this chat *may* orphan, before the records are
        # removed. (Empty set when no store — nothing to reclaim.)
        candidates: set[str] = set()
        if self._attachment_store is not None:
            candidates = await self._repo.attachment_sha256_for_chat(telegram_chat_id)

        conversation = await self._repo.reset_conversation(telegram_chat_id, telegram_user_id)
        await self._reclaim_attachments(telegram_chat_id, candidates)
        return conversation.id

    async def _reclaim_attachments(self, telegram_chat_id: int, candidates: set[str]) -> None:
        """Delete blobs no longer referenced anywhere after a reset.

        Fail-safe: if we cannot determine references, or a delete fails, we log
        (safely) and continue — a GC hiccup must never prevent ``/new`` from
        creating its new conversation, and it must never touch a shared blob.
        """
        store = self._attachment_store
        if store is None or not candidates:
            return
        try:
            still_referenced = await self._repo.distinct_attachment_sha256()
        except Exception:
            logger.warning(
                "attachment gc skipped (could not determine references)",
                extra={"telegram_chat_id": telegram_chat_id},
                exc_info=True,
            )
            return
        for digest in candidates:
            if digest in still_referenced:
                continue  # shared with another conversation — keep the blob
            try:
                store.delete(digest)
            except AttachmentStoreError:
                # Deletion failed; leave it for a later reset to retry. Safe log.
                logger.warning(
                    "attachment blob cleanup failed",
                    extra={"telegram_chat_id": telegram_chat_id, "digest": digest[:8]},
                    exc_info=True,
                )

    # ------------------------------------------------------------ persistence
    async def _persist_attachments(
        self, message_id: int, message: AgentMessage
    ) -> None:
        """Write this message's image blobs and link metadata rows to it.

        Runs only when a store is configured and the message carries images.
        Compensates on failure: blobs created during this call that end up with
        no metadata reference are removed, and a user-safe :class:`AgentError`
        is raised so the (un-persisted) image is never sent to the LLM.
        """
        store = self._attachment_store
        if store is None:
            return
        image_parts = [p for p in message.contents if isinstance(p, ImageContent)]
        if not image_parts:
            return

        created: list[str] = []
        specs: list[dict[str, object]] = []
        try:
            for part in image_parts:
                blob = store.save(part.data)
                specs.append(
                    {
                        "sha256": blob.sha256,
                        "storage_key": blob.storage_key,
                        "size_bytes": blob.size_bytes,
                        "content_type": "image",
                        "mime_type": part.mime_type,
                        "filename": part.filename,
                        # Position of this image within the message's content, so
                        # a photo + caption rehydrates in the original order.
                        "position": message.contents.index(part),
                    }
                )
                if blob.created:
                    created.append(blob.sha256)
            await self._repo.add_message_attachments(message_id, specs)
        except Exception:
            # Compensate: remove any blob this call created that is now
            # unreferenced. Never let the cleanup mask the original failure.
            for digest in created:
                try:
                    store.delete(digest)
                except Exception:
                    logger.warning(
                        "attachment compensation cleanup failed",
                        extra={"message_id": message_id, "digest": digest[:8]},
                        exc_info=True,
                    )
            logger.error(
                "attachment persistence failed",
                extra={"message_id": message_id},
                exc_info=True,
            )
            raise AgentError(_user_safe_for("attachment_error"), "attachment_error")

    # ------------------------------------------------------------ rehydration
    def _rehydrate_content(self, msg: MessageWithAttachments, conversation_id: int):
        """Rebuild the OpenAI ``content`` for a stored history message.

        * A message with no attachments → its plain-text ``content`` (the
          phase-1 wire shape, byte-for-byte unchanged).
        * A message with image attachments → an ordered list of OpenAI parts,
          reading each blob from the store. If a blob is missing or corrupt the
          image part is *skipped* (its text is kept) and a safe warning logged —
          one bad blob must never crash the turn or feed the model a fake image.
        """
        if not msg.attachments:
            return msg.content

        usable: dict[int, ImageContent] = {}
        for att in msg.attachments:  # already sorted by position
            try:
                data = self._attachment_store.read(att.sha256)
            except Exception as exc:
                category = exc.category if isinstance(exc, AttachmentStoreError) else "attachment_read_error"
                logger.warning(
                    "history image skipped",
                    extra={
                        "conversation_id": conversation_id,
                        "message_id": att.message_id,
                        "attachment_id": att.attachment_id,
                        "digest": att.sha256[:8],
                        "category": category,
                    },
                )
                continue
            usable[att.position] = ImageContent(data=data, mime_type=att.mime_type, filename=att.filename)

        if not usable:
            # Every image was unreadable → plain text (phase-1 shape preserved).
            return msg.content

        # The message's text occupies the single slot not taken by an image
        # ``position``; rebuild the part list in position order so [Image, Text]
        # (and future multi-part layouts) rehydrate faithfully.
        used = set(usable)
        text_pos = 0
        while text_pos in used:
            text_pos += 1
        parts = []
        for pos in range(max(max(usable), text_pos) + 1):
            if pos in usable:
                parts.append(usable[pos])
            elif pos == text_pos and msg.content:
                parts.append(TextContent(msg.content))
        return agent_message_to_openai_content(AgentMessage(contents=parts))

    # ------------------------------------------------------------ process
    async def process_message(self, conversation_id: int, user_message: str | AgentMessage) -> str:
        """Process one inbound message and return the assistant reply text.

        ``user_message`` is either a plain ``str`` (phase-one callers, and
        existing tests) or a channel-independent :class:`AgentMessage` carrying
        text and/or image parts. A bare string is normalised to a single-part
        text :class:`AgentMessage`, so both entry points share one code path.

        Persistence stores the user turn's **text** in the DB; any image parts
        are additionally persisted as content-addressed blobs (phase 2.2) so
        they can be re-attached to history on later turns and after a restart.
        The assistant reply is always a ``str``.
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
            # Load the full history with attachment metadata (detached). The
            # recent window is chosen below; only in-window images are read back.
            history_wa = await self._repo.get_messages_with_attachments(conversation_id)

            # Persist the *text* of the user turn, then any image blobs it has.
            user_message_obj = await self._repo.add_message(conversation_id, "user", text)
            await self._persist_attachments(user_message_obj.id, message)

            # Rebuild the recent window (message count, not tokens), matching
            # build_context's truncation so an out-of-window image is never
            # read from disk. The current turn is always in the window.
            max_n = self._max_context_messages
            prior_in_window = history_wa[-(max_n - 1):] if max_n > 1 else []
            history = [
                ChatMessage(role=m.role, content=self._rehydrate_content(m, conversation_id))
                for m in prior_in_window
            ]
            # The current turn may be multimodal (a list of OpenAI parts) and
            # rides the in-memory bytes; history images were read back above.
            current_content = agent_message_to_openai_content(message)
            context = build_context(self._system_prompt, [*history, ChatMessage("user", current_content)], max_n)

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
