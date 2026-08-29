"""Phase 9 — the background cron :class:`Scheduler` loop (required #4, #10).

The scheduler is a single background ``asyncio.Task`` that only (1) watches an
injected clock and (2) fires an injected ``runner`` coroutine when a schedule is
due. Nothing here touches Telegram, the LLM, or the DB — the runner is a fake.
The clock (``now_fn``) and the sleep are injected so every test is deterministic:
the :class:`_Harness` parks the loop in its injected sleep, and the test advances
the clock + releases the sleep to drive exactly one tick at a time. The invariants
pinned:

* on-time fire; several schedules due in the same minute all fire;
* per-task single-flight (a previous in-flight run makes the next due tick skip);
* fault isolation (one runner's exception is logged by *name* + exception *class*
  only — never the exception text — and never stops the loop or the other tasks);
* a ``next_fire is None`` (calendar-impossible) schedule is safe-skipped;
* **no catch-up** — a (re)start recomputes every next fire from *now*, so a cron
  that would have fired while "down" is not replayed;
* ``start``/``stop`` are idempotent and ``stop`` leaves no dangling task.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from fibrecase_agent_backend.automation.scheduler import Scheduler
from fibrecase_agent_backend.config import ScheduleSpec

TZ = ZoneInfo("UTC")


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def _spec(name: str, cron: str) -> ScheduleSpec:
    return ScheduleSpec(name=name, cron=cron, chat_id=1, user_id=1, prompt=f"prompt-{name}")


class _Recorder:
    """A fake runner: records who it started + finished for, optionally holding a
    task in flight (``gate``), and optionally raising for a named schedule."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.finished: list[str] = []
        self.gate: asyncio.Event | None = None  # when set, a fired run waits here
        self.raise_on: dict[str, BaseException] = {}

    async def run(self, spec) -> None:
        self.started.append(spec.name)
        if spec.name in self.raise_on:
            raise self.raise_on[spec.name]
        if self.gate is not None:
            await self.gate.wait()
        self.finished.append(spec.name)


