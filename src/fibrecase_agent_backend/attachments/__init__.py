"""Channel-independent attachment (blob) storage.

The :class:`~fibrecase_agent_backend.attachments.store.AttachmentStore` is the
only component that knows how attachment bytes live on the local filesystem. It
is content-addressed, deduplicated, and atomic, and it stays free of any
Telegram / OpenAI-SDK / ORM imports so the agent layer can depend on it without
pulling in those concerns.
"""

from __future__ import annotations

from .store import (
    AttachmentCorruptError,
    AttachmentNotFoundError,
    AttachmentStore,
    AttachmentStoreError,
    AttachmentStorageError,
    StoredBlob,
)

__all__ = [
    "AttachmentStore",
    "AttachmentStoreError",
    "AttachmentNotFoundError",
    "AttachmentCorruptError",
    "AttachmentStorageError",
    "StoredBlob",
]
