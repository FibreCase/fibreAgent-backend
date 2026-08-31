"""Built-in file toolset: read and manipulate files/directories, confined, opt-in.

This replaces the old single ``edit`` tool with a small set of first-class file
tools, each confined to a single configured directory (``FILE_WORKDIR``). The
set is **off by default** (``ENABLE_FILE_TOOL``); when enabled the tools are
added to the registry and ride the entire phase-3 gate (policy → JSON-Schema
validation → fail-closed pre-audit → optional one-time approval → timeout →
terminal audit).

**Permissions** split by capability, per operation:

* ``file_read`` and ``file_ls`` are strictly read-only and declare
  :attr:`ToolPermission.ALLOW` — they run without a per-call approval, exactly
  like the ``get_current_time`` / ``echo`` built-ins.
* Every mutating tool (``file_edit`` / ``file_write`` / ``file_append`` /
  ``file_mv`` / ``file_rm`` / ``file_mkdir`` / ``file_rmdir`` / ``file_cp`` /
  ``file_touch``) declares :attr:`ToolPermission.ASK` — each call needs a one-time
  human Approve.

**Defence in depth** (all *inside* each tool's ``execute`` — the tool loop is
untouched):

1. **Path confinement — the core safety property.** :meth:`_FileTool._resolve`
   resolves the requested path (collapsing ``..`` *and* following **symlinks**)
   and refuses anything that does not land inside the root *before any I/O*
   (``file_path_escape``), so a ``../`` escape, an out-of-root absolute path, or
   a symlink pointing outside is never read or written — even after the owner
   approves. This is why ``FILE_WORKDIR`` is *required* when the set is enabled
   (config refuses to start without it).
2. **Narrow verbs, no shell.** Each tool does exactly one thing. ``file_rm``
   deletes a *regular file only* (never a directory); ``file_rmdir`` removes an
   *empty* directory only; ``file_mv`` / ``file_cp`` never overwrite an existing
   target. The two whole-file writers (``file_write`` / ``file_append``) are the
   one deliberate exception to "no whole-file write": they create a file or
   replace / append its *entire* content — shell ``>`` / ``>>`` — each confined
   to the root and gated behind per-call approval, and both bounded. There is no
   arbitrary rename-to-anything and no shell — the model cannot name anything
   outside the root.
3. **Atomic write** — :meth:`_FileTool._atomic_write` writes new content to a
   same-directory temp file (``fsync`` + ``os.replace``), so a mid-write crash
   never leaves a half-written file (the attachment-store / permissions-file
   idiom). ``file_write`` uses it directly; ``file_append`` reads the existing
   content (if any) and atomically writes the concatenation, which keeps the
   append crash-safe too.

**Output bounding:** a ``file_read`` result is tail-truncated to
``max_read_chars`` and a ``file_ls`` result is capped at ``max_list_entries``
both with a fixed marker/flag (the exec idiom — truncation, not an error,
because the read/list was already human-approved or is read-only).
``file_edit``'s ``old_string`` / ``new_string`` are bounded by
``max_string_chars``, which is also baked into the parameter schema's
``maxLength`` so the model's proposal — and the approval card — stays bounded.
The whole-file writers are bounded by ``max_content_chars``: it caps the
``file_write`` / ``file_append`` ``content`` (baked into the schema
``maxLength`` as with the edit strings) and, for ``file_append``, the size of
the *resulting* file after appending (enforced on the merged content — the
write side, which the ``content`` cap alone would not cover).

**Logging rule:** paths, file content, and the old/new strings are returned to
the *model only*. They are **never** logged here and **cannot** reach the audit
table (which stores only the tool name, a stable code, latency, and a hashed
scope). On failure only a stable code is produced.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..base import Tool
from ..policy import ToolPermission


# Fixed, non-echoing model-facing messages for the file-specific result codes.
# (These are *tool* codes, not the loop-level codes in ``..audit``.)
_MESSAGES = {
    "file_path_escape": "The path resolves outside the permitted working directory and was not touched.",
    "file_not_found": "The path does not exist.",
    "file_not_a_file": "The path is not a regular file.",
    "file_not_a_directory": "The path is not a directory.",
    "file_is_directory": "The path is a directory; file.rm deletes files only (use file.rmdir for an empty directory).",
    "file_read_failed": "The file could not be read as UTF-8 text.",
    "file_invalid_path": "The path argument was invalid.",
    "file_invalid_args": "The arguments were invalid for the requested operation.",
    "file_not_replaced": "The text to replace was not found in the file.",
    "file_not_unique": "The text to replace is not unique; use replace_all or a more specific string.",
    "file_write_failed": "The file could not be written.",
    "file_result_too_large": "The resulting file would exceed the maximum content size.",
    "file_not_empty": "The directory is not empty; remove its contents first.",
    "file_already_exists": "A file or directory already exists at that path.",
    "file_fs_failed": "The file operation could not be completed.",
}


class _Escape(Exception):
    """Internal sentinel: the requested path resolves outside the root."""


def _error(code: str) -> str:
    """A stable, short JSON error result for a non-successful file outcome
    (fed to the model). Returned (not raised) so the specific code reaches the
    model — a raised exception would be flattened to ``tool_execution_failed``
    by the loop.
    """
    return json.dumps({"error": {"code": code, "message": _MESSAGES[code]}})


def _bound_text(text: str, cap: int) -> str:
    """Tail-truncate a ``read`` result to ``cap`` chars, prefixing a fixed
    marker naming how many *earlier* chars were dropped (a number — no content
    echo).
    """
    if len(text) <= cap:
        return text
    dropped = len(text) - cap
    return f"[{dropped} chars of earlier output truncated]\n{text[-cap:]}"


class _FileTool(Tool):
    """Shared confinement machinery for every file tool.

    Holds the resolved root and the three helpers every tool needs: resolve a
    requested path *inside* the root (``_locate`` / ``_resolve``), render a
    target as a root-relative path (``_rel``), and an atomic write. Concrete
    tools set ``name`` / ``description`` / ``parameters`` and implement
    ``execute`` (and, where useful, the approval hooks).
    """

    #: State-changing file tools default to ``ask``; the read-only tools
    #: (``file_read`` / ``file_ls``) override this with ``allow``.
    default_permission = ToolPermission.ASK

    def __init__(self, workdir: str) -> None:
        # Resolve the root once so the confinement check is stable regardless of
        # the process cwd or how the config path was spelled.
        self._root = Path(workdir).resolve()

    def _resolve(self, path: str) -> Path:
        """Resolve ``path`` to a real path *inside* ``self._root``.

        A relative path is interpreted against the root; an absolute path is
        allowed only if it still lands inside it. ``resolve()`` collapses ``..``
        and follows symlinks, so a ``../`` escape, an out-of-root absolute path,
        or a symlink that points outside all resolve to a path whose ancestors
        do not include the root (the root itself is allowed) —
        :class:`_Escape` is raised. A path that is malformed for the OS
        (``resolve()`` raises) is treated the same: a "don't touch" outcome,
        never a read/write.
        """
        base = Path(path)
        if not base.is_absolute():
            base = self._root / base
        try:
            resolved = base.resolve()
        except (ValueError, OSError):
            raise _Escape() from None
        if resolved != self._root and self._root not in resolved.parents:
            raise _Escape()
        return resolved

    def _locate(self, path: Any) -> "Path | str":
        """Validate + resolve a requested path.

        Returns the resolved in-root :class:`Path` on success, or a stable
        error-code *string* on failure (``file_invalid_path`` /
        ``file_path_escape``) — callers branch on ``isinstance`` and return
        ``_error(code)`` for the string case. The check runs *before* any I/O.
        """
        if not isinstance(path, str) or not path.strip():
            return "file_invalid_path"
        try:
            return self._resolve(path)
        except _Escape:
            return "file_path_escape"

    def _rel(self, target: Path) -> str:
        """The target as a path relative to the root (cleaner + leaks no
        absolute system path in the model-facing result)."""
        try:
            return str(target.relative_to(self._root))
        except ValueError:
            return str(target)

    @staticmethod
    def _atomic_write(target: Path, text: str) -> None:
        """Publish ``text`` to ``target`` atomically (same-dir temp + fsync +
        ``os.replace``). The target already exists and is inside the root, so the
        parent directory is present — no mkdir. Raises ``OSError`` on a real
        write failure (mapped to ``file_write_failed``)."""
        parent = target.parent
        fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix="." + target.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


# ===========================================================================
# read-only (allow)
# ===========================================================================
class FileReadTool(_FileTool):
    """Read a UTF-8 text file inside the working directory (read-only, ``allow``)."""

    name = "file_read"
    description = (
        "Read the content of a UTF-8 text file inside the working directory. "
        "Args: path (absolute, or relative to the working directory). Read-only; "
        "runs without approval."
    )
    default_permission = ToolPermission.ALLOW
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path, absolute or relative to the working directory.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, *, workdir: str, max_read_chars: int) -> None:
        super().__init__(workdir)
        self._max_read = max_read_chars

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        return "Read a UTF-8 text file within the working directory (read-only)."

    async def execute(self, arguments: dict[str, Any]) -> str:
        loc = self._locate(arguments.get("path"))
        if isinstance(loc, str):
            return _error(loc)
        if not loc.is_file():
            return _error("file_not_a_file" if loc.exists() else "file_not_found")
        try:
            content = loc.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return _error("file_read_failed")
        return json.dumps(
            {
                "path": self._rel(loc),
                "content": _bound_text(content, self._max_read),
            }
        )


class FileLsTool(_FileTool):
    """List a directory's entries inside the working directory (read-only, ``allow``)."""

    name = "file_ls"
    description = (
        "List the immediate entries (files and directories) of a directory inside "
        "the working directory. Args: path (a directory path, or the working "
        "directory root). Directories are shown with a trailing '/'. Read-only; "
        "runs without approval."
    )
    default_permission = ToolPermission.ALLOW
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path, absolute or relative to the working directory.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, *, workdir: str, max_list_entries: int) -> None:
        super().__init__(workdir)
        self._max_list = max_list_entries

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        return "List the entries of a directory within the working directory (read-only)."

    async def execute(self, arguments: dict[str, Any]) -> str:
        loc = self._locate(arguments.get("path"))
        if isinstance(loc, str):
            return _error(loc)
        if not loc.is_dir():
            return _error("file_not_a_directory" if loc.exists() else "file_not_found")
        try:
            all_entries = sorted(entry.name + ("/" if entry.is_dir() else "") for entry in loc.iterdir())
        except OSError:
            return _error("file_fs_failed")
        truncated = len(all_entries) > self._max_list
        entries = all_entries[: self._max_list]
        return json.dumps({"path": self._rel(loc), "entries": entries, "truncated": truncated})


