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

from dataclasses import dataclass
from enum import Enum


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
    ``ask``); the composition root builds the policy so that an *override*
    (``TOOL_PERMISSION_OVERRIDES``) wins, then the tool default, then ``ask``.
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


def parse_tool_permission_overrides(raw: str | None) -> dict[str, ToolPermission]:
    """Parse ``TOOL_PERMISSION_OVERRIDES`` into a name→permission map.

    The value is a comma-separated list of ``<tool>=allow|ask|deny`` pairs, e.g.
    ``echo=allow,my_risky_tool=deny``. Empty / ``None`` → ``{}`` (use tool
    defaults).

    Parsing is *strict on purpose*: a mistyped tool name or permission must be a
    startup ``ConfigError`` — silently ignoring a botched security setting would
    be a hole. Malformed entries (no ``=``, empty name, bad permission, empty
    value) all raise :class:`ToolPolicyError`. Duplicate tool names are also an
    error (a policy is a function of the tool, not a bag of rules).
    """
    if not raw or not raw.strip():
        return {}

    result: dict[str, ToolPermission] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ToolPolicyError(
                f"invalid TOOL_PERMISSION_OVERRIDES entry {chunk!r} (expected <tool>=allow|ask|deny)"
            )
        name, _, value = chunk.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            raise ToolPolicyError(
                f"TOOL_PERMISSION_OVERRIDES entry {chunk!r} has an empty tool name"
            )
        if not value:
            raise ToolPolicyError(
                f"TOOL_PERMISSION_OVERRIDES entry {chunk!r} has an empty permission"
            )
        permission = parse_permission(value)
        if name in result:
            raise ToolPolicyError(
                f"duplicate tool in TOOL_PERMISSION_OVERRIDES: {name!r}"
            )
        result[name] = permission
    return result


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
