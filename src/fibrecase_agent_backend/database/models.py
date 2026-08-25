"""SQLAlchemy ORM models for conversations, messages, and attachments.

The schema deliberately allows a ``tool`` role (used by future tool/MCP
support) even though phase one only stores ``user`` / ``assistant`` turns.

The ``attachments`` table (phase 2.2) stores *metadata* about persisted media:
the raw bytes live on disk in a content-addressed blob store (see
:mod:`..attachments`), never in the database. A message may have zero or more
attachments, kept in a stable in-message ``position`` so a photo + caption can
be rehydrated in the original order. ``sha256`` is the content id of the blob
and is shared across any number of attachment records (dedup); ``storage_key``
is the store-relative path, never derived from user input.

The ``memories`` table (phase 2.5) stores the user's *explicit* long-term
memories. A memory is a short, user-supplied fact, isolated by an opaque
``scope`` string (built by the transport adapter — e.g. ``telegram:<user_id>`` —
never stored as a Telegram-specific column). It is **not** tied to any
conversation or message: it survives ``/new`` and restarts. Only *short text*
is stored — never image bytes, base64, an attachment path, or any Telegram
identifier. ``normalized_content`` is a search-only form (see
:mod:`..memory.text`); ``last_retrieved_at`` is set only when a memory is
actually injected into a live LLM context.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    # sqlite_autoincrement makes the conversation id strictly increase, so a
    # reset always yields a visibly *new* id rather than reusing a freed one.
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_chat_id: Mapped[int] = mapped_column(index=True, nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.id",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<Conversation id={self.id} chat={self.telegram_chat_id}>"


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Roles are intentionally constrained to the set we will ever use;
        # "tool" is allowed ahead of time for future tool calling.
        CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_messages_role",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # Pure-text content only. Media is never stored here (no base64, no bytes);
    # it lives in the blob store and is referenced by ``attachments``.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
    # A message may carry zero or more attachments, kept in a stable order.
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Attachment.position",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<Message id={self.id} conv={self.conversation_id} role={self.role}>"


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        # ``sha256`` is indexed so "is this blob still referenced anywhere?" is a
        # cheap lookup during /new garbage collection.
        Index("ix_attachments_sha256", "sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # One attachment links exactly one message. The message's conversation is
    # reached through Message.conversation_id (we do not denormalise it here).
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Content id of the on-disk blob (deduplicated — many rows can share it).
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Store-relative path ("ab/abcdef...") produced by the store, never user input.
    storage_key: Mapped[str] = mapped_column(String(68), nullable=False)
    # Currently always "image"; left a column so future file/audio/video slot in.
    content_type: Mapped[str] = mapped_column(String(16), nullable=False, default="image")
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False, default="image/jpeg")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Optional; a Telegram photo does not currently carry a filename.
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Stable order of this attachment within its message's content.
    position: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    message: Mapped["Message"] = relationship(back_populates="attachments")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<Attachment id={self.id} msg={self.message_id} sha={self.sha256[:8]}... pos={self.position}>"


class Memory(Base):
    """One explicit long-term memory for a single opaque ``scope`` (phase 2.5).

    Isolated by ``scope`` only — there is deliberately **no** conversation or
    message foreign key, so a memory outlives ``/new`` and restarts and is
    shared across all of a principal's conversations. ``content`` is the
    user's original short text; ``normalized_content`` is a search-only form and
    is never shown to the user. ``last_retrieved_at`` is updated only when the
    memory is actually injected into a live LLM context.
    """

    __tablename__ = "memories"
    # ``scope`` is indexed so per-principal list/search/clear is a cheap lookup;
    # the ``(scope, id)`` lookups used by /forget rely on it.
    __table_args__ = (Index("ix_memories_scope", "scope"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Opaque principal identity, e.g. "telegram:<effective_user.id>". Built by
    # the adapter; the DB/agent/memory layers treat it as an opaque string.
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    # The user's original short text fact, verbatim (no media / paths / ids).
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Deterministic search form (casefold + whitespace-collapsed); not shown.
    normalized_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
    # Set only when the memory was actually injected into a live context.
    last_retrieved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<Memory id={self.id} scope_len={len(self.scope)} content_len={len(self.content)}>"

