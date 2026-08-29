"""Automation — time-triggered scheduling (phase 9 first slice).

This package is **pure** with respect to the rest of the system: neither module
imports Telegram, the OpenAI SDK, the ORM, or the Agent service. It provides

* :mod:`.cron` — a strict 5-field cron parser + bounded :meth:`CronSpec.next_fire`
  (shared by :mod:`..config` at startup and the scheduler at runtime), and
* :mod:`.scheduler` — the background :class:`Scheduler` that only watches the
  clock and fires an injected ``runner`` coroutine.

All Telegram knowledge (the dedicated fresh conversation, the formatted
notification, the ``delivery_chat_id`` approval routing) lives in the
composition root (:mod:`..main`), keeping this package as a leaf dependency —
the same layering rule as :mod:`..attachments` / :mod:`..memory` / :mod:`..mcp`.
"""

from .cron import CronError, CronSpec, parse_cron
from .scheduler import Scheduler

__all__ = ["CronError", "CronSpec", "parse_cron", "Scheduler"]
