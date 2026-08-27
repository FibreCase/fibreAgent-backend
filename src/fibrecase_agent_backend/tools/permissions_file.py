"""Dedicated MCP tool-permission file (phase 4.x).

The permission overrides for MCP tools (``mcp_<server>__<remote>``) live in a
standalone JSON **file** named by ``MCP_PERMISSIONS_FILE`` (a path relative to
the working directory) — replacing the old inline ``TOOL_PERMISSION_OVERRIDES``
env var, which the backend no longer reads. This module is the *only* place that
knows the file's on-disk shape and how to merge it with the current MCP tool
set. It is channel-/protocol-/ORM-free and holds no secrets (just tool names +
``allow``/``ask``/``deny``), so its error paths are safe to log, but it still
follows the house rule of naming the offending field/tool only.

File shape — a JSON **array** of objects::

    [
      { "tool": "mcp_alpha__get_weather", "permission": "deny" },
      { "tool": "mcp_fs__read_file", "permission": "" }
    ]

* ``tool`` — the namespaced MCP tool name (``[A-Za-z0-9_-]+``).
* ``permission`` — ``"allow"`` / ``"ask"`` / ``"deny"`` / ``""``. ``""`` (or the
  field absent) means *use the tool's default* (``ask`` for MCP tools). A ``""``
  / absent entry is the **unfilled** one the backend may prune; a non-empty entry
  is **filled** and is always preserved.

Two behaviours live here, both pure and testable:

* :func:`reconcile_permissions_file` — the **seed/sync** (backend → file): rewrite
  the file so every current tool has an entry, filled entries are preserved
  verbatim (even for tools that later vanish), and unfilled entries for tools
  that no longer exist are dropped. Atomic write; skipped when the output is
  byte-identical to the existing file (no mtime churn).
* :func:`parse_permissions_json` / :func:`load_permissions_file` — the **read**
  side, used by the composition root (the fail-to-start gate) and the
  hot-reload wrapper.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# A legal namespaced tool name (``mcp_<server>__<remote>``) — the same charset
# the registry / policy / audit layers accept.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# ``""`` (empty) is a legal *unfilled* permission; the three real values too.
_ALLOWED_PERMISSIONS = frozenset({"allow", "ask", "deny", ""})

# The only two fields an entry may carry; anything else is a violation.
_ALLOWED_FIELDS = frozenset({"tool", "permission"})


class PermissionsFileError(ValueError):
    """Raised for a present-but-malformed permissions file (bad JSON, a
    non-array, a bad entry, an unknown field, an illegal tool name or
    permission, or a duplicate tool). The caller maps this to a startup
    ``ConfigError`` (fail-to-start) or, on the hot-reload path, keeps the
    last-good policy and logs a warning."""


def parse_permissions_json(text: str) -> list[dict]:
    """Strictly parse the file body into an ordered list of entry dicts.

    Raises :class:`PermissionsFileError` on any violation. Only the offending
    field/tool is named in the message — never the file's other contents.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PermissionsFileError(f"invalid MCP permissions JSON: {exc.msg}") from exc

    if not isinstance(data, list):
        raise PermissionsFileError("MCP permissions file must be a JSON array of tool objects")

    entries: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise PermissionsFileError(f"MCP permissions entry #{i} must be an object")
        for key in item:
            if key not in _ALLOWED_FIELDS:
                raise PermissionsFileError(f"MCP permissions entry #{i} has an unknown field {key!r}")
        tool = item.get("tool")
        if not isinstance(tool, str) or not tool:
            raise PermissionsFileError(f"MCP permissions entry #{i} is missing a valid 'tool' name")
        if not _TOOL_NAME_RE.match(tool):
            raise PermissionsFileError(f"MCP permissions entry #{i} has an invalid tool name {tool!r}")
        if "permission" in item:
            permission = item["permission"]
            if not isinstance(permission, str) or permission not in _ALLOWED_PERMISSIONS:
                raise PermissionsFileError(
                    f"MCP permissions entry {tool!r} has an invalid permission (expected allow/ask/deny or empty)"
                )
        else:
            permission = ""
        if tool in seen:
            raise PermissionsFileError(f"duplicate tool in MCP permissions file: {tool!r}")
        seen.add(tool)
        entries.append({"tool": tool, "permission": permission})
    return entries


def load_permissions_file(path: str | Path) -> list[dict]:
    """Read + strictly parse ``path``. A missing or blank (0-byte / whitespace)
    file is *not* an error — it means "no overrides" and returns ``[]``. A
    present-but-malformed file raises :class:`PermissionsFileError`."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise PermissionsFileError(f"cannot read MCP permissions file '{path}': {exc.strerror or exc}") from exc
    if not text.strip():
        return []
    return parse_permissions_json(text)


def merge_permissions(existing: list[dict], current_names: list[str]) -> list[dict]:
    """Merge the existing file entries with the *current* MCP tool set.

    Result order is deterministic: current tools first (in the given order),
    each carrying its existing permission (or ``""`` if newly discovered); then
    the surviving **filled** orphans (a name no longer in ``current_names`` whose
    permission is non-empty) sorted by tool name. An unfilled (``""``) entry for
    a tool that no longer exists is dropped. Idempotent.
    """
    existing_by_tool = {e["tool"]: e["permission"] for e in existing}
    merged: list[dict] = []
    for name in current_names:
        merged.append({"tool": name, "permission": existing_by_tool.get(name, "")})
    orphans = [
        {"tool": t, "permission": perm}
        for t, perm in existing_by_tool.items()
        if t not in set(current_names) and perm != ""
    ]
    orphans.sort(key=lambda e: e["tool"])
    merged.extend(orphans)
    return merged


def serialize(entries: list[dict]) -> str:
    """The single canonical serialisation, used for *both* the byte-compare
    (skip-if-unchanged) and the write, so a round-trip is byte-identical."""
    return json.dumps(entries, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def atomic_write(path: str | Path, text: str) -> bool:
    """Atomically publish ``text`` to ``path`` (same-dir temp + ``os.replace``).

    Returns ``True`` if the file was written, ``False`` if the existing content
    was already byte-identical and the write was skipped (no mtime churn). Logs
    only the path, byte count, and entry count — never the content.
    """
    p = Path(path)
    try:
        existing = p.read_text(encoding="utf-8")
        if existing == text:
            return False
    except FileNotFoundError:
        pass
    except OSError:
        # A read failure is not fatal to the seed: fall through and rewrite.
        pass

    parent = p.parent
    if str(parent) and str(parent) != ".":
        parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(parent or "."), prefix="." + p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, p)
    except Exception as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        logger.error(
            "MCP permissions file write failed",
            extra={"path": str(p), "bytes": len(text.encode("utf-8"))},
            exc_info=exc,
        )
        raise
    logger.info(
        "MCP permissions file written",
        extra={"path": str(p), "bytes": len(text.encode("utf-8"))},
    )
    return True


def reconcile_permissions_file(path: str | Path, current_names: list[str]) -> None:
    """Seed/sync the file to the current MCP tool set (backend → file).

    Load (missing/blank → ``[]``) → merge → canonical serialize → byte-compare →
    atomic write (skipped when unchanged). Raises :class:`PermissionsFileError`
    if the existing file is present-but-malformed (the caller decides whether
    that is a startup error or a logged warning).
    """
    existing = load_permissions_file(path)
    merged = merge_permissions(existing, list(current_names))
    text = serialize(merged)
    atomic_write(path, text)
