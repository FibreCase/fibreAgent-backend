"""Channel-independent, content-addressed attachment blob storage.

This is the *only* module that knows how blobs live on the local filesystem. It
is deliberately channel- and protocol-agnostic: it deals in raw ``bytes``, a
SHA-256 digest, and a relative path under a root directory — it imports **none**
of Telegram, the OpenAI SDK, or the ORM. The agent service decides *what* to
persist (an :class:`~fibrecase_agent_backend.agent.messages.ImageContent`) and
*whether* to read it back; the store only answers "put these bytes here" and
"give me the bytes for this digest".

Design goals (see the phase-2.2 spec):

* **Content-addressed**: a blob's identity is the SHA-256 hex digest of its
  raw bytes, laid out as ``<root>/<digest[:2]>/<digest>``. The path is never
  derived from a user filename, caption, or Telegram ``file_id``.
* **Deduplicated**: identical bytes always map to one blob; re-sending the same
  image never writes the file twice.
* **Atomic writes**: each new blob is written to a temp file in the same
  directory, flushed and ``fsync``-ed, then published with an atomic rename — a
  crash can never leave a half-written blob at the final name.
* **Traversal-safe**: reads/reads/deletes are located by digest only, and a
  digest must be exactly 64 lowercase hex characters or it is rejected, so no
  caller-supplied value can escape the root.
* **Log-safe**: this module logs only a short digest prefix, byte counts and
  the operation result — never bytes, base64, MIME captions, or full paths.

A missing or corrupt blob is surfaced as a distinct exception type so the
caller can *skip* that image from history (keeping its text) without crashing
the whole process.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("attachments")

# A blob is addressed by the SHA-256 hex digest of its bytes — exactly 64
# lowercase hex characters. Anything else is rejected (this is the guard that
# keeps a caller-supplied value from being treated as a filesystem path).
_DIGEST_LEN = 64


def _is_valid_digest(value: str) -> bool:
    return len(value) == _DIGEST_LEN and all(c in "0123456789abcdef" for c in value)


class AttachmentStoreError(Exception):
    """Base class for attachment-store failures (safe to surface to a user).

    ``category`` is a stable key for logging; ``user_safe`` is a generic
    message that never leaks a path, bytes, or base64.
    """

    def __init__(self, user_safe: str = "附件处理失败，请稍后重试。", category: str = "attachment_error") -> None:
        super().__init__(user_safe)
        self.user_safe = user_safe
        self.category = category


class AttachmentNotFoundError(AttachmentStoreError):
    """The referenced blob is not present on disk (deleted, moved, or lost)."""

    def __init__(self, digest: str) -> None:
        super().__init__(user_safe="部分历史图片已不可用，已按文字继续。", category="attachment_missing")
        self.digest = digest


class AttachmentCorruptError(AttachmentStoreError):
    """The blob is present but its bytes no longer match its digest."""

    def __init__(self, digest: str) -> None:
        super().__init__(user_safe="部分历史图片已不可用，已按文字继续。", category="attachment_corrupt")
        self.digest = digest


class AttachmentStorageError(AttachmentStoreError):
    """A filesystem failure while writing or deleting a blob (I/O error)."""

    def __init__(self, category: str) -> None:
        # ``from <exc>`` at the raise site preserves the original traceback.
        super().__init__(user_safe="附件保存失败，请稍后重试。", category=category)


def _short(digest: str) -> str:
    """A short, log-safe prefix of a digest (never the full value)."""
    return digest[:8]


@dataclass(frozen=True)
class StoredBlob:
    """The result of persisting a blob: its content id and store-relative key."""

    sha256: str
    storage_key: str
    size_bytes: int
    created: bool  # True if this call wrote the file, False if it already existed


class AttachmentStore:
    """A content-addressed, deduplicated blob store rooted at a local directory."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    # ------------------------------------------------------------------ paths
    def _bucket_dir(self, digest: str) -> Path:
        return self._root / digest[:2]

    def _blob_path(self, digest: str) -> Path:
        """The on-disk path for a digest.

        ``digest`` must already have been validated by :meth:`_validate_digest`
        (callers go through a public method that validates first).
        """
        return self._bucket_dir(digest) / digest

    @staticmethod
    def _validate_digest(digest: str) -> str:
        if not isinstance(digest, str) or not _is_valid_digest(digest):
            # Log only the *kind* of problem and a safe-length hint — never the
            # raw (possibly attacker-controlled) value, which could carry path
            # separators or other sensitive input.
            logger.warning("rejected malformed attachment digest", extra={"length": len(digest) if isinstance(digest, str) else 0})
            raise ValueError("invalid attachment digest")
        return digest

    def storage_key_for(self, digest: str) -> str:
        """The store-relative, trusted key for a digest (``<xx>/<digest>``)."""
        self._validate_digest(digest)
        return f"{digest[:2]}/{digest}"

    # ------------------------------------------------------------------- write
    def save(self, data: bytes) -> StoredBlob:
        """Atomically persist ``data`` and return its content id + key.

        If the exact bytes are already stored, the existing blob is reused
        (``created`` is False) and no file is rewritten.
        """
        digest = hashlib.sha256(data).hexdigest()
        final_path = self._blob_path(digest)
        if final_path.exists():
            logger.debug("attachment blob reused", extra={"digest": _short(digest), "size_bytes": len(data)})
            return StoredBlob(sha256=digest, storage_key=self.storage_key_for(digest), size_bytes=len(data), created=False)

        bucket = final_path.parent
        bucket.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the *same* directory so the final publish is
        # an atomic rename on the same filesystem (never a direct write to the
        # final name, which could leave a torn file on a crash).
        fd, tmp_name = tempfile.mkstemp(dir=bucket, prefix=f".{digest}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, final_path)
        except Exception as exc:
            # Best-effort cleanup of the partial temp file; never let it mask
            # the original error. Log only safe metadata.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            logger.error("attachment write failed", extra={"digest": _short(digest), "size_bytes": len(data)}, exc_info=exc)
            raise AttachmentStorageError("attachment_write_failed") from exc

        logger.info("attachment blob stored", extra={"digest": _short(digest), "size_bytes": len(data)})
        return StoredBlob(sha256=digest, storage_key=self.storage_key_for(digest), size_bytes=len(data), created=True)

    # ------------------------------------------------------------------- read
    def exists(self, digest: str) -> bool:
        self._validate_digest(digest)
        return self._blob_path(digest).exists()

    def read(self, digest: str) -> bytes:
        """Read and integrity-check the bytes for ``digest``.

        Raises :class:`AttachmentNotFoundError` if the blob is absent and
        :class:`AttachmentCorruptError` if the stored bytes no longer hash to
        ``digest``. Both are distinct so a caller can skip the image (keeping
        its text) and log a stable category.
        """
        self._validate_digest(digest)
        path = self._blob_path(digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            logger.warning("attachment blob missing", extra={"digest": _short(digest)})
            raise AttachmentNotFoundError(digest) from exc
        except OSError as exc:
            logger.error("attachment read I/O failure", extra={"digest": _short(digest)}, exc_info=exc)
            raise AttachmentStorageError("attachment_read_failed") from exc

        if hashlib.sha256(data).hexdigest() != digest:
            logger.warning("attachment blob corrupt (hash mismatch)", extra={"digest": _short(digest), "size_bytes": len(data)})
            raise AttachmentCorruptError(digest)
        return data

    # ------------------------------------------------------------------ delete
    def delete(self, digest: str) -> bool:
        """Delete a blob. A missing file is treated as already-cleaned.

        Returns True if a file was removed, False if it was not present. Raises
        :class:`AttachmentStorageError` only on a genuine I/O failure during
        deletion (the caller should log and continue — a failed GC must never
        prevent a ``/new`` from creating its new conversation).
        """
        self._validate_digest(digest)
        path = self._blob_path(digest)
        try:
            if not path.exists():
                return False
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.error("attachment delete I/O failure", extra={"digest": _short(digest)}, exc_info=exc)
            raise AttachmentStorageError("attachment_delete_failed") from exc

        # Tidy up the now-empty bucket directory (best effort, ignore races).
        try:
            bucket = path.parent
            if bucket.exists() and not any(bucket.iterdir()):
                bucket.rmdir()
        except OSError:
            pass
        logger.info("attachment blob deleted", extra={"digest": _short(digest)})
        return True

    # ------------------------------------------------------------- discovery
    def iter_blobs(self) -> Iterator[str]:
        """Yield the digest of every blob present under the root.

        Used by callers that need to reconcile disk state against the database
        (e.g. an optional manual reconcile). Yields nothing if the root does
        not exist yet.
        """
        if not self._root.exists():
            return
        for bucket in sorted(self._root.iterdir()):
            if not bucket.is_dir():
                continue
            for blob in sorted(bucket.iterdir()):
                if blob.is_file() and _is_valid_digest(blob.name):
                    yield blob.name
