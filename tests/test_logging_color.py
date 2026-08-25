"""Console log colouring (LOG_COLOR) and its config normalisation.

Pure — no I/O, no LLM, no Telegram, no network. Verifies the ANSI-level
formatter, the ``auto``/``true``/``false`` resolution (including the terminal
auto-detection), the byte-for-byte plain path when colour is off, and the
``LOG_COLOR`` env normalisation in :mod:`..config`.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from fibrecase_agent_backend.config import ConfigError, _normalize_log_color
from fibrecase_agent_backend.logging_setup import (
    _ColorFormatter,
    _resolve_color,
    configure_logging,
)

_RESET = "\x1b[0m"
_ANSI = "\x1b["


@contextmanager
def _isolate_root_logger():
    """Run a block with the root logger's handlers/level snapshotted, then
    restored, so a global :func:`configure_logging` (which ``dictConfig``s the
    root logger) can't leak into the ``caplog``-based tests that run later.
    """
    root = logging.root
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.level = saved_level


def _fmt(level: int, name: str, msg: str) -> str:
    rec = logging.LogRecord(name, level, "test", 1, msg, None, None)
    return _ColorFormatter("%(levelname)s [%(name)s] %(message)s", None, use_color=True).format(rec)


# ---------------------------------------------------------------------------
# _resolve_color
# ---------------------------------------------------------------------------
def test_resolve_color_bool_passthrough():
    assert _resolve_color(True) is True
    assert _resolve_color(False) is False


def test_resolve_color_explicit_strings():
    assert _resolve_color("true") is True
    assert _resolve_color("TRUE") is True  # case-insensitive
    assert _resolve_color("on") is True
    assert _resolve_color("false") is False
    assert _resolve_color("off") is False
    assert _resolve_color("0") is False


def test_resolve_color_auto_follows_isatty(monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert _resolve_color("auto") is True
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert _resolve_color("auto") is False
    # Empty / None / unknown fall back to "auto" (isatty-driven).
    assert _resolve_color("") is False
    assert _resolve_color(None) is False


# ---------------------------------------------------------------------------
# The ANSI formatter
# ---------------------------------------------------------------------------
def test_formatter_colors_each_level():
    assert f"\x1b[32mINFO{_RESET}" in _fmt(logging.INFO, "telegram", "hello")
    assert f"\x1b[33mWARNING{_RESET}" in _fmt(logging.WARNING, "telegram", "careful")
    assert f"\x1b[31mERROR{_RESET}" in _fmt(logging.ERROR, "tools", "boom")
    assert f"\x1b[41;97mCRITICAL{_RESET}" in _fmt(logging.CRITICAL, "x", "dead")


def test_formatter_message_and_name_are_untouched():
    out = _fmt(logging.INFO, "telegram", "hello world")
    # Colour wraps only the level tag; the name and message stay plain.
    assert "[telegram]" in out
    assert "hello world" in out
    # Exactly one opening colour code (the level) and its reset.
    assert out.count(_ANSI) == 2  # the color + the reset


def test_formatter_does_not_mutate_record():
    rec = logging.LogRecord("telegram", logging.INFO, "test", 1, "hi", None, None)
    f = _ColorFormatter("%(levelname)s", None, use_color=True)
    f.format(rec)
    assert rec.levelname == "INFO", "record must be restored to its plain level name"
    # Formatting twice yields the identical string (no residual colour).
    assert f.format(rec) == f.format(rec)


def test_formatter_plain_when_disabled():
    rec = logging.LogRecord("telegram", logging.WARNING, "test", 1, "careful", None, None)
    out = _ColorFormatter("%(levelname)s [%(name)s] %(message)s", None, use_color=False).format(rec)
    assert _ANSI not in out
    assert out == "WARNING [telegram] careful"


# ---------------------------------------------------------------------------
# configure_logging installs the right formatter on the console handler
# ---------------------------------------------------------------------------
def test_configure_logging_color_on(monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)  # auto -> on
    with _isolate_root_logger():
        configure_logging("INFO", color="auto")
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, _ColorFormatter)
        assert handler.formatter.use_color is True


def test_configure_logging_color_off_is_byte_for_byte_plain():
    with _isolate_root_logger():
        configure_logging("INFO", color="false")
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, _ColorFormatter)
        assert handler.formatter.use_color is False
        rec = logging.LogRecord("x", logging.ERROR, "t", 1, "boom", None, None)
        assert _ANSI not in handler.formatter.format(rec)


# ---------------------------------------------------------------------------
# LOG_COLOR config normalisation
# ---------------------------------------------------------------------------
def test_normalize_log_color():
    assert _normalize_log_color("") == "auto"  # default
    assert _normalize_log_color("auto") == "auto"
    assert _normalize_log_color("tty") == "auto"
    assert _normalize_log_color("true") == "true"
    assert _normalize_log_color("1") == "true"
    assert _normalize_log_color("on") == "true"
    assert _normalize_log_color("TRUE") == "true"  # case-insensitive
    assert _normalize_log_color("false") == "false"
    assert _normalize_log_color("0") == "false"
    assert _normalize_log_color("off") == "false"
    with pytest.raises(ConfigError):
        _normalize_log_color("sometimes")
