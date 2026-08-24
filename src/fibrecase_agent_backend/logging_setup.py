"""Logging configuration.

A single, consistent format used across all components:

    2026-08-24 12:00:00,123 [INFO] [telegram] received message ...

We deliberately do NOT log secrets, full user messages, or model responses —
only identifiers, lengths, and latencies.
"""

from __future__ import annotations

import logging
import logging.config

LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S,%f"
DATE_FORMAT = DATE_FORMAT[:-3]  # millisecond precision, not microseconds


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger once, idempotently."""
    resolved = level.upper() if isinstance(level, str) else logging.INFO
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {"format": LOG_FORMAT, "datefmt": DATE_FORMAT},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "default",
                },
            },
            "root": {"level": resolved, "handlers": ["console"]},
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
