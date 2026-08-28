"""Exec shell tool — the four config knobs (opt-in default off).

Local only: no subprocess, no network. Verifies that ``ENABLE_EXEC_TOOL`` (default
off), ``MAX_EXEC_TOOL_RESULT_CHARS``, ``EXEC_WORKDIR``, and ``EXEC_POLICY_DENY_PATTERNS``
are parsed and validated correctly. The numeric/workdir knobs are validated **only
when the tool is enabled** — a default (off) deployment never requires them, matching
how the other optional providers (MCP / infra) are config-gated rather than on-by-default.

The deny patterns are always parse-validated (fail-closed): a malformed list is a
startup ``ConfigError`` even before the tool is built.
"""

from __future__ import annotations

import json

import pytest

from fibrecase_agent_backend.config import ConfigError, load_config

_KNOBS = (
    "ENABLE_EXEC_TOOL",
    "MAX_EXEC_TOOL_RESULT_CHARS",
    "EXEC_WORKDIR",
    "EXEC_POLICY_DENY_PATTERNS",
)


def _env(**extra):
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
    for knob in _KNOBS:
        monkeypatch.delenv(knob, raising=False)
    for k, v in _env(**extra).items():
        monkeypatch.setenv(k, v)
    return load_config()


# ===========================================================================
# opt-in default
# ===========================================================================
def test_exec_disabled_by_default(monkeypatch):
    cfg = _load(monkeypatch)
    assert cfg.enable_exec_tool is False


def test_exec_enabled(monkeypatch):
    cfg = _load(monkeypatch, ENABLE_EXEC_TOOL="true")
    assert cfg.enable_exec_tool is True


# ===========================================================================
# MAX_EXEC_TOOL_RESULT_CHARS
# ===========================================================================
def test_exec_result_chars_default(monkeypatch):
    assert _load(monkeypatch).max_exec_tool_result_chars == 8000


def test_exec_result_chars_custom(monkeypatch):
    assert _load(monkeypatch, ENABLE_EXEC_TOOL="true", MAX_EXEC_TOOL_RESULT_CHARS="123").max_exec_tool_result_chars == 123


def test_exec_result_chars_zero_rejected_when_enabled(monkeypatch):
    with pytest.raises(ConfigError, match="MAX_EXEC_TOOL_RESULT_CHARS"):
        _load(monkeypatch, ENABLE_EXEC_TOOL="true", MAX_EXEC_TOOL_RESULT_CHARS="0")


def test_exec_result_chars_zero_ignored_when_disabled(monkeypatch):
    # Off => the numeric knob is not validated (default deploy needs no exec config).
    assert _load(monkeypatch, MAX_EXEC_TOOL_RESULT_CHARS="0").max_exec_tool_result_chars == 0


# ===========================================================================
# EXEC_WORKDIR
# ===========================================================================
def test_exec_workdir_default_none(monkeypatch):
    assert _load(monkeypatch).exec_workdir is None


def test_exec_workdir_unset_empty_is_none(monkeypatch):
    assert _load(monkeypatch, ENABLE_EXEC_TOOL="true", EXEC_WORKDIR="   ").exec_workdir is None


def test_exec_workdir_existing_dir_ok(monkeypatch, tmp_path):
    assert _load(monkeypatch, ENABLE_EXEC_TOOL="true", EXEC_WORKDIR=str(tmp_path)).exec_workdir == str(tmp_path)


def test_exec_workdir_missing_dir_rejected_when_enabled(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="EXEC_WORKDIR"):
        _load(monkeypatch, ENABLE_EXEC_TOOL="true", EXEC_WORKDIR=str(tmp_path / "missing"))


def test_exec_workdir_missing_dir_ignored_when_disabled(monkeypatch, tmp_path):
    assert _load(monkeypatch, EXEC_WORKDIR=str(tmp_path / "missing")).exec_workdir == str(tmp_path / "missing")


def test_exec_workdir_file_not_dir_rejected_when_enabled(monkeypatch, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(ConfigError, match="EXEC_WORKDIR"):
        _load(monkeypatch, ENABLE_EXEC_TOOL="true", EXEC_WORKDIR=str(f))


# ===========================================================================
# EXEC_POLICY_DENY_PATTERNS (always parse-validated, add-only)
# ===========================================================================
def test_exec_deny_patterns_default_empty(monkeypatch):
    assert _load(monkeypatch).exec_policy_deny_patterns == ()


def test_exec_deny_patterns_valid(monkeypatch):
    pats = ["\\bdocker\\b", "\\bkubectl\\b"]
    cfg = _load(monkeypatch, ENABLE_EXEC_TOOL="true", EXEC_POLICY_DENY_PATTERNS=json.dumps(pats))
    assert cfg.exec_policy_deny_patterns == tuple(pats)


def test_exec_deny_patterns_empty_array(monkeypatch):
    assert _load(monkeypatch, ENABLE_EXEC_TOOL="true", EXEC_POLICY_DENY_PATTERNS="[]").exec_policy_deny_patterns == ()


def test_exec_deny_patterns_bad_json(monkeypatch):
    with pytest.raises(ConfigError, match="EXEC_POLICY_DENY_PATTERNS"):
        _load(monkeypatch, EXEC_POLICY_DENY_PATTERNS="{not json")


def test_exec_deny_patterns_non_array(monkeypatch):
    with pytest.raises(ConfigError, match="EXEC_POLICY_DENY_PATTERNS"):
        _load(monkeypatch, EXEC_POLICY_DENY_PATTERNS=json.dumps({"a": 1}))


def test_exec_deny_patterns_non_string_element(monkeypatch):
    with pytest.raises(ConfigError, match="EXEC_POLICY_DENY_PATTERNS"):
        _load(monkeypatch, EXEC_POLICY_DENY_PATTERNS=json.dumps(["\\bx\\b", 3]))


def test_exec_deny_patterns_empty_string_element(monkeypatch):
    with pytest.raises(ConfigError, match="EXEC_POLICY_DENY_PATTERNS"):
        _load(monkeypatch, EXEC_POLICY_DENY_PATTERNS=json.dumps(["  "]))


def test_exec_deny_patterns_invalid_regex(monkeypatch):
    with pytest.raises(ConfigError, match="EXEC_POLICY_DENY_PATTERNS"):
        _load(monkeypatch, EXEC_POLICY_DENY_PATTERNS=json.dumps(["("]))


def test_exec_deny_patterns_always_validated_even_when_disabled(monkeypatch):
    # The deny-list parse is fail-closed regardless of the opt-in flag — a bad
    # list must never be silently dropped (that would weaken the backstop).
    with pytest.raises(ConfigError, match="EXEC_POLICY_DENY_PATTERNS"):
        _load(monkeypatch, EXEC_POLICY_DENY_PATTERNS=json.dumps(["("]))


def test_exec_deny_patterns_error_names_index_not_body(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        _load(monkeypatch, EXEC_POLICY_DENY_PATTERNS=json.dumps(["ok", "("]))
    assert "EXEC_POLICY_DENY_PATTERNS[1]" in str(exc.value)
    # …but never the offending pattern body.
    assert "(SECRET" not in str(exc.value)
