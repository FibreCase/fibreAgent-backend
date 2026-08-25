"""Channel-independent inbound message model.

A transport (today Telegram, later a web UI, an HTTP API, a camera, …) normalises
everything it receives into one of these channel-independent types before handing
it to :class:`~fibrecase_agent_backend.agent.service.AgentService`. The agent
layer must never see a ``telegram.Message`` / ``PhotoSize`` / ``file_id`` — those
are the adapter's private concern.

Only :class:`TextContent` and :class:`ImageContent` exist in this phase. The
:class:`ContentPart` union is deliberately shaped so a future ``FileContent`` /
``AudioContent`` / ``VideoContent`` / ``StickerContent`` can be added without
touching the agent, tool loop, or the OpenAI converter's dispatch (each part
knows how to render itself; see :mod:`..llm.message_converter`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union


@dataclass(frozen=True)
class TextContent:
    """A plain-text part.

    Ordinary Unicode (including emoji such as ``你好 😀 👍 🚀``) is carried
    verbatim — there is *no* special-casing of emoji or any other text. A photo
    caption is represented as a :class:`TextContent` just like a typed message.
    """

    text: str


@dataclass(frozen=True)
class ImageContent:
    """An in-memory image part.

    ``data`` holds the raw image bytes (kept in memory for the single LLM
    request that consumes it — never written to disk, never persisted).
    ``mime_type`` is one of the supported image MIME types; ``filename`` is
    optional metadata. The bytes are base64-encoded at OpenAI-wire time only.
    """

    data: bytes
    mime_type: str
    filename: str | None = None


#: The content a message may carry. Grows over time (FileContent, AudioContent, …).
ContentPart = Union[TextContent, ImageContent]


@dataclass(frozen=True)
class AgentMessage:
    """A normalised inbound message, independent of the originating channel.

    ``contents`` is the ordered list of parts (e.g. ``[ImageContent, TextContent]``
    for a photo with a caption). ``source`` names the channel (``"telegram"``)
    and ``metadata`` carries any channel-specific, non-sensitive details
    (e.g. a message id for logging).
    """

    contents: list[ContentPart]
    source: str = "telegram"
    metadata: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The concatenation of the text parts ("" if none).

        This is what gets *persisted* to the conversation store — the
        channel-independent, human-readable representation of the message.
        """
        return "".join(part.text for part in self.contents if isinstance(part, TextContent))

    def has_image(self) -> bool:
        return any(isinstance(part, ImageContent) for part in self.contents)

    def is_empty(self) -> bool:
        """True if the message carries neither text nor media."""
        return all(not isinstance(part, ImageContent) and not part.text.strip()
                   for part in self.contents)
