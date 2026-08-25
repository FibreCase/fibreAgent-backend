"""Database package: models, session/engine, and the repository."""

from .models import Attachment, Base, Conversation, Message
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
    "AttachmentRef",
    "ConversationRepository",
    "MessageRecord",
    "MessageWithAttachments",
    "create_engine",
    "create_session_factory",
    "init_db",
]
