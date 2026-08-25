"""Phase 2.2 — persistent image attachment storage.

Everything is mocked: the LLM is a scripted fake (no real endpoint), and blobs
are written to pytest's ``tmp_path`` (never the repo's real ``data/``). Covers
the 20 required behaviours:

* **store / db** — content-addressed save/read (JPEG/PNG/WebP), dedup
  (one blob, many references), fresh-DB init *and* a simulated v1.3.0 upgrade,
  detached records (no lazy load), and no attachment for a text-only message.
* **service / LLM context** — image persisted + sent in the current turn,
  history image replayed on a follow-up, a genuine cross-restart rebuild,
  out-of-window images neither read nor sent, multi-part order + ``position``,
  missing-blob skip (text kept), write-failure compensation, image still
  replayable after an LLM error, and tools-on / tools-off replay.
* **/new GC** — orphan blob + metadata removed, shared blobs kept until the
  last reference, and a reset that succeeds even when a blob is missing / its
  delete fails.
* **regression / safety** — the plain-text wire shape is unchanged with a store
  attached, and logs carry no bytes / base64 / caption / token / key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import inspect

from fibrecase_agent_backend.agent.messages import AgentMessage, ImageContent, TextContent
from fibrecase_agent_backend.agent.service import AgentError, AgentService
from fibrecase_agent_backend.attachments import (
    AttachmentStore,
    AttachmentStorageError,
)
from fibrecase_agent_backend.database.models import Base, Conversation, Message
from fibrecase_agent_backend.database.repository import ConversationRepository
from fibrecase_agent_backend.database.session import (
    create_engine,
    create_session_factory,
    init_db,
)
from fibrecase_agent_backend.llm.client import LLMError, LLMResult
from fibrecase_agent_backend.tools import build_default_tools

# Distinct, minimal byte signatures for the three supported image formats.
JPEG = b"\xff\xd8\xff\xe0" + b"JPEG-PAYLOAD-ATTACH"
PNG = b"\x89PNG\r\n\x1a\n" + b"PNG-PAYLOAD-ATTACH"
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"WEBP-PAYLOAD-ATTACH"


# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------
class RecordingMultimodalLLM:
    """Replays scripted ``LLMResult``s, recording each call's messages + tools.

    ``self.calls`` is a list of ``(messages_dicts, tools)``. An entry that is an
    ``Exception`` is *raised* instead of returned (e.g. ``LLMError``).
    """

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[tuple[list[dict], object]] = []

    async def complete(self, messages, *, tools=None):
        self.calls.append(([{**m.to_dict()} for m in messages], tools))
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RecordingStore(AttachmentStore):
    """An AttachmentStore that records which digests were read from disk."""

    def __init__(self, root):
        super().__init__(root)
        self.reads: list[str] = []

    def read(self, digest: str) -> bytes:
        self.reads.append(digest)
        return super().read(digest)


class _FailingAttachRepo:
    """Wraps a repository but makes ``add_message_attachments`` fail.

    Used to simulate a DB write failure *after* the blob has hit disk, which
    must trigger compensation and a user-safe error (no LLM call).
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def add_message_attachments(self, message_id, specs):
        raise RuntimeError("simulated attachment metadata write failure")


def _service_with_store(repo, llm, store, max_context=50, **kwargs) -> AgentService:
    return AgentService(
        repo,
        llm,
        system_prompt="You are a test agent.",
        max_context_messages=max_context,
        attachment_store=store,
        **kwargs,
    )


def _image_user_call(ctx) -> list[dict]:
    """User turns in a recorded LLM context that carry an image part (any position)."""
    return [
        m
        for m in ctx
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and any(part.get("type") == "image_url" for part in m["content"])
    ]


def _decode_data_url(part: dict) -> bytes:
    url = part["image_url"]["url"]
    assert url.split(",", 1)[0].startswith("data:")
    return base64.b64decode(url.split(",", 1)[1])


async def _table_names(engine) -> set[str]:
    async with engine.connect() as conn:
        def _sync(sync_conn):
            return set(inspect(sync_conn).get_table_names())
        return await conn.run_sync(_sync)


