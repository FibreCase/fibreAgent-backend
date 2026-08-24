"""SQLAlchemy ORM models for conversations and messages.

The schema deliberately allows a ``tool`` role (used by future tool/MCP
support) even though phase one only stores ``user`` / ``assistant`` turns.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
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
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<Message id={self.id} conv={self.conversation_id} role={self.role}>"
