"""Database package: models, session/engine, and the repository."""

from .models import Attachment, Base, Conversation, Message, SCHEDULE_CHAT_ID_BASE, SCHEDULE_CHAT_ID_MAX, schedule_chat_id
from .repository import (
    AttachmentRef,
    ConversationRepository,
    MessageRecord,
    MessageWithAttachments,
)
from .session import create_engine, create_session_factory, init_db

__all__ = [
    "Attachment",
    "Base",
    "Conversation",
    "Message",
    "SCHEDULE_CHAT_ID_BASE",
    "SCHEDULE_CHAT_ID_MAX",
    "schedule_chat_id",
    "AttachmentRef",
    "ConversationRepository",
    "MessageRecord",
    "MessageWithAttachments",
    "create_engine",
    "create_session_factory",
    "init_db",
]
