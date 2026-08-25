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
   (phase 2.3 — images now survive a restart),
4. **plan the context** (phase 2.4): before reading any attachment blob, run
   :func:`.context.plan_context` on lightweight candidates to choose complete
   history turns that fit *both* the message cap and the estimated-token budget
   (a history turn whose images won't fit is downgraded to text-only), then
   rehydrate *only* the selected attachments from the store in their original
   order;
5. call the LLM — via the tool loop when tools are enabled, or a single
   completion when they are not (phase-one behaviour) — unless the plan reports
   the current request itself is over budget, in which case no LLM call is made;
6. persist the assistant reply,
7. return the reply text.

Only the user turn and the *final* assistant turn are persisted; the
intermediate tool-call / tool-result turns of the loop are not stored, so the
conversation schema is unchanged from phase one. **Text** is the only thing
stored in ``messages.content``; image **bytes** live in the content-addressed
attachment store, referenced by metadata rows.

Provider, storage, and context-budget failures are translated into an
:class:`AgentError` whose ``user_safe`` message is generic and safe to show to a
user, while the structured cause is logged for operators (never with bytes,
base64, paths, or secrets).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from ..attachments import AttachmentStore, AttachmentStoreError
from ..database.repository import (
    ConversationRepository,
    MemoryRecord,
    MessageWithAttachments,
    ToolAuditRecord,
)
from ..llm.client import LLMError, OpenAIClient
from ..llm.message_converter import agent_message_to_openai_content
from ..memory import MemoryCandidate, build_memory_reference_text, hash_scope, normalize_text, rank_memories
from ..tools.approval import ToolApprovalProvider
from ..tools.audit import NoopAuditor, ToolAuditor
from ..tools.policy import ToolPolicy
from ..tools.registry import ToolRegistry
from .context import (
    ChatMessage,
    PlannedMessage,
    TurnCandidate,
    plan_context,
)
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
    "context_limit": "当前消息超出可处理的上下文限制，请缩短文字或减少图片后重试。",
    # Phase 2.5 memory commands are English (consistent with /help, /status, …).
    "memory_invalid": "Memory content is empty or too long.",
    "memory_limit": "Memory limit reached for your account. Forget a memory first.",
    "memory_error": "Memory operation failed. Please try again.",
    "memory_not_found": "Memory not found.",
    "memory_clear_confirmation": "Confirm clearing all memories with: /forget all CONFIRM",
    # Phase 3 tool-security command.
    "tool_audit_error": "Could not read the tool audit log. Please try again.",
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
        max_context_estimated_tokens: int = 24000,
        context_image_estimated_tokens: int = 2000,
        registry: ToolRegistry | None = None,
        enable_tools: bool = False,
        max_tool_iterations: int = 5,
        attachment_store: AttachmentStore | None = None,
        max_memories_per_scope: int = 200,
        max_memory_chars: int = 1000,
        max_retrieved_memories: int = 5,
        max_memory_estimated_tokens: int = 3000,
        # Phase 3: tool-security runtime, injected by the composition root.
        # ``policy=None`` → allow-all (the three safe built-ins need no prompt);
        # a real deployment builds it from config overrides + tool defaults.
        policy: ToolPolicy | None = None,
        approval_provider: ToolApprovalProvider | None = None,
        auditor: ToolAuditor | None = None,
        tool_timeout_seconds: float = 30.0,
        tool_approval_timeout_seconds: float = 60.0,
    ) -> None:
        self._repo = repository
        self._llm = llm
        self._system_prompt = system_prompt
        self._max_context_messages = max_context_messages
        # Phase 2.4: a conservative, model-agnostic estimated-token budget on top
        # of the message cap. Before any attachment blob is read, the planner
        # chooses complete turns so the request fits *both* limits; a history
        # turn's images can be downgraded to text when the full form won't fit.
        self._max_context_estimated_tokens = max_context_estimated_tokens
        self._context_image_estimated_tokens = context_image_estimated_tokens
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
        # Phase 2.5: explicit long-term memory. Written only via the /remember,
        # /memories, /forget commands; retrieved (no LLM) and injected as a
        # separate reference user message on normal text messages.
        self._max_memories_per_scope = max_memories_per_scope
        self._max_memory_chars = max_memory_chars
        self._max_retrieved_memories = max_retrieved_memories
        self._max_memory_estimated_tokens = max_memory_estimated_tokens
        # Phase 3: the tool-security runtime. The auditor defaults to a no-op so
        # a bare service (and unit tests) runs without a DB; production injects
        # a real RepositoryToolAuditor. ``memory_scope`` (a principal string) is
        # threaded into each tool call for audit hashing + approval binding.
        self._policy = policy
        self._approval_provider = approval_provider
        self._auditor = auditor if auditor is not None else NoopAuditor()
        self._tool_timeout_seconds = tool_timeout_seconds
        self._tool_approval_timeout_seconds = tool_approval_timeout_seconds
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

    async def list_tool_audit_events(self, scope: str, limit: int = 20) -> list[ToolAuditRecord]:
        """The most recent tool-audit events for ``scope`` (newest first).

        Scope-isolated: the repository filters by the **hashed** scope in SQL, so
        a foreign principal's events are never returned. Returns a safe,
        user-presentable list (tool name, event type, code, latency, time, id) —
        never arguments, results, or exception text. A repository failure raises
        an :class:`AgentError` (``tool_audit_error``).
        """
        try:
            return await self._repo.list_tool_audit_events(hash_scope(scope), limit)
        except Exception:
            logger.error(
                "tool audit read failed",
                extra={"scope_hash": hash_scope(scope), "category": "tool_audit_error"},
                exc_info=True,
            )
            raise AgentError(_user_safe_for("tool_audit_error"), "tool_audit_error")

    async def context_status(self, conversation_id: int) -> dict[str, object]:
        """A preview of the context that *would* be sent for the next request.

        Runs the same :func:`.context.plan_context` the live path uses, but with
        an empty "current" user candidate — so it reports the stored history and
        how much of it (and how many of its images) would fit both the message
        cap and the estimated-token budget. It reads **no** attachment bytes
        (planning is metadata-only) and returns only counts/costs — never
        message text, captions, digests, paths, or secrets. The figures are the
        same conservative, model-agnostic *estimates* as at request time.
        """
        history_wa = await self._repo.get_messages_with_attachments(conversation_id)
        history_candidates = [
            TurnCandidate(
                role=m.role,
                text=m.content,
                message_id=m.message_id,
                attachments=tuple(
                    (att.sha256, att.mime_type, att.filename, att.position) for att in m.attachments
                ),
                image_count=len(m.attachments),
            )
            for m in history_wa
        ]
        # An empty placeholder current user: the mandatory "always kept" slot is
        # present (so the planner's arithmetic matches a real request) but costs
        # only its per-message envelope and carries no image.
        current_candidate = TurnCandidate(role="user", text="", message_id=0)
        plan = plan_context(
            self._system_prompt,
            current_candidate,
            history_candidates,
            max_messages=self._max_context_messages,
            max_estimated_tokens=self._max_context_estimated_tokens,
            image_cost=self._context_image_estimated_tokens,
        )
        images_kept = sum(len(pm.attachments) for pm in plan.selected if pm.keep_images)
        images_in_store = sum(len(m.attachments) for m in history_wa)
        return {
            "conversation_id": conversation_id,
            "cap": self._max_context_messages,
            "budget": self._max_context_estimated_tokens,
            "image_cost": self._context_image_estimated_tokens,
            "stored_messages": len(history_wa),
            "history_messages": len(plan.selected),
            "estimated_cost": plan.estimated_cost,
            "system_cost": plan.system_cost,
            "images_kept": images_kept,
            "images_in_store": images_in_store,
        }

    # --------------------------------------------------------- long-term memory
    # Phase 2.5. These are the *only* way a user changes their memories — all
    # through explicit commands. They are scope-isolated (every by-id read/delete
    # is filtered by ``scope + id`` in the repository), never call the LLM, and
    # fail safe into a stable :class:`AgentError` category the adapter renders.
    # Logging records only a short, irreversible scope hash — never the raw
    # scope, the memory text, or the user id.

    async def remember_memory(self, scope: str, content: str) -> MemoryRecord:
        """Save one explicit memory for ``scope``. Returns the stored record.

        Raises :class:`AgentError` (``memory_invalid`` / ``memory_limit`` /
        ``memory_error``) — never writes to the DB in the invalid/over-limit
        cases. The raw ``content`` is stored verbatim; only its normalized form
        is used for later retrieval.
        """
        trimmed = content.strip()
        if not trimmed:
            raise AgentError(_user_safe_for("memory_invalid"), "memory_invalid")
        if len(trimmed) > self._max_memory_chars:
            raise AgentError(_user_safe_for("memory_invalid"), "memory_invalid")
        try:
            count = await self._repo.count_memories(scope)
        except Exception:
            logger.error(
                "memory read failed",
                extra={"scope_hash": hash_scope(scope), "category": "memory_error"},
                exc_info=True,
            )
            raise AgentError(_user_safe_for("memory_error"), "memory_error")
        if count >= self._max_memories_per_scope:
            logger.info("memory limit reached", extra={"scope_hash": hash_scope(scope), "count": count})
            raise AgentError(_user_safe_for("memory_limit"), "memory_limit")
        try:
            record = await self._repo.add_memory(scope, trimmed, normalize_text(trimmed))
        except Exception:
            logger.error(
                "memory write failed",
                extra={"scope_hash": hash_scope(scope), "category": "memory_error"},
                exc_info=True,
            )
            raise AgentError(_user_safe_for("memory_error"), "memory_error")
        return record

    async def list_memories(self, scope: str) -> list[MemoryRecord]:
        """All of ``scope``'s memories (oldest first), or a safe error on failure."""
        try:
            return await self._repo.list_memories(scope)
        except Exception:
            logger.error(
                "memory read failed",
                extra={"scope_hash": hash_scope(scope), "category": "memory_error"},
                exc_info=True,
            )
            raise AgentError(_user_safe_for("memory_error"), "memory_error")

    async def forget_memory(self, scope: str, memory_id: int) -> None:
        """Delete one memory in ``scope``. A foreign/missing id → ``memory_not_found``.

        The repository filters by ``scope + id`` so a foreign id is indistinguishable
        from a missing one — no existence leak across principals.
        """
        try:
            removed = await self._repo.delete_memory(scope, memory_id)
        except Exception:
            logger.error(
                "memory delete failed",
                extra={"scope_hash": hash_scope(scope), "category": "memory_error"},
                exc_info=True,
            )
            raise AgentError(_user_safe_for("memory_error"), "memory_error")
        if not removed:
            raise AgentError(_user_safe_for("memory_not_found"), "memory_not_found")

    async def forget_all_memories(self, scope: str) -> int:
        """Delete *all* of ``scope``'s memories. Returns the count removed."""
        try:
            removed = await self._repo.clear_memories(scope)
        except Exception:
            logger.error(
                "memory clear failed",
                extra={"scope_hash": hash_scope(scope), "category": "memory_error"},
                exc_info=True,
            )
            raise AgentError(_user_safe_for("memory_error"), "memory_error")
        return removed

    async def _retrieve_memories_for(self, scope: str, query: str) -> list[MemoryCandidate]:
        """Fetch and rank the memories relevant to ``query`` for ``scope``.

        No LLM, no embeddings — pure deterministic lexical scoring over the
        scope's own memories. An empty / no-term query yields ``[]`` (no search).
        Any repository failure raises :class:`AgentError`` (``memory_error``) so
        normal chat fails safe and never calls the LLM with unknown context.
        """
        try:
            records = await self._repo.list_memories_for_search(scope)
        except Exception:
            logger.error(
                "memory retrieval failed; chat will not use memory",
                extra={"scope_hash": hash_scope(scope), "category": "memory_error"},
                exc_info=True,
            )
            raise AgentError(_user_safe_for("memory_error"), "memory_error")
        if not records:
            return []
        candidates = [
            MemoryCandidate(
                id=r.id,
                content=r.content,
                normalized_content=r.normalized_content,
                updated_at=r.updated_at,
            )
            for r in records
        ]
        ranked = rank_memories(query, candidates, self._max_retrieved_memories)
        if ranked:
            logger.info(
                "memory retrieved",
                extra={"scope_hash": hash_scope(scope), "hits": len(ranked), "ids": [c.id for c in ranked]},
            )
        return ranked

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
    async def process_message(
        self,
        conversation_id: int,
        user_message: str | AgentMessage,
        *,
        memory_scope: str | None = None,
    ) -> str:
        """Process one inbound message and return the assistant reply text.

        ``user_message`` is either a plain ``str`` (phase-one callers, and
        existing tests) or a channel-independent :class:`AgentMessage` carrying
        text and/or image parts. A bare string is normalised to a single-part
        text :class:`AgentMessage`, so both entry points share one code path.

        Persistence stores the user turn's **text** in the DB; any image parts
        are additionally persisted as content-addressed blobs (phase 2.2) so
        they can be re-attached to history on later turns and after a restart.
        The assistant reply is always a ``str``.

        ``memory_scope`` (phase 2.5, optional) is an opaque principal identity
        (e.g. ``telegram:<user_id>``) supplied by the adapter. When present and
        the message has a non-empty text query, the service deterministically
        retrieves relevant long-term memories (no LLM) and, if they fit the
        memory sub-budget, injects them as a single reference *user* message
        after the main prompt and before history. Without ``memory_scope`` the
        path is byte-for-byte the phase-2.4 behaviour. Memory retrieval never
        touches the tool loop or the attachment store.
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

            # Phase 2.5: deterministically retrieve relevant long-term memories
            # for this principal. Only a normal *text* message with a scope
            # triggers a search; image-only / empty / command-free-of-scope paths
            # skip it entirely (→ byte-for-byte phase-2.4 context). A retrieval
            # DB failure raises memory_error and aborts before the LLM is called.
            memory_candidates: list[MemoryCandidate] = []
            if memory_scope is not None and text:
                memory_candidates = await self._retrieve_memories_for(memory_scope, text)

            # Phase 2.4: before reading any attachment blob, plan which complete
            # history turns fit *both* the message cap and the estimated-token
            # budget. History is candidate metadata only (no bytes); the current
            # turn is a separate, always-kept candidate whose images are never
            # downgraded. Phase 2.5: the ranked memories are selected here too,
            # within the memory sub-budget.
            history_candidates = [
                TurnCandidate(
                    role=m.role,
                    text=m.content,
                    message_id=m.message_id,
                    attachments=tuple(
                        (att.sha256, att.mime_type, att.filename, att.position) for att in m.attachments
                    ),
                    image_count=len(m.attachments),
                )
                for m in history_wa
            ]
            current_images = sum(1 for part in message.contents if isinstance(part, ImageContent))
            current_candidate = TurnCandidate(
                role="user",
                text=text,
                message_id=user_message_obj.id,
                image_count=current_images,
            )
            plan = plan_context(
                self._system_prompt,
                current_candidate,
                history_candidates,
                max_messages=self._max_context_messages,
                max_estimated_tokens=self._max_context_estimated_tokens,
                image_cost=self._context_image_estimated_tokens,
                memories=memory_candidates or None,
                max_memory_estimated_tokens=self._max_memory_estimated_tokens,
            )

            # system + the current user request alone already exceed the budget:
            # do not call the LLM. The user's text and any image it carried were
            # already persisted above (consistent with other LLM-failure paths).
            if plan.status == "current_over_budget":
                logger.warning(
                    "request over context budget; skipping llm",
                    extra={
                        "conversation_id": conversation_id,
                        "budget": self._max_context_estimated_tokens,
                        "system_cost": plan.system_cost,
                        "current_cost": plan.current_cost,
                    },
                )
                raise AgentError(_user_safe_for("context_limit"), "context_limit")

            # Rehydrate only the turns the plan selected; downgraded turns and
            # unselected (older) turns are sent as plain text or omitted — their
            # blobs are never read from disk.
            by_id = {m.message_id: m for m in history_wa}
            history: list[ChatMessage] = []
            for pm in plan.selected:
                stored = by_id[pm.message_id]
                if pm.keep_images and self._attachment_store is not None and stored.attachments:
                    content = self._rehydrate_content(stored, conversation_id)
                else:
                    content = stored.content
                history.append(ChatMessage(role=pm.role, content=content))

            # The current turn rides its in-memory bytes (text and/or a list of
            # OpenAI parts) and is always kept, images and all.
            current_content = agent_message_to_openai_content(message)

            # Phase 2.5: the injected reference memories become ONE separate
            # message placed right after the main prompt and before history. It
            # is a ``user``-role message (the canonical place for retrieved
            # reference material), NOT a second ``system`` message: many
            # OpenAI-compatible endpoints reject a request that carries more than
            # one system message (or a system message that is not first) with a
            # 400, which is what broke memory-bearing turns. The raw memory text
            # is shown verbatim inside a fixed, non-instructional wrapper; a
            # user-role message can never alter the main prompt's role, tools, or
            # permissions. With no selected memories this list is empty and the
            # context is byte-for-byte the phase-2.4 shape.
            memory_messages: list[ChatMessage] = []
            if plan.selected_memories:
                memory_messages = [
                    ChatMessage(role="user", content=build_memory_reference_text(list(plan.selected_memories)))
                ]

            context = [
                ChatMessage(role="system", content=self._system_prompt),
                *memory_messages,
                *history,
                ChatMessage(role="user", content=current_content),
            ]

            images_kept = sum(len(pm.attachments) for pm in plan.selected if pm.keep_images)
            images_downgraded = sum(len(pm.attachments) for pm in plan.selected if not pm.keep_images)
            logger.info(
                "context planned",
                extra={
                    "conversation_id": conversation_id,
                    "budget": self._max_context_estimated_tokens,
                    "estimated_cost": plan.estimated_cost,
                    "system_cost": plan.system_cost,
                    "current_cost": plan.current_cost,
                    "selected_messages": len(plan.selected),
                    "history_messages": len(history_candidates),
                    "cap": self._max_context_messages,
                    "images_kept": images_kept,
                    "images_downgraded": images_downgraded,
                    "memories_selected": len(plan.selected_memories),
                    "memory_cost": plan.memory_cost,
                },
            )

            # Phase 2.5: only the memories that actually made it into the context
            # are stamped ``last_retrieved_at``, batched before the LLM call. A
            # memory that failed the sub-budget (not in ``selected_memories``) is
            # not touched. Documented semantics: if the LLM call then fails, the
            # retrieval still counts as having happened. This is a best-effort
            # timestamp write — if it fails we log (safely) and still send the
            # context, rather than dropping a turn that is otherwise ready.
            if plan.selected_memories and memory_scope is not None:
                try:
                    await self._repo.mark_memories_retrieved(
                        memory_scope, [m.id for m in plan.selected_memories]
                    )
                except Exception:
                    logger.warning(
                        "failed to mark memories retrieved",
                        extra={"scope_hash": hash_scope(memory_scope), "count": len(plan.selected_memories)},
                        exc_info=True,
                    )

            try:
                if self._enable_tools:
                    result = await run_tool_loop(
                        self._llm,
                        context,
                        self._registry,
                        max_iterations=self._max_tool_iterations,
                        policy=self._policy,
                        approval_provider=self._approval_provider,
                        auditor=self._auditor,
                        tool_timeout_seconds=self._tool_timeout_seconds,
                        approval_timeout_seconds=self._tool_approval_timeout_seconds,
                        conversation_id=conversation_id,
                        scope=memory_scope,
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
