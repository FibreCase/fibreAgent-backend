"""Exec tool static policy — a small, conservative command denylist (backstop).

This is the **static** layer of the exec tool's defence-in-depth. It sits *in
front of the shell* (checked first thing in :meth:`ExecTool.execute`, before any
spawn) and vetoes a fixed set of **catastrophic** command shapes even if the
owner has just approved the call — the guard against mis-approval from fatigue.

It is deliberately **not** a substitute for the per-call human approval
(``default_permission = ask``): approval is the real gate; the denylist only
catches the small, obvious set of commands whose blast radius is the whole
machine. It is a *backstop*, not a sandbox — it cannot reason about intent, so
false negatives are expected and tolerated (that is what approval is for), while
false positives are acceptable for a veto (an over-cautious refusal is safe).

Design rules:
- **Pure + stdlib-only.** No subprocess, no import of the tool, no I/O. ``re``
  only, so it is trivially unit-testable with no process spawned.
- **Add-only.** :data:`CORE_DENY_PATTERNS` is compiled in code and always
  active; the operator may *add* patterns (``EXEC_POLICY_DENY_PATTERNS``) but
  can never remove the core ones. :func:`compile_denylist` just concatenates.
- **The command is never logged here.** :func:`check_exec_policy` returns only
  the *matched pattern's source* (a stable, testable token), never the command.
"""

from __future__ import annotations

import re

# Core catastrophic-command shapes, matched anywhere with :func:`re.search`
# (case-sensitive — shell commands are). Intentionally small and conservative.
CORE_DENY_PATTERNS: tuple[re.Pattern[str], ...] = (
    # rm with --no-preserve-root, or a recursive delete rooted at / or $HOME
    # (a bare ``~`` = the home dir; ``rm -rf ~/subdir`` is *not* caught — that is
    # a user's own subdirectory and the approval gate covers it).
    re.compile(r"\b--no-preserve-root\b"),
    re.compile(r"\brm\s+(-[^\s]*[rR][^\s]*\s+)*(/\s*$|/\s|~(?=\s|$)|\$HOME(?=\s|$))"),
    # Fork bomb: :(){ :|:& };:  (the leading `:(){ :|:&` is distinctive enough).
    re.compile(r"\:\(\)\s*\{\s*:\s*\|\s*:\s*&"),
    # Pipe a downloaded payload straight into a shell: curl/wget … | sh.
    re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|da)?sh\b"),
    # Raw block-device writes: dd of=/dev/… or redirecting straight to a disk.
    re.compile(r"\bdd\b[^|;&]*\bof=/dev/"),
    re.compile(r">\s*/dev/(sd|nvme|hd|disk|xvd)"),
    # Formatting / swapping a device.
    re.compile(r"\bmkfs(\.[a-z0-9]+)?\b|\bmkswap\b"),
    # Power / halt / init-level changes, anchored to a command boundary so a
    # mid-argument word does not trip it.
    re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?(shutdown|reboot|halt|poweroff|init\s+[06])\b"),
    # World-writable the root (or home) tree.
    re.compile(r"\bchmod\s+(-R\s+)?777\s+(/\s*$|/\s|~/)"),
)


def compile_denylist(extra: tuple[str, ...] = ()) -> tuple[re.Pattern[str], ...]:
    """The effective denylist: the code-compiled core list + the operator's
    add-only patterns.

    ``extra`` are the raw strings from ``EXEC_POLICY_DENY_PATTERNS``; they are
    compiled here (they were already compile-checked at config load, so this
    cannot raise in the normal path). The core list is always present and is
    never filtered out by ``extra``.
    """
    return CORE_DENY_PATTERNS + tuple(re.compile(p) for p in extra)


def check_exec_policy(command: str, denylist: tuple[re.Pattern[str], ...]) -> str | None:
    """Return the source of the first denylist pattern that matches, else ``None``.

    ``None`` means "not vetoed by the static policy" (the command may still be
    human-approved and run). The return value is only used by tests to assert
    *which* rule fired; the tool ignores it and returns a fixed error code. The
    command itself is never returned or logged.
    """
    for pattern in denylist:
        if pattern.search(command):
            return pattern.pattern
    return None