# ===========================================================================
# mutating (ask)
# ===========================================================================
class FileEditTool(_FileTool):
    """Precisely edit a UTF-8 text file inside the working directory (``ask``)."""

    name = "file_edit"
    description = (
        "Precisely edit a UTF-8 text file inside the working directory: replace an "
        "exact old_string with new_string. old_string must occur exactly once in "
        "the file unless replace_all is true. Paths must stay within the working "
        "directory. Every call requires human approval before it runs."
    )
    # ``parameters`` is built in ``__init__`` (its ``maxLength`` depends on
    # config), so there is no class-level default here.
    parameters: dict[str, object] = {}  # populated per-instance in __init__

    def __init__(
        self,
        *,
        workdir: str,
        max_string_chars: int,
        max_read_chars: int,
    ) -> None:
        super().__init__(workdir)
        self._max_string = max_string_chars
        self._max_read = max_read_chars
        # ``maxLength`` on the two replace strings bounds the model's proposal and,
        # with it, the approval card's argument block.
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, absolute or relative to the working directory.",
                },
                "old_string": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": max_string_chars,
                    "description": "The exact existing text to find (unique unless replace_all).",
                },
                "new_string": {
                    "type": "string",
                    "maxLength": max_string_chars,
                    "description": "The text to substitute (an empty string deletes old_string).",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of requiring a unique match.",
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        }

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        # Fixed and argument-free. The path + old/new strings are shown in the
        # card's separate "Action:" block (a git-style diff), so they are
        # deliberately NOT echoed here (secret-free convention).
        return "Precisely edit a UTF-8 text file within the working directory. Requires approval."

    def approval_detail(self, arguments: dict[str, Any]) -> str:
        """Render the edit as a **git-style unified diff** on the approval card.

        Shown in place of the generic JSON "Arguments:" block (see
        :meth:`Tool.approval_detail`): every line of the exact ``old_string``
        prefixed ``-`` and every line of the exact ``new_string`` prefixed ``+``
        (under ``--- a/<path>`` / ``+++ b/<path>`` headers) — so the owner
        approves the change the way they read any diff, rather than a wall of
        escaped JSON. A ``new_string`` of ``""`` renders as a pure deletion (no
        ``+`` lines). Plain text, no markup: the approval provider HTML-escapes
        and length-bounds it and wraps it in a code block (which preserves the
        newlines). See :meth:`approval_language` for the matching ``diff`` label.
        """
        path = arguments.get("path")
        replace_all = bool(arguments.get("replace_all", False))
        old = "" if arguments.get("old_string") is None else str(arguments["old_string"])
        new = "" if arguments.get("new_string") is None else str(arguments["new_string"])
        lines = [
            f"📄 File: {path}",
            f"🔁 Operation: replace (replace_all: {'yes' if replace_all else 'no'})",
            f"--- a/{path}",
            f"+++ b/{path}",
        ]
        for ln in old.split("\n"):
            lines.append(f"-{ln}")
        if new != "":  # an empty new_string deletes the old text: no additions
            for ln in new.split("\n"):
                lines.append(f"+{ln}")
        return "\n".join(lines)

    def approval_language(self, arguments: dict[str, Any]) -> str:
        # The detail view is a git-style diff, so highlight it as such. (Fixed
        # vocabulary — never derived from the argument content.)
        return "diff"

    async def execute(self, arguments: dict[str, Any]) -> str:
        loc = self._locate(arguments.get("path"))
        if isinstance(loc, str):
            return _error(loc)
        if not loc.is_file():
            return _error("file_not_a_file" if loc.exists() else "file_not_found")

        old = arguments.get("old_string")
        new = arguments.get("new_string")
        replace_all = bool(arguments.get("replace_all", False))
        if not isinstance(old, str) or not old or not isinstance(new, str):
            return _error("file_invalid_args")

        try:
            content = loc.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return _error("file_read_failed")

        count = content.count(old)
        if count == 0:
            return _error("file_not_replaced")
        if count > 1 and not replace_all:
            return _error("file_not_unique")

        new_content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        try:
            self._atomic_write(loc, new_content)
        except OSError:
            return _error("file_write_failed")
        return json.dumps(
            {
                "path": self._rel(loc),
                "replacements": count if replace_all else 1,
            }
        )


