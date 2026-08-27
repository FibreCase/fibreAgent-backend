"""Tool policy + config wiring (phase 3 — required #1, #2).

Pure logic: no LLM, no Telegram, no DB. Verifies the permission model, the
single-permission parser, the composition-root ``build_policy`` precedence, and
that the config knobs fail fast (``ConfigError``) on a botched setting. The
MCP-permissions *file* source is covered in ``tests/test_mcp_permissions_file.py``.
"""

from __future__ import annotations

import pytest

from fibrecase_agent_backend.config import ConfigError, load_config
from fibrecase_agent_backend.tools import (
    ToolPermission,
    ToolPolicy,
    ToolPolicyError,
    build_default_tools,
    build_policy,
    parse_permission,
)
from fibrecase_agent_backend.tools.base import Tool
from fibrecase_agent_backend.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# required #1 — built-ins default allow; a new tool defaults to ask
# ---------------------------------------------------------------------------
def test_builtin_tools_declare_allow():
    reg = build_default_tools()
    perms = reg.default_permissions()
    assert set(perms) == {"get_current_time", "echo", "system_info"}
    # get_current_time + echo are safe read-only tools that run without approval.
    # (system_info is deliberately set to ``ask`` to exercise the approval flow —
    # see the comment on ``SystemInfoTool.default_permission``.)
    assert perms["get_current_time"] is ToolPermission.ALLOW
    assert perms["echo"] is ToolPermission.ALLOW


def test_base_tool_defaults_to_ask():
    # A new tool that does not declare a permission must default to ``ask`` —
    # it can never run bare by accident.
    class Fresh(Tool):
        name = "fresh_tool"
        description = "new"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, arguments):  # pragma: no cover
            return ""

    reg = ToolRegistry().register(Fresh())
    assert reg.default_permissions()["fresh_tool"] is ToolPermission.ASK


def test_build_policy_honours_builtins_and_default_ask():
    reg = build_default_tools()
    policy = build_policy({}, registry=reg)
    # Built-ins keep their declared allow; an *unknown* name falls back to ask.
    assert policy.resolve("echo") is ToolPermission.ALLOW
    assert policy.resolve("get_current_time") is ToolPermission.ALLOW
    assert policy.resolve("some_unknown_tool") is ToolPermission.ASK


def test_build_policy_override_beats_default():
    reg = build_default_tools()
    policy = build_policy({"echo": ToolPermission.DENY}, registry=reg)
    assert policy.resolve("echo") is ToolPermission.DENY
    # A non-overridden built-in is unaffected (get_current_time stays allow).
    assert policy.resolve("get_current_time") is ToolPermission.ALLOW


def test_build_policy_keeps_unregistered_override():
    reg = build_default_tools()
    policy = build_policy({"future_tool": ToolPermission.ALLOW}, registry=reg)
    assert policy.resolve("future_tool") is ToolPermission.ALLOW


# ---------------------------------------------------------------------------
# advertised names: deny is withheld, allow/ask are advertised
# ---------------------------------------------------------------------------
def test_advertised_names_withhold_deny_only():
    reg = build_default_tools()
    policy = build_policy({"echo": ToolPermission.DENY}, registry=reg)
    advertised = policy.advertised_names(set(reg.names()))
    assert "echo" not in advertised
    assert "get_current_time" in advertised
    assert "system_info" in advertised


# ---------------------------------------------------------------------------
# required #2 — single-permission parsing (case-insensitive; malformed raises)
# ---------------------------------------------------------------------------
def test_parse_permission_is_case_insensitive():
    assert parse_permission("ALLOW") is ToolPermission.ALLOW
    assert parse_permission(" Ask ") is ToolPermission.ASK


@pytest.mark.parametrize("raw", ["maybe", "", "  ", "all", "deny " * 0])
def test_parse_permission_invalid_raises(raw):
    with pytest.raises(ToolPolicyError):
        parse_permission(raw)


# ---------------------------------------------------------------------------
# required #2 — config load fails fast on bad security settings
# ---------------------------------------------------------------------------
def _env(**extra):
    """A minimal, otherwise-valid env for load_config."""
    base = {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_ALLOWED_USER_IDS": "1",
        "OPENAI_BASE_URL": "https://h/v1",
        "OPENAI_API_KEY": "k",
        "OPENAI_MODEL": "m",
    }
    base.update(extra)
    return base


def _load(monkeypatch, **extra):
    # Clear the knobs under test first (a stray .env value must not interfere);
    # then apply the explicit values the test wants.
    for knob in ("MCP_PERMISSIONS_FILE", "TOOL_APPROVAL_TIMEOUT_SECONDS", "TOOL_TIMEOUT_SECONDS"):
        monkeypatch.delenv(knob, raising=False)
    for k, v in _env(**extra).items():
        monkeypatch.setenv(k, v)
    return load_config()


def test_config_no_permissions_file_by_default(monkeypatch):
    # Unset MCP_PERMISSIONS_FILE → no policy file (built-ins ride declared defaults).
    cfg = _load(monkeypatch)
    assert cfg.mcp_permissions_file is None
    assert cfg.tool_approval_timeout_seconds == 60.0
    assert cfg.tool_timeout_seconds == 30.0


def test_config_captures_permissions_file_path(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, MCP_PERMISSIONS_FILE=str(tmp_path / "perm.json"))
    # A set-but-missing file is fine (seeded at startup); the path is captured.
    assert cfg.mcp_permissions_file == tmp_path / "perm.json"


@pytest.mark.parametrize("knob", ["TOOL_APPROVAL_TIMEOUT_SECONDS", "TOOL_TIMEOUT_SECONDS"])
@pytest.mark.parametrize("val", ["0", "-5"])
def test_config_rejects_nonpositive_timeout(monkeypatch, knob, val):
    with pytest.raises(ConfigError):
        _load(monkeypatch, **{knob: val})


def test_config_parses_custom_timeouts(monkeypatch):
    cfg = _load(monkeypatch, TOOL_APPROVAL_TIMEOUT_SECONDS="12", TOOL_TIMEOUT_SECONDS="0.5")
    assert cfg.tool_approval_timeout_seconds == 12.0
    assert cfg.tool_timeout_seconds == 0.5
