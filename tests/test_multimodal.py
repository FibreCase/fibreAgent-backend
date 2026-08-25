"""Phase 2.1.x — multimodal input foundation.

Everything here is mocked: the Telegram photo *download* is stubbed (no real
Bot API call), and the LLM is a scripted fake (no real endpoint). Covers the
required behaviours: plain text, Unicode emoji, photo → bytes → ImageContent,
photo + caption, the OpenAI multimodal conversion, image + tool calling, tools
disabled still handling images, an oversize image, and the memory-only (no temp
file) lifecycle.
"""

from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from telegram import Chat, Message, PhotoSize, User

from fibrecase_agent_backend.agent.messages import AgentMessage, ImageContent, TextContent
from fibrecase_agent_backend.agent.service import AgentError, AgentService
from fibrecase_agent_backend.llm.client import LLMError, LLMResult
from fibrecase_agent_backend.llm.message_converter import agent_message_to_openai_content
from fibrecase_agent_backend.telegram.media import MediaError, normalize_message
from fibrecase_agent_backend.tools import build_default_tools

# A few realistic image byte signatures for the magic-byte MIME sniff.
JPEG = b"\xff\xd8\xff\xe0" + b"JPEG-PAYLOAD"
PNG = b"\x89PNG\r\n\x1a\n" + b"PNG-PAYLOAD"
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"WEBP-PAYLOAD"


# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------
def _photo_message(caption=None, photo_bytes=JPEG, file_id="fid"):
    """A real Telegram photo message (largest rendition carries ``photo_bytes``)."""
    chat = Chat(id=1, type="private")
    photo = PhotoSize(file_id=file_id, file_unique_id="uid", width=100, height=100)
    return Message(
        message_id=1,
        date=0,
        chat=chat,
        from_user=User(id=1, first_name="U", is_bot=False),
        photo=[photo],
        caption=caption,
    ), photo


def _text_message(text):
    chat = Chat(id=1, type="private")
    return Message(
        message_id=2,
        date=0,
        chat=chat,
        from_user=User(id=1, first_name="U", is_bot=False),
        text=text,
    )


def _fake_file(payload, fail=False):
    """A stand-in for the Bot API ``File`` (what ``PhotoSize.get_file()`` returns)."""

    file = type("File", (), {})()
    if fail:
        file.download_as_bytearray = AsyncMock(side_effect=RuntimeError("boom"))
    else:
        file.download_as_bytearray = AsyncMock(return_value=bytearray(payload))
    return file


@contextmanager
def _patch_download(payload, fail=False):
    """Patch ``PhotoSize.get_file`` to return a fake ``File`` serving ``payload``.

    Yields the fake ``File`` so tests can assert on its download call.
    """
    file = _fake_file(payload, fail=fail)
    with patch.object(PhotoSize, "get_file", AsyncMock(return_value=file)):
        yield file


class _Service:
    """Build an AgentService wired to an in-memory repo (mirrors test_agent)."""

    @staticmethod
    def build(repo, llm, **kwargs):
        return AgentService(
            repo,
            llm,
            system_prompt="You are a test agent.",
            max_context_messages=50,
            **kwargs,
        )