def _tc(name, args, cid="call_1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


# ===========================================================================
# 1-5 — store & database
# ===========================================================================
async def test_store_save_read_jpeg_png_webp(tmp_path):
    store = AttachmentStore(tmp_path / "attach")
    for payload, mime in [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")]:
        blob = store.save(payload)
        # Content-addressed path: <root>/<first-two>/<full-digest>.
        expected = tmp_path / "attach" / blob.sha256[:2] / blob.sha256
        assert expected.exists()
        assert blob.size_bytes == len(payload)
        # Read back: bytes identical, and still content-addressed.
        assert store.read(blob.sha256) == payload
        # The blob id is the SHA-256 of the bytes.
        import hashlib

        assert blob.sha256 == hashlib.sha256(payload).hexdigest()


async def test_dedup_one_blob_two_message_references(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="ok"), LLMResult(content="ok")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    # The SAME image bytes in two messages (different captions).
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("first")])
    )
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("second")])
    )
    # One blob on disk (deduped)…
    assert len(list(store.iter_blobs())) == 1
    # …but two attachment records reference it.
    wa = await repo.get_messages_with_attachments(conv.id)
    refs = [att for m in wa if m.role == "user" for att in m.attachments]
    assert len(refs) == 2
    assert refs[0].sha256 == refs[1].sha256


async def test_fresh_db_and_v13_upgrade_create_attachments_table():
    # (a) A brand-new database gets all three tables.
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp}/fresh.db")
        await init_db(engine)
        try:
            names = await _table_names(engine)
            assert {"conversations", "messages", "attachments"} <= names
        finally:
            await engine.dispose()

    # (b) A simulated v1.3.0 database (only conversations + messages) gains the
    #     attachments table on init_db, without losing existing messages.
    with tempfile.TemporaryDirectory() as tmp:
        db = f"{tmp}/v13.db"
        engine = create_engine(f"sqlite+aiosqlite:///{db}")
        async with engine.begin() as conn:
            # Create ONLY the two legacy tables (the phase-2.2 table is absent).
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Conversation.__table__, Message.__table__]))
        repo = ConversationRepository(create_session_factory(engine))
        conv = await repo.get_or_create_conversation(1, 1)
        await repo.add_message(conv.id, "user", "pre-existing message")
        await init_db(engine)  # must add the missing attachments table only
        try:
            assert "attachments" in await _table_names(engine)
            recs = await repo.get_messages(conv.id)
            assert [r.content for r in recs] == ["pre-existing message"]
        finally:
            await engine.dispose()


async def test_repo_returns_detached_records_no_lazy_load(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="ok")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=PNG, mime_type="image/png"), TextContent("pic")])
    )
    # This call opens and closes its own session internally.
    records = await repo.get_messages_with_attachments(conv.id)
    # After the session has closed, the attachment data must still be readable —
    # i.e. it was eager-loaded into detached records, not a live lazy
    # relationship (which would raise DetachedInstanceError here).
    user = next(m for m in records if m.role == "user")
    assert len(user.attachments) == 1
    att = user.attachments[0]
    assert att.mime_type == "image/png"
    assert att.size_bytes == len(PNG)
    assert att.position == 0
    assert store.read(att.sha256) == PNG  # the digest round-trips


async def test_text_message_creates_no_attachment(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="ok")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    await service.process_message(conv.id, "just text")
    # No attachment record on either message.
    records = await repo.get_messages_with_attachments(conv.id)
    assert all(not m.has_attachments() for m in records)
    # The legacy get_messages() is unchanged in behaviour.
    plain = await repo.get_messages(conv.id)
    assert [(r.role, r.content) for r in plain] == [("user", "just text"), ("assistant", "ok")]
    # And nothing was written to the store.
    assert list(store.iter_blobs()) == []


