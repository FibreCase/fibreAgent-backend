"""File toolset — real (hermetic) filesystem behaviour.

These tests exercise the eleven :mod:`fibrecase_agent_backend.tools.builtin.file`
tools against the **real** filesystem, confined to ``tmp_path`` — the only way a
file-manipulating tool can be meaningfully tested. Every test is local (no
network, no LLM, no Telegram, no DB). The path-confinement tests (the toolset's
core safety property) create a file *outside* the root (in ``tmp_path.parent``)
and assert it is never read or written — including through a symlink that points
out of the root.

Permission split under test: ``file_read`` / ``file_ls`` declare ``allow``
(read-only, no per-call approval); every mutating tool declares ``ask``.
"""

from __future__ import annotations

import json
import os

import pytest

from fibrecase_agent_backend.tools.builtin.file import (
    FileAppendTool,
    FileCpTool,
    FileEditTool,
    FileLsTool,
    FileMkdirTool,
    FileMvTool,
    FileReadTool,
    FileRmTool,
    FileRmdirTool,
    FileTouchTool,
    FileWriteTool,
)
from fibrecase_agent_backend.tools.policy import ToolPermission


def _read(root, max_read_chars: int = 100_000) -> FileReadTool:
    return FileReadTool(workdir=str(root), max_read_chars=max_read_chars)


def _ls(root, max_list_entries: int = 1000) -> FileLsTool:
    return FileLsTool(workdir=str(root), max_list_entries=max_list_entries)


def _edit(root, max_string_chars: int = 2000, max_read_chars: int = 100_000) -> FileEditTool:
    return FileEditTool(workdir=str(root), max_string_chars=max_string_chars, max_read_chars=max_read_chars)


def _write(root, max_content_chars: int = 100_000) -> FileWriteTool:
    return FileWriteTool(workdir=str(root), max_content_chars=max_content_chars)


def _append(root, max_content_chars: int = 100_000) -> FileAppendTool:
    return FileAppendTool(workdir=str(root), max_content_chars=max_content_chars)


def _mv(root) -> FileMvTool:
    return FileMvTool(workdir=str(root))


def _cp(root) -> FileCpTool:
    return FileCpTool(workdir=str(root))


def _rm(root) -> FileRmTool:
    return FileRmTool(workdir=str(root))


def _mkdir(root) -> FileMkdirTool:
    return FileMkdirTool(workdir=str(root))


def _rmdir(root) -> FileRmdirTool:
    return FileRmdirTool(workdir=str(root))


def _touch(root) -> FileTouchTool:
    return FileTouchTool(workdir=str(root))


def _parse(result: str) -> dict:
    return json.loads(result)


# ===========================================================================
# permission declarations
# ===========================================================================
def test_read_and_ls_declare_allow(tmp_path):
    assert _read(tmp_path).default_permission is ToolPermission.ALLOW
    assert _ls(tmp_path).default_permission is ToolPermission.ALLOW


def test_mutating_tools_declare_ask(tmp_path):
    for tool in (
        _edit(tmp_path),
        _write(tmp_path),
        _append(tmp_path),
        _mv(tmp_path),
        _cp(tmp_path),
        _rm(tmp_path),
        _mkdir(tmp_path),
        _rmdir(tmp_path),
        _touch(tmp_path),
    ):
        assert tool.default_permission is ToolPermission.ASK, tool.name


# ===========================================================================
# file_read
# ===========================================================================
async def test_read_returns_content_and_relative_path(tmp_path):
    (tmp_path / "a.txt").write_text("hello\nworld", encoding="utf-8")
    data = _parse(await _read(tmp_path).execute({"path": "a.txt"}))
    assert data["path"] == "a.txt"  # root-relative, not the absolute path
    assert data["content"] == "hello\nworld"


async def test_read_absolute_path_inside_root(tmp_path):
    (tmp_path / "b.txt").write_text("abs", encoding="utf-8")
    data = _parse(await _read(tmp_path).execute({"path": str(tmp_path / "b.txt")}))
    assert data["path"] == "b.txt"
    assert data["content"] == "abs"