class FileWriteTool(_FileTool):
    """Create a file or replace its entire content inside the working directory (``ask``).

    Shell ``>`` semantics: the target file is created if absent, or its *whole*
    content replaced if present. This is the deliberate exception to the set's
    "no whole-file write" rule — the model can put arbitrary content into a
    single file, but only inside the root and behind per-call approval.
    """

    name = "file_write"
    description = (
        "Write content to a file inside the working directory, creating it if "
        "absent or replacing its entire content if it exists (shell '>' "
        "semantics). The path must stay within the working directory and must "
        "not be a directory. Every call requires human approval before it runs."
    )
    # ``parameters`` is built in ``__init__`` (its ``maxLength`` depends on
    # config), so there is no class-level default here.
    parameters: dict[str, object] = {}  # populated per-instance in __init__

    def __init__(self, *, workdir: str, max_content_chars: int) -> None:
        super().__init__(workdir)
        # ``maxLength`` on ``content`` bounds the model's proposal and, with it,
        # the approval card's argument block (same idiom as file_edit's strings).
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, absolute or relative to the working directory.",
                },
                "content": {
                    "type": "string",
                    "maxLength": max_content_chars,
                    "description": "The complete new content for the file (replaces any existing content).",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        # Fixed and argument-free. The path + content are shown verbatim in the
        # card's separate "Action:" block, so they are deliberately NOT echoed
        # here (secret-free convention).
        return "Write a file (create it or replace its entire content) within the working directory. Requires approval."

    def approval_detail(self, arguments: dict[str, Any]) -> str:
        """Render the write as a **git-style addition** on the approval card.

        Shown in place of the generic JSON "Arguments:" block (see
        :meth:`Tool.approval_detail`): a ``📄 File:`` / ``🔁 Operation:`` header,
        then a ``--- a/<path>`` / ``+++ b/<path>`` pair followed by every line of
        the exact ``content`` prefixed ``+`` (a new file is a pure addition; the
        existing content, if any, is discarded — matching ``>``). Plain text, no
        markup: the provider HTML-escapes and length-bounds it and wraps it in a
        code block (which preserves the newlines). See :meth:`approval_language`
        for the matching ``diff`` label.
        """
        path = arguments.get("path")
        content = "" if arguments.get("content") is None else str(arguments["content"])
        lines = [
            f"📄 File: {path}",
            "🔁 Operation: write (replace entire content)",
            f"--- a/{path}",
            f"+++ b/{path}",
        ]
        for ln in content.split("\n"):
            lines.append(f"+{ln}")
        return "\n".join(lines)

    def approval_language(self, arguments: dict[str, Any]) -> str:
        # The detail view is a git-style diff, so highlight it as such. (Fixed
        # vocabulary — never derived from the argument content.)
        return "diff"

    async def execute(self, arguments: dict[str, Any]) -> str:
        loc = self._locate(arguments.get("path"))
        if isinstance(loc, str):
            return _error(loc)
        if loc.is_dir():
            return _error("file_not_a_file")
        content = arguments.get("content")
        if not isinstance(content, str):
            return _error("file_invalid_args")
        try:
            self._atomic_write(loc, content)
        except OSError:
            return _error("file_write_failed")
        return json.dumps({"path": self._rel(loc), "bytes": len(content.encode("utf-8"))})


class FileAppendTool(_FileTool):
    """Append content to a file inside the working directory, creating it if absent (``ask``).

    Shell ``>>`` semantics: the file is created (empty then this content) if
    absent, or the content is appended to the existing content if present.
    Crash-safe: the merged content is written atomically, so a mid-write crash
    leaves the file with either the old content or the full new content — never
    a half-appended tail.
    """

    name = "file_append"
    description = (
        "Append content to a file inside the working directory, creating it "
        "first if it does not exist (shell '>>' semantics). The path must stay "
        "within the working directory and must not be a directory. Every call "
        "requires human approval before it runs."
    )
    # ``parameters`` is built in ``__init__`` (its ``maxLength`` depends on
    # config), so there is no class-level default here.
    parameters: dict[str, object] = {}  # populated per-instance in __init__

    def __init__(self, *, workdir: str, max_content_chars: int) -> None:
        super().__init__(workdir)
        self._max_content = max_content_chars
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, absolute or relative to the working directory.",
                },
                "content": {
                    "type": "string",
                    "maxLength": max_content_chars,
                    "description": "The text to append (the file is created with exactly this content if absent).",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        # Fixed and argument-free. The path + content are shown verbatim in the
        # card's separate "Action:" block, so they are deliberately NOT echoed
        # here (secret-free convention).
        return "Append content to a file (creating it if absent) within the working directory. Requires approval."

    def approval_detail(self, arguments: dict[str, Any]) -> str:
        """Render the append on the approval card.

        Shown in place of the generic JSON "Arguments:" block (see
        :meth:`Tool.approval_detail`): a ``🔁 Operation: append`` line noting the
        existing content is preserved (or the file is created), then every line
        of the exact ``content`` to be appended prefixed ``+`` under a
        ``+++ b/<path>`` header. The existing content is *not* dumped here — the
        owner can read it with ``file_read`` — but the appended text (the thing
        this call adds) is shown verbatim, so the owner approves exactly what
        will be added. Plain text, no markup: the provider HTML-escapes and
        length-bounds it and wraps it in a code block (which preserves the
        newlines). See :meth:`approval_language` for the matching ``diff`` label.
        """
        path = arguments.get("path")
        content = "" if arguments.get("content") is None else str(arguments["content"])
        lines = [
            f"📄 File: {path}",
            "🔁 Operation: append (existing content preserved; file created if absent)",
            f"+++ b/{path}",
        ]
        for ln in content.split("\n"):
            lines.append(f"+{ln}")
        return "\n".join(lines)

    def approval_language(self, arguments: dict[str, Any]) -> str:
        # The detail view is a git-style diff, so highlight it as such. (Fixed
        # vocabulary — never derived from the argument content.)
        return "diff"

    async def execute(self, arguments: dict[str, Any]) -> str:
        loc = self._locate(arguments.get("path"))
        if isinstance(loc, str):
            return _error(loc)
        if loc.is_dir():
            return _error("file_not_a_file")
        content = arguments.get("content")
        if not isinstance(content, str):
            return _error("file_invalid_args")
        try:
            existing = loc.read_text(encoding="utf-8") if loc.is_file() else ""
        except (OSError, UnicodeDecodeError):
            return _error("file_read_failed")
        merged = existing + content
        # The schema bounds only the *appended* content (as characters); the size
        # of the *resulting* file (existing + appended) is the write side and is
        # checked explicitly — in characters, matching MAX_FILE_CONTENT_CHARS — so
        # a large pre-existing file cannot be grown past the cap.
        if len(merged) > self._max_content:
            return _error("file_result_too_large")
        try:
            self._atomic_write(loc, merged)
        except OSError:
            return _error("file_write_failed")
        return json.dumps({"path": self._rel(loc), "bytes": len(merged.encode("utf-8"))})


class FileMvTool(_FileTool):
    """Move / rename a file or directory inside the working directory (``ask``)."""

    name = "file_mv"
    description = (
        "Move or rename a file or directory inside the working directory. Both "
        "source and target must stay within the working directory; the target must "
        "not already exist (no overwrite). Every call requires human approval "
        "before it runs."
    )
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Path of the file or directory to move."},
            "target": {"type": "string", "description": "Destination path (must not already exist)."},
        },
        "required": ["source", "target"],
        "additionalProperties": False,
    }

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        return "Move or rename a file or directory within the working directory. Requires approval."

    async def execute(self, arguments: dict[str, Any]) -> str:
        src = self._locate(arguments.get("source"))
        if isinstance(src, str):
            return _error(src)
        dst = self._locate(arguments.get("target"))
        if isinstance(dst, str):
            return _error(dst)
        if not src.exists():
            return _error("file_not_found")
        if dst.exists():
            return _error("file_already_exists")
        try:
            shutil.move(str(src), str(dst))
        except OSError:
            return _error("file_fs_failed")
        return json.dumps({"source": self._rel(src), "target": self._rel(dst)})