# ===========================================================================
# 6-14 — service & LLM context
# ===========================================================================
async def test_photo_caption_persists_and_sends_current_image(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="ok")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("记住这张图")])
    )
    # The current LLM call's user turn is a list: image data URL + caption.
    user_call = llm.calls[0][0][-1]
    assert user_call["role"] == "user"
    assert isinstance(user_call["content"], list)
    assert user_call["content"][0]["type"] == "image_url"
    assert user_call["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert _decode_data_url(user_call["content"][0]) == JPEG
    assert user_call["content"][1] == {"type": "text", "text": "记住这张图"}
    # DB: caption stored + attachment metadata; the blob is in the store.
    wa = await repo.get_messages_with_attachments(conv.id)
    user = next(m for m in wa if m.role == "user")
    assert user.content == "记住这张图"
    assert len(user.attachments) == 1
    att = user.attachments[0]
    assert att.mime_type == "image/jpeg"
    assert att.size_bytes == len(JPEG)
    assert store.exists(att.sha256)


async def test_history_image_replayed_on_followup(repo, tmp_path):
    store = RecordingStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="first"), LLMResult(content="second")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=PNG, mime_type="image/png"), TextContent("记住这张图")])
    )
    reads_after_first = len(store.reads)  # 0 — nothing to rehydrate yet
    # Plain-text follow-up; no second Telegram download happens (service never
    # talks to Telegram) — the image can only come from the store.
    await service.process_message(conv.id, "这张图有什么？")
    rehyd = _image_user_call(llm.calls[1][0])
    assert len(rehyd) == 1
    assert rehyd[0]["content"][0]["type"] == "image_url"
    assert _decode_data_url(rehyd[0]["content"][0]) == PNG
    assert rehyd[0]["content"][1] == {"type": "text", "text": "记住这张图"}
    # The store was actually read to rehydrate (proof of the source).
    assert len(store.reads) > reads_after_first
    # Still exactly one blob on disk (the follow-up created nothing new).
    assert len(list(AttachmentStore(tmp_path / "a").iter_blobs())) == 1


async def test_image_survives_rebuild_across_restart(tmp_path):
    db = f"{tmp_path}/agent.db"
    store_root = tmp_path / "attach"

    # --- first process: receive + persist the image ---
    engine1 = create_engine(f"sqlite+aiosqlite:///{db}")
    await init_db(engine1)
    repo1 = ConversationRepository(create_session_factory(engine1))
    store1 = AttachmentStore(store_root)
    llm1 = RecordingMultimodalLLM([LLMResult(content="hi")])
    svc1 = _service_with_store(repo1, llm1, store1)
    conv = await repo1.get_or_create_conversation(1, 1)
    await svc1.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=PNG, mime_type="image/png"), TextContent("记住")])
    )
    await engine1.dispose()

    # --- "restart": brand-new engine/repo/store over the SAME db + store dir ---
    engine2 = create_engine(f"sqlite+aiosqlite:///{db}")
    await init_db(engine2)  # idempotent; attachments table already exists
    repo2 = ConversationRepository(create_session_factory(engine2))
    store2 = AttachmentStore(store_root)  # same dir -> same blobs
    llm2 = RecordingMultimodalLLM([LLMResult(content="it's there")])
    svc2 = _service_with_store(repo2, llm2, store2)
    conv2 = await repo2.get_conversation(1)
    assert conv2 is not None and conv2.id == conv.id  # conversation survived
    await svc2.process_message(conv2.id, "还是那张图")
    rehyd = _image_user_call(llm2.calls[0][0])
    assert len(rehyd) == 1
    assert _decode_data_url(rehyd[0]["content"][0]) == PNG
    await engine2.dispose()


async def test_out_of_window_image_not_read_or_sent(repo, tmp_path):
    store = RecordingStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="ok")] * 5)
    service = _service_with_store(repo, llm, store, max_context=2)
    conv = await repo.get_or_create_conversation(1, 1)
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=PNG, mime_type="image/png"), TextContent("pic")])
    )
    await service.process_message(conv.id, "t1")
    await service.process_message(conv.id, "t2")
    # The image message is now far outside the 2-message window.
    reads_before = len(store.reads)
    await service.process_message(conv.id, "t3")
    # Out-of-window: the blob was not read and no image part was sent.
    assert len(store.reads) == reads_before
    assert not any(isinstance(m["content"], list) for m in llm.calls[-1][0])


async def test_multi_part_order_and_position_preserved(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="a"), LLMResult(content="b"), LLMResult(content="c")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    # [Image, Text] (position 0 image) and [Text, Image] (position 1 image).
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("图在前")])
    )
    await service.process_message(
        conv.id, AgentMessage(contents=[TextContent("字在前"), ImageContent(data=PNG, mime_type="image/png")])
    )
    # Replay on the next turn.
    await service.process_message(conv.id, "顺序？")
    ctx = llm.calls[2][0]
    rehyd = _image_user_call(ctx)
    assert len(rehyd) == 2
    # Order within each message is preserved: [image, text] then [text, image].
    assert rehyd[0]["content"][0]["type"] == "image_url"
    assert rehyd[0]["content"][1]["type"] == "text"
    assert rehyd[1]["content"][0]["type"] == "text"
    assert rehyd[1]["content"][1]["type"] == "image_url"


