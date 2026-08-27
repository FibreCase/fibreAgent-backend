"""Tool execution policy (phase 3 — Tool Security).

A *policy* is the per-tool decision about whether a tool call may run at all,
and whether it needs a one-time human approval before it does. It is a pure,
provider-/channel-agnostic value object: it knows nothing about Telegram, the
database, or the OpenAI SDK — it only maps a tool name to one of three
``ToolPermission`` levels.

The policy is the single source of truth for three things:

* which tools are **advertised** to the model (``allow`` + ``ask``; ``deny`` is
  withheld so the model rarely asks for it, and a stray request is still refused);
* whether a tool call is **allowed to execute** at all; and
* whether it must first wait for a **one-time human approval**.

It is re-judged on **every** tool call (never decided once at startup), so a
config change or a tool that is absent from the policy (default ``ask``) is
always enforced at the point of execution.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .permissions_file import PermissionsFileError, load_permissions_file

logger = logging.getLogger(__name__)


class ToolPermission(str, Enum):
    """Whether a tool call may run, and whether it needs human approval.

    * ``allow`` — run immediately (no approval). The safe built-ins live here.
    * ``ask``   — run, but only after a one-time human approval. The **default**
      for any tool that does not explicitly opt into ``allow``, so a future
      state-changing tool can never run bare by accident.
    * ``deny``  — never run; not advertised to the model.
    """

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ToolPolicyError(ValueError):
    """Raised at config-parse time for a malformed policy override."""


@dataclass(frozen=True)
class ToolPolicy:
    """An immutable name→permission map plus the permission to fall back to.

    ``default`` is what a tool resolves to when it is not explicitly mapped. It
    is normally the *tool's own declared* ``default_permission`` (base default
    ``ask``); the composition root builds the policy so that an *override* (from
    the ``MCP_PERMISSIONS_FILE``, via :class:`FileBackedToolPolicy`) wins, then
    the tool default, then ``ask``.
    """

    default: ToolPermission
    _overrides: frozenset[tuple[str, ToolPermission]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_overrides", frozenset(self._overrides))

    @classmethod
    def from_items(
        cls, items: dict[str, ToolPermission], *, default: ToolPermission = ToolPermission.ASK
    ) -> "ToolPolicy":
        return cls(default=default, _overrides=tuple(items.items()))

    def resolve(self, tool_name: str) -> ToolPermission:
        """The effective permission for ``tool_name`` (override, else default)."""
        for name, perm in self._overrides:
            if name == tool_name:
                return perm
        return self.default

    def needs_approval(self, tool_name: str) -> bool:
        return self.resolve(tool_name) is ToolPermission.ASK

    def is_denied(self, tool_name: str) -> bool:
        return self.resolve(tool_name) is ToolPermission.DENY

    def advertised_names(self, known: "set[str] | frozenset[str] | list[str]") -> list[str]:
        """The subset of ``known`` tool names the model is allowed to see.

        ``deny`` tools are withheld from the OpenAI ``tools=`` schema so the
        model does not waste a call on them. A tool absent from the policy
        advertises at its (default) permission: ``ask`` and ``allow`` are both
        advertised; only ``deny`` is withheld.
        """
        return [n for n in known if self.resolve(n) is not ToolPermission.DENY]


_VALID_PERMISSIONS = frozenset({"allow", "ask", "deny"})


def parse_permission(raw: str) -> ToolPermission:
    """Parse one of the textual permission levels, case-insensitively."""
    value = raw.strip().lower()
    if value not in _VALID_PERMISSIONS:
        raise ToolPolicyError(f"invalid tool permission: {raw!r} (expected allow/ask/deny)")
    return ToolPermission(value)


def build_policy(
    overrides: dict[str, ToolPermission],
    *,
    registry,
    default: ToolPermission = ToolPermission.ASK,
) -> ToolPolicy:
    """Build a :class:`ToolPolicy` from config overrides + tool declarations.

    Precedence, per tool: an explicit *override* wins, else the tool's own
    ``default_permission``, else ``default`` (``ask``). The per-tool mapping is
    built here from the registry (so a built-in that declares ``allow`` stays
    ``allow``), with ``default`` remaining the fallback for a name that is
    neither registered nor overridden — that is how a stray model request for an
    *unknown* tool is treated as ``ask``-worthy rather than silently allowed.

    The resulting :class:`ToolPolicy` re-resolves on **every** call, so the
    decision is always made at execution time, never cached per tool.
    """
    items: dict[str, ToolPermission] = {}
    for name in registry.names():
        tool = registry.get(name)
        declared = getattr(tool, "default_permission", default)
        items[name] = overrides.get(name, declared)
    # Keep any override for a name that is not (yet) registered — e.g. a tool
    # the operator pre-approves — so it is honoured if it later appears.
    for name, perm in overrides.items():
        items.setdefault(name, perm)
    return ToolPolicy.from_items(items, default=default)


class FileBackedToolPolicy(ToolPolicy):
    """A :class:`ToolPolicy` whose overrides are read from a JSON file and
    **hot-reloaded** — a permission the operator edits in the file takes effect
    on the very next tool call, with no restart.

    It exposes the same surface the tool loop uses (``resolve`` +
    ``advertised_names``), but those delegate to a cached *inner*
    :class:`ToolPolicy` that is rebuilt whenever the file's mtime/size changes.
    Rebuilding goes through :func:`build_policy`, so the built-in declared
    defaults (``allow`` for ``get_current_time``/``echo``, ``ask`` for
    ``system_info``) still flow through unchanged — only the file's MCP-tool
    entries are overrides on top.

    Reload semantics (all on the calling thread, no background task):
    * first use — a missing/blank file means "no overrides" (everything resolves
      to its declared default);
    * file content changed — re-read and rebuild; a *present-but-malformed* file
      (invalid JSON / bad entry) keeps the **last-good** inner policy and logs a
      warning (it must never crash the loop), and the bad version is marked seen
      so it is not re-parsed — and re-warned — every call;
    * file deleted — resolves back to all-defaults (ask).
    """

    def __init__(self, path: str | Path, registry) -> None:
        # Give the frozen base valid placeholder fields (so its generated
        # __eq__/__hash__ never touch an unset attribute); the *live* decision
        # always lives in ``_inner``.
        super().__init__(default=ToolPermission.ASK, _overrides=frozenset())
        object.__setattr__(self, "_path", Path(path))
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_inner", None)
        object.__setattr__(self, "_last_key", None)

    def __repr__(self) -> str:
        # Safe: the path only, never the override contents.
        return f"FileBackedToolPolicy(path={str(self._path)!r})"

    def resolve(self, tool_name: str) -> ToolPermission:
        return self._ensure().resolve(tool_name)

    def advertised_names(self, known: "set[str] | frozenset[str] | list[str]") -> list[str]:
        return self._ensure().advertised_names(known)

    # ------------------------------------------------------------------ reload
    def _ensure(self) -> ToolPolicy:
        self._maybe_reload()
        assert self._inner is not None  # always set by _maybe_reload
        return self._inner

    def _maybe_reload(self) -> None:
        if self._inner is None:
            self._initial_load()
            return
        key = self._stat_key()  # None if the file is now missing/unreadable
        if key == self._last_key:
            return  # unchanged (this also covers "still missing")
        try:
            entries = load_permissions_file(self._path)  # missing -> []
        except PermissionsFileError:
            # Present-but-malformed at runtime: keep last-good, mark this version
            # seen (no re-parse / re-warn every call), and pick up a fix next time
            # the file changes again.
            self._last_key = key
            logger.warning(
                "MCP permissions file became invalid; keeping last-known policy",
                extra={"path": str(self._path)},
            )
            return
        self._last_key = key
        self._inner = build_policy(self._build_overrides(entries), registry=self._registry)

    def _initial_load(self) -> None:
        try:
            entries = load_permissions_file(self._path)  # missing/blank -> []
        except PermissionsFileError:
            # Should not happen (config-load already validated a pre-existing
            # file); be safe and resolve everything to its declared default.
            logger.warning(
                "MCP permissions file invalid on first load; using tool defaults",
                extra={"path": str(self._path)},
            )
            entries = []
        self._last_key = self._stat_key()
        self._inner = build_policy(self._build_overrides(entries), registry=self._registry)

    def _stat_key(self):
        """``(st_mtime_ns, st_size)`` for change detection, or ``None`` when the
        file is missing/unreadable (stat needs only dir execute, so a missing
        file is the ``None`` case here)."""
        try:
            st = os.stat(self._path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    @staticmethod
    def _build_overrides(entries: list[dict]) -> dict[str, ToolPermission]:
        """Map file entries to a name→permission dict, *omitting* unfilled
        (``""``) entries so ``build_policy`` falls through to each tool's
        declared default. Entries are already strictly validated upstream."""
        return {e["tool"]: ToolPermission(e["permission"]) for e in entries if e["permission"] != ""}
