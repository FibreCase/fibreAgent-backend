"""Tool policy, override parsing, and config wiring (phase 3 — required #1, #2).

Pure logic: no LLM, no Telegram, no DB. Verifies the permission model, the
strict override parser, the composition-root ``build_policy`` precedence, and
that the config knobs fail fast (``ConfigError``) on a botched security setting.
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
    parse_tool_permission_overrides,
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
    overrides = parse_tool_permission_overrides("echo=deny")
    policy = build_policy(overrides, registry=reg)
    assert policy.resolve("echo") is ToolPermission.DENY
    # A non-overridden built-in is unaffected (get_current_time stays allow).
    assert policy.resolve("get_current_time") is ToolPermission.ALLOW


def test_build_policy_keeps_unregistered_override():
    reg = build_default_tools()
    overrides = parse_tool_permission_overrides("future_tool=allow")
    policy = build_policy(overrides, registry=reg)
    assert policy.resolve("future_tool") is ToolPermission.ALLOW


# ---------------------------------------------------------------------------
# advertised names: deny is withheld, allow/ask are advertised
# ---------------------------------------------------------------------------
def test_advertised_names_withhold_deny_only():
    reg = build_default_tools()
    policy = build_policy(parse_tool_permission_overrides("echo=deny"), registry=reg)
    advertised = policy.advertised_names(set(reg.names()))
    assert "echo" not in advertised
    assert "get_current_time" in advertised
    assert "system_info" in advertised


# ---------------------------------------------------------------------------
# required #2 — strict override parsing (malformed entries raise)
# ---------------------------------------------------------------------------
def test_parse_empty_overrides_is_empty():
    assert parse_tool_permission_overrides("") == {}
    assert parse_tool_permission_overrides(None) == {}
    assert parse_tool_permission_overrides("   ") == {}


def test_parse_valid_overrides():
    out = parse_tool_permission_overrides("a=allow, b=ask , c=deny")
    assert out == {"a": ToolPermission.ALLOW, "b": ToolPermission.ASK, "c": ToolPermission.DENY}


def test_parse_permission_is_case_insensitive():
    assert parse_tool_permission_overrides("a=ALLOW")["a"] is ToolPermission.ALLOW


@pytest.mark.parametrize(
    "raw",
    [
        "echo",            # no '='
        "=allow",          # empty name
        "echo=",           # empty permission
        "echo=maybe",      # bad permission
        "a=allow,a=ask",   # duplicate tool name
    ],
)
def test_parse_invalid_override_raises(raw):
    with pytest.raises(ToolPolicyError):
        parse_tool_permission_overrides(raw)


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
    for knob in ("TOOL_PERMISSION_OVERRIDES", "TOOL_APPROVAL_TIMEOUT_SECONDS", "TOOL_TIMEOUT_SECONDS"):
        monkeypatch.delenv(knob, raising=False)
    for k, v in _env(**extra).items():
        monkeypatch.setenv(k, v)
    return load_config()


def test_config_parses_valid_overrides(monkeypatch):
    cfg = _load(monkeypatch, TOOL_PERMISSION_OVERRIDES="echo=deny, get_current_time=allow")
    assert cfg.tool_permission_overrides == {
        "echo": ToolPermission.DENY,
        "get_current_time": ToolPermission.ALLOW,
    }
    assert cfg.tool_approval_timeout_seconds == 60.0
    assert cfg.tool_timeout_seconds == 30.0


def test_config_rejects_bad_permission(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, TOOL_PERMISSION_OVERRIDES="echo=bogus")


def test_config_rejects_invalid_tool_name(monkeypatch):
    # A tool name outside [A-Za-z0-9_-]+ is a startup error, never silently ignored.
    with pytest.raises(ConfigError):
        _load(monkeypatch, TOOL_PERMISSION_OVERRIDES="bad name=allow")


def test_config_rejects_duplicate_override(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, TOOL_PERMISSION_OVERRIDES="echo=allow,echo=deny")


@pytest.mark.parametrize("knob", ["TOOL_APPROVAL_TIMEOUT_SECONDS", "TOOL_TIMEOUT_SECONDS"])
@pytest.mark.parametrize("val", ["0", "-5"])
def test_config_rejects_nonpositive_timeout(monkeypatch, knob, val):
    with pytest.raises(ConfigError):
        _load(monkeypatch, **{knob: val})


def test_config_parses_custom_timeouts(monkeypatch):
    cfg = _load(monkeypatch, TOOL_APPROVAL_TIMEOUT_SECONDS="12", TOOL_TIMEOUT_SECONDS="0.5")
    assert cfg.tool_approval_timeout_seconds == 12.0
    assert cfg.tool_timeout_seconds == 0.5
