"""Phase 9 — the pure-Python strict 5-field cron parser (required #2).

Everything is local: no network, no clock, no scheduler. ``parse_cron`` is a
pure function (string in → :class:`CronSpec` out) and :meth:`CronSpec.next_fire`
is a bounded wall-clock search, so the whole surface is deterministic under a
fixed, caller-supplied timezone. The cases pin the grammar, the field forms
(``*`` / value / ``a-b`` / ``*/n`` / ``a-b/n`` / lists, month + day names, ``0``
and ``7`` both meaning Sunday), the Vixie day-of-month/day-of-week **OR** rule,
the strict rejections (wrong field count, ``?``, ``@``-shorthand, inverted
range, out-of-bounds, empty field, unknown token), and the bounded
calendar-impossible case (Feb 31 → ``None``, never an infinite loop).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from fibrecase_agent_backend.automation.cron import (
    CronError,
    CronSpec,
    parse_cron,
)

TZ = ZoneInfo("UTC")


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


# ===========================================================================
# valid expressions parse; each field form resolves to the right value set
# ===========================================================================
def test_star_is_full_range():
    spec = parse_cron("* * * * *")
    assert spec.minutes == frozenset(range(0, 60))
    assert spec.hours == frozenset(range(0, 24))
    assert spec.days_of_month == frozenset(range(1, 32))
    assert spec.months == frozenset(range(1, 13))
    assert spec.days_of_week == frozenset(range(0, 7))
    assert spec.dom_star is True and spec.dow_star is True


def test_single_values():
    spec = parse_cron("5 4 2 6 3")
    assert spec.minutes == {5}
    assert spec.hours == {4}
    assert spec.days_of_month == {2}
    assert spec.months == {6}
    assert spec.days_of_week == {3}
    assert spec.dom_star is False and spec.dow_star is False


def test_range_expands_inclusive():
    spec = parse_cron("1-4 * * * *")
    assert spec.minutes == {1, 2, 3, 4}


def test_step_over_full_range():
    spec = parse_cron("*/15 * * * *")
    assert spec.minutes == {0, 15, 30, 45}


def test_step_over_range():
    spec = parse_cron("0-10/3 * * * *")
    assert spec.minutes == {0, 3, 6, 9}


def test_comma_list_of_forms():
    # A mix of single, range, and step forms in one list.
    spec = parse_cron("1,5-7,*/20 * * * *")
    assert spec.minutes == {0, 1, 5, 6, 7, 20, 40}


def test_month_names_are_case_insensitive():
    assert parse_cron("0 0 1 jan *").months == {1}
    assert parse_cron("0 0 1 Feb *").months == {2}
    assert parse_cron("0 0 1 DEC *").months == {12}
    # A range of names.
    assert parse_cron("0 0 1 JAN-MAR *").months == {1, 2, 3}


def test_day_names_are_case_insensitive():
    assert parse_cron("0 0 * * mon").days_of_week == {1}
    assert parse_cron("0 0 * * SUN").days_of_week == {0}
    assert parse_cron("0 0 * * fri-sat").days_of_week == {5, 6}


def test_seven_is_sunday():
    # A bare ``7`` and a ``7`` in a range both normalise to ``0`` (Sunday).
    assert parse_cron("0 0 * * 7").days_of_week == {0}
    assert parse_cron("0 0 * * 5-7").days_of_week == {5, 6, 0}  # Fri..Sun


def test_zero_is_sunday():
    assert parse_cron("0 0 * * 0").days_of_week == {0}


# ===========================================================================
# strict rejections (each is a CronError, never a silent "never fires")
# ===========================================================================
@pytest.mark.parametrize(
    "expr",
    [
        "* * * *",     # 4 fields
        "* * * * * *",  # 6 fields (seconds)
    ],
)
def test_rejects_wrong_field_count(expr):
    with pytest.raises(CronError):
        parse_cron(expr)


def test_rejects_three_fields():
    with pytest.raises(CronError):
        parse_cron("* * *")


@pytest.mark.parametrize("expr", ["@daily", "@hourly", "@reboot", "0 0 * * @weekly"])
def test_rejects_at_shorthand(expr):
    with pytest.raises(CronError):
        parse_cron(expr)


@pytest.mark.parametrize("expr", ["0 0 ? * *", "0 0 * ? *", "0 0 * * ?"])
def test_rejects_quartz_question(expr):
    with pytest.raises(CronError):
        parse_cron(expr)


@pytest.mark.parametrize(
    "expr",
    ["0 10-4 * * *",   # inverted hour range
     "0 0 30-1 * *",   # inverted dom range
     "0 0 * 12-1 *",   # inverted month range
     "0 0 * * 5-1",    # inverted dow range
    ],
)
def test_rejects_inverted_range(expr):
    with pytest.raises(CronError):
        parse_cron(expr)


@pytest.mark.parametrize(
    "expr",
    ["60 * * * *",     # minute out of range
     "0 24 * * *",     # hour out of range
     "0 0 0 * *",      # dom 0 out of range (1-31)
     "0 0 32 * *",     # dom 32 out of range
     "0 0 * 0 *",      # month 0 out of range
     "0 0 * 13 *",     # month 13 out of range
     "0 0 * * 8",      # dow 8 out of range (0-7)
    ],
)
def test_rejects_out_of_bounds(expr):
    with pytest.raises(CronError):
        parse_cron(expr)


def test_extra_interior_and_outer_whitespace_is_tolerated():
    # ``str.split()`` collapses whitespace runs, so extra spaces (interior or
    # surrounding) still parse — the grammar is whitespace-tolerant, not strict
    # about spacing between the five fields.
    assert parse_cron("0  0 * * *").hours == {0}
    assert parse_cron("  0 7 * * *  ").hours == {7}


@pytest.mark.parametrize(
    "expr",
    ["a * * * *",     # unknown token
     "0 0 * * x",     # unknown day name
     "0 0 * 13a *",   # month with junk
     "0 0 * * 5-",    # dangling range endpoint
     "0 0 * * -5",    # dangling range start
     "0/5 * * * *",   # step on a single value (not allowed)
     "0 * * * */0",   # step must be >= 1
     "1,,2 * * * *",  # empty list element
    ],
)
def test_rejects_unknown_or_malformed_tokens(expr):
    with pytest.raises(CronError):
        parse_cron(expr)


def test_rejects_empty_expression():
    with pytest.raises(CronError):
        parse_cron("")
    with pytest.raises(CronError):
        parse_cron("   ")


def test_rejects_non_string():
    with pytest.raises(CronError):
        parse_cron(5)  # type: ignore[arg-type]


# ===========================================================================
# next_fire — representative expressions in a fixed timezone
# ===========================================================================
def test_daily_7am_same_day_when_before():
    assert parse_cron("0 7 * * *").next_fire(_t("2026-08-29T06:00:00"), TZ) == _t("2026-08-29T07:00:00")


def test_daily_7am_rolls_to_next_day_when_at_or_after():
    # Strictly after: exactly 07:00:00 is not itself a fire, so the next is tomorrow.
    assert parse_cron("0 7 * * *").next_fire(_t("2026-08-29T07:00:00"), TZ) == _t("2026-08-30T07:00:00")


def test_every_five_minutes():
    assert parse_cron("*/5 * * * *").next_fire(_t("2026-08-29T06:03:00"), TZ) == _t("2026-08-29T06:05:00")


def test_strictly_after_skips_exact_minute():
    # 06:05:00 itself is not a fire (strict); the next */5 is 06:10.
    assert parse_cron("*/5 * * * *").next_fire(_t("2026-08-29T06:05:00"), TZ) == _t("2026-08-29T06:10:00")


def test_weekday_monday_9am():
    # 2026-08-29 is a Saturday. The next Monday 09:00 is 2026-08-31.
    nxt = parse_cron("0 9 * * MON").next_fire(_t("2026-08-29T00:00:00"), TZ)
    assert nxt == _t("2026-08-31T09:00:00")
    assert nxt.weekday() == 0  # Python Monday=0


def test_dow_seven_fires_on_sunday_like_zero():
    sat = _t("2026-08-29T00:00:00")
    assert parse_cron("0 0 * * 7").next_fire(sat, TZ) == _t("2026-08-30T00:00:00")
    assert parse_cron("0 0 * * 0").next_fire(sat, TZ) == _t("2026-08-30T00:00:00")


def test_leap_year_february_29():
    # The next Feb 29 after 2026-01-01 is 2028-02-29 (2027 is not a leap year).
    assert parse_cron("0 0 29 2 *").next_fire(_t("2026-01-01T00:00:00"), TZ) == _t("2028-02-29T00:00:00")


def test_month_name_january_rolls_year():
    assert parse_cron("0 0 1 JAN *").next_fire(_t("2026-08-29T00:00:00"), TZ) == _t("2027-01-01T00:00:00")


def test_next_fire_is_in_the_supplied_tz():
    # A timezone with a non-zero UTC offset: the fire wall-clock time holds in tz.
    asia = ZoneInfo("Asia/Shanghai")
    nxt = parse_cron("0 7 * * *").next_fire(
        datetime(2026, 8, 29, 6, 0, tzinfo=asia), asia
    )
    assert nxt.tzinfo is not None
    assert (nxt.hour, nxt.minute) == (7, 0)


# ===========================================================================
# Vixie day-of-month / day-of-week OR semantics
# ===========================================================================
def test_vixie_both_star_always_matches():
    # Both restricted to "*": the day always matches; next fire is today/tomorrow 00:00.
    assert parse_cron("0 0 * * *").next_fire(_t("2026-08-29T01:00:00"), TZ) == _t("2026-08-30T00:00:00")


def test_vixie_one_star_restricted_dominates_dom():
    # dom restricted (the 1st), dow "*": fires only on day 1 of a month.
    # After 2026-08-29 → 2026-09-01.
    assert parse_cron("0 0 1 * *").next_fire(_t("2026-08-29T00:00:00"), TZ) == _t("2026-09-01T00:00:00")


def test_vixie_one_star_restricted_dominates_dow():
    # dow restricted (Monday), dom "*": fires on every Monday.
    assert parse_cron("0 0 * * MON").next_fire(_t("2026-08-29T00:00:00"), TZ) == _t("2026-08-31T00:00:00")


def test_vixie_both_restricted_either_matches_dom():
    # dom = 1st, dow = Monday. Under Vixie, *either* matching day fires.
    # 2026-09-01 is a Tuesday but is the 1st → it matches on day-of-month.
    spec = parse_cron("0 0 1 * MON")
    assert spec._day_matches(datetime(2026, 9, 1).date()) is True  # dom (1st) match
    # …and a Monday that is *not* the 1st also matches (dow match).
    assert spec._day_matches(datetime(2026, 9, 7).date()) is True  # dow (Monday) match
    # …and a day that is neither the 1st nor a Monday does not.
    assert spec._day_matches(datetime(2026, 9, 2).date()) is False


# ===========================================================================
# calendar-impossible → bounded None, never an infinite loop
# ===========================================================================
def test_calendar_impossible_feb_31_returns_none():
    # Syntax is valid (31 is a legal dom value) but no such day exists: the
    # bounded search exhausts and returns None (does not hang).
    assert parse_cron("0 0 31 2 *").next_fire(_t("2026-01-01T00:00:00"), TZ) is None


def test_calendar_impossible_is_stable_across_calls():
    spec = parse_cron("0 0 31 2 *")
    assert spec.next_fire(_t("2026-01-01T00:00:00"), TZ) is None
    assert spec.next_fire(_t("2026-06-15T12:00:00"), TZ) is None


def test_spec_is_frozen_and_hashable():
    spec = parse_cron("0 7 * * *")
    assert isinstance(spec, CronSpec)
    with pytest.raises(Exception):
        spec.minutes = frozenset()  # frozen dataclass → mutation refused
