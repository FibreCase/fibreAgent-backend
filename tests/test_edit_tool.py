"""Edit file tool — real (hermetic) filesystem behaviour.

These tests exercise :class:`fibrecase_agent_backend.tools.builtin.edit.EditTool`
against the **real** filesystem, confined to ``tmp_path`` — the only way a
file-writing tool can be meaningfully tested. Every test is local (no network,
no LLM, no Telegram, no DB). The path-confinement tests (the tool's core safety
property) create a file *outside* the root (in ``tmp_path.parent``) and assert it
is never read or written — including through a symlink that points out of the
root.
"""

from __future__ import annotations

import json
import os

import pytest

from fibrecase_agent_backend.tools.builtin.edit import EditTool
from fibrecase_agent_backend.tools.policy import ToolPermission


def _tool(root, max_string_chars: int = 2000, max_read_chars: int = 100_000) -> EditTool:
    return EditTool(workdir=str(root), max_string_chars=max_string_chars, max_read_chars=max_read_chars)


def _parse(result: str) -> dict:
    return json.loads(result)


# ===========================================================================
# read
# ===========================================================================
async def test_read_returns_content_and_relative_path(tmp_path):
    (tmp_path / "a.txt").write_text("hello\nworld", encoding="utf-8")
    data = _parse(await _tool(tmp_path).execute({"operation": "read", "path": "a.txt"}))
    assert data["operation"] == "read"
    assert data["path"] == "a.txt"  # root-relative, not the absolute path
    assert data["content"] == "hello\nworld"


async def test_read_absolute_path_inside_root(tmp_path):
    (tmp_path / "b.txt").write_text("abs", encoding="utf-8")
    data = _parse(await _tool(tmp_path).execute({"operation": "read", "path": str(tmp_path / "b.txt")}))
    assert data["path"] == "b.txt"
    assert data["content"] == "abs"


async def test_read_tail_truncated_with_marker(tmp_path):
    (tmp_path / "big.txt").write_text("X" * 100, encoding="utf-8")
    data = _parse(await _tool(tmp_path, max_read_chars=40).execute({"operation": "read", "path": "big.txt"}))
    content = data["content"]
    assert content.startswith("[") and "truncated" in content
    assert content.endswith("X" * 40)  # the tail is kept


async def test_read_missing_file(tmp_path):
    data = _parse(await _tool(tmp_path).execute({"operation": "read", "path": "nope.txt"}))
    assert data["error"]["code"] == "edit_file_not_found"


async def test_read_directory_is_not_a_file(tmp_path):
    sub = tmp_path / "subdir"
    sub.mkdir()
    data = _parse(await _tool(tmp_path).execute({"operation": "read", "path": "subdir"}))
    assert data["error"]["code"] == "edit_not_a_file"


