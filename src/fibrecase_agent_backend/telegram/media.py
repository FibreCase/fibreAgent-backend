"""Telegram media extraction: photo [+ caption] → an :class:`AgentMessage`.

This is the *only* module that knows how to fetch Telegram media. It turns a
``telegram.Message`` into a channel-independent
:class:`~fibrecase_agent_backend.agent.messages.AgentMessage`, downloading the
image bytes through the Bot API so the LLM endpoint never needs to reach out to
Telegram (or anywhere else) to get the image.

Two things stay private to this module:

* the Telegram ``file_id`` / ``PhotoSize`` (never handed to the agent or LLM);
* the image bytes (held in memory for the single request that consumes them —
  never written to disk, never persisted, never logged).

Failures are surfaced as :class:`MediaError` — a user-safe message plus a stable
``category`` for logging — so a bad/oversize image can never crash the backend.
"""

from __future__ import annotations

import logging

from telegram import Message

from ..agent.messages import AgentMessage, ImageContent, TextContent

logger = logging.getLogger("telegram.media")

# The image MIME types this phase handles. Anything else is rejected with a
# user-friendly error rather than guessed.
SUPPORTED_IMAGE_MIME = ("image/jpeg", "image/png", "image/webp")

# Magic-number signatures: (leading bytes, mime). Checked in order.
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


class MediaError(Exception):
    """A media-handling failure, safe to surface to the user.

    ``user_safe`` is the generic message to send to Telegram; ``category`` is a
    stable key for logging (never shown to the user).
    """

    def __init__(self, user_safe: str, category: str) -> None:
        super().__init__(user_safe)
        self.user_safe = user_safe
        self.category = category


def _sniff_mime(data: bytes) -> str | None:
    """Best-effort MIME from the leading bytes; ``None`` if unrecognised."""
    for prefix, mime in _MAGIC:
        if data.startswith(prefix):
            return mime
    # WebP is RIFF....WEBP (bytes 0-3 "RIFF", bytes 8-11 "WEBP").
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _resolve_mime(data: bytes, declared: str | None) -> str:
    """Determine the image MIME type.

    Trust a magic-byte match first (Telegram's own ``mime_type`` is often
    ``None``); otherwise accept a declared value *if it is one we support*;
    otherwise refuse — we never default to JPEG for bytes we can't identify.
    """
    sniffed = _sniff_mime(data)
    if sniffed:
        return sniffed
    if declared in SUPPORTED_IMAGE_MIME:
        return declared
    raise MediaError("暂时不支持该图片格式。", "unsupported_mime")


def _text_of(message: Message) -> str:
    """The text of a plain (non-media) Telegram message, stripped."""
    return (message.text or "").strip()


async def extract_image_message(message: Message, max_bytes: int) -> AgentMessage:
    """Normalise a Telegram *photo* message (with optional caption).

    Downloads the largest rendition Telegram provides, validates size and MIME,
    and returns ``AgentMessage([ImageContent, TextContent?])`` (caption last,
    matching the way a human reads a photo + caption).

    Raises :class:`MediaError` (never a raw Telegram/size error) on a download
    failure, an oversize image, or an unsupported MIME type.
    """
    if not message.photo:
        raise MediaError("无法下载图片，请稍后重试。", "download_failed")
    # Telegram provides several renditions in ascending size; the last is the
    # largest (still bounded by Telegram's own photo limit).
    source = message.photo[-1]
    declared = getattr(source, "mime_type", None)

    try:
        # PhotoSize.get_file() resolves the Bot API File, which can be fetched
        # into memory. No temp file is used — the bytes live for this request only.
        remote_file = await source.get_file()
        data = bytes(await remote_file.download_as_bytearray())
    except Exception as exc:  # any Bot API / network failure
        logger.error("telegram image download failed", extra={"message_id": message.message_id}, exc_info=True)
        raise MediaError("无法下载图片，请稍后重试。", "download_failed") from exc

    if len(data) > max_bytes:
        # Refuse before we build anything — no huge payload reaches the LLM.
        logger.warning(
            "image exceeds size limit",
            extra={"message_id": message.message_id, "size_bytes": len(data), "limit_bytes": max_bytes},
        )
        raise MediaError("图片过大，暂时无法处理。", "image_too_large")

    mime = _resolve_mime(data, declared)

    # Log metadata only — never the bytes, base64, or any secret.
    logger.info(
        "image attached",
        extra={
            "message_id": message.message_id,
            "content_type": "image",
            "mime_type": mime,
            "size_bytes": len(data),
        },
    )

    parts: list = [ImageContent(data=data, mime_type=mime)]
    if message.caption:
        parts.append(TextContent(message.caption))
    return AgentMessage(contents=parts, source="telegram", metadata={"message_id": message.message_id})


async def normalize_message(message: Message, max_bytes: int) -> AgentMessage:
    """Turn a Telegram ``Message`` (text *or* photo) into an ``AgentMessage``.

    * a photo (with or without a caption) → :func:`extract_image_message`;
    * a plain text message → a single :class:`TextContent` (Unicode/emoji kept
      verbatim, no special handling).
    """
    if message.photo:
        return await extract_image_message(message, max_bytes)
    text = _text_of(message)
    parts = [TextContent(text)] if text else []
    return AgentMessage(contents=parts, source="telegram", metadata={"message_id": message.message_id})
