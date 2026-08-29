"""Data-access layer for conversations and messages.

Callers never write SQL directly — they go through this repository, which is
the only layer aware of the ORM. This keeps the Telegram/Agent layers storage
agnostic and makes a future migration to another database a contained change.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from ..memory import hash_scope
from .models import (
    SCHEDULE_CHAT_ID_BASE,
    SCHEDULE_CHAT_ID_MAX,
    Attachment,
    Conversation,
    Memory,
    Message,
    ToolAuditEvent,
    utcnow,
)

logger = logging.getLogger("database")


@dataclass(frozen=True)
class MessageRecord:
    """A minimal, detached view of a stored message (safe after session close)."""

    role: str
    content: str


@dataclass(frozen=True)
class AttachmentRef:
    """A minimal, detached view of a stored attachment (safe after session close).

    Carries only the *metadata* needed to locate and rehydrate the blob — never
    the bytes themselves. ``position`` is the attachment's stable order within
    its message's content.
    """

    attachment_id: int
    message_id: int
    sha256: str
    storage_key: str
    content_type: str
    mime_type: str
    size_bytes: int
    filename: str | None
    position: int


@dataclass(frozen=True)
class MessageWithAttachments:
    """A detached message view that includes its attachments (safe after session close).

    ``attachments`` is already materialised (ordered by ``position``), so it can
    be read after the session that produced it has closed — no lazy loading.
    """

    role: str
    content: str
    message_id: int = 0
    attachments: tuple[AttachmentRef, ...] = ()

    def has_attachments(self) -> bool:
        return bool(self.attachments)


@dataclass(frozen=True)
class MemoryRecord:
    """A detached view of one long-term memory (safe after session close).

    ``content`` is the user's original text; ``normalized_content`` is the
    search-only form. Only *short text* is carried — never media, paths, or any
    Telegram identifier.
    """

    id: int
    scope: str
    content: str
    normalized_content: str
    created_at: object
    updated_at: object
    last_retrieved_at: object | None


@dataclass(frozen=True)
class ToolAuditRecord:
    """A detached, *safe* view of one tool-audit event (phase 3).

    Carries only what the owner may see: the event's id, time, tool name, event
    type, code, latency, and the **hashed** scope — never the raw scope, the
    arguments, a result, or any exception text. ``conversation_id`` and
    ``tool_call_id`` are the safe, nullable identifiers the table stores.
    """

    id: int
    created_at: object
    conversation_id: int | None
    tool_name: str
    tool_call_id: str | None
    iteration: int | None
    event_type: str
    code: str | None
    latency_ms: int | None
    scope_hash: str


class ConversationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            yield session

    async def get_conversation(self, telegram_chat_id: int) -> Conversation | None:
        async with self._session() as session:
            result = await session.execute(
                select(Conversation).where(Conversation.telegram_chat_id == telegram_chat_id)
            )
            return result.scalar_one_or_none()

    async def get_or_create_conversation(self, telegram_chat_id: int, telegram_user_id: int) -> Conversation:
        async with self._session() as session:
            result = await session.execute(
                select(Conversation).where(Conversation.telegram_chat_id == telegram_chat_id)
            )
            conversation = result.scalar_one_or_none()
            if conversation is None:
                conversation = Conversation(
                    telegram_chat_id=telegram_chat_id, telegram_user_id=telegram_user_id
                )
                session.add(conversation)
                await session.commit()
                await session.refresh(conversation)
                logger.info("conversation created", extra={"conversation_id": conversation.id})
            return conversation

    async def add_message(self, conversation_id: int, role: str, content: str) -> Message:
        async with self._session() as session:
            message = Message(conversation_id=conversation_id, role=role, content=content)
            session.add(message)
            await session.commit()
            await session.refresh(message)
            return message

    async def add_message_attachments(
        self,
        message_id: int,
        specs: Sequence[dict[str, object]],
    ) -> list[int]:
        """Attach already-persisted blobs to a message. Returns the new ids.

        Each spec supplies the attachment metadata: ``sha256``, ``storage_key``,
        ``size_bytes`` and, optionally, ``content_type`` / ``mime_type`` /
        ``filename`` / ``position``. All rows are written in a single commit so
        the metadata is all-or-nothing for this message.
        """
        if not specs:
            return []
        async with self._session() as session:
            attachments: list[Attachment] = []
            for spec in specs:
                attachments.append(
                    Attachment(
                        message_id=message_id,
                        sha256=str(spec["sha256"]),
                        storage_key=str(spec["storage_key"]),
                        content_type=str(spec.get("content_type", "image")),
                        mime_type=str(spec.get("mime_type", "image/jpeg")),
                        size_bytes=int(spec.get("size_bytes", 0)),  # type: ignore[arg-type]
                        filename=spec.get("filename"),  # type: ignore[arg-type]
                        position=int(spec.get("position", 0)),  # type: ignore[arg-type]
                    )
                )
            session.add_all(attachments)
            await session.commit()
            for att in attachments:
                await session.refresh(att)
            ids = [att.id for att in attachments]
            logger.info("attachments linked", extra={"message_id": message_id, "count": len(ids)})
            return ids

    async def get_messages(self, conversation_id: int) -> list[MessageRecord]:
        async with self._session() as session:
            result = await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.asc())
            )
            rows = result.scalars().all()
            return [MessageRecord(role=row.role, content=row.content) for row in rows]

    async def get_messages_with_attachments(self, conversation_id: int) -> list[MessageWithAttachments]:
        """Return a conversation's messages *with their attachments*, detached.

        Attachments are eager-loaded (``selectinload``) and materialised into
        :class:`AttachmentRef` while the session is open, so the returned records
        are safe to use after the session closes — no lazy relationship access.
        Text-only messages come back with an empty ``attachments`` tuple, so the
        common case is byte-for-byte the same data as :meth:`get_messages`.
        """
        async with self._session() as session:
            result = await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .options(selectinload(Message.attachments))
                .order_by(Message.id.asc())
            )
            rows = result.scalars().all()
            out: list[MessageWithAttachments] = []
            for row in rows:
                refs = tuple(
                    AttachmentRef(
                        attachment_id=a.id,
                        message_id=a.message_id,
                        sha256=a.sha256,
                        storage_key=a.storage_key,
                        content_type=a.content_type,
                        mime_type=a.mime_type,
                        size_bytes=a.size_bytes,
                        filename=a.filename,
                        position=a.position,
                    )
                    for a in sorted(row.attachments, key=lambda a: a.position)
                )
                out.append(MessageWithAttachments(role=row.role, content=row.content, message_id=row.id, attachments=refs))
            return out

    async def count_messages(self, conversation_id: int) -> int:
        async with self._session() as session:
            result = await session.execute(
                select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
            )
            return int(result.scalar_one())

    async def attachment_sha256_for_chat(self, telegram_chat_id: int) -> set[str]:
        """The set of blob digests referenced by a chat's *current* conversation.

        Collected *before* a reset so the caller knows which blobs *might*
        become orphaned once that conversation is dropped.
        """
        async with self._session() as session:
            result = await session.execute(
                select(Attachment.sha256)
                .join(Message, Attachment.message_id == Message.id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.telegram_chat_id == telegram_chat_id)
            )
            return {row[0] for row in result.all()}

    async def distinct_attachment_sha256(self) -> set[str]:
        """Every blob digest still referenced anywhere in the database."""
        async with self._session() as session:
            result = await session.execute(select(Attachment.sha256).distinct())
            return {row[0] for row in result.all()}

    async def reset_conversation(self, telegram_chat_id: int, telegram_user_id: int) -> Conversation:
        """Delete the existing conversation (and its messages/attachments) and start fresh.

        Attachments and messages are removed explicitly (rather than relying on
        the backend enforcing the ``ON DELETE CASCADE``) so a reset works the same
        regardless of FK enforcement. The caller is responsible for reclaiming
        now-unreferenced blob *files* — this method only removes DB records.
        """
        async with self._session() as session:
            result = await session.execute(
                select(Conversation).where(Conversation.telegram_chat_id == telegram_chat_id)
            )
            old = result.scalar_one_or_none()
            if old is not None:
                # Attachments reference messages, so remove them first.
                await session.execute(
                    delete(Attachment).where(
                        Attachment.message_id.in_(
                            select(Message.id).where(Message.conversation_id == old.id)
                        )
                    )
                )
                # Remove messages explicitly so a reset does not rely on the
                # foreign-key cascade being enforced by the backend.
                await session.execute(delete(Message).where(Message.conversation_id == old.id))
                await session.delete(old)
                await session.commit()
                logger.info("conversation reset", extra={"telegram_chat_id": telegram_chat_id})

            fresh = Conversation(telegram_chat_id=telegram_chat_id, telegram_user_id=telegram_user_id)
            session.add(fresh)
            await session.commit()
            await session.refresh(fresh)
            return fresh

    async def delete_conversation(self, conversation_id: int) -> bool:
        """Explicitly delete one conversation (and its messages + attachments) by PK.

        The phase-9 scheduled-run runner calls this in its ``finally`` so a run's
        dedicated conversation leaves **no** trace after the run. Attachments and
        messages are removed explicitly (mirroring :meth:`reset_conversation`)
        rather than relying on the backend enforcing ``ON DELETE CASCADE``. A
        missing id is a no-op returning ``False`` (the caller treats both the
        "deleted" and "already gone" cases as success). Returns ``True`` if a
        row was removed.
        """
        async with self._session() as session:
            result = await session.execute(
                select(Conversation.id).where(Conversation.id == conversation_id)
            )
            if result.scalar_one_or_none() is None:
                return False
            # Attachments reference messages, so remove them first.
            await session.execute(
                delete(Attachment).where(
                    Attachment.message_id.in_(
                        select(Message.id).where(Message.conversation_id == conversation_id)
                    )
                )
            )
            await session.execute(delete(Message).where(Message.conversation_id == conversation_id))
            await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
            await session.commit()
            # Log by the *conversation id* only (a synthetic scheduled-run id is
            # safe to log; a real id would never reach here in this flow).
            logger.info("conversation deleted", extra={"conversation_id": conversation_id})
            return True

    async def clear_ephemeral_conversations(self) -> int:
        """Delete **every** reserved-range (scheduled-run) conversation, by startup sweep.

        Removes conversations whose ``telegram_chat_id`` falls in the reserved
        range (``SCHEDULE_CHAT_ID_BASE < id < SCHEDULE_CHAT_ID_MAX``) together
        with their messages and attachments — the orphan-cleanup for a run whose
        process was killed, and for a task that was later removed from config (and
        so will never self-heal on its next run). A *real* chat id is never in that
        range, so no interactive conversation can be touched. Returns the number of
        conversation rows removed (0 when there is nothing to sweep — the empty
        ``SCHEDULES`` case, where this is a harmless no-op).
        """
        async with self._session() as session:
            result = await session.execute(
                select(Conversation.id).where(
                    Conversation.telegram_chat_id > SCHEDULE_CHAT_ID_BASE,
                    Conversation.telegram_chat_id < SCHEDULE_CHAT_ID_MAX,
                )
            )
            ids = [row[0] for row in result.all()]
            if not ids:
                return 0
            await session.execute(
                delete(Attachment).where(
                    Attachment.message_id.in_(
                        select(Message.id).where(Message.conversation_id.in_(ids))
                    )
                )
            )
            await session.execute(delete(Message).where(Message.conversation_id.in_(ids)))
            await session.execute(delete(Conversation).where(Conversation.id.in_(ids)))
            await session.commit()
            logger.info("ephemeral conversations cleared", extra={"count": len(ids)})
            return len(ids)

    # ------------------------------------------------------------- memories
    # Phase 2.5: explicit long-term memory. Every read/delete is filtered by
    # (scope, id) in the SQL itself — never "query by id, then compare scope" —
    # so one principal can neither see nor delete another principal's memory.
    # Only short text is written (the caller has already enforced the length and
    # count caps); the repository stores exactly what it is handed.

    def _memory_record(self, row: Memory) -> MemoryRecord:
        return MemoryRecord(
            id=row.id,
            scope=row.scope,
            content=row.content,
            normalized_content=row.normalized_content,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_retrieved_at=row.last_retrieved_at,
        )

    async def add_memory(self, scope: str, content: str, normalized_content: str) -> MemoryRecord:
        """Insert one memory for ``scope``. Returns the detached record.

        ``content`` is the user's original text; ``normalized_content`` is the
        search-only form the caller already computed (see :mod:`..memory.text`).
        """
        async with self._session() as session:
            row = Memory(scope=scope, content=content, normalized_content=normalized_content)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            record = self._memory_record(row)
            logger.info(
                "memory added",
                extra={"scope_hash": _scope_hash(scope), "memory_id": row.id, "content_length": len(content)},
            )
            return record

    async def list_memories(self, scope: str) -> list[MemoryRecord]:
        """All of ``scope``'s memories, oldest first (id ascending)."""
        async with self._session() as session:
            result = await session.execute(
                select(Memory).where(Memory.scope == scope).order_by(Memory.id.asc())
            )
            return [self._memory_record(row) for row in result.scalars().all()]

    async def get_memory(self, scope: str, memory_id: int) -> MemoryRecord | None:
        """One memory *if it belongs to ``scope``*; ``None`` otherwise.

        The ``scope`` filter is in the query, so a foreign id is indistinguishable
        from a missing one (no existence leak).
        """
        async with self._session() as session:
            result = await session.execute(
                select(Memory).where(Memory.scope == scope, Memory.id == memory_id)
            )
            row = result.scalar_one_or_none()
            return self._memory_record(row) if row is not None else None

    async def delete_memory(self, scope: str, memory_id: int) -> bool:
        """Delete ``memory_id`` *within* ``scope``. Returns True if one was removed."""
        async with self._session() as session:
            result = await session.execute(
                delete(Memory).where(Memory.scope == scope, Memory.id == memory_id)
            )
            removed = result.rowcount > 0
            await session.commit()
            if removed:
                logger.info(
                    "memory deleted",
                    extra={"scope_hash": _scope_hash(scope), "memory_id": memory_id},
                )
            return removed

    async def clear_memories(self, scope: str) -> int:
        """Delete **all** of ``scope``'s memories. Returns the number removed."""
        async with self._session() as session:
            result = await session.execute(delete(Memory).where(Memory.scope == scope))
            removed = result.rowcount or 0
            await session.commit()
            if removed:
                logger.info(
                    "memories cleared",
                    extra={"scope_hash": _scope_hash(scope), "count": removed},
                )
            return removed

    async def count_memories(self, scope: str) -> int:
        async with self._session() as session:
            result = await session.execute(
                select(func.count()).select_from(Memory).where(Memory.scope == scope)
            )
            return int(result.scalar_one())

    async def list_memories_for_search(self, scope: str) -> list[MemoryRecord]:
        """All of ``scope``'s memories, as detached records, for pure-function ranking.

        This hands back the *whole* scope so :func:`..memory.text.rank_memories`
        can score them in memory (no FTS5 / no SQL text matching). Only short
        text is returned; the caller must restrict the scope before ranking.
        """
        async with self._session() as session:
            result = await session.execute(
                select(Memory).where(Memory.scope == scope).order_by(Memory.id.asc())
            )
            return [self._memory_record(row) for row in result.scalars().all()]

    async def mark_memories_retrieved(
        self, scope: str, memory_ids: Sequence[int]
    ) -> None:
        """Set ``last_retrieved_at`` (now, UTC) on the memories *actually injected*.

        Only called for memories that made it into a live LLM context. Scoped to
        ``scope`` and the given ids; a no-op for an empty list.
        """
        if not memory_ids:
            return
        async with self._session() as session:
            await session.execute(
                Memory.__table__.update()
                .where(Memory.scope == scope, Memory.id.in_(memory_ids))
                .values(last_retrieved_at=utcnow())
            )
            await session.commit()
            logger.info(
                "memories marked retrieved",
                extra={"scope_hash": _scope_hash(scope), "count": len(memory_ids)},
            )

    # ------------------------------------------------------- tool audit (phase 3)
    # Append-only, safe execution log. Every *list* query is filtered by
    # ``scope_hash`` in the SQL, so one principal can never read another's tool
    # events. The record stores only safe metadata — never args/results/exceptions.

    def _audit_record(self, row: ToolAuditEvent) -> ToolAuditRecord:
        return ToolAuditRecord(
            id=row.id,
            created_at=row.created_at,
            conversation_id=row.conversation_id,
            tool_name=row.tool_name,
            tool_call_id=row.tool_call_id,
            iteration=row.iteration,
            event_type=row.event_type,
            code=row.code,
            latency_ms=row.latency_ms,
            scope_hash=row.scope_hash,
        )

    async def add_tool_audit_event(self, event: dict[str, object]) -> bool:
        """Append one safe audit event. Returns ``True`` on success.

        ``event`` supplies only the safe columns: ``scope_hash``, ``tool_name``,
        ``event_type``, and the optional ``code`` / ``conversation_id`` /
        ``tool_call_id`` / ``iteration`` / ``latency_ms``. Never pass arguments,
        results, or exception text — this method stores exactly what it is
        handed, and the loop is responsible for handing it only safe fields.
        Returns ``False`` (and logs) on a write failure so the caller can fail
        closed; the exception is *not* re-raised here.
        """
        try:
            async with self._session() as session:
                row = ToolAuditEvent(
                    conversation_id=event.get("conversation_id"),
                    scope_hash=str(event["scope_hash"]),
                    tool_name=str(event["tool_name"]),
                    tool_call_id=event.get("tool_call_id"),
                    iteration=event.get("iteration"),
                    event_type=str(event["event_type"]),
                    code=event.get("code"),
                    latency_ms=event.get("latency_ms"),
                )
                session.add(row)
                await session.commit()
            return True
        except Exception:
            logger.error(
                "tool audit write failed",
                extra={"tool": event.get("tool_name"), "event": event.get("event_type")},
                exc_info=True,
            )
            return False

    async def list_tool_audit_events(self, scope_hash: str, limit: int = 20) -> list[ToolAuditRecord]:
        """The most recent tool-audit events for ``scope_hash`` (newest first).

        The ``scope_hash`` filter is in the query — a foreign or missing hash
        yields ``[]`` and leaks nothing. ``limit`` is clamped by the caller to a
        sane bound (the command clamps to 50).
        """
        limit = max(1, int(limit))
        async with self._session() as session:
            result = await session.execute(
                select(ToolAuditEvent)
                .where(ToolAuditEvent.scope_hash == scope_hash)
                .order_by(ToolAuditEvent.id.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
            # Newest first in the query; present newest-first (matches display).
            return [self._audit_record(row) for row in rows]

    async def get_conversation_by_id(self, conversation_id: int) -> Conversation | None:
        """Fetch a conversation by its primary key (used for audit context)."""
        async with self._session() as session:
            result = await session.execute(select(Conversation).where(Conversation.id == conversation_id))
            return result.scalar_one_or_none()



def _scope_hash(scope: str) -> str:
    """A short, irreversible fingerprint of a scope, for safe logging.

    The raw scope (and thus the raw Telegram user id it encodes) is never logged.
    Delegates to the pure :func:`..memory.hash_scope` so every layer hashes the
    same way.
    """
    return hash_scope(scope)

