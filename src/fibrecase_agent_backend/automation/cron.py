"""The pure-Python strict 5-field cron parser (phase 9 — Automation).

Zero-dependency and pure: this module is shared by :mod:`..config` (which uses
:func:`parse_cron` to fail-fast-validate an operator's ``SCHEDULES`` at startup)
and the :class:`..automation.scheduler.Scheduler` (which uses
:meth:`CronSpec.next_fire` to compute each schedule's next fire time). It imports
nothing from Telegram, the OpenAI SDK, the ORM, or the Agent service.

Expression grammar (standard 5 fields — ``minute hour day-of-month month
day-of-week``):

* ``minute`` 0-59, ``hour`` 0-23, ``day-of-month`` 1-31, ``month`` 1-12 (or
  ``JAN``-``DEC``), ``day-of-week`` 0-7 (``0`` **and** ``7`` are both Sunday;
  ``MON``-``SAT`` by name, case-insensitive).
* Each field is ``*`` | a single value | ``a-b`` (range) | ``*/n`` (step) |
  ``a-b/n`` (range step) | a comma-separated list of any of the above.

Explicitly **rejected** (as :class:`CronError`, which :mod:`..config` turns into a
startup ``ConfigError``): the wrong number of fields (4, or 6 with seconds),
``?`` (Quartz), ``@``-shorthand (``@daily``/``@hourly``/…), an inverted ``a > b``
range, an out-of-bounds value, an empty field / list element, and any unknown
token.

Day-of-month / day-of-week use **Vixie cron OR semantics** (the part most likely
to get wrong): when *both* fields are restricted (neither is ``*``) the schedule
fires when *either* matches; when exactly one is ``*`` the restricted one
dominates; when both are ``*`` the day always matches.

:func:`parse_cron` validates **syntax only** — it does not decide whether a
calendar-impossible expression (e.g. ``0 0 31 2 *``) can ever fire. That is left
to :meth:`CronSpec.next_fire`, which searches a *bounded* window and returns
``None`` when no fire is found in it (never an infinite loop).

All time handling is on *wall-clock* values in a caller-supplied IANA timezone
(``zoneinfo``); tests inject an explicit tz (e.g. ``UTC`` / ``Asia/Shanghai``)
for determinism, while a real deployment uses ``SCHEDULE_TIMEZONE`` (or the
process-local tz).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo  # noqa: F401  (re-exported for the composition root)

__all__ = ["CronError", "CronSpec", "parse_cron"]

# How many days :meth:`CronSpec.next_fire` will look forward before giving up
# (returning ``None``). A little over 5 years comfortably exceeds the longest
# possible gap to a valid fire (a Feb-29 cron is at most ~4 years away) while
# keeping the search cheap; a calendar-impossible expression simply exhausts it.
_MAX_SEARCH_DAYS = 1830

# 3-letter (uppercase) month / weekday names, case-insensitive at parse time.
# Sunday is ``0`` here (``7`` is normalised to ``0`` in :func:`parse_cron`).
_MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_DOW_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}

# A bare (unsigned) decimal value token.
_NUM = re.compile(r"[0-9]+\Z")


class CronError(ValueError):
    """Raised when a cron expression is syntactically invalid.

    :mod:`..config` catches this at startup and re-raises a ``ConfigError`` (a
    bad cron is a *startup* failure, never a silent "never fires"). The message
    names the field and the offending token; it never carries any other schedule
    field (the prompt, a chat id, a user id).
    """


@dataclass(frozen=True)
class CronSpec:
    """A parsed cron expression, kept as per-field integer sets.

    ``dom_star`` / ``dow_star`` record whether that field was a literal ``*``
    (unrestricted) — the two flags that drive the Vixie day-matching rule (see
    :meth:`_day_matches`). Everything else is a plain set of matching values.
    """

    minutes: frozenset
    hours: frozenset
    days_of_month: frozenset
    months: frozenset
    days_of_week: frozenset
    dom_star: bool
    dow_star: bool

    def _day_matches(self, day: date) -> bool:
        """Whether ``day`` satisfies the day-of-month / day-of-week rule.

        Vixie cron: both ``*`` → always; exactly one ``*`` → the restricted one
        must match; both restricted → **either** matching fires the schedule.
        """
        if self.dom_star and self.dow_star:
            return True
        dom_ok = day.day in self.days_of_month
        # Python: Monday=0..Sunday=6. Cron: Sunday=0, Monday=1..Saturday=6.
        cron_dow = (day.weekday() + 1) % 7
        dow_ok = cron_dow in self.days_of_week
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok

    def next_fire(self, after: datetime, tz) -> datetime | None:
        """The next fire time **strictly after** ``after``, as an aware datetime in ``tz``.

        ``after`` must be tz-aware. The search walks *wall-clock* days from the
        day of ``after`` (in ``tz``), and within a day the smallest matching
        hour/minute that is still after ``after``. It is **bounded** by
        :data:`_MAX_SEARCH_DAYS`; an expression with no fire in that window
        (e.g. ``0 0 31 2 *``) returns ``None`` rather than looping forever.
        """
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        local = after.astimezone(tz)
        start_day = local.date()
        for offset in range(_MAX_SEARCH_DAYS):
            day = start_day + timedelta(days=offset)
            if day.month not in self.months:
                continue
            if not self._day_matches(day):
                continue
            for h in hours:
                if offset == 0 and h < local.hour:
                    continue
                for mi in minutes:
                    if offset == 0 and h == local.hour and mi <= local.minute:
                        continue
                    cand = datetime(day.year, day.month, day.day, h, mi, tzinfo=tz)
                    if cand > after:  # strict; the guard also absorbs DST gaps
                        return cand
        return None


def _resolve_token(tok: str, lo: int, hi: int, names: dict | None, what: str) -> int:
    """Resolve one value token (a number or a name) to an in-range int."""
    tok = tok.strip()
    if not tok:
        raise CronError(f"{what}: empty value")
    if names and tok.upper() in names:
        return names[tok.upper()]
    if not _NUM.fullmatch(tok):
        raise CronError(f"{what}: invalid value {tok!r}")
    value = int(tok)
    if value < lo or value > hi:
        raise CronError(f"{what}: value {value} out of range {lo}-{hi}")
    return value


def _expand_part(part: str, lo: int, hi: int, names: dict | None, what: str) -> frozenset:
    """Expand one comma-list element (``*`` / ``a`` / ``a-b`` / with optional ``/n``)."""
    step = 1
    base = part
    if "/" in part:
        base, _sep, step_s = part.partition("/")
        if not _NUM.fullmatch(step_s):
            raise CronError(f"{what}: invalid step in {part!r}")
        step = int(step_s)
        if step < 1:
            raise CronError(f"{what}: step must be >= 1 in {part!r}")

    if base == "*":
        start, end = lo, hi
    elif "-" in base:
        a, _sep, b = base.partition("-")
        if not a or not b or "-" in b:
            raise CronError(f"{what}: invalid range {base!r}")
        start = _resolve_token(a, lo, hi, names, what)
        end = _resolve_token(b, lo, hi, names, what)
        if start > end:
            raise CronError(f"{what}: inverted range {base!r}")
    else:
        if "/" in part:
            raise CronError(f"{what}: step not allowed on a single value {part!r}")
        return frozenset({_resolve_token(base, lo, hi, names, what)})

    return frozenset(range(start, end + 1, step))


def _parse_field_values(field: str, lo: int, hi: int, names: dict | None, what: str) -> frozenset:
    """Parse one field into the frozenset of its matching integer values."""
    if not field:
        raise CronError(f"{what}: empty field")
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{what}: empty list element")
        values |= _expand_part(part, lo, hi, names, what)
    if not values:
        raise CronError(f"{what}: field matches no values")
    return frozenset(values)


def parse_cron(expr: str) -> CronSpec:
    """Parse + strictly validate a 5-field cron expression into a :class:`CronSpec`.

    Raises :class:`CronError` on any syntax violation (see module docstring). The
    day-of-week field accepts ``7`` as an alias for Sunday and normalises it to
    ``0`` in the stored set, so a ``5-7`` (Fri..Sun) range and a bare ``7`` both
    resolve correctly.
    """
    if not isinstance(expr, str):
        raise CronError("cron expression must be a string")
    stripped = expr.strip()
    if not stripped:
        raise CronError("cron expression is empty")
    if stripped.startswith("@") or "@" in stripped:
        raise CronError("cron '@'-shorthand (e.g. @daily) is not supported")
    if "?" in stripped:
        raise CronError("cron '?' (Quartz) is not supported")

    fields = stripped.split()
    if len(fields) != 5:
        raise CronError(f"cron must have exactly 5 fields (minute hour dom month dow); got {len(fields)}")
    minute_f, hour_f, dom_f, month_f, dow_f = fields

    minutes = _parse_field_values(minute_f, 0, 59, None, "minute")
    hours = _parse_field_values(hour_f, 0, 23, None, "hour")
    days_of_month = _parse_field_values(dom_f, 1, 31, None, "day-of-month")
    months = _parse_field_values(month_f, 1, 12, _MONTH_NAMES, "month")
    # day-of-week range is 0-7 (7 == Sunday); normalise 7 -> 0 afterwards.
    days_of_week = _parse_field_values(dow_f, 0, 7, _DOW_NAMES, "day-of-week")
    days_of_week = frozenset(0 if v == 7 else v for v in days_of_week)

    return CronSpec(
        minutes=minutes,
        hours=hours,
        days_of_month=days_of_month,
        months=months,
        days_of_week=days_of_week,
        dom_star=(dom_f == "*"),
        dow_star=(dow_f == "*"),
    )