class _Harness:
    """Injectable clock + gated sleep that lets a test drive one tick at a time.

    ``start()`` runs the scheduler's loop, which ticks then parks in
    ``sleep_fn``. ``tick_to(dt)`` waits until the loop is parked, sets the clock
    to ``dt``, and releases the sleep so exactly one more tick runs.
    """

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.last_delay: float | None = None
        self._release = asyncio.Event()
        self._in_sleep = asyncio.Event()

    def now_fn(self) -> datetime:
        return self.now

    async def sleep_fn(self, delay: float) -> None:
        self.last_delay = delay
        self._release.clear()
        self._in_sleep.set()
        await self._release.wait()

    async def tick_to(self, dt: datetime) -> None:
        await self._wait_parked()
        self.now = dt
        self._release.set()

    async def _wait_parked(self, timeout: float = 2.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._in_sleep.is_set() and not self._release.is_set():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("scheduler did not park in sleep in time")


async def _poll_until(predicate, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.005)
    return True


async def _make(schedules, start, *, recorder=None):
    h = _Harness(start)
    rec = recorder if recorder is not None else _Recorder()
    sched = Scheduler(schedules, TZ, rec.run, now_fn=h.now_fn, sleep_fn=h.sleep_fn)
    return sched, h, rec


# ===========================================================================
# on-time fire
# ===========================================================================
async def test_fires_on_time():
    sched, h, rec = await _make([_spec("daily", "0 7 * * *")], _t("2026-08-29T06:00:00"))
    sched.start()
    try:
        # Not due before 07:00 (06:00 < 07:00): the first tick does not fire.
        await h.tick_to(_t("2026-08-29T07:00:00"))
        assert await _poll_until(lambda: len(rec.started) == 1)
        assert rec.finished == ["daily"]
    finally:
        await sched.stop()


async def test_does_not_fire_before_due():
    # Advance to just before the fire; nothing fires.
    sched, h, rec = await _make([_spec("daily", "0 7 * * *")], _t("2026-08-29T06:00:00"))
    sched.start()
    try:
        await h.tick_to(_t("2026-08-29T06:59:00"))
        assert rec.started == []  # 06:59 < 07:00
    finally:
        await sched.stop()


# ===========================================================================
# several schedules due in the same minute all fire
# ===========================================================================
async def test_multiple_schedules_same_minute_all_fire():
    sched, h, rec = await _make(
        [_spec("a", "0 7 * * *"), _spec("b", "0 7 * * *")], _t("2026-08-29T06:00:00")
    )
    sched.start()
    try:
        await h.tick_to(_t("2026-08-29T07:00:00"))
        assert await _poll_until(lambda: len(rec.finished) == 2)
        assert sorted(rec.finished) == ["a", "b"]
    finally:
        await sched.stop()


# ===========================================================================
# per-task single-flight: an in-flight run makes the next due tick skip
# ===========================================================================
async def test_single_flight_skips_while_in_flight():
    rec = _Recorder()
    rec.gate = asyncio.Event()  # hold each fired run in flight until released
    sched, h, _ = await _make([_spec("m", "* * * * *")], _t("2026-08-29T00:00:00"), recorder=rec)
    sched.start()
    try:
        # 00:01 → due, fires; the run is held in flight by the gate.
        await h.tick_to(_t("2026-08-29T00:01:00"))
        assert await _poll_until(lambda: len(rec.started) == 1)
        # 00:02 → due again, but the previous run is still in flight → SKIP.
        await h.tick_to(_t("2026-08-29T00:02:00"))
        # A brief settle: the skip must NOT have started a second run.
        await asyncio.sleep(0.02)
        assert rec.started == ["m"]  # still exactly one
        # Release the first run; it finishes.
        rec.gate.set()
        assert await _poll_until(lambda: rec.finished == ["m"])
        # 00:03 → due, previous run now done → fires again.
        rec.gate = asyncio.Event()  # a fresh gate so this run is not held
        await h.tick_to(_t("2026-08-29T00:03:00"))
        assert await _poll_until(lambda: len(rec.started) == 2)
    finally:
        rec.gate.set()
        await sched.stop()


# ===========================================================================
# fault isolation: one runner's exception never stops the loop or others
# ===========================================================================
async def test_one_failing_schedule_does_not_affect_others_or_loop(caplog):
    rec = _Recorder()
    rec.raise_on = {"boom": RuntimeError("TOP-SECRET-DETAIL")}
    sched, h, _ = await _make(
        [_spec("boom", "0 7 * * *"), _spec("fine", "0 7 * * *")],
        _t("2026-08-29T06:00:00"),
        recorder=rec,
    )
    sched.start()
    try:
        with caplog.at_level("WARNING"):
            await h.tick_to(_t("2026-08-29T07:00:00"))
            assert await _poll_until(lambda: "fine" in rec.finished)
        # The healthy schedule finished; the failing one started but did not finish.
        assert "fine" in rec.finished
        assert "boom" in rec.started
        assert "boom" not in rec.finished
        # Fault isolation log: the *name* and exception *class* are recorded…
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "boom" in logged
        assert "RuntimeError" in logged
        # …but the exception *text* is never logged (privacy invariant).
        assert "TOP-SECRET-DETAIL" not in logged

        # The loop is still alive: advance to the next day and the healthy one fires again.
        await h.tick_to(_t("2026-08-30T07:00:00"))
        assert await _poll_until(lambda: rec.finished.count("fine") == 2)
    finally:
        await sched.stop()


async def test_failing_schedule_does_not_crash_loop_single_schedule(caplog):
    # Even with a *single* always-failing schedule, the loop survives and keeps ticking.
    rec = _Recorder()
    rec.raise_on = {"only": RuntimeError("x")}
    sched, h, _ = await _make([_spec("only", "0 7 * * *")], _t("2026-08-29T06:00:00"), recorder=rec)
    sched.start()
    try:
        with caplog.at_level("WARNING"):
            await h.tick_to(_t("2026-08-29T07:00:00"))
            await asyncio.sleep(0.02)
        assert rec.started == ["only"]  # it ran, raised, was swallowed
        assert rec.finished == []
        # Loop still alive: the next day fires (and fails) again.
        await h.tick_to(_t("2026-08-30T07:00:00"))
        await asyncio.sleep(0.02)
        assert rec.started == ["only", "only"]
    finally:
        await sched.stop()


# ===========================================================================
# a next_fire is None (calendar-impossible) schedule is safe-skipped
# ===========================================================================
async def test_calendar_impossible_is_safe_skipped(caplog):
    # "0 0 31 2 *" parses but can never fire → due stays None, never fires.
    sched, h, rec = await _make(
        [_spec("impossible", "0 0 31 2 *"), _spec("ok", "0 7 * * *")],
        _t("2026-08-29T06:00:00"),
    )
    sched.start()
    try:
        # start() should have logged that the impossible schedule will not run.
        assert any("impossible" in r.getMessage() for r in caplog.records)
        # The healthy one still fires; the impossible one never does.
        await h.tick_to(_t("2026-08-29T07:00:00"))
        assert await _poll_until(lambda: rec.finished == ["ok"])
        # Advance many days; the impossible one still never fires.
        await h.tick_to(_t("2026-09-15T07:00:00"))
        await asyncio.sleep(0.02)
        assert "impossible" not in rec.started
    finally:
        await sched.stop()


# ===========================================================================
# no catch-up: a (re)start recomputes next fire from now, not replayed
# ===========================================================================
async def test_no_catch_up_on_restart():
    sched, h, rec = await _make([_spec("daily", "0 7 * * *")], _t("2026-08-29T06:00:00"))
    sched.start()
    try:
        await h.tick_to(_t("2026-08-29T06:30:00"))
        assert rec.started == []  # nothing fired yet
    finally:
        await sched.stop()

    # The process was "down" across 07:00 on the 29th AND the 30th. On
    # restart at 08-31 08:00 the next fire is recomputed from *now* — the
    # missed fires are NOT replayed.
    h2 = _Harness(_t("2026-08-31T08:00:00"))
    rec2 = _Recorder()
    sched2 = Scheduler([_spec("daily", "0 7 * * *")], TZ, rec2.run, now_fn=h2.now_fn, sleep_fn=h2.sleep_fn)
    sched2.start()
    try:
        # Immediate tick at 08-31 08:00: next fire is 09-01 07:00 (not due yet).
        await h2.tick_to(_t("2026-08-31T08:00:00"))
        assert rec2.started == []  # no catch-up replay of the missed 07:00s
        # …and it does fire on time at the genuinely-next occurrence.
        await h2.tick_to(_t("2026-09-01T07:00:00"))
        assert await _poll_until(lambda: rec2.finished == ["daily"])
    finally:
        await sched2.stop()


# ===========================================================================
# lifecycle: idempotent start/stop, no dangling task after stop
# ===========================================================================
async def test_start_is_idempotent():
    sched, h, rec = await _make([_spec("a", "0 7 * * *")], _t("2026-08-29T06:00:00"))
    sched.start()
    task_before = sched._task
    try:
        sched.start()  # a second start must not spawn a second task
        assert sched._task is task_before
        assert sched.running is True
    finally:
        await sched.stop()


async def test_stop_is_idempotent_and_leaves_no_task():
    sched, h, rec = await _make([_spec("a", "0 7 * * *")], _t("2026-08-29T06:00:00"))
    sched.start()
    await sched.stop()
    assert sched.running is False
    assert sched._task is None
    # A second stop is a harmless no-op.
    await sched.stop()
    assert sched.running is False


async def test_not_running_before_start():
    sched, h, rec = await _make([_spec("a", "0 7 * * *")], _t("2026-08-29T06:00:00"))
    assert sched.running is False


async def test_empty_scheduler_is_harmless_noop():
    # A constructed-but-empty scheduler is a harmless no-op: it can start/stop
    # without firing anything (the composition root simply does not build one).
    sched, h, rec = await _make([], _t("2026-08-29T06:00:00"))
    sched.start()
    try:
        await h.tick_to(_t("2026-08-29T07:00:00"))
        await asyncio.sleep(0.02)
        assert rec.started == []
    finally:
        await sched.stop()


async def test_stop_drains_an_in_flight_run_and_leaves_no_task():
    # A run in flight at stop time is given a bounded chance to finish during the
    # drain; the stop must return (not hang), the run is allowed to complete, and
    # no dangling task is left. We release the gate *during* the drain so the run
    # finishes and the drain returns promptly rather than waiting out the 30s cap.
    rec = _Recorder()
    rec.gate = asyncio.Event()
    sched, h, _ = await _make([_spec("m", "* * * * *")], _t("2026-08-29T00:00:00"), recorder=rec)
    sched.start()
    try:
        await h.tick_to(_t("2026-08-29T00:01:00"))
        assert await _poll_until(lambda: len(rec.started) == 1)
        # The run is in flight (held by the gate). Release it shortly so the
        # drain sees it finish and returns promptly.
        asyncio.get_running_loop().call_later(0.05, rec.gate.set)

        import time as _time

        t0 = _time.monotonic()
        await sched.stop()
        elapsed = _time.monotonic() - t0
        assert rec.finished == ["m"]  # the run completed during the drain
        assert sched.running is False
        assert sched._task is None  # no dangling task
        assert elapsed < 5.0  # did not wait out the 30s drain cap
    finally:
        rec.gate.set()
        # Ensure a fully-settled shutdown either way.
        await sched.stop()


async def test_stop_lets_a_quick_run_finish_and_clears_venue():
    # A run that finishes quickly on its own is *allowed* to complete during the
    # bounded drain (so its cleanup runs) rather than being cancelled mid-way.
    async def runner(spec):
        rec.started.append(spec.name)
        await asyncio.sleep(0.02)
        rec.finished.append(spec.name)

    h = _Harness(_t("2026-08-29T00:00:00"))
    rec = _Recorder()
    sched = Scheduler([_spec("q", "* * * * *")], TZ, runner, now_fn=h.now_fn, sleep_fn=h.sleep_fn)
    sched.start()
    try:
        await h.tick_to(_t("2026-08-29T00:01:00"))
        assert await _poll_until(lambda: len(rec.started) == 1)
        # Stop immediately while the quick run is in flight; it should finish.
        await sched.stop()
        assert rec.finished == ["q"]
    finally:
        await sched.stop()
