"""The background cron scheduler (phase 9 — Automation).

A single :class:`Scheduler` runs as one ``asyncio.Task`` on the PTB event loop.
It is deliberately **channel- and service-agnostic**: it imports nothing from
Telegram, the OpenAI SDK, the ORM, or the Agent service. It only knows how to
(1) watch the wall clock and (2) fire an injected ``runner`` coroutine when a
configured schedule is due. The Telegram-specific runner (dedicated fresh
conversation → ``process_message`` → formatted notification → cleanup) is
provided by the composition root (:mod:`..main`).

Behavioural invariants (see ``TASK.md`` §4.2; the unit tests in
``tests/test_scheduler.py`` pin each one):

* **Single task, owned lifecycle.** Started in ``post_init`` (after ``init_db``
  and MCP discovery), stopped in ``post_shutdown`` (after the approval broker
  has drained its pending approvals, before the LLM/DB close). :meth:`start` /
  :meth:`stop` are **idempotent** and **never raise** — a broken scheduler must
  never take the bot down (mirrors the OAuth callback server).
* **No catch-up.** Every schedule's next fire is (re)computed from *now* at
  start and after each fire — a fire missed while the process was down is
  never replayed.
* **Per-task single-flight.** If a schedule's previous run is still in flight
  (e.g. awaiting an approval), its next due tick is *skipped* (safe log) and the
  due time advanced, so a stuck LLM turn never builds a queue.
* **Fault isolation.** Each run is wrapped: one schedule's runner exception is
  logged by *name* + a stable category and never propagates to the other
  schedules or the loop.
* **Injectable clock & sleep.** ``now_fn`` and the sleep awaitable are injected
  so every test is deterministic (no real sleeping).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .cron import CronSpec, parse_cron

__all__ = ["Scheduler"]

logger = logging.getLogger(__name__)

# Cap each sleep so stop() latency stays bounded and the loop periodically
# re-evaluates (rather than sleeping minutes ahead and drifting on a clock
# change or a missed wake).
_MAX_SLEEP_SECONDS = 30.0


class _Entry:
    """One configured schedule's runtime state (parsed cron + next due + flight)."""

    __slots__ = ("spec", "name", "cron", "due", "flight")

    def __init__(self, spec, cron: CronSpec) -> None:
        self.spec = spec
        self.name = spec.name
        self.cron = cron
        self.due: datetime | None = None  # None == calendar-impossible, never fires
        self.flight: asyncio.Task | None = None


class Scheduler:
    """Watches the clock and fires an injected ``runner`` per due schedule.

    Parameters
    ----------
    schedules:
        Iterable of objects exposing ``.name`` (str) and ``.cron`` (str) — i.e.
        :class:`..config.ScheduleSpec`. Empty means "no automation"; the
        composition root simply does not construct a ``Scheduler`` in that case,
        but a constructed-but-empty scheduler is also a harmless no-op.
    tz:
        The :class:`zoneinfo.ZoneInfo` the cron wall clock is evaluated in.
    runner:
        ``async def runner(spec) -> None`` — invoked (as a fire-and-forget task)
        when ``spec`` is due. The composition root supplies the Telegram runner.
    now_fn:
        ``() -> datetime`` (tz-aware) — injectable clock. Defaults to
        ``datetime.now(tz)``.
    sleep_fn:
        ``async def sleep_fn(seconds) -> None`` — injectable sleep. Defaults to
        :func:`asyncio.sleep`. Tests supply a controllable sleep so the loop
        never actually waits.
    """

    def __init__(self, schedules, tz, runner, *, now_fn=None, sleep_fn=None) -> None:
        self._tz = tz
        self._runner = runner
        self._now_fn = now_fn if now_fn is not None else (lambda: datetime.now(tz))
        self._sleep_fn = sleep_fn if sleep_fn is not None else asyncio.sleep
        self._entries: list[_Entry] = []
        for spec in schedules:
            self._entries.append(_Entry(spec, parse_cron(spec.cron)))
        self._task: asyncio.Task | None = None
        self._flights: set[asyncio.Task] = set()

    # -- lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the background task (idempotent, never raises).

        Recomputes every schedule's next fire from *now* — the no-catch-up rule.
        """
        if self._task is not None and not self._task.done():
            return  # already running
        now = self._now_fn()
        for entry in self._entries:
            entry.due = entry.cron.next_fire(now, self._tz)
            if entry.due is None:
                logger.warning(
                    "scheduler: schedule %s has no fire time in the search window; it will not run",
                    entry.name,
                )
        self._task = asyncio.get_running_loop().create_task(self._run(), name="agent-scheduler")

    async def stop(self) -> None:
        """Cancel the loop and let in-flight runs settle (idempotent, never raises).

        The loop task is cancelled first; any schedule runs still in flight are
        then given a bounded chance to finish so a stop never abandons a turn in
        the middle of an LLM call or a DB write.
        """
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive; loop never raises
                pass

        # Let in-flight runs settle (bounded) so we never close the LLM/DB out
        # from under an executing turn; any that have not finished by the
        # deadline (a genuinely stuck turn) are *cancelled* so shutdown never
        # hangs — the runner's ``finally`` still unwinds its venue.
        flights = list(self._flights)
        if flights:
            try:
                _done, pending = await asyncio.wait(flights, timeout=30.0)
            except Exception:  # pragma: no cover - defensive
                pending = set(flights)
            if pending:
                for t in pending:
                    t.cancel()
                try:
                    await asyncio.wait(list(pending), timeout=5.0)
                except Exception:  # pragma: no cover - defensive
                    pass

    # -- loop ---------------------------------------------------------------

    async def _run(self) -> None:
        """The single background loop: evaluate dues, fire, sleep to next due."""
        try:
            while True:
                delay = await self._tick()
                await self._sleep_fn(delay)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive; a loop bug must not crash the bot
            logger.exception("scheduler: loop crashed")

    async def _tick(self) -> float:
        """One evaluation pass. Returns the seconds to sleep before the next pass."""
        now = self._now_fn()
        for entry in self._entries:
            if entry.due is None:
                continue  # calendar-impossible; never fires
            if entry.due > now:
                continue  # not due yet
            if entry.flight is not None and not entry.flight.done():
                # Single-flight: a previous run is still going; skip this tick.
                logger.warning(
                    "scheduler: schedule %s tick skipped (previous run still in flight)",
                    entry.name,
                )
            else:
                flight = asyncio.get_running_loop().create_task(
                    self._guarded_run(entry), name=f"schedule-run-{entry.name}"
                )
                entry.flight = flight
                self._flights.add(flight)
                flight.add_done_callback(self._flights.discard)
            # Advance the due time from *now* (no catch-up, no re-fire next pass).
            entry.due = entry.cron.next_fire(now, self._tz)

        pending = [e.due for e in self._entries if e.due is not None]
        if not pending:
            # Nothing triggerable at all — idle on the cap (keeps re-eval cheap).
            return _MAX_SLEEP_SECONDS
        next_due = min(pending)
        delay = (next_due - now).total_seconds()
        if delay < 0:  # defensive: clock moved backwards
            delay = 0.0
        return min(delay, _MAX_SLEEP_SECONDS)

    async def _guarded_run(self, entry: _Entry) -> None:
        """Invoke the runner for ``entry`` with fault isolation (never raises)."""
        try:
            await self._runner(entry.spec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Fault isolation: one schedule's failure must not reach the loop or
            # the other schedules. Log by name + exception *class* only — never
            # the exception text (which could carry a prompt / reply body).
            logger.warning(
                "scheduler: run for schedule %s failed (%s)",
                entry.name,
                type(exc).__name__,
            )
