"""Phase 9 — the composition-root wiring of the scheduler (required #1).

This pins the *build-time* half of the empty-``SCHEDULES`` case that neither the
pure scheduler tests nor the runner tests can: with no schedules configured,
``AgentBackend`` does **not** construct a :class:`Scheduler` at all — so there is
no background task to start, the ``_run_schedule`` runner is never wired or
called, and the startup sweep is a no-op (0). With schedules configured, a
``Scheduler`` exists and is wired to the bound ``_run_schedule`` runner and the
``SCHEDULE_TIMEZONE`` wall clock.

The backend is constructed for real (in-memory DB, fake LLM credentials) but the
PTB application's polling is never started, so nothing touches the network.
"""

from __future__ import annotations

import pytest

from fibrecase_agent_backend.config import load_config
from fibrecase_agent_backend.main import AgentBackend


def _base_env(**extra) -> dict[str, str]:
    base = {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_ALLOWED_USER_IDS": "1",
        "OPENAI_BASE_URL": "https://h/v1",
        "OPENAI_API_KEY": "k",
        "OPENAI_MODEL": "m",
        "DATABASE_URL": "sqlite+aiosqlite://",
    }
    base.update(extra)
    return base


def _build(monkeypatch, **extra) -> AgentBackend:
    for knob in ("SCHEDULES", "SCHEDULES_FILE", "SCHEDULE_TIMEZONE"):
        monkeypatch.delenv(knob, raising=False)
    for k, v in _base_env(**extra).items():
        monkeypatch.setenv(k, v)
    return AgentBackend(load_config())


async def test_empty_schedules_builds_no_scheduler(monkeypatch):
    backend = _build(monkeypatch)
    try:
        # No schedules → no scheduler object at all: no background task is ever
        # created or started, so the runner is never wired or called.
        assert backend.scheduler is None
    finally:
        await backend.engine.dispose()


async def test_empty_schedules_sweep_is_noop(monkeypatch):
    # The startup sweep removes reserved-range venues; with an empty DB (and an
    # empty SCHEDULES there are none) it returns 0 — a harmless no-op.
    backend = _build(monkeypatch)
    try:
        from fibrecase_agent_backend.database.session import init_db

        await init_db(backend.engine)  # _post_init does this before the sweep
        assert await backend.repository.clear_ephemeral_conversations() == 0
    finally:
        await backend.engine.dispose()


async def test_nonempty_schedules_wires_runner_and_tz(monkeypatch):
    import json
    from zoneinfo import ZoneInfo

    schedules = json.dumps(
        [{"name": "nightly", "cron": "0 7 * * *", "chat_id": 1, "user_id": 1, "prompt": "p"}]
    )
    backend = _build(monkeypatch, SCHEDULES=schedules, SCHEDULE_TIMEZONE="Asia/Shanghai")
    try:
        sched = backend.scheduler
        assert sched is not None
        # The runner is the *bound* ``_run_schedule`` (the Telegram runner):
        # a bound method's ``__self__`` is the backend and ``__name__`` is the
        # method (``is`` on bound methods never holds — a fresh wrapper each access).
        assert sched._runner.__self__ is backend
        assert sched._runner.__name__ == "_run_schedule"
        # One entry per configured schedule.
        assert len(sched._entries) == 1
        assert sched._entries[0].name == "nightly"
        # The wall clock is evaluated in the configured SCHEDULE_TIMEZONE.
        assert sched._tz is ZoneInfo("Asia/Shanghai")
        # Not started yet (start happens in _post_init), so no task exists.
        assert sched.running is False and sched._task is None
    finally:
        await backend.engine.dispose()


async def test_unparseable_schedule_is_rejected_at_config(monkeypatch):
    # A malformed SCHEDULES is a ConfigError at load (strict, fail-fast) — the
    # backend is never built from a bad schedule set.
    import json
    from fibrecase_agent_backend.config import ConfigError

    monkeypatch.setenv("SCHEDULES", json.dumps([{"name": "bad", "cron": "* * *", "chat_id": 1, "user_id": 1, "prompt": "p"}]))
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError):
        load_config()
