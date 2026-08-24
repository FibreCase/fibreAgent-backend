"""Data-access layer for conversations and messages.

Callers never write SQL directly — they go through this repository, which is
the only layer aware of the ORM. This keeps the Telegram/Agent layers storage
agnostic and makes a future migration to another database a contained change.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import Conversation, Message

logger = logging.getLogger("database")


@dataclass(frozen=True)
class MessageRecord:
    """A minimal, detached view of a stored message (safe after session close)."""

    role: str
    content: str


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

    async def get_messages(self, conversation_id: int) -> list[MessageRecord]:
        async with self._session() as session:
            result = await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.asc())
            )
            rows = result.scalars().all()
            return [MessageRecord(role=row.role, content=row.content) for row in rows]

    async def count_messages(self, conversation_id: int) -> int:
        async with self._session() as session:
            result = await session.execute(
                select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
            )
            return int(result.scalar_one())

    async def reset_conversation(self, telegram_chat_id: int, telegram_user_id: int) -> Conversation:
        """Delete the existing conversation (and its messages) and start fresh."""
        async with self._session() as session:
            result = await session.execute(
                select(Conversation).where(Conversation.telegram_chat_id == telegram_chat_id)
            )
            old = result.scalar_one_or_none()
            if old is not None:
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