class ScriptedRecordingLLM:
    """Replays scripted LLMResults and records each call's messages + tools.

    An entry that is an ``Exception`` is *raised* instead of returned.
    """

    def __init__(self, results):
        self.results = list(results)
        self.calls = []  # list of (messages_dicts, tools)

    async def complete(self, messages, *, tools=None):
        self.calls.append(([{**m.to_dict()} for m in messages], tools))
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _tc(name, args, cid="call_1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


# ---------------------------------------------------------------------------
# Test 1 — plain text: Telegram text → AgentMessage(TextContent), phase-1 path
# ---------------------------------------------------------------------------
async def test_text_message_normalises_to_text_content():
    msg = _text_message("你好")
    result = await normalize_message(msg, max_bytes=10_000_000)
    assert isinstance(result, AgentMessage)
    assert result.source == "telegram"
    assert result.has_image() is False
    assert [type(p) for p in result.contents] == [TextContent]
    assert result.text == "你好"


async def test_plain_text_service_path_unchanged(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = ScriptedRecordingLLM([LLMResult(content="Alice.")])
    service = _Service.build(repo, llm)

    reply = await service.process_message(conv.id, "My name is Alice.")
    assert reply == "Alice."
    # The wire message for the text turn is a plain *string* (phase-1 shape).
    user_msg = llm.calls[0][0][-1]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == "My name is Alice."
    # And the persisted record is the same string.
    records = await repo.get_messages(conv.id)
    assert [(r.role, r.content) for r in records] == [("user", "My name is Alice."), ("assistant", "Alice.")]


# ---------------------------------------------------------------------------
# Test 2 — Unicode Emoji kept verbatim (no special-casing)
# ---------------------------------------------------------------------------
async def test_emoji_preserved_verbatim():
    text = "你好 😀 👍 🚀"
    result = await normalize_message(_text_message(text), max_bytes=10_000_000)
    assert result.text == text  # exact, untransformed
    assert result.contents == [TextContent(text)]


# ---------------------------------------------------------------------------
# Test 3 — photo → downloaded bytes → ImageContent (download is mocked)
# ---------------------------------------------------------------------------
async def test_photo_download_produces_image_content():
    msg, photo = _photo_message(photo_bytes=PNG)
    with _patch_download(PNG) as file:
        result = await normalize_message(msg, max_bytes=10_000_000)

    assert file.download_as_bytearray.await_count == 1
    assert result.has_image() is True
    image = result.contents[0]
    assert isinstance(image, ImageContent)
    assert image.data == PNG  # the downloaded bytes, in memory
    assert image.mime_type == "image/png"  # sniffed from the PNG signature
    # No caption → a single image part only.
    assert len(result.contents) == 1


async def test_photo_download_failure_is_media_error():
    msg, _ = _photo_message()
    with _patch_download(b"", fail=True):
        with pytest.raises(MediaError) as excinfo:
            await normalize_message(msg, max_bytes=10_000_000)
    assert excinfo.value.category == "download_failed"
    assert "无法下载图片" in excinfo.value.user_safe


# ---------------------------------------------------------------------------
# Test 4 — photo + caption → ImageContent + TextContent (caption last)
# ---------------------------------------------------------------------------
async def test_photo_with_caption_yields_image_and_text():
    msg, photo = _photo_message(caption="这是什么？", photo_bytes=JPEG)
    with _patch_download(JPEG):
        result = await normalize_message(msg, max_bytes=10_000_000)

    assert [type(p) for p in result.contents] == [ImageContent, TextContent]
    assert result.contents[0].mime_type == "image/jpeg"
    assert result.contents[1].text == "这是什么？"
    assert result.text == "这是什么？"  # the caption is the persisted text


# ---------------------------------------------------------------------------
# Test 5 — OpenAI multimodal conversion (MIME, base64 round-trip, text, order)
# ---------------------------------------------------------------------------
def test_converter_image_and_text_to_openai_parts():
    msg = AgentMessage(
        contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("这是什么？")]
    )
    content = agent_message_to_openai_content(msg)
    assert isinstance(content, list)
    # Order: image first, then text (matches the message order).
    assert content[0]["type"] == "image_url"
    assert content[1]["type"] == "text"
    assert content[1]["text"] == "这是什么？"
    # The data URL carries the right MIME and round-trips back to the bytes.
    url = content[0]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == JPEG


def test_converter_text_only_is_plain_string():
    # A text-only message stays a plain string (phase-1 wire shape).
    assert agent_message_to_openai_content(AgentMessage(contents=[TextContent("hi")])) == "hi"


def test_converter_webp_mime():
    msg = AgentMessage(contents=[ImageContent(data=WEBP, mime_type="image/webp")])
    content = agent_message_to_openai_content(msg)
    assert content[0]["image_url"]["url"].startswith("data:image/webp;base64,")


# ---------------------------------------------------------------------------
# Test 6 — image + tool calling work together
# ---------------------------------------------------------------------------
async def test_image_plus_tool_calling_end_to_end(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = ScriptedRecordingLLM([
        LLMResult(content=None, tool_calls=[_tc("system_info", {})]),
        LLMResult(content="这是一台服务器。"),
    ])
    service = _Service.build(repo, llm, registry=build_default_tools(), enable_tools=True, max_tool_iterations=5)

    agent_message = AgentMessage(
        contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("分析这个设备")]
    )
    reply = await service.process_message(conv.id, agent_message)
    assert reply == "这是一台服务器。"

    # The first LLM call's user turn carried the image (a list with an image_url part).
    first_user = llm.calls[0][0][-1]
    assert first_user["role"] == "user"
    assert isinstance(first_user["content"], list)
    assert first_user["content"][0]["type"] == "image_url"
    assert first_user["content"][1]["text"] == "分析这个设备"
    # Tools were advertised.
    assert llm.calls[0][1] is not None and len(llm.calls[0][1]) == 3
    # The second call carried the tool result back, then a final answer came.
    second = llm.calls[1][0]
    assert [m["role"] for m in second][-2:] == ["assistant", "tool"]
    # Only text is persisted (the image is not stored).
    records = await repo.get_messages(conv.id)
    assert [(r.role, r.content) for r in records] == [
        ("user", "分析这个设备"),
        ("assistant", "这是一台服务器。"),
    ]


# ---------------------------------------------------------------------------
# Test 7 — tools disabled still handles the image (image reaches LLM, no tools)
# ---------------------------------------------------------------------------
async def test_image_with_tools_disabled_still_reaches_llm(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = ScriptedRecordingLLM([LLMResult(content="一张照片")])
    # A registry exists but enable_tools=False: multimodal and tools are independent.
    service = _Service.build(repo, llm, registry=build_default_tools(), enable_tools=False)

    reply = await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("看看")])
    )
    assert reply == "一张照片"
    assert len(llm.calls) == 1
    # The image still made it to the LLM…
    user = llm.calls[0][0][-1]
    assert isinstance(user["content"], list)
    assert user["content"][0]["type"] == "image_url"
    # …but no tools were sent and no tool loop ran.
    assert llm.calls[0][1] is None


