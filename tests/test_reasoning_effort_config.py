"""Reasoning-effort knob — the ``REASONING_EFFORT`` config.

Local only: no subprocess, no network. Verifies that ``REASONING_EFFORT``
(default **low**) is parsed correctly and fails fast on a bad value, like every
other enum-ish knob (see the ``LOG_COLOR`` tests).
"""

from __future__ import annotations

import pytest

from fibrecase_agent_backend.config import (
    ConfigError,
    _normalize_reasoning_effort,
    load_config,
)

_KNOBS = (
    "REASONING_EFFORT",
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


def test_reasoning_effort_default_low(monkeypatch):
    assert _load(monkeypatch).reasoning_effort == "low"


def test_reasoning_effort_explicit_low(monkeypatch):
    assert _load(monkeypatch, REASONING_EFFORT="low").reasoning_effort == "low"


def test_reasoning_effort_explicit_medium(monkeypatch):
    assert _load(monkeypatch, REASONING_EFFORT="medium").reasoning_effort == "medium"


def test_reasoning_effort_explicit_high(monkeypatch):
    assert _load(monkeypatch, REASONING_EFFORT="high").reasoning_effort == "high"


def test_reasoning_effort_explicit_xhigh(monkeypatch):
    assert _load(monkeypatch, REASONING_EFFORT="xhigh").reasoning_effort == "xhigh"


def test_reasoning_effort_case_insensitive(monkeypatch):
    assert _load(monkeypatch, REASONING_EFFORT="HIGH").reasoning_effort == "high"


def test_reasoning_effort_bad_value_fails_fast(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, REASONING_EFFORT="extreme")


# ---------------------------------------------------------------------------
# normalizer unit tests (mirror the LOG_COLOR normalizer tests)
# ---------------------------------------------------------------------------
def test_normalizer_empty_returns_default():
    assert _normalize_reasoning_effort("") == "low"
    assert _normalize_reasoning_effort("   ") == "low"


def test_normalizer_each_allowed_value_maps_to_itself():
    for value in ("low", "medium", "high", "xhigh"):
        assert _normalize_reasoning_effort(value) == value


def test_normalizer_is_case_insensitive():
    assert _normalize_reasoning_effort("Medium") == "medium"


def test_normalizer_unknown_value_fails_fast():
    with pytest.raises(ConfigError):
        _normalize_reasoning_effort("sometimes")
