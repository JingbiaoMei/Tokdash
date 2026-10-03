"""#145: a date window is local midnight *of its own dates*, not today's offset.

Anchoring every boundary to the offset captured once -- ``datetime.now().
astimezone().tzinfo`` -- dates a January range an hour early for half the year,
and the Sessions panel built its month start that way while Overview did not,
so the first hour of a month counted on one tab and not the other.

What these tests pin is that a boundary carries the offset that *its* date has,
which is the only rule that keeps the windows and the day buckets together:
entries are bucketed in the OS's local time and the heatmap buckets in SQLite
with ``'localtime'``, so a window resolved from any other source of truth can
disagree with both.

They drive the default path -- no arguments, no environment of ours -- because
that is what a user's install runs. ``TZ`` plus ``time.tzset()`` is how the OS
local zone is moved on a POSIX box; Windows has no ``time.tzset()`` and its C
runtime reads the zone straight from the OS, so those tests skip there. Three of
them run on every platform and state the rule without needing a zone to move.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from tokdash import compute, sessions
from tokdash.dateutil import local_midnight, parse_date_range
from tokdash.sources import openclaw

# Windows reads its zone from the OS and has no time.tzset(), so a TZ override
# there is ignored and a test written against it would assert nothing.
needs_tzset = pytest.mark.skipif(
    not hasattr(time, "tzset"),
    reason="the OS local zone cannot be overridden without time.tzset()",
)

LONDON = "Europe/London"
# London leaves BST on 25 Oct 2026, so these two frozen instants sit either side
# of that change: "now" is on GMT and every date before it is on BST.
AFTER_THE_CHANGE = datetime(2026, 10, 30, 12, 0)
ON_THE_CHANGE = datetime(2026, 10, 25, 12, 0)


@pytest.fixture
def os_zone(monkeypatch):
    """Point the OS local zone at ``name`` for the duration of one test."""

    def _set(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield _set
    # Undo the environment first: the C library re-reads it on tzset().
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def frozen_local_now(monkeypatch):
    """Freeze ``datetime.now()`` in every module that builds a window.

    Each module imports ``datetime`` by name, so the attribute is what has to
    be replaced. ``_Frozen`` stays a datetime subclass, so ``combine``,
    ``strptime`` and ``fromtimestamp`` keep returning real datetimes.
    """

    def _freeze(local_now: datetime) -> None:
        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return local_now
                return local_now.replace(tzinfo=local_now.astimezone().tzinfo).astimezone(tz)

        for module in (compute, sessions, openclaw):
            monkeypatch.setattr(module, "datetime", _Frozen)

    return _freeze


def _utc_ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


# --- the rule itself ------------------------------------------------------


@needs_tzset
@pytest.mark.parametrize("day", ["2026-01-05", "2026-07-05", "2026-03-28"])
def test_each_boundary_carries_the_offset_its_own_date_has(os_zone, day):
    os_zone(LONDON)
    start = datetime.strptime(day, "%Y-%m-%d")
    since, until = parse_date_range(day, day)
    assert since.utcoffset() == start.astimezone().utcoffset()
    # `until` is the *next* day's midnight, so it carries that day's offset.
    assert until.utcoffset() == (start + timedelta(days=1)).astimezone().utcoffset()


@needs_tzset
def test_a_winter_day_window_still_captures_a_late_night_event(os_zone):
    # The report in #145: an event recorded at 23:30 local on the last day of
    # the range fell outside a window dated with the wrong offset.
    os_zone(LONDON)
    since, until = parse_date_range("2026-01-05", "2026-01-05")
    event = datetime(2026, 1, 5, 23, 30).astimezone(timezone.utc)
    assert since <= event < until


@needs_tzset
@pytest.mark.parametrize(
    "day,hours",
    [("2026-03-29", 23), ("2026-10-25", 25)],  # spring forward, fall back
)
def test_a_clock_change_day_is_23_or_25_hours_of_local_time(os_zone, day, hours):
    # Anchoring both ends to one offset makes every day 24 hours, which is how
    # a window silently gains or loses an hour on the day the clocks move.
    # Compared as instants on purpose: subtracting two aware datetimes that
    # share a tzinfo object is a naive subtraction and would report 24.
    os_zone(LONDON)
    since, until = parse_date_range(day, day)
    elapsed = until.astimezone(timezone.utc) - since.astimezone(timezone.utc)
    assert elapsed == timedelta(hours=hours)


def test_parse_date_range_rejects_an_inverted_range():
    with pytest.raises(ValueError, match="date_from must be on or before date_to"):
        parse_date_range("2026-01-06", "2026-01-05")


def test_local_midnight_leaves_an_aware_datetime_alone():
    # The caller already knows which instant it means; re-resolving it would
    # move it.
    aware = datetime(2026, 1, 5, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert local_midnight(aware) is aware


def test_local_midnight_resolves_a_naive_value_with_the_offset_for_that_date():
    # Runs on every platform, DST-observing zone or not: the boundary is
    # whatever the OS says that date's offset is.
    for day in (datetime(2026, 1, 5), datetime(2026, 7, 5)):
        assert local_midnight(day).utcoffset() == day.astimezone().utcoffset()


# --- the default path, driven the way a user's install runs it ------------


@needs_tzset
def test_overview_and_sessions_agree_on_the_month_start_after_a_clock_change(os_zone, frozen_local_now):
    os_zone(LONDON)
    frozen_local_now(AFTER_THE_CHANGE)

    overview_since, _ = compute._current_period_range("month")
    sessions_since_ms, _ = sessions._period_range("month")

    # 1 Oct was still on BST, so the month starts an hour before 1 Oct UTC.
    assert overview_since == datetime(2026, 10, 1).astimezone(timezone.utc)
    assert sessions_since_ms == _utc_ms(datetime(2026, 10, 1))


@needs_tzset
@pytest.mark.parametrize(
    "period,start",
    [("today", "2026-10-30"), ("7d", "2026-10-24"), ("month", "2026-10-01")],
)
def test_overview_and_sessions_agree_on_every_period_after_a_clock_change(
    os_zone, frozen_local_now, period, start
):
    os_zone(LONDON)
    frozen_local_now(AFTER_THE_CHANGE)

    overview_since, _ = compute._current_period_range(period)
    sessions_since_ms, _ = sessions._period_range(period)
    assert sessions_since_ms == _utc_ms(overview_since) == _utc_ms(datetime.strptime(start, "%Y-%m-%d"))


@needs_tzset
def test_the_sessions_window_ends_at_the_next_local_midnight_on_a_clock_change_day(os_zone, frozen_local_now):
    os_zone(LONDON)
    frozen_local_now(ON_THE_CHANGE)

    _, until_ms = sessions._period_range("7d")

    # 26 Oct is back on GMT: the window has to run to 00:00 UTC, not to 23:00
    # the previous day, which is what adding a day after converting gave.
    assert until_ms == _utc_ms(datetime(2026, 10, 26))


@needs_tzset
def test_todays_cli_window_spans_the_whole_local_day_across_a_clock_change(os_zone, frozen_local_now):
    os_zone(LONDON)
    frozen_local_now(ON_THE_CHANGE)

    since, until = compute._date_range_from_args(["--today"])

    assert since == datetime(2026, 10, 25).astimezone(timezone.utc)
    assert until == datetime(2026, 10, 26).astimezone(timezone.utc)


@needs_tzset
def test_the_previous_month_starts_at_its_own_dates_offset(os_zone, frozen_local_now):
    os_zone(LONDON)
    frozen_local_now(datetime(2026, 11, 5, 12, 0))  # November is on GMT; October's 1st on BST

    prev_since, prev_until = compute.previous_period_range("month")

    # The month before November started on 1 Oct, which was still on BST, so
    # its first midnight is an hour before 1 Oct UTC. Reaching that by
    # .replace(day=1) on an aware November instant carried GMT onto it instead.
    assert prev_until == datetime(2026, 11, 1).astimezone(timezone.utc)
    assert prev_since == datetime(2026, 10, 1).astimezone(timezone.utc)


@needs_tzset
@pytest.mark.parametrize(
    "now,period,prev_start",
    [
        (datetime(2026, 10, 26, 12, 0), "today", datetime(2026, 10, 25)),
        (datetime(2026, 11, 1, 12, 0), "7d", datetime(2026, 10, 19)),
    ],
)
def test_the_previous_window_starts_at_its_own_dates_offset(
    os_zone, frozen_local_now, now, period, prev_start
):
    os_zone(LONDON)
    frozen_local_now(now)

    current_since, _ = compute._current_period_range(period)
    prev_since, prev_until = compute.previous_period_range(period)

    assert prev_until == current_since
    # The clock change falls exactly on the boundary between this window and the
    # one before it, so this window's start is on GMT and the previous window's
    # start is still on BST: its first midnight is an hour before that date's
    # 00:00 UTC. Subtracting a timedelta from an aware UTC instant landed on
    # 00:00 UTC instead and dropped that hour.
    assert prev_since == local_midnight(prev_start)
    assert prev_since.astimezone().utcoffset() == timedelta(hours=1)


@needs_tzset
def test_the_previous_day_after_a_clock_change_is_25_hours_long(os_zone, frozen_local_now):
    os_zone(LONDON)
    frozen_local_now(datetime(2026, 10, 26, 12, 0))  # the day after London leaves BST

    prev_since, prev_until = compute.previous_period_range("today")

    # 25 Oct began on BST and ran 25 hours. A 24-hour step back from a GMT
    # instant dropped its first hour, which is exactly what the "vs previous
    # period" comparison is measuring.
    assert prev_since == datetime(2026, 10, 25).astimezone(timezone.utc)
    assert prev_until - prev_since == timedelta(hours=25)


@needs_tzset
@pytest.mark.parametrize(
    "call,args,expected",
    [
        ("get_usage_for_month", (), datetime(2026, 10, 1)),
        ("get_usage_for_days", (1,), datetime(2026, 10, 30)),  # one day back, inclusive
        ("get_usage_for_days", (7,), datetime(2026, 10, 24)),
    ],
)
def test_openclaw_starts_its_windows_at_their_own_dates_offset(
    os_zone, frozen_local_now, monkeypatch, call, args, expected
):
    os_zone(LONDON)
    frozen_local_now(AFTER_THE_CHANGE)
    captured = {}

    def _capture(sessions_dir, since_date, until_date):
        captured["since"] = since_date
        return {}

    monkeypatch.setattr(openclaw, "get_session_usage", _capture)

    getattr(openclaw, call)(*args)

    assert captured["since"] == expected.astimezone(timezone.utc)


@needs_tzset
def test_openclaw_year_bounds_straddle_the_year_in_its_own_offsets(os_zone, monkeypatch):
    os_zone(LONDON)
    captured = {}

    def _capture(sessions_dir, since_date, until_date):
        captured["since"] = since_date
        captured["until"] = until_date
        return {}

    monkeypatch.setattr(openclaw, "get_session_usage", _capture)

    openclaw.get_usage_for_year(2026)

    assert captured["since"] == datetime(2026, 1, 1).astimezone(timezone.utc)
    assert captured["until"] == datetime(2027, 1, 1).astimezone(timezone.utc) - timedelta(microseconds=1)


@needs_tzset
def test_no_environment_variable_moves_the_window(os_zone, monkeypatch):
    # The OS zone is the only source of truth. A second one -- a TOKDASH_TZ
    # override, a stale /etc/timezone -- is how the windows and the day buckets
    # drift apart, so an inherited one has to be ignored rather than honoured.
    os_zone(LONDON)
    monkeypatch.setenv("TOKDASH_TZ", "Pacific/Kiritimati")

    since, until = parse_date_range("2026-01-05", "2026-01-05")

    assert since == datetime(2026, 1, 5).astimezone(timezone.utc)
    assert until == datetime(2026, 1, 6).astimezone(timezone.utc)
