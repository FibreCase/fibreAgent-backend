"""Convert a channel-independent :class:`AgentMessage` to an OpenAI payload.

This is the *only* place that turns the agent-layer content model into the
OpenAI-compatible wire shape. It is intentionally a pure function — it reads
:class:`~fibrecase_agent_backend.agent.messages` types and emits plain dicts
ready for the SDK, so it can be unit-tested without the OpenAI SDK, Telegram, or
any network.

Image parts are inlined as ``data:`` URLs (base64) so the LLM endpoint never has
to reach out to Telegram (or anywhere else) to fetch media.

A message with no image parts renders to a **plain string** (the concatenation
of its text parts) — byte-for-byte what the OpenAI client already sends today,
so the phase-one text path is unchanged. Only when an image is present is the
``content`` field an *array* of typed parts.
"""

from __future__ import annotations

import base64

from ..agent.messages import AgentMessage, ContentPart, ImageContent, TextContent


def _image_url(part: ImageContent) -> dict:
    """Render one image part as an OpenAI ``image_url`` content part.

    ``url`` is a self-contained ``data:<mime>;base64,<payload>`` string so the
    image travels with the request and the endpoint needs no outbound access.
    """
    payload = base64.b64encode(part.data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{part.mime_type};base64,{payload}"}}


def _render(part: ContentPart) -> dict:
    """Render a single content part to its OpenAI content-part dict."""
    if isinstance(part, TextContent):
        return {"type": "text", "text": part.text}
    if isinstance(part, ImageContent):
        return _image_url(part)
    raise TypeError(f"unsupported content part: {part!r}")


def agent_message_to_openai_content(message: AgentMessage) -> str | list[dict]:
    """Return the OpenAI ``content`` field for an :class:`AgentMessage`.

    * Text-only → a plain ``str`` (the joined text), matching the existing
      single-string behaviour for phase-one messages.
    * Any image → a ``list`` of typed parts in the message's order
      (``{"type": "text", ...}`` and ``{"type": "image_url", ...}``).

    An empty message yields ``""``.
    """
    if not any(isinstance(part, ImageContent) for part in message.contents):
        return "".join(part.text for part in message.contents if isinstance(part, TextContent))
    return [_render(part) for part in message.contents]