async def test_missing_blob_skips_image_keeps_text(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="a"), LLMResult(content="b")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=PNG, mime_type="image/png"), TextContent("pic")])
    )
    blob = next(iter(store.iter_blobs()))
    store.delete(blob)  # simulate a lost / corrupt blob
    # Follow-up: the image is skipped but its text is kept; no exception.
    await service.process_message(conv.id, "还在吗")
    ctx = llm.calls[1][0]
    first_user = [m for m in ctx if m["role"] == "user"][0]
    assert first_user["content"] == "pic"  # plain text now — image dropped
    assert not isinstance(first_user["content"], list)


async def test_write_failure_compensates_and_skips_llm(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="must not be called")])
    wrapped = _FailingAttachRepo(repo)
    service = _service_with_store(wrapped, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    with pytest.raises(AgentError) as exc:
        await service.process_message(
            conv.id, AgentMessage(contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("x")])
        )
    assert exc.value.category == "attachment_error"
    # No LLM call happened for an image that could not be persisted.
    assert llm.calls == []
    # Compensation removed the newly-created, unreferenced blob.
    assert list(store.iter_blobs()) == []


async def test_image_replayable_after_llm_error(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMError("http_error"), LLMResult(content="ok")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    with pytest.raises(AgentError) as exc:
        await service.process_message(
            conv.id, AgentMessage(contents=[ImageContent(data=PNG, mime_type="image/png"), TextContent("pic")])
        )
    assert exc.value.category == "http_error"
    # The user turn (text + image) was still persisted despite the LLM failure.
    wa = await repo.get_messages_with_attachments(conv.id)
    user = next(m for m in wa if m.role == "user")
    assert user.content == "pic" and len(user.attachments) == 1
    assert store.exists(user.attachments[0].sha256)
    # A later request can still replay the image.
    await service.process_message(conv.id, "还在吗")
    assert len(_image_user_call(llm.calls[1][0])) == 1


async def test_history_image_replay_with_tools(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([
        LLMResult(content=None, tool_calls=[_tc("echo", {"message": "x"})]),
        LLMResult(content="answer1"),
        LLMResult(content="answer2"),
    ])
    service = _service_with_store(
        repo, llm, store, registry=build_default_tools(), enable_tools=True, max_tool_iterations=5
    )
    conv = await repo.get_or_create_conversation(1, 1)
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("pic")])
    )
    await service.process_message(conv.id, "再看")
    # The follow-up's first (tool-loop) LLM call carried the history image…
    ctx = llm.calls[2][0]
    assert len(_image_user_call(ctx)) == 1
    # …and tools were advertised.
    assert llm.calls[2][1] is not None


async def test_history_image_replay_tools_disabled(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="a"), LLMResult(content="b")])
    service = _service_with_store(repo, llm, store, enable_tools=False)
    conv = await repo.get_or_create_conversation(1, 1)
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=PNG, mime_type="image/png"), TextContent("pic")])
    )
    await service.process_message(conv.id, "再看")
    ctx = llm.calls[1][0]
    assert len(_image_user_call(ctx)) == 1
    # No tools advertised when disabled.
    assert llm.calls[1][1] is None


# ===========================================================================
# 15-17 — /new GC & reclaim
# ===========================================================================
async def test_new_removes_orphan_blob_and_metadata(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="a")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("x")])
    )
    digest = next(iter(store.iter_blobs()))
    assert store.exists(digest)
    new_id = await service.reset(1, 1)
    assert new_id > conv.id  # a brand-new, larger conversation id
    # Blob and metadata are gone.
    assert not store.exists(digest)
    wa = await repo.get_messages_with_attachments(new_id)
    assert all(not m.has_attachments() for m in wa)
    assert list(store.iter_blobs()) == []


async def test_shared_blob_not_deleted_until_last_reference(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="a")] * 4)
    service = _service_with_store(repo, llm, store)
    a = await repo.get_or_create_conversation(1, 1)
    b = await repo.get_or_create_conversation(2, 1)
    # Both chats send the SAME bytes -> one deduped, shared blob.
    await service.process_message(
        a.id, AgentMessage(contents=[ImageContent(data=PNG, mime_type="image/png"), TextContent("a")])
    )
    await service.process_message(
        b.id, AgentMessage(contents=[ImageContent(data=PNG, mime_type="image/png"), TextContent("b")])
    )
    assert len(list(store.iter_blobs())) == 1
    digest = next(iter(store.iter_blobs()))
    # Resetting one of two references keeps the shared blob…
    await service.reset(1, 1)
    assert store.exists(digest), "a blob still referenced by another chat must survive"
    # …resetting the last reference removes it.
    await service.reset(2, 1)
    assert not store.exists(digest)


