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

from .models import Attachment, Conversation, Message

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