class FileCpTool(_FileTool):
    """Copy a file (or directory tree) inside the working directory (``ask``)."""

    name = "file_cp"
    description = (
        "Copy a file, or a directory tree, inside the working directory. Both "
        "source and target must stay within the working directory; the target must "
        "not already exist (no overwrite). Copying a directory requires recursive="
        "true. Every call requires human approval before it runs."
    )
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Path of the file or directory to copy."},
            "target": {"type": "string", "description": "Destination path (must not already exist)."},
            "recursive": {
                "type": "boolean",
                "description": "Required true when copying a directory (copies the tree).",
            },
        },
        "required": ["source", "target"],
        "additionalProperties": False,
    }

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        return "Copy a file or directory tree within the working directory. Requires approval."

    async def execute(self, arguments: dict[str, Any]) -> str:
        src = self._locate(arguments.get("source"))
        if isinstance(src, str):
            return _error(src)
        dst = self._locate(arguments.get("target"))
        if isinstance(dst, str):
            return _error(dst)
        recursive = bool(arguments.get("recursive", False))
        if not src.exists():
            return _error("file_not_found")
        if dst.exists():
            return _error("file_already_exists")
        is_dir = src.is_dir()
        if is_dir and not recursive:
            # Copying a directory without recursive=true is a caller error, not an
            # I/O failure — surface it distinctly.
            return _error("file_invalid_args")
        try:
            if is_dir:
                shutil.copytree(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))
        except OSError:
            return _error("file_fs_failed")
        return json.dumps({"source": self._rel(src), "target": self._rel(dst)})