async def test_new_succeeds_when_blob_missing(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="a")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    other = await repo.get_or_create_conversation(2, 1)
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("x")])
    )
    store.delete(next(iter(store.iter_blobs())))  # already gone on disk
    # /new still creates a new conversation and leaves other chats untouched.
    new_id = await service.reset(1, 1)
    assert new_id > conv.id
    assert await repo.count_messages(other.id) == 0
    assert (await repo.get_conversation(1)).id == new_id


async def test_new_succeeds_when_blob_delete_raises(repo, tmp_path, monkeypatch):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="a")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    await service.process_message(
        conv.id, AgentMessage(contents=[ImageContent(data=JPEG, mime_type="image/jpeg"), TextContent("x")])
    )

    def _boom(digest):
        raise AttachmentStorageError("attachment_delete_failed")

    monkeypatch.setattr(store, "delete", _boom)
    # A failing delete must not prevent the new conversation from being created.
    new_id = await service.reset(1, 1)
    assert new_id > conv.id


async def test_store_delete_io_failure_raises_storage_error(tmp_path, monkeypatch):
    # Directly exercises the *real* delete() I/O path (not a monkeypatched
    # delete): a genuine unlink failure must surface as an
    # AttachmentStorageError (a kind of AttachmentStoreError) — not a
    # TypeError — so the /new GC path's `except AttachmentStoreError` still
    # catches it and never blocks the new conversation.
    store = AttachmentStore(tmp_path / "a")
    digest = store.save(JPEG).sha256
    assert store.exists(digest)

    real_unlink = Path.unlink

    def _boom(self):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "unlink", _boom)
    try:
        with pytest.raises(AttachmentStorageError) as exc:
            store.delete(digest)
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)
    assert exc.value.category == "attachment_delete_failed"
    # The blob is untouched on disk (the delete failed), so a later /new retry
    # can still find and remove it.
    assert store.exists(digest)


# ===========================================================================
# 18-19 — regression & safety
# ===========================================================================
async def test_text_path_unchanged_with_store_attached(repo, tmp_path):
    # Even with a store configured, a plain text message keeps the phase-1
    # wire shape (a plain str, no tools, no blobs).
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="Alice.")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    reply = await service.process_message(conv.id, "My name is Alice.")
    assert reply == "Alice."
    user_call = llm.calls[0][0][-1]
    assert user_call["content"] == "My name is Alice."  # str, not a list
    assert llm.calls[0][1] is None  # no tools
    assert list(store.iter_blobs()) == []


class _LogCapture(logging.Handler):
    """A minimal logging handler that records emitted records for inspection."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _installed_logging_names() -> list[str]:
    return ["agent", "agent.tools", "database", "attachments", "telegram", "main", "telegram.media"]


async def test_logs_do_not_leak_media_or_secrets(repo, tmp_path):
    store = AttachmentStore(tmp_path / "a")
    llm = RecordingMultimodalLLM([LLMResult(content="ok")])
    service = _service_with_store(repo, llm, store)
    conv = await repo.get_or_create_conversation(1, 1)
    secret_caption = "SECRET_CAPTION_独特文本"

    capture = _LogCapture()
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.DEBUG)
    for name in _installed_logging_names():
        logging.getLogger(name).addHandler(capture)
    try:
        await service.process_message(
            conv.id, AgentMessage(contents=[ImageContent(data=PNG, mime_type="image/png"), TextContent(secret_caption)])
        )
    finally:
        for name in _installed_logging_names():
            logging.getLogger(name).removeHandler(capture)
        root.setLevel(old_level)

    def _all_text(record) -> str:
        parts = [record.getMessage()]
        # The record's extra fields (digest prefix, mime, size, …) are stored as
        # attributes; inspect their values too.
        for value in vars(record).values():
            parts.append(str(value))
        return " ".join(parts)

    dumped = "\n".join(_all_text(r) for r in capture.records)
    b64 = base64.b64encode(PNG).decode("ascii")
    assert b64 not in dumped, "base64 image data must never be logged"
    assert secret_caption not in dumped, "captions must never be logged"
    digest = next(iter(store.iter_blobs()))
    assert digest not in dumped, "the full digest must not be logged"
    assert digest[:8] in dumped, "a short digest prefix is the only allowed form"
    for needle in ("Bearer", "API_KEY", "api_key", "TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY"):
        assert needle not in dumped
