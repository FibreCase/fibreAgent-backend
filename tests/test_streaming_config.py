"""Streaming replies — the ``ENABLE_STREAMING`` config knob.

Local only: no subprocess, no network. Verifies that ``ENABLE_STREAMING``
(default **on**) is parsed correctly and fails fast on a bad value, like every
other boolean knob. Streaming only affects *private* chats (group/channel chats
degrade in the adapter), so the knob itself needs no cross-validation.
"""

from __future__ import annotations

import pytest

from fibrecase_agent_backend.config import ConfigError, load_config

_KNOBS = (
    "ENABLE_STREAMING",
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


def test_streaming_enabled_by_default(monkeypatch):
    assert _load(monkeypatch).enable_streaming is True


def test_streaming_explicit_on(monkeypatch):
    assert _load(monkeypatch, ENABLE_STREAMING="true").enable_streaming is True


def test_streaming_explicit_off(monkeypatch):
    assert _load(monkeypatch, ENABLE_STREAMING="false").enable_streaming is False


def test_streaming_case_insensitive(monkeypatch):
    assert _load(monkeypatch, ENABLE_STREAMING="On").enable_streaming is True


def test_streaming_bad_value_fails_fast(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, ENABLE_STREAMING="not-a-bool")
