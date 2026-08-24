"""Telegram adapter: authorization and long-message chunking.

The handlers are exercised with lightweight fakes — no real Telegram.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fibrecase_agent_backend.telegram.bot import (
    CHUNK_SIZE,
    _is_authorized,
    cmd_start,
    handle_message,
    split_into_chunks,
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
def _make_context(*, user_id, chat_id, text=None, allowed=(1,), bot_data=None):
    chat = SimpleNamespace(id=chat_id, send_message=AsyncMock(), send_chat_action=AsyncMock())
    message = None
    if text is not None:
        message = SimpleNamespace(text=text, message_id=1, user=None, chat=chat)
    application = SimpleNamespace(bot_data=bot_data if bot_data is not None else {"allowed_user_ids": set(allowed)})
    user = None if user_id is None else SimpleNamespace(id=user_id)
    return SimpleNamespace(user=user, chat=chat, message=message, application=application)


# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------
async def test_authorized_user_passes():
    ctx = _make_context(user_id=1, chat_id=1, allowed=(1,))
    assert _is_authorized(ctx) is True


async def test_unauthorized_user_rejected():
    ctx = _make_context(user_id=999, chat_id=1, allowed=(1,))
    assert _is_authorized(ctx) is False


async def test_unknown_user_rejected():
    ctx = _make_context(user_id=None, chat_id=1, allowed=(1,))
    assert _is_authorized(ctx) is False


async def test_multiple_allowed_ids():
    ctx = _make_context(user_id=2, chat_id=1, allowed=(1, 2))
    assert _is_authorized(ctx) is True


async def test_unauthorized_message_is_not_replied_to():
    """An unauthorised message must not produce any outgoing message."""
    ctx = _make_context(user_id=999, chat_id=1, text="hello", allowed=(1,))
    await handle_message(None, ctx)
    ctx.chat.send_message.assert_not_awaited()
    ctx.chat.send_chat_action.assert_not_awaited()


async def test_unauthorized_command_is_not_replied_to():
    ctx = _make_context(user_id=999, chat_id=1, text="/start", allowed=(1,))
    await cmd_start(None, ctx)
    ctx.chat.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------
def test_short_message_single_chunk():
    assert split_into_chunks("hi") == ["hi"]


def test_chunks_preserve_all_content():
    text = "line\n" * 500 + "tail"
    chunks = split_into_chunks(text, limit=200)
    assert "".join(chunks) == text, "chunking must never lose or add content"
    assert all(len(c) <= 200 for c in chunks)


def test_default_limit_under_telegram_cap():
    assert CHUNK_SIZE <= 4096


def test_hard_split_of_huge_line():
    text = "x" * 10_000  # one enormous line, no newlines
    chunks = split_into_chunks(text, limit=400)
    assert "".join(chunks) == text
    assert all(len(c) <= 400 for c in chunks)


def test_prefers_newline_boundaries():
    text = ("a\n" * 300)  # many short lines
    chunks = split_into_chunks(text, limit=100)
    assert "".join(chunks) == text
    # Each chunk (except possibly the last) should be near the limit and not
    # split a line: every boundary is at a newline.
    for c in chunks[:-1]:
        assert c.endswith("\n"), "chunks should break on newlines where possible"
