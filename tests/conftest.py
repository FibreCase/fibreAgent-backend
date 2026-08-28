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

from fibrecase_agent_backend import config as _config_module
from fibrecase_agent_backend.database.models import Base
from fibrecase_agent_backend.database.repository import ConversationRepository
from fibrecase_agent_backend.llm.client import LLMError, LLMResult


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Keep every test hermetic: never read the developer's real ``.env``.

    ``load_config`` auto-loads a ``.env`` from the working directory via
    ``load_dotenv``. Without this, any real ``.env`` present in the repo would
    leak live values (e.g. ``MCP_SERVERS``, ``OAUTH_CALLBACK_BASE_URL``) into
    tests that assert on *default* (unset) config — ``load_dotenv(override=False)``
    happily refills a key a test just ``delenv``'d. All config tests set their
    own env explicitly, so the ambient file must be inert.
    """
    monkeypatch.setattr(_config_module, "load_dotenv", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_default_infra_targets_file(monkeypatch):
    """Never let the *default* infra-SSH-targets file leak into config tests.

    ``load_config`` reads ``config/infra_ssh_targets.json`` (a well-known default
    path, like ``config/system_prompt.txt``) whenever ``INFRA_SSH_TARGETS_FILE`` is
    unset. A developer's real file (with real targets) must not be parsed during
    unrelated config tests. Pointing the default at a path that does not exist makes
    the default read a no-op, so those tests fall back to the inline
    ``INFRA_SSH_TARGETS`` exactly as before. ``test_infra_config.py`` restores a
    concrete default-path value (into its own ``tmp_path``) when it actually wants
    to exercise the default-file path — an inner fixture override wins.
    """
    monkeypatch.setattr(_config_module, "_INFRA_TARGETS_DEFAULT_FILE", "_no_default_infra_targets.json")


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
