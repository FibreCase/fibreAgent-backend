"""Async engine / session factory and schema initialisation.

Everything here is driven by the ``DATABASE_URL`` so the storage backend can
later be swapped (e.g. PostgreSQL) without touching callers.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

logger = logging.getLogger("database")


def _ensure_parent_dir(database_url: str) -> None:
    """Create the parent directory of a file-backed SQLite database.

    For non-file URLs (postgres, in-memory, etc.) there is nothing to do.
    """
    if not database_url.startswith("sqlite"):
        return
    # e.g. "sqlite+aiosqlite:///./data/agent.db" -> "./data/agent.db"
    path = database_url.split("///", 1)[-1]
    if not path or path == ":memory:":
        return
    parent = Path(path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def _enable_sqlite_fks(engine: AsyncEngine) -> None:
    """Turn on SQLite foreign-key enforcement (off by default).

    Needed so ``ondelete='CASCADE'`` works when a conversation's messages are
    removed during a reset.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()


def create_engine(database_url: str) -> AsyncEngine:
    _ensure_parent_dir(database_url)
    connect_args: dict[str, object] = {}
    # Avoid the synchronous SQLite "busy" error while a write transaction is
    # in flight; WAL is not required for our access pattern.
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_async_engine(database_url, echo=False, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        _enable_sqlite_fks(engine)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create any missing tables. Safe to call on every startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database schema ready (%s)", _describe_url(engine.url))


def _describe_url(url) -> str:
    """A short, non-sensitive description of the database target."""
    try:
        if url.drivername.startswith("sqlite"):
            return f"sqlite:{url.database}"
        return f"{url.get_backend_name()}://{url.host}"
    except Exception:  # pragma: no cover - defensive
        return url.get_backend_name()
