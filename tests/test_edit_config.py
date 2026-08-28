"""Edit file tool — the four config knobs (opt-in default off).

Local only: no subprocess, no network. Verifies that ``ENABLE_EDIT_TOOL`` (default
off), ``EDIT_WORKDIR``, ``MAX_EDIT_STRING_CHARS``, and ``MAX_EDIT_READ_CHARS`` are
parsed and validated correctly.

Unlike ``EXEC_WORKDIR`` (optional), ``EDIT_WORKDIR`` is **required when the tool is
enabled** — the confinement root is the edit tool's core safety property, so a
missing/misconfigured root refuses to start rather than fall back to an unrestricted
cwd. The numeric knobs and the root are all validated **only when the tool is
enabled** — a default (off) deployment never requires them, matching the exec / MCP /
infra config-gating convention.
"""

from __future__ import annotations

import pytest

from fibrecase_agent_backend.config import ConfigError, load_config

_KNOBS = (
    "ENABLE_EDIT_TOOL",
    "EDIT_WORKDIR",
    "MAX_EDIT_STRING_CHARS",
    "MAX_EDIT_READ_CHARS",
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
def test_edit_disabled_by_default(monkeypatch):
    cfg = _load(monkeypatch)
    assert cfg.enable_edit_tool is False


def test_edit_enabled(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, ENABLE_EDIT_TOOL="true", EDIT_WORKDIR=str(tmp_path))
    assert cfg.enable_edit_tool is True


# ===========================================================================
# MAX_EDIT_STRING_CHARS
# ===========================================================================
def test_edit_string_chars_default(monkeypatch):
    assert _load(monkeypatch).max_edit_string_chars == 2000


def test_edit_string_chars_custom(monkeypatch, tmp_path):
    assert _load(monkeypatch, ENABLE_EDIT_TOOL="true", EDIT_WORKDIR=str(tmp_path), MAX_EDIT_STRING_CHARS="500").max_edit_string_chars == 500


def test_edit_string_chars_zero_rejected_when_enabled(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="MAX_EDIT_STRING_CHARS"):
        _load(monkeypatch, ENABLE_EDIT_TOOL="true", EDIT_WORKDIR=str(tmp_path), MAX_EDIT_STRING_CHARS="0")


def test_edit_string_chars_zero_ignored_when_disabled(monkeypatch):
    # Off => the numeric knob is not validated (default deploy needs no edit config).
    assert _load(monkeypatch, MAX_EDIT_STRING_CHARS="0").max_edit_string_chars == 0


# ===========================================================================
# MAX_EDIT_READ_CHARS
# ===========================================================================
def test_edit_read_chars_default(monkeypatch):
    assert _load(monkeypatch).max_edit_read_chars == 8000


def test_edit_read_chars_custom(monkeypatch, tmp_path):
    assert _load(monkeypatch, ENABLE_EDIT_TOOL="true", EDIT_WORKDIR=str(tmp_path), MAX_EDIT_READ_CHARS="123").max_edit_read_chars == 123


def test_edit_read_chars_zero_rejected_when_enabled(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="MAX_EDIT_READ_CHARS"):
        _load(monkeypatch, ENABLE_EDIT_TOOL="true", EDIT_WORKDIR=str(tmp_path), MAX_EDIT_READ_CHARS="0")


def test_edit_read_chars_zero_ignored_when_disabled(monkeypatch):
    assert _load(monkeypatch, MAX_EDIT_READ_CHARS="0").max_edit_read_chars == 0


# ===========================================================================
# EDIT_WORKDIR — required (an existing directory) when enabled
# ===========================================================================
def test_edit_workdir_default_none(monkeypatch):
    assert _load(monkeypatch).edit_workdir is None


def test_edit_workdir_blank_is_none_when_disabled(monkeypatch):
    # Off => no workdir required (default deploy needs no edit config); blank stays None.
    assert _load(monkeypatch, EDIT_WORKDIR="   ").edit_workdir is None


def test_edit_workdir_existing_dir_ok(monkeypatch, tmp_path):
    assert _load(monkeypatch, ENABLE_EDIT_TOOL="true", EDIT_WORKDIR=str(tmp_path)).edit_workdir == str(tmp_path)


def test_edit_workdir_missing_dir_rejected_when_enabled(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="EDIT_WORKDIR"):
        _load(monkeypatch, ENABLE_EDIT_TOOL="true", EDIT_WORKDIR=str(tmp_path / "missing"))


def test_edit_workdir_file_not_dir_rejected_when_enabled(monkeypatch, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(ConfigError, match="EDIT_WORKDIR"):
        _load(monkeypatch, ENABLE_EDIT_TOOL="true", EDIT_WORKDIR=str(f))


def test_edit_workdir_required_when_enabled(monkeypatch):
    # Enabled but no EDIT_WORKDIR => ConfigError (confinement is mandatory).
    with pytest.raises(ConfigError, match="EDIT_WORKDIR"):
        _load(monkeypatch, ENABLE_EDIT_TOOL="true")


def test_edit_workdir_missing_dir_ignored_when_disabled(monkeypatch, tmp_path):
    # Off => the root is not validated (a set-but-missing value is harmless).
    assert _load(monkeypatch, EDIT_WORKDIR=str(tmp_path / "missing")).edit_workdir == str(tmp_path / "missing")
