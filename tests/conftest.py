"""Shared fixtures for the test suite.

All tests use an in-memory SQLite database and a fake LLM — nothing ever
talks to the real LLM endpoint or Telegram.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from fibrecase_agent_backend.database.models import Base
from fibrecase_agent_backend.database.repository import ConversationRepository
from fibrecase_agent_backend.llm.client import LLMError, LLMResult


@pytest.fixture
async def repo():
    """An in-memory SQLite-backed repository with a fresh schema per test."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,  # share one in-memory DB across all connections
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield ConversationRepository(factory)
    await engine.dispose()


@dataclass
class FakeLLM:
    """A stand-in for :class:`OpenAIClient` used by the Agent service tests."""

    replies: list[str] = field(default_factory=lambda: ["ok"])
    raise_error: LLMError | None = None
    calls: list[list[dict[str, str]]] = field(default_factory=list)

    async def complete(self, messages, **_kwargs: Any) -> LLMResult:
        self.calls.append([m.to_dict() for m in messages])
        if self.raise_error is not None:
            raise self.raise_error
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return LLMResult(content=reply)


@dataclass
class RecordingLLM:
    """An LLM fake that records how many completions run concurrently."""

    delay: float = 0.05
    replies: list[str] = field(default_factory=lambda: ["ok"])
    active: int = 0
    max_active: int = 0
    calls: list[list[dict[str, str]]] = field(default_factory=list)

    async def complete(self, messages, **_kwargs: Any) -> LLMResult:
        self.calls.append([m.to_dict() for m in messages])
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        return LLMResult(content=self.replies[-1])
