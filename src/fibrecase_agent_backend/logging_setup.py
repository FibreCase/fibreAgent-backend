"""Logging configuration.

A single, consistent format used across all components:

    2026-08-24 12:00:00,123 [INFO] [telegram] received message ...

We deliberately do NOT log secrets, full user messages, or model responses —
only identifiers, lengths, and latencies.

The level tag is colourised **only when it is safe to do so**: by default
(``color="auto"``) ANSI codes are emitted when stdout is a terminal and
suppressed when the output is piped or redirected, so captured logs (files,
``docker logs``) never carry raw escape sequences. ``color`` is a tri-state —
``"auto"``, ``"true"``, or ``"false"`` (case-insensitive) — matching the
``LOG_COLOR`` env var.
"""

from __future__ import annotations

import logging
import logging.config
import sys

LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S,%f"
DATE_FORMAT = DATE_FORMAT[:-3]  # millisecond precision, not microseconds

# ANSI colours per level (each tag is terminated by the reset \x1b[0m).
_COLORS = {
    "DEBUG": "\x1b[36m",  # cyan
    "INFO": "\x1b[32m",  # green
    "WARNING": "\x1b[33m",  # yellow
    "ERROR": "\x1b[31m",  # red
    "CRITICAL": "\x1b[41;97m",  # white on red
}


def _resolve_color(color: str | bool = "auto") -> bool:
    """Decide whether to emit ANSI colour for the console handler.

    ``"auto"`` (the default) colours only when stdout is a terminal, so piped
    and redirected output stays plain. ``True``/``"true"`` force colour on;
    ``False``/``"false"`` force it off. Unknown strings fall back to ``"auto"``.
    """
    if isinstance(color, bool):
        return color
    value = (color or "auto").strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    return sys.stdout.isatty()


class _ColorFormatter(logging.Formatter):
    """The standard format, with the level tag wrapped in an ANSI colour.

    When ``use_color`` is false this is byte-for-byte the plain formatter, so
    the same handler config works whether or not colour is wanted.
    """

    def __init__(self, format: str | None = None, datefmt: str | None = None, use_color: bool = True) -> None:
        super().__init__(format, datefmt)
        self.use_color = bool(use_color)

    def format(self, record: logging.LogRecord) -> str:
        if not self.use_color:
            return super().format(record)
        original = record.levelname
        color = _COLORS.get(record.levelname)
        if color:
            record.levelname = f"{color}{record.levelname}\x1b[0m"
        try:
            return super().format(record)
        finally:
            # Restore the plain level name so the record is safe to reuse.
            record.levelname = original


def configure_logging(level: str = "INFO", color: str | bool = "auto") -> None:
    """Configure the root logger once, idempotently."""
    resolved_level = level.upper() if isinstance(level, str) else logging.INFO
    use_color = _resolve_color(color)
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                # The custom formatter is referenced by importable path so
                # dictConfig can build it; use_color is baked in up front.
                "default": {
                    "()": "fibrecase_agent_backend.logging_setup._ColorFormatter",
                    "format": LOG_FORMAT,
                    "datefmt": DATE_FORMAT,
                    "use_color": use_color,
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "default",
                },
            },
            "root": {"level": resolved_level, "handlers": ["console"]},
            # Third-party libraries are usually noisy at DEBUG/INFO.
            "loggers": {
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
                "httpx2": {"level": "WARNING"},
                "openai": {"level": "WARNING"},
                "telegram": {"level": "WARNING"},
            },
        }
    )