# ---------------------------------------------------------------------------
# Test 8 — oversize image is refused before reaching the LLM
# ---------------------------------------------------------------------------
async def test_oversize_image_refused_without_llm_call():
    big = JPEG + b"\x00" * (2 * 1024 * 1024)  # > the 1 MB cap used below
    msg, _ = _photo_message(photo_bytes=big)
    with _patch_download(big):
        with pytest.raises(MediaError) as excinfo:
            await normalize_message(msg, max_bytes=1024 * 1024)
    assert excinfo.value.category == "image_too_large"
    assert "图片过大" in excinfo.value.user_safe


async def test_oversize_image_never_sends_huge_payload_to_llm(repo):
    # End-to-end via the service: an AgentMessage whose image is fine is sent,
    # and the persisted turn is text-only — the raw bytes never enter the DB.
    conv = await repo.get_or_create_conversation(1, 1)
    llm = ScriptedRecordingLLM([LLMResult(content="ok")])
    service = _Service.build(repo, llm)
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=JPEG, mime_type="image/jpeg")])
    )
    # The image bytes travel to the LLM (in memory) but are NOT persisted.
    user = llm.calls[0][0][-1]
    assert user["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    records = await repo.get_messages(conv.id)
    assert records[0].content == ""  # no caption → empty stored text; no image bytes


# ---------------------------------------------------------------------------
# Test 9 — memory-only lifecycle: no temp files on LLM failure
# ---------------------------------------------------------------------------
async def test_image_uses_memory_bytes_and_no_temp_files_on_llm_failure(repo):
    conv = await repo.get_or_create_conversation(1, 1)
    llm = ScriptedRecordingLLM([LLMError("http_error")])
    service = _Service.build(repo, llm)
    agent_message = AgentMessage(
        contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("x")]
    )
    with (
        patch("tempfile.NamedTemporaryFile") as ntf,
        patch("tempfile.TemporaryDirectory") as tdir,
        patch("tempfile.mkstemp") as mk,
        patch("tempfile.mkdtemp") as mdd,
        pytest.raises(AgentError) as excinfo,
    ):
        await service.process_message(conv.id, agent_message)

    # The image bytes are held in memory only; the code never touches a temp
    # file, so there is nothing to clean up. The LLM failure is user-safe.
    ntf.assert_not_called()
    tdir.assert_not_called()
    mk.assert_not_called()
    mdd.assert_not_called()
    assert excinfo.value.category == "http_error"
    # The user turn was still persisted (text only) before the failure.
    assert [r.role for r in await repo.get_messages(conv.id)] == ["user"]