class FileRmTool(_FileTool):
    """Delete a regular file inside the working directory (``ask``)."""

    name = "file_rm"
    description = (
        "Delete a regular file inside the working directory. The path must be a "
        "file (not a directory — use file.rmdir for an empty directory) and must "
        "stay within the working directory. Every call requires human approval "
        "before it runs."
    )
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path of the file to delete."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        return "Delete a file within the working directory. Requires approval."

    async def execute(self, arguments: dict[str, Any]) -> str:
        loc = self._locate(arguments.get("path"))
        if isinstance(loc, str):
            return _error(loc)
        if loc.is_dir():
            return _error("file_is_directory")
        if not loc.is_file():
            return _error("file_not_found")
        try:
            loc.unlink()
        except OSError:
            return _error("file_fs_failed")
        return json.dumps({"path": self._rel(loc)})


class FileMkdirTool(_FileTool):
    """Create a new directory inside the working directory (``ask``)."""

    name = "file_mkdir"
    description = (
        "Create a new directory inside the working directory. The target must not "
        "already exist and must stay within the working directory. Set parents=true "
        "to also create missing intermediate directories. Every call requires human "
        "approval before it runs."
    )
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path of the directory to create."},
            "parents": {
                "type": "boolean",
                "description": "Create missing intermediate directories too (mkdir -p).",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        return "Create a new directory within the working directory. Requires approval."

    async def execute(self, arguments: dict[str, Any]) -> str:
        loc = self._locate(arguments.get("path"))
        if isinstance(loc, str):
            return _error(loc)
        parents = bool(arguments.get("parents", False))
        if loc.exists():
            return _error("file_already_exists")
        try:
            loc.mkdir(parents=parents)
        except OSError:
            return _error("file_fs_failed")
        return json.dumps({"path": self._rel(loc)})


class FileRmdirTool(_FileTool):
    """Remove an empty directory inside the working directory (``ask``)."""

    name = "file_rmdir"
    description = (
        "Remove an empty directory inside the working directory. The path must be a "
        "directory and must be empty (remove its contents first); it must stay "
        "within the working directory. Every call requires human approval before "
        "it runs."
    )
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path of the empty directory to remove."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        return "Remove an empty directory within the working directory. Requires approval."

    async def execute(self, arguments: dict[str, Any]) -> str:
        loc = self._locate(arguments.get("path"))
        if isinstance(loc, str):
            return _error(loc)
        if not loc.is_dir():
            return _error("file_not_a_directory" if loc.exists() else "file_not_found")
        try:
            if any(loc.iterdir()):
                return _error("file_not_empty")
        except OSError:
            return _error("file_fs_failed")
        try:
            loc.rmdir()
        except OSError:
            return _error("file_fs_failed")
        return json.dumps({"path": self._rel(loc)})


class FileTouchTool(_FileTool):
    """Create an empty file / update a file's mtime inside the working directory (``ask``)."""

    name = "file_touch"
    description = (
        "Create an empty file (or update the modification time of an existing file) "
        "inside the working directory. The path must stay within the working "
        "directory and must not be a directory. Every call requires human approval "
        "before it runs."
    )
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path of the file to create or touch."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        return "Create an empty file (or update a file's mtime) within the working directory. Requires approval."

    async def execute(self, arguments: dict[str, Any]) -> str:
        loc = self._locate(arguments.get("path"))
        if isinstance(loc, str):
            return _error(loc)
        if loc.is_dir():
            return _error("file_not_a_file")
        try:
            loc.touch()  # creates if absent, updates mtime if present
        except OSError:
            return _error("file_fs_failed")
        return json.dumps({"path": self._rel(loc)})


#: The full file toolset, in registration order: read-only first, then the
#: mutating tools. Assembled by :func:`build_file_tools`.
_ALL_FILE_TOOLS = (
    FileReadTool,
    FileLsTool,
    FileEditTool,
    FileWriteTool,
    FileAppendTool,
    FileMvTool,
    FileCpTool,
    FileRmTool,
    FileMkdirTool,
    FileRmdirTool,
    FileTouchTool,
)


def build_file_tools(
    *,
    workdir: str,
    max_string_chars: int,
    max_read_chars: int,
    max_list_entries: int,
    max_content_chars: int,
) -> list[Tool]:
    """Build the full, confined file toolset for ``workdir``.

    Returns the eleven tools in a fixed order (read-only ``file_read`` /
    ``file_ls`` first, then the mutating ``ask`` tools) for
    :func:`build_default_tools` to ``registry.add``. ``workdir`` must already be
    a validated existing directory (config enforces this when the set is
    enabled).
    """
    return [
        FileReadTool(workdir=workdir, max_read_chars=max_read_chars),
        FileLsTool(workdir=workdir, max_list_entries=max_list_entries),
        FileEditTool(workdir=workdir, max_string_chars=max_string_chars, max_read_chars=max_read_chars),
        FileWriteTool(workdir=workdir, max_content_chars=max_content_chars),
        FileAppendTool(workdir=workdir, max_content_chars=max_content_chars),
        FileMvTool(workdir=workdir),
        FileCpTool(workdir=workdir),
        FileRmTool(workdir=workdir),
        FileMkdirTool(workdir=workdir),
        FileRmdirTool(workdir=workdir),
        FileTouchTool(workdir=workdir),
    ]
