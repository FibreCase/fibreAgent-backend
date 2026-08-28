"""Built-in tool: read or precisely edit a text file (confined, opt-in).

This is the second *state-changing* built-in tool (after ``exec``). It reads a
UTF-8 text file, or performs a **precise** edit — replacing an exact
``old_string`` with ``new_string`` — inside a single configured directory.
It is **off by default** (``ENABLE_EDIT_TOOL``) and always declares
``ToolPermission.ASK``: every call needs a one-time human Approve before it
touches the filesystem. The full arguments (``path`` / ``old_string`` /
``new_string``) are shown verbatim on the approval card — via a structured
``Action:`` block (:meth:`EditTool.approval_detail`) that lays out the exact
old/new text — so the owner sees precisely what will change.

Where ``exec`` is "run an arbitrary command", ``edit`` is deliberately a much
narrower capability: the model does not write shell and cannot touch files
outside the configured root. Defence in depth (all *inside* ``execute`` — the
tool loop is untouched):

1. **Path confinement** — :meth:`EditTool._resolve` resolves the requested path
   (``..`` and **symlinks**) and refuses anything that does not land inside the
   configured ``EDIT_WORKDIR`` *before any read or write*. This is the tool's
   core safety property and the reason ``EDIT_WORKDIR`` is *required* when the
   tool is enabled (config refuses to start without it). A ``..`` escape, an
   absolute path outside the root, or a symlink pointing outside all yield
   ``edit_path_escape`` — even after the owner approves.
2. **Exact-match semantics** — ``old_string`` must appear **exactly once** in
   the file unless ``replace_all`` is set (``edit_not_found`` /
   ``edit_not_unique``). No fuzzy matching, no whole-file rewrite.
3. **Atomic write** — the new content is written to a same-directory temp file
   (``fsync`` + ``os.replace``), so a mid-write crash never leaves a half-written
   file (the same idiom the attachment store and the MCP permissions file use).

**Output bounding:** a ``read`` result's content is tail-truncated to
``max_read_chars`` with a fixed ``[N chars … truncated]`` marker (the exec
idiom — truncation, not an error, because the read was already human-approved).
``old_string`` / ``new_string`` are separately bounded by ``max_string_chars``,
which is also baked into the parameter schema's ``maxLength`` so the model's
proposal — and thus the approval card — stays a manageable size.

**Logging rule:** the path, file content, and the old/new strings are returned
to the *model only*. They are **never** logged here and **cannot** reach the
audit table (which stores only the tool name, a stable code, latency, and a
hashed scope). On failure only a stable code is produced.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..base import Tool
from ..policy import ToolPermission


# Fixed, non-echoing model-facing messages for the edit-specific result codes.
# (These are *tool* codes, not the loop-level codes in ``..audit``.)
_MESSAGES = {
    "edit_path_escape": "The path resolves outside the permitted working directory and was not touched.",
    "edit_file_not_found": "The file does not exist.",
    "edit_not_a_file": "The path is not a regular file.",
    "edit_read_failed": "The file could not be read as UTF-8 text.",
    "edit_invalid_op": "The edit arguments were invalid for the requested operation.",
    "edit_not_found": "The text to replace was not found in the file.",
    "edit_not_unique": "The text to replace is not unique; use replace_all or a more specific string.",
    "edit_write_failed": "The file could not be written.",
}


class _Escape(Exception):
    """Internal sentinel: the requested path resolves outside the root."""


def _error(code: str) -> str:
    """A stable, short JSON error result for a non-successful edit outcome (fed
    to the model). Returned (not raised) so the specific code reaches the model —
    a raised exception would be flattened to ``tool_execution_failed`` by the loop.
    """
    return json.dumps({"error": {"code": code, "message": _MESSAGES[code]}})


def _bound_text(text: str, cap: int) -> str:
    """Tail-truncate a ``read`` result to ``cap`` chars, prefixing a fixed marker
    naming how many *earlier* chars were dropped (a number — no content echo).
    """
    if len(text) <= cap:
        return text
    dropped = len(text) - cap
    return f"[{dropped} chars of earlier output truncated]\n{text[-cap:]}"


class EditTool(Tool):
    """Read or precisely edit a UTF-8 text file within a configured directory
    (opt-in, always ``ask``)."""

    name = "edit"
    description = (
        "Read or precisely edit a UTF-8 text file inside the configured working "
        "directory. operation='read' returns the file's content; operation='replace' "
        "replaces old_string with new_string (old_string must occur exactly once in "
        "the file unless replace_all is true). Paths must stay within the working "
        "directory. Every call requires human approval before it runs."
    )
    # State-changing: must default to ``ask`` (never ``allow``) — the owner
    # approves every call, and the path + old/new strings are shown on the card.
    # ``parameters`` is built in ``__init__`` (its ``maxLength`` depends on config),
    # so there is no class-level default here.
    default_permission = ToolPermission.ASK
    parameters: dict[str, object] = {}  # populated per-instance in __init__

    def __init__(
        self,
        *,
        workdir: str,
        max_string_chars: int,
        max_read_chars: int,
    ) -> None:
        # Resolve the root once so the confinement check is stable regardless of
        # the process cwd or how the config path was spelled.
        self._root = Path(workdir).resolve()
        self._max_string = max_string_chars
        self._max_read = max_read_chars
        # ``maxLength`` on the two replace strings bounds the model's proposal and,
        # with it, the approval card's argument block (kept well under Telegram's
        # single-message limit); the read result is bounded separately.
        self.parameters = {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "replace"],
                    "description": "'read' to view a file, 'replace' to edit it.",
                },
                "path": {
                    "type": "string",
                    "description": "File path, absolute or relative to the working directory.",
                },
                "old_string": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": max_string_chars,
                    "description": "replace only: the exact existing text to find (unique unless replace_all).",
                },
                "new_string": {
                    "type": "string",
                    "maxLength": max_string_chars,
                    "description": "replace only: the text to substitute (an empty string deletes old_string).",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "replace only: replace every occurrence instead of requiring a unique match.",
                },
            },
            "required": ["operation", "path"],
            "additionalProperties": False,
        }

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        # Fixed and argument-free. The path + old/new strings are already shown in
        # the card's separate "Arguments:" block, so they are deliberately NOT
        # echoed here (secret-free convention — the strings may contain anything).
        return (
            "Read or precisely edit a UTF-8 text file within the configured working "
            "directory. Requires approval."
        )

    def approval_detail(self, arguments: dict[str, Any]) -> str:
        """A human-friendly, faithful view of this call for the approval card.

        Shown in place of the generic JSON "Arguments:" block (see
        :meth:`Tool.approval_detail`). For ``replace`` it renders the **exact**
        ``old_string`` / ``new_string`` values — newlines and all — so the owner
        approves precisely what will be matched and what it becomes, rather than a
        wall of escaped JSON. Plain text, no markup: the approval provider
        HTML-escapes and length-bounds it and wraps it in a code block (which
        preserves the newlines). ``read`` shows just the target file.
        """
        path = arguments.get("path")
        if arguments.get("operation") == "replace":
            replace_all = bool(arguments.get("replace_all", False))
            old = arguments.get("old_string")
            new = arguments.get("new_string")
            lines = [
                f"📄 File: {path}",
                f"🔁 Operation: replace (replace_all: {'yes' if replace_all else 'no'})",
                "── old_string ──",
                "" if old is None else str(old),
                "── new_string ──",
                "(empty — this deletes old_string)" if new == "" else ("" if new is None else str(new)),
            ]
        else:
            lines = [
                f"📄 File: {path}",
                "Operation: read",
            ]
        return "\n".join(lines)

    def _resolve(self, path: str) -> Path:
        """Resolve ``path`` to a real path *inside* ``self._root``.

        A relative path is interpreted against the root; an absolute path is
        allowed only if it still lands inside it. ``resolve()`` collapses ``..``
        and follows symlinks, so a ``../`` escape, an out-of-root absolute path,
        or a symlink that points outside all resolve to a path whose ancestors do
        not include the root — :class:`_Escape` is raised. A path that is
        malformed for the OS (``resolve()`` raises) is treated the same: a
        "don't touch" outcome, never a read/write.
        """
        base = Path(path)
        if not base.is_absolute():
            base = self._root / base
        try:
            resolved = base.resolve()
        except (ValueError, OSError):
            raise _Escape() from None
        if self._root not in resolved.parents:
            raise _Escape()
        return resolved

    def _rel(self, target: Path) -> str:
        """The target as a path relative to the root (cleaner + leaks no absolute
        system path in the model-facing result)."""
        try:
            return str(target.relative_to(self._root))
        except ValueError:
            return str(target)

    @staticmethod
    def _atomic_write(target: Path, text: str) -> None:
        """Publish ``text`` to ``target`` atomically (same-dir temp + fsync +
        ``os.replace``). The target already exists and is inside the root, so the
        parent directory is present — no mkdir. Raises ``OSError`` on a real
        write failure (mapped to ``edit_write_failed``)."""
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

    async def execute(self, arguments: dict[str, Any]) -> str:
        operation = arguments.get("operation")
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            return _error("edit_invalid_op")

        # (1) Confinement — before any I/O. A rejected path is never read or written.
        try:
            target = self._resolve(path)
        except _Escape:
            return _error("edit_path_escape")

        if not target.is_file():
            return _error("edit_not_a_file" if target.exists() else "edit_file_not_found")

        if operation == "read":
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return _error("edit_read_failed")
            return json.dumps(
                {
                    "operation": "read",
                    "path": self._rel(target),
                    "content": _bound_text(content, self._max_read),
                }
            )

        if operation != "replace":
            # A non-enum value would normally be stopped by the schema gate; this
            # is the defensive backstop for a direct (test) call.
            return _error("edit_invalid_op")

        old = arguments.get("old_string")
        new = arguments.get("new_string")
        replace_all = bool(arguments.get("replace_all", False))
        if not isinstance(old, str) or not old or not isinstance(new, str):
            return _error("edit_invalid_op")

        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return _error("edit_read_failed")

        count = content.count(old)
        if count == 0:
            return _error("edit_not_found")
        if count > 1 and not replace_all:
            return _error("edit_not_unique")

        new_content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        try:
            self._atomic_write(target, new_content)
        except OSError:
            return _error("edit_write_failed")
        return json.dumps(
            {
                "operation": "replace",
                "path": self._rel(target),
                "replacements": count if replace_all else 1,
            }
        )