async def test_read_tail_truncated_with_marker(tmp_path):
    (tmp_path / "big.txt").write_text("X" * 100, encoding="utf-8")
    data = _parse(await _read(tmp_path, max_read_chars=40).execute({"path": "big.txt"}))
    content = data["content"]
    assert content.startswith("[") and "truncated" in content
    assert content.endswith("X" * 40)  # the tail is kept


async def test_read_missing_file(tmp_path):
    data = _parse(await _read(tmp_path).execute({"path": "nope.txt"}))
    assert data["error"]["code"] == "file_not_found"


async def test_read_directory_is_not_a_file(tmp_path):
    (tmp_path / "subdir").mkdir()
    data = _parse(await _read(tmp_path).execute({"path": "subdir"}))
    assert data["error"]["code"] == "file_not_a_file"


async def test_read_non_utf8_is_read_failed(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
    data = _parse(await _read(tmp_path).execute({"path": "bin.dat"}))
    assert data["error"]["code"] == "file_read_failed"


async def test_read_empty_path_is_invalid(tmp_path):
    data = _parse(await _read(tmp_path).execute({"path": "   "}))
    assert data["error"]["code"] == "file_invalid_path"


# ===========================================================================
# file_ls
# ===========================================================================
async def test_ls_lists_files_and_dirs_with_marker(tmp_path):
    (tmp_path / "one.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    data = _parse(await _ls(tmp_path).execute({"path": "."}))
    assert data["path"] == "."
    assert data["entries"] == ["one.txt", "sub/"]
    assert data["truncated"] is False


async def test_ls_empty_directory(tmp_path):
    data = _parse(await _ls(tmp_path).execute({"path": "."}))
    assert data["entries"] == []
    assert data["truncated"] is False


async def test_ls_missing_path(tmp_path):
    data = _parse(await _ls(tmp_path).execute({"path": "nope"}))
    assert data["error"]["code"] == "file_not_found"


async def test_ls_file_is_not_a_directory(tmp_path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    data = _parse(await _ls(tmp_path).execute({"path": "f.txt"}))
    assert data["error"]["code"] == "file_not_a_directory"


async def test_ls_truncates_at_cap(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    data = _parse(await _ls(tmp_path, max_list_entries=3).execute({"path": "."}))
    assert data["entries"] == ["f0.txt", "f1.txt", "f2.txt"]
    assert data["truncated"] is True


# ===========================================================================
# file_edit — semantics
# ===========================================================================
async def test_edit_unique_updates_file(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("alpha beta gamma", encoding="utf-8")
    data = _parse(await _edit(tmp_path).execute({"path": "c.txt", "old_string": "beta", "new_string": "BETA"}))
    assert data["path"] == "c.txt"
    assert data["replacements"] == 1
    assert f.read_text(encoding="utf-8") == "alpha BETA gamma"


async def test_edit_empty_new_string_deletes(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("keep [drop] this", encoding="utf-8")
    data = _parse(await _edit(tmp_path).execute({"path": "d.txt", "old_string": "[drop]", "new_string": ""}))
    assert data["replacements"] == 1
    assert f.read_text(encoding="utf-8") == "keep  this"


async def test_edit_not_replaced(tmp_path):
    (tmp_path / "e.txt").write_text("only this here", encoding="utf-8")
    data = _parse(await _edit(tmp_path).execute({"path": "e.txt", "old_string": "absent", "new_string": "x"}))
    assert data["error"]["code"] == "file_not_replaced"


async def test_edit_not_unique_without_replace_all(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("dup dup dup", encoding="utf-8")
    data = _parse(await _edit(tmp_path).execute({"path": "f.txt", "old_string": "dup", "new_string": "X"}))
    assert data["error"]["code"] == "file_not_unique"
    assert f.read_text(encoding="utf-8") == "dup dup dup"  # untouched


async def test_edit_replace_all(tmp_path):
    f = tmp_path / "g.txt"
    f.write_text("a b a b a", encoding="utf-8")
    data = _parse(await _edit(tmp_path).execute({"path": "g.txt", "old_string": "a", "new_string": "A", "replace_all": True}))
    assert data["replacements"] == 3
    assert f.read_text(encoding="utf-8") == "A b A b A"


async def test_edit_missing_old_string_is_invalid(tmp_path):
    (tmp_path / "h.txt").write_text("x", encoding="utf-8")
    data = _parse(await _edit(tmp_path).execute({"path": "h.txt", "new_string": "y"}))
    assert data["error"]["code"] == "file_invalid_args"


async def test_edit_missing_new_string_is_invalid(tmp_path):
    (tmp_path / "i.txt").write_text("x", encoding="utf-8")
    data = _parse(await _edit(tmp_path).execute({"path": "i.txt", "old_string": "x"}))
    assert data["error"]["code"] == "file_invalid_args"


async def test_edit_directory_is_not_a_file(tmp_path):
    (tmp_path / "s").mkdir()
    data = _parse(await _edit(tmp_path).execute({"path": "s", "old_string": "a", "new_string": "b"}))
    assert data["error"]["code"] == "file_not_a_file"


# ===========================================================================
# file_write — shell '>' semantics (create or replace entire content)
# ===========================================================================
async def test_write_creates_a_new_file(tmp_path):
    data = _parse(await _write(tmp_path).execute({"path": "new.txt", "content": "hello\nworld"}))
    assert data["path"] == "new.txt"
    assert data["bytes"] == len(b"hello\nworld")
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\nworld"


async def test_write_replaces_existing_content(tmp_path):
    f = tmp_path / "w.txt"
    f.write_text("old content, much longer than the new one", encoding="utf-8")
    data = _parse(await _write(tmp_path).execute({"path": "w.txt", "content": "new"}))
    assert data["bytes"] == 3
    assert f.read_text(encoding="utf-8") == "new"  # whole content replaced, not merged


async def test_write_empty_content_truncates_to_empty(tmp_path):
    f = tmp_path / "w2.txt"
    f.write_text("to be emptied", encoding="utf-8")
    await _write(tmp_path).execute({"path": "w2.txt", "content": ""})
    assert f.read_text(encoding="utf-8") == ""  # an empty write = a zero-byte file


async def test_write_refuses_a_directory(tmp_path):
    (tmp_path / "d").mkdir()
    data = _parse(await _write(tmp_path).execute({"path": "d", "content": "x"}))
    assert data["error"]["code"] == "file_not_a_file"


async def test_write_missing_content_is_invalid(tmp_path):
    data = _parse(await _write(tmp_path).execute({"path": "w.txt"}))
    assert data["error"]["code"] == "file_invalid_args"


async def test_write_content_utf8_byte_count(tmp_path):
    # bytes reports the UTF-8 byte length, not the character count.
    data = _parse(await _write(tmp_path).execute({"path": "u.txt", "content": "é中文"}))
    assert data["bytes"] == len("é中文".encode("utf-8"))
    assert (tmp_path / "u.txt").read_text(encoding="utf-8") == "é中文"


# ===========================================================================
# file_append — shell '>>' semantics (create if absent, else append)
# ===========================================================================
async def test_append_creates_a_new_file(tmp_path):
    data = _parse(await _append(tmp_path).execute({"path": "fresh.txt", "content": "first"}))
    assert data["path"] == "fresh.txt"
    assert data["bytes"] == len(b"first")
    assert (tmp_path / "fresh.txt").read_text(encoding="utf-8") == "first"


async def test_append_appends_to_existing_content(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("line1\n", encoding="utf-8")
    data = _parse(await _append(tmp_path).execute({"path": "log.txt", "content": "line2\n"}))
    assert data["bytes"] == len(b"line1\nline2\n")
    assert f.read_text(encoding="utf-8") == "line1\nline2\n"  # existing preserved + appended


async def test_append_does_not_truncate_existing(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("keep", encoding="utf-8")
    await _append(tmp_path).execute({"path": "a.txt", "content": "-me"})
    assert f.read_text(encoding="utf-8") == "keep-me"  # '>>' never discards the old content


async def test_append_refuses_a_directory(tmp_path):
    (tmp_path / "d").mkdir()
    data = _parse(await _append(tmp_path).execute({"path": "d", "content": "x"}))
    assert data["error"]["code"] == "file_not_a_file"


async def test_append_missing_content_is_invalid(tmp_path):
    data = _parse(await _append(tmp_path).execute({"path": "a.txt"}))
    assert data["error"]["code"] == "file_invalid_args"


async def test_append_rejects_resulting_file_over_cap(tmp_path):
    # The schema caps only the *appended* content; the *resulting* file size is
    # enforced separately, so a large pre-existing file blocks an append that
    # would push the total past the cap.
    f = tmp_path / "big.txt"
    f.write_text("A" * 100, encoding="utf-8")
    data = _parse(await _append(tmp_path, max_content_chars=150).execute({"path": "big.txt", "content": "B" * 51}))
    assert data["error"]["code"] == "file_result_too_large"
    assert f.read_text(encoding="utf-8") == "A" * 100  # untouched


async def test_append_allows_result_at_cap(tmp_path):
    f = tmp_path / "big2.txt"
    f.write_text("A" * 100, encoding="utf-8")
    data = _parse(await _append(tmp_path, max_content_chars=150).execute({"path": "big2.txt", "content": "B" * 50}))
    assert data["bytes"] == 150  # exactly at the cap is allowed
    assert f.read_text(encoding="utf-8") == "A" * 100 + "B" * 50


# ===========================================================================
# file_mv
# ===========================================================================
async def test_mv_renames_a_file(tmp_path):
    (tmp_path / "src.txt").write_text("data", encoding="utf-8")
    data = _parse(await _mv(tmp_path).execute({"source": "src.txt", "target": "dst.txt"}))
    assert data["source"] == "src.txt" and data["target"] == "dst.txt"
    assert not (tmp_path / "src.txt").exists()
    assert (tmp_path / "dst.txt").read_text(encoding="utf-8") == "data"


async def test_mv_moves_a_directory(tmp_path):
    (tmp_path / "d" / "inner.txt").parent.mkdir()
    (tmp_path / "d" / "inner.txt").write_text("x", encoding="utf-8")
    await _mv(tmp_path).execute({"source": "d", "target": "moved"})
    assert (tmp_path / "moved" / "inner.txt").read_text(encoding="utf-8") == "x"
    assert not (tmp_path / "d").exists()


async def test_mv_target_must_not_exist(tmp_path):
    (tmp_path / "src.txt").write_text("a", encoding="utf-8")
    (tmp_path / "dst.txt").write_text("b", encoding="utf-8")
    data = _parse(await _mv(tmp_path).execute({"source": "src.txt", "target": "dst.txt"}))
    assert data["error"]["code"] == "file_already_exists"
    assert (tmp_path / "src.txt").read_text(encoding="utf-8") == "a"  # untouched


async def test_mv_missing_source(tmp_path):
    data = _parse(await _mv(tmp_path).execute({"source": "ghost.txt", "target": "x.txt"}))
    assert data["error"]["code"] == "file_not_found"


# ===========================================================================
# file_cp
# ===========================================================================
async def test_cp_copies_a_file(tmp_path):
    (tmp_path / "src.txt").write_text("data", encoding="utf-8")
    data = _parse(await _cp(tmp_path).execute({"source": "src.txt", "target": "copy.txt"}))
    assert data["target"] == "copy.txt"
    assert (tmp_path / "src.txt").exists()  # source kept
    assert (tmp_path / "copy.txt").read_text(encoding="utf-8") == "data"


async def test_cp_copies_a_directory_tree_with_recursive(tmp_path):
    (tmp_path / "src" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "src" / "sub" / "b.txt").write_text("b", encoding="utf-8")
    await _cp(tmp_path).execute({"source": "src", "target": "dst", "recursive": True})
    assert (tmp_path / "dst" / "a.txt").read_text(encoding="utf-8") == "a"
    assert (tmp_path / "dst" / "sub" / "b.txt").read_text(encoding="utf-8") == "b"


async def test_cp_directory_without_recursive_is_invalid(tmp_path):
    (tmp_path / "src").mkdir()
    data = _parse(await _cp(tmp_path).execute({"source": "src", "target": "dst"}))
    assert data["error"]["code"] == "file_invalid_args"


async def test_cp_target_must_not_exist(tmp_path):
    (tmp_path / "src.txt").write_text("a", encoding="utf-8")
    (tmp_path / "dst.txt").write_text("b", encoding="utf-8")
    data = _parse(await _cp(tmp_path).execute({"source": "src.txt", "target": "dst.txt"}))
    assert data["error"]["code"] == "file_already_exists"


# ===========================================================================
# file_rm — regular files only
# ===========================================================================
async def test_rm_deletes_a_file(tmp_path):
    (tmp_path / "gone.txt").write_text("x", encoding="utf-8")
    data = _parse(await _rm(tmp_path).execute({"path": "gone.txt"}))
    assert data["path"] == "gone.txt"
    assert not (tmp_path / "gone.txt").exists()


async def test_rm_refuses_a_directory(tmp_path):
    (tmp_path / "keepme").mkdir()
    data = _parse(await _rm(tmp_path).execute({"path": "keepme"}))
    assert data["error"]["code"] == "file_is_directory"
    assert (tmp_path / "keepme").exists()  # untouched


async def test_rm_missing_file(tmp_path):
    data = _parse(await _rm(tmp_path).execute({"path": "ghost.txt"}))
    assert data["error"]["code"] == "file_not_found"


# ===========================================================================
# file_mkdir
# ===========================================================================
async def test_mkdir_creates_a_directory(tmp_path):
    data = _parse(await _mkdir(tmp_path).execute({"path": "newdir"}))
    assert data["path"] == "newdir"
    assert (tmp_path / "newdir").is_dir()


async def test_mkdir_refuses_existing(tmp_path):
    (tmp_path / "exist").mkdir()
    data = _parse(await _mkdir(tmp_path).execute({"path": "exist"}))
    assert data["error"]["code"] == "file_already_exists"


async def test_mkdir_parents_creates_intermediates(tmp_path):
    data = _parse(await _mkdir(tmp_path).execute({"path": "a/b/c", "parents": True}))
    assert data["path"] == "a/b/c"
    assert (tmp_path / "a/b/c").is_dir()


async def test_mkdir_without_parents_fails_on_missing_intermediate(tmp_path):
    data = _parse(await _mkdir(tmp_path).execute({"path": "a/b/c"}))
    assert data["error"]["code"] == "file_fs_failed"


# ===========================================================================
# file_rmdir — empty directories only
# ===========================================================================
async def test_rmdir_removes_an_empty_directory(tmp_path):
    (tmp_path / "empty").mkdir()
    data = _parse(await _rmdir(tmp_path).execute({"path": "empty"}))
    assert data["path"] == "empty"
    assert not (tmp_path / "empty").exists()


async def test_rmdir_refuses_non_empty(tmp_path):
    (tmp_path / "full" / "child.txt").parent.mkdir()
    (tmp_path / "full" / "child.txt").write_text("x", encoding="utf-8")
    data = _parse(await _rmdir(tmp_path).execute({"path": "full"}))
    assert data["error"]["code"] == "file_not_empty"
    assert (tmp_path / "full").exists()  # untouched


async def test_rmdir_refuses_a_file(tmp_path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    data = _parse(await _rmdir(tmp_path).execute({"path": "f.txt"}))
    assert data["error"]["code"] == "file_not_a_directory"


async def test_rmdir_missing(tmp_path):
    data = _parse(await _rmdir(tmp_path).execute({"path": "ghost"}))
    assert data["error"]["code"] == "file_not_found"


# ===========================================================================
# file_touch
# ===========================================================================
async def test_touch_creates_an_empty_file(tmp_path):
    data = _parse(await _touch(tmp_path).execute({"path": "marker.txt"}))
    assert data["path"] == "marker.txt"
    assert (tmp_path / "marker.txt").is_file()
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == ""


async def test_touch_updates_mtime_of_existing(tmp_path):
    f = tmp_path / "old.txt"
    f.write_text("x", encoding="utf-8")
    os.utime(str(f), (1_000_000_000, 1_000_000_000))  # a fixed past mtime
    before = f.stat().st_mtime
    await _touch(tmp_path).execute({"path": "old.txt"})
    assert f.stat().st_mtime > before


async def test_touch_refuses_a_directory(tmp_path):
    (tmp_path / "d").mkdir()
    data = _parse(await _touch(tmp_path).execute({"path": "d"}))
    assert data["error"]["code"] == "file_not_a_file"


# ===========================================================================
# path confinement — the core safety property (across tools)
# ===========================================================================
async def test_dotdot_escape_is_rejected_and_untouched(tmp_path):
    outside = tmp_path.parent / "outside_dotdot.txt"
    outside.write_text("secret", encoding="utf-8")
    assert _parse(await _read(tmp_path).execute({"path": "../outside_dotdot.txt"}))["error"]["code"] == "file_path_escape"
    assert outside.read_text(encoding="utf-8") == "secret"  # never read/written
    assert _parse(await _rm(tmp_path).execute({"path": "../outside_dotdot.txt"}))["error"]["code"] == "file_path_escape"
    assert outside.read_text(encoding="utf-8") == "secret"  # never deleted either


async def test_absolute_path_outside_root_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside_abs.txt"
    outside.write_text("secret", encoding="utf-8")
    assert _parse(await _read(tmp_path).execute({"path": str(outside)}))["error"]["code"] == "file_path_escape"
    assert outside.read_text(encoding="utf-8") == "secret"


async def test_symlink_pointing_out_of_root_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    os.symlink(outside, link)
    assert _parse(await _read(tmp_path).execute({"path": "link.txt"}))["error"]["code"] == "file_path_escape"
    assert outside.read_text(encoding="utf-8") == "secret"


async def test_symlink_escape_blocks_a_write(tmp_path):
    outside = tmp_path.parent / "outside_write.txt"
    outside.write_text("original", encoding="utf-8")
    link = tmp_path / "wlink.txt"
    os.symlink(outside, link)
    data = _parse(await _edit(tmp_path).execute({"path": "wlink.txt", "old_string": "original", "new_string": "pwned"}))
    assert data["error"]["code"] == "file_path_escape"
    assert outside.read_text(encoding="utf-8") == "original"


async def test_write_dotdot_escape_is_rejected_and_untouched(tmp_path):
    outside = tmp_path.parent / "outside_write2.txt"
    outside.write_text("secret", encoding="utf-8")
    assert _parse(await _write(tmp_path).execute({"path": "../outside_write2.txt", "content": "pwned"}))["error"]["code"] == "file_path_escape"
    assert outside.read_text(encoding="utf-8") == "secret"  # never overwritten


async def test_append_symlink_escape_is_rejected_and_untouched(tmp_path):
    outside = tmp_path.parent / "outside_append.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "alink.txt"
    os.symlink(outside, link)
    assert _parse(await _append(tmp_path).execute({"path": "alink.txt", "content": "-pwned"}))["error"]["code"] == "file_path_escape"
    assert outside.read_text(encoding="utf-8") == "secret"  # never appended to


async def test_write_absolute_path_outside_root_is_rejected(tmp_path):
    # A relative path with an out-of-root absolute target is refused outright.
    assert _parse(await _write(tmp_path).execute({"path": str(tmp_path.parent / "abs_new.txt"), "content": "x"}))["error"]["code"] == "file_path_escape"
    assert not (tmp_path.parent / "abs_new.txt").exists()


async def test_mv_cannot_move_out_of_root(tmp_path):
    (tmp_path / "src.txt").write_text("x", encoding="utf-8")
    outside = tmp_path.parent / "moved_out.txt"
    data = _parse(await _mv(tmp_path).execute({"source": "src.txt", "target": str(outside)}))
    assert data["error"]["code"] == "file_path_escape"
    assert (tmp_path / "src.txt").exists()  # still there
    assert not outside.exists()


# ===========================================================================
# atomic write
# ===========================================================================
async def test_no_temp_file_left_after_edit(tmp_path):
    f = tmp_path / "k.txt"
    f.write_text("one two", encoding="utf-8")
    await _edit(tmp_path).execute({"path": "k.txt", "old_string": "one", "new_string": "ONE"})
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftovers == []
    assert f.read_text(encoding="utf-8") == "ONE two"


async def test_no_temp_file_left_after_write_or_append(tmp_path):
    await _write(tmp_path).execute({"path": "w.txt", "content": "brand new"})
    await _append(tmp_path).execute({"path": "w.txt", "content": " + more"})
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftovers == []
    assert (tmp_path / "w.txt").read_text(encoding="utf-8") == "brand new + more"


# ===========================================================================
# declarations / schema
# ===========================================================================
def test_edit_schema_shape(tmp_path):
    tool = _edit(tmp_path, max_string_chars=123)
    assert tool.parameters["required"] == ["path", "old_string", "new_string"]
    assert tool.parameters["additionalProperties"] is False
    assert tool.parameters["properties"]["old_string"]["maxLength"] == 123
    assert tool.parameters["properties"]["new_string"]["maxLength"] == 123
    assert "operation" not in tool.parameters["properties"]  # the old discriminator is gone


def test_single_path_schemas(tmp_path):
    for tool in (_read(tmp_path), _ls(tmp_path), _rm(tmp_path), _mkdir(tmp_path), _rmdir(tmp_path), _touch(tmp_path)):
        assert tool.parameters["required"] == ["path"]
        assert tool.parameters["additionalProperties"] is False


def test_write_and_append_schema_shapes(tmp_path):
    for tool in (_write(tmp_path, max_content_chars=77), _append(tmp_path, max_content_chars=77)):
        assert tool.parameters["required"] == ["path", "content"]
        assert tool.parameters["additionalProperties"] is False
        assert tool.parameters["properties"]["content"]["type"] == "string"
        assert tool.parameters["properties"]["content"]["maxLength"] == 77


# ===========================================================================
# approval views (file_edit / file_write / file_append render a structured
# git-diff detail; the rest ride the generic JSON Arguments block)
# ===========================================================================
def test_edit_approval_summary_never_echoes_arguments(tmp_path):
    tool = _edit(tmp_path)
    s = tool.approval_summary({"path": "/secrets/top", "old_string": "TOPSECRET", "new_string": "X"})
    assert s == tool.approval_summary({})
    assert "TOPSECRET" not in s
    assert "/secrets" not in s


def test_edit_approval_detail_is_a_git_diff(tmp_path):
    tool = _edit(tmp_path)
    old = "timeout: 30\nretries: 1"
    new = "timeout: 60\nretries: 3"
    detail = tool.approval_detail({"path": "config/settings.yaml", "old_string": old, "new_string": new})
    assert "config/settings.yaml" in detail
    assert "replace_all: no" in detail
    assert "--- a/config/settings.yaml" in detail
    assert "+++ b/config/settings.yaml" in detail
    assert "-timeout: 30" in detail and "-retries: 1" in detail
    assert "+timeout: 60" in detail and "+retries: 3" in detail
    assert detail.index("-timeout: 30") < detail.index("+timeout: 60")
    assert tool.approval_language({"path": "x", "old_string": "a", "new_string": "b"}) == "diff"


def test_edit_approval_detail_replace_all_and_empty_new(tmp_path):
    tool = _edit(tmp_path)
    all_detail = tool.approval_detail({"path": "f.txt", "old_string": "x", "new_string": "y", "replace_all": True})
    assert "replace_all: yes" in all_detail
    delete_detail = tool.approval_detail({"path": "f.txt", "old_string": "x", "new_string": ""})
    lines = delete_detail.split("\n")
    assert "-x" in lines  # the old text, removed
    assert not any(l.startswith("+") and l != "+++ b/f.txt" for l in lines)  # no additions


def test_write_approval_summary_never_echoes_arguments(tmp_path):
    tool = _write(tmp_path)
    s = tool.approval_summary({"path": "/secrets/top", "content": "TOPSECRET"})
    assert s == tool.approval_summary({})
    assert "TOPSECRET" not in s
    assert "/secrets" not in s


def test_write_approval_detail_is_a_git_addition(tmp_path):
    tool = _write(tmp_path)
    content = "timeout: 60\nretries: 3"
    detail = tool.approval_detail({"path": "config/settings.yaml", "content": content})
    assert "config/settings.yaml" in detail
    assert "write (replace entire content)" in detail
    assert "--- a/config/settings.yaml" in detail
    assert "+++ b/config/settings.yaml" in detail
    assert "+timeout: 60" in detail and "+retries: 3" in detail
    # every content line is an addition (a new file); there are no deletions
    lines = detail.split("\n")
    assert not any(l.startswith("-") and l != "--- a/config/settings.yaml" for l in lines)
    assert tool.approval_language({"path": "x", "content": "y"}) == "diff"


def test_append_approval_detail_shows_appended_lines(tmp_path):
    tool = _append(tmp_path)
    detail = tool.approval_detail({"path": "log.txt", "content": "extra line"})
    assert "log.txt" in detail
    assert "append" in detail
    assert "existing content preserved" in detail
    assert "+++ b/log.txt" in detail
    assert "+extra line" in detail
    assert tool.approval_language({"path": "x", "content": "y"}) == "diff"


def test_append_approval_summary_never_echoes_arguments(tmp_path):
    tool = _append(tmp_path)
    s = tool.approval_summary({"path": "/secrets/top", "content": "TOPSECRET"})
    assert "TOPSECRET" not in s
    assert "/secrets" not in s


def test_other_tools_have_purpose_summaries_not_details(tmp_path):
    # Every file tool has a purpose line; only file_edit / file_write /
    # file_append override approval_detail (a structured diff) — the rest fall
    # back to the generic JSON Arguments block.
    assert _edit(tmp_path).approval_detail({"path": "f", "old_string": "a", "new_string": "b"}) is not None
    assert _write(tmp_path).approval_detail({"path": "f", "content": "c"}) is not None
    assert _append(tmp_path).approval_detail({"path": "f", "content": "c"}) is not None
    for tool in (_read(tmp_path), _ls(tmp_path), _mv(tmp_path), _cp(tmp_path), _rm(tmp_path), _mkdir(tmp_path), _rmdir(tmp_path), _touch(tmp_path)):
        assert tool.approval_summary({})  # a non-empty purpose line
        assert tool.approval_detail({}) is None


# ===========================================================================
# registration wiring (opt-in)
# ===========================================================================
def test_build_default_tools_adds_file_only_when_enabled(tmp_path):
    from fibrecase_agent_backend.tools.builtin import build_default_tools

    off = build_default_tools()
    assert "file_read" not in off.names() and "file_edit" not in off.names()
    on = build_default_tools(enable_file=True, file_workdir=str(tmp_path))
    expected = [
        "file_read",
        "file_ls",
        "file_edit",
        "file_write",
        "file_append",
        "file_mv",
        "file_cp",
        "file_rm",
        "file_mkdir",
        "file_rmdir",
        "file_touch",
    ]
    assert on.names()[-11:] == expected  # the eleven file tools, in order, after the read-only built-ins (and exec)