async def test_read_non_utf8_is_read_failed(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
    data = _parse(await _tool(tmp_path).execute({"operation": "read", "path": "bin.dat"}))
    assert data["error"]["code"] == "edit_read_failed"


# ===========================================================================
# replace — semantics
# ===========================================================================
async def test_replace_unique_updates_file(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("alpha beta gamma", encoding="utf-8")
    data = _parse(
        await _tool(tmp_path).execute(
            {"operation": "replace", "path": "c.txt", "old_string": "beta", "new_string": "BETA"}
        )
    )
    assert data["operation"] == "replace"
    assert data["path"] == "c.txt"
    assert data["replacements"] == 1
    assert f.read_text(encoding="utf-8") == "alpha BETA gamma"


async def test_replace_empty_new_string_deletes(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("keep [drop] this", encoding="utf-8")
    data = _parse(
        await _tool(tmp_path).execute(
            {"operation": "replace", "path": "d.txt", "old_string": "[drop]", "new_string": ""}
        )
    )
    assert data["replacements"] == 1
    assert f.read_text(encoding="utf-8") == "keep  this"


async def test_replace_not_found(tmp_path):
    (tmp_path / "e.txt").write_text("only this here", encoding="utf-8")
    data = _parse(
        await _tool(tmp_path).execute(
            {"operation": "replace", "path": "e.txt", "old_string": "absent", "new_string": "x"}
        )
    )
    assert data["error"]["code"] == "edit_not_found"


async def test_replace_not_unique_without_replace_all(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("dup dup dup", encoding="utf-8")
    data = _parse(
        await _tool(tmp_path).execute(
            {"operation": "replace", "path": "f.txt", "old_string": "dup", "new_string": "X"}
        )
    )
    assert data["error"]["code"] == "edit_not_unique"
    assert f.read_text(encoding="utf-8") == "dup dup dup"  # untouched


async def test_replace_all_replaces_every_occurrence(tmp_path):
    f = tmp_path / "g.txt"
    f.write_text("a b a b a", encoding="utf-8")
    data = _parse(
        await _tool(tmp_path).execute(
            {"operation": "replace", "path": "g.txt", "old_string": "a", "new_string": "A", "replace_all": True}
        )
    )
    assert data["replacements"] == 3
    assert f.read_text(encoding="utf-8") == "A b A b A"


# ===========================================================================
# replace — argument validation
# ===========================================================================
async def test_replace_missing_old_string_is_invalid_op(tmp_path):
    (tmp_path / "h.txt").write_text("x", encoding="utf-8")
    data = _parse(await _tool(tmp_path).execute({"operation": "replace", "path": "h.txt", "new_string": "y"}))
    assert data["error"]["code"] == "edit_invalid_op"


async def test_replace_missing_new_string_is_invalid_op(tmp_path):
    (tmp_path / "i.txt").write_text("x", encoding="utf-8")
    data = _parse(await _tool(tmp_path).execute({"operation": "replace", "path": "i.txt", "old_string": "x"}))
    assert data["error"]["code"] == "edit_invalid_op"


async def test_empty_path_is_invalid_op(tmp_path):
    data = _parse(await _tool(tmp_path).execute({"operation": "read", "path": "   "}))
    assert data["error"]["code"] == "edit_invalid_op"


async def test_unknown_operation_is_invalid_op(tmp_path):
    (tmp_path / "j.txt").write_text("x", encoding="utf-8")
    data = _parse(await _tool(tmp_path).execute({"operation": "delete", "path": "j.txt"}))
    assert data["error"]["code"] == "edit_invalid_op"


# ===========================================================================
# path confinement — the core safety property
# ===========================================================================
async def test_dotdot_escape_is_rejected_and_untouched(tmp_path):
    outside = tmp_path.parent / "outside_dotdot.txt"
    outside.write_text("secret", encoding="utf-8")
    data = _parse(await _tool(tmp_path).execute({"operation": "read", "path": "../outside_dotdot.txt"}))
    assert data["error"]["code"] == "edit_path_escape"
    assert outside.read_text(encoding="utf-8") == "secret"  # never read/written


async def test_absolute_path_outside_root_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside_abs.txt"
    outside.write_text("secret", encoding="utf-8")
    data = _parse(await _tool(tmp_path).execute({"operation": "read", "path": str(outside)}))
    assert data["error"]["code"] == "edit_path_escape"
    assert outside.read_text(encoding="utf-8") == "secret"


async def test_symlink_pointing_out_of_root_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    os.symlink(outside, link)
    data = _parse(await _tool(tmp_path).execute({"operation": "read", "path": "link.txt"}))
    assert data["error"]["code"] == "edit_path_escape"
    assert outside.read_text(encoding="utf-8") == "secret"  # never written through the link


async def test_symlink_escape_blocks_a_write(tmp_path):
    outside = tmp_path.parent / "outside_write.txt"
    outside.write_text("original", encoding="utf-8")
    link = tmp_path / "wlink.txt"
    os.symlink(outside, link)
    data = _parse(
        await _tool(tmp_path).execute(
            {"operation": "replace", "path": "wlink.txt", "old_string": "original", "new_string": "pwned"}
        )
    )
    assert data["error"]["code"] == "edit_path_escape"
    assert outside.read_text(encoding="utf-8") == "original"


# ===========================================================================
# atomic write
# ===========================================================================
async def test_no_temp_file_left_after_replace(tmp_path):
    f = tmp_path / "k.txt"
    f.write_text("one two", encoding="utf-8")
    await _tool(tmp_path).execute({"operation": "replace", "path": "k.txt", "old_string": "one", "new_string": "ONE"})
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftovers == []
    assert f.read_text(encoding="utf-8") == "ONE two"


# ===========================================================================
# declarations / schema
# ===========================================================================
def test_edit_declares_ask_and_shape(tmp_path):
    tool = EditTool(workdir=str(tmp_path), max_string_chars=123, max_read_chars=456)
    assert tool.default_permission is ToolPermission.ASK
    assert tool.parameters["required"] == ["operation", "path"]
    assert tool.parameters["additionalProperties"] is False
    assert tool.parameters["properties"]["operation"]["enum"] == ["read", "replace"]
    assert tool.parameters["properties"]["old_string"]["maxLength"] == 123
    assert tool.parameters["properties"]["new_string"]["maxLength"] == 123


def test_approval_summary_never_echoes_arguments(tmp_path):
    tool = EditTool(workdir=str(tmp_path), max_string_chars=100, max_read_chars=100)
    s = tool.approval_summary({"path": "/secrets/top", "old_string": "TOPSECRET", "new_string": "X"})
    assert s == tool.approval_summary({})
    assert "TOPSECRET" not in s
    assert "/secrets" not in s


def test_approval_detail_replace_is_a_git_diff(tmp_path):
    # The detail view (shown on the approval card in place of the JSON block)
    # renders the edit as a git-style diff: every old_string line prefixed "-"
    # and every new_string line prefixed "+", under --- a/<path> / +++ b/<path>
    # headers — so the owner sees exactly what is removed and what replaces it.
    tool = EditTool(workdir=str(tmp_path), max_string_chars=2000, max_read_chars=100)
    old = "timeout: 30\nretries: 1"
    new = "timeout: 60\nretries: 3"
    detail = tool.approval_detail(
        {"operation": "replace", "path": "config/settings.yaml", "old_string": old, "new_string": new}
    )
    assert "config/settings.yaml" in detail
    assert "Operation: replace" in detail
    # replace_all defaults to no.
    assert "replace_all: no" in detail
    # git-diff structure…
    assert "--- a/config/settings.yaml" in detail
    assert "+++ b/config/settings.yaml" in detail
    # …each old line prefixed "-" and each new line prefixed "+", verbatim.
    assert "-timeout: 30" in detail and "-retries: 1" in detail
    assert "+timeout: 60" in detail and "+retries: 3" in detail
    # The removed (old) lines come before the added (new) lines.
    assert detail.index("-timeout: 30") < detail.index("+timeout: 60")
    # A replace is labelled a diff so the card highlights it as such.
    assert (
        tool.approval_language({"operation": "replace", "path": "x", "old_string": "a", "new_string": "b"})
        == "diff"
    )


def test_approval_detail_replace_all_and_empty_new(tmp_path):
    tool = EditTool(workdir=str(tmp_path), max_string_chars=100, max_read_chars=100)
    # replace_all=true is surfaced in the operation line…
    all_detail = tool.approval_detail(
        {"operation": "replace", "path": "f.txt", "old_string": "x", "new_string": "y", "replace_all": True}
    )
    assert "replace_all: yes" in all_detail

    # …and an empty new_string means "delete old_string": the diff carries the
    # removed "-" line but no added "+" line (a pure deletion).
    delete_detail = tool.approval_detail(
        {"operation": "replace", "path": "f.txt", "old_string": "x", "new_string": ""}
    )
    assert "replace_all: no" in delete_detail
    lines = delete_detail.split("\n")
    assert "-x" in lines  # the old text, removed
    assert not any(l.startswith("+") and l != "+++ b/f.txt" for l in lines)  # no additions


def test_approval_detail_read_shows_file_only(tmp_path):
    # A read has no diff to show — just the target file and the operation. It is
    # left unlabelled (approval_language → None) since there is no diff body.
    tool = EditTool(workdir=str(tmp_path), max_string_chars=100, max_read_chars=100)
    detail = tool.approval_detail({"operation": "read", "path": "notes.md"})
    assert "notes.md" in detail
    assert "Operation: read" in detail
    assert "old_string" not in detail and "new_string" not in detail
    assert tool.approval_language({"operation": "read", "path": "notes.md"}) is None


# ===========================================================================
# registration wiring (opt-in)
# ===========================================================================
def test_build_default_tools_adds_edit_only_when_enabled(tmp_path):
    from fibrecase_agent_backend.tools.builtin import build_default_tools

    off = build_default_tools()
    assert "edit" not in off.names()
    on = build_default_tools(enable_edit=True, edit_workdir=str(tmp_path))
    assert on.names()[-1] == "edit"  # added last, after the three read-only tools (and exec)
