"""The shared range model: what a preset means, and what stepping may do.

`tokdash.tui.ranges` is the single source of truth for the dates four panes
display and fetch with, so its rules get locked here rather than inferred from
screens: calendar presets step by WHOLE calendar windows, rolling presets by
their own inclusive length, nothing steps into the future, All Time is inert, and
every dated selection travels as an explicit date pair.
"""
from __future__ import annotations

import datetime as dt

import pytest

from tokdash.tui.ranges import (
    PRESETS,
    PRESET_BY_KEY,
    RANGE_KEYS,
    DateWindow,
    StatsWindow,
    calendar_window,
    custom_window,
    parse_custom_date,
    window_for,
    window_for_days,
    window_for_period,
)

# 2026-10-19 is a Monday, so week arithmetic is unambiguous.
TODAY = dt.date(2026, 10, 19)


# ---------------------------------------------------------------- presets --

def test_every_preset_resolves_to_exact_inclusive_dates():
    for preset in PRESETS:
        window = window_for(preset.key, TODAY)
        if preset.all_time:
            assert (window.date_from, window.date_to) == (None, None)
            continue
        assert window.date_from <= window.date_to <= TODAY, window
        # describe() names both ends; a preset name alone never reaches the user
        text = window.describe()
        assert window.date_from.isoformat() in text
        assert window.date_to.isoformat() in text


def test_rolling_presets_are_inclusive_of_today():
    assert window_for("7d", TODAY).days == 7
    assert window_for("365d", TODAY).date_to == TODAY
    assert window_for("365d", TODAY).days == 365
    assert window_for("7d", TODAY).date_from == TODAY - dt.timedelta(days=6)


def test_calendar_presets_clamp_the_current_window_and_complete_the_past_one():
    # "This Month" on the 19th is the 1st..19th; "Last Month" is the WHOLE
    # September, not the 19 days that would make it the same length.
    assert calendar_window("month", TODAY, 0) == (dt.date(2026, 10, 1), TODAY)
    assert calendar_window("month", TODAY, 1) == (
        dt.date(2026, 9, 1),
        dt.date(2026, 9, 30),
    )
    assert calendar_window("year", TODAY, 0) == (dt.date(2026, 1, 1), TODAY)
    assert calendar_window("year", TODAY, 1) == (
        dt.date(2025, 1, 1),
        dt.date(2025, 12, 31),
    )
    # On a Monday the current week is one day long; the week before it is the
    # full Monday-to-Sunday span, which is what "Last Week (Mon-Sun)" promises.
    assert calendar_window("week", TODAY, 0) == (TODAY, TODAY)
    assert calendar_window("week", TODAY, 1) == (
        dt.date(2026, 10, 12),
        dt.date(2026, 10, 18),
    )


def test_single_key_shortcuts_name_the_window_the_spec_promises():
    # "d" is Yesterday and "w" is the rolling seven days. The old "w" (calendar
    # week) survives as a named preset, so nothing was lost by the re-key.
    assert window_for(RANGE_KEYS["d"], TODAY).date_from == TODAY - dt.timedelta(days=1)
    assert window_for(RANGE_KEYS["w"], TODAY).days == 7
    assert PRESET_BY_KEY["lastweek"].unit == "week"


def test_labels_describe_the_resolved_window_not_the_pressed_key():
    # Yesterday stepped forward IS today; the label must not keep saying Yesterday.
    yesterday = window_for("yesterday", TODAY)
    assert yesterday.label == "Yesterday"
    assert yesterday.step(1, TODAY).label == "Today"
    assert window_for("lastmonth", TODAY).label == "Last Month"
    assert window_for("lastmonth", TODAY).step(-1, TODAY).label == "2 months back"


# -------------------------------------------------------------- requests --

def test_dated_windows_fetch_with_explicit_dates_and_all_time_with_a_token():
    assert window_for("today", TODAY).request() == (
        "today",
        "2026-10-19",
        "2026-10-19",
    )
    assert window_for("7d", TODAY).request() == (
        "today",
        "2026-10-13",
        "2026-10-19",
    )
    # An endpoint label like "lastWeek" is never sent as a period: the resolver
    # would quietly fall back to all time and widen every number on screen.
    assert window_for("all", TODAY).request() == ("all", None, None)


def test_stepping_uses_whole_calendar_windows_and_own_length_when_rolling():
    month = window_for("month", TODAY)
    back = month.step(-1, TODAY)
    assert (back.date_from, back.date_to) == (dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert back.step(1, TODAY) == month  # forward returns the partial month

    week = window_for("7d", TODAY)
    earlier = week.step(-1, TODAY)
    assert earlier.days == 7, "a seven-day window must never step into six"
    assert earlier.date_to == dt.date(2026, 10, 12)  # one whole 7-day hop back
    assert earlier.step(1, TODAY) == week


def test_nothing_steps_into_the_future_and_all_time_is_inert():
    assert window_for("today", TODAY).step(1, TODAY) is None
    assert window_for("7d", TODAY).step(1, TODAY) is None
    assert window_for("all", TODAY).step(-1, TODAY) is None
    assert window_for("all", TODAY).step(1, TODAY) is None


def test_a_custom_pair_steps_by_its_own_length_and_stops_at_today():
    window, err = custom_window("2026-09-01", "2026-09-14", TODAY)
    assert err is None and window.days == 14
    back = window.step(-1, TODAY)
    assert (back.date_from, back.date_to) == (
        dt.date(2026, 8, 18),
        dt.date(2026, 8, 31),
    )
    # A pair that already ends today cannot move forward into the future.
    current, err = custom_window("2026-10-06", "2026-10-19", TODAY)
    assert err is None and current.step(1, TODAY) is None


# --------------------------------------------------------------- rollover --

def test_midnight_reanchors_a_preset_but_never_a_typed_pair():
    later = TODAY + dt.timedelta(days=1)
    assert window_for("month", TODAY).redate(later).date_to == later
    assert window_for("today", TODAY).redate(later).date_from == later
    typed, _ = custom_window("2026-09-01", "2026-09-14", TODAY)
    assert typed.redate(later) == typed
    assert window_for("all", TODAY).redate(later).all_time


def test_a_shifted_window_keeps_its_shift_across_midnight():
    shifted = window_for("lastmonth", TODAY).step(-1, TODAY)  # shift 2
    moved = shifted.redate(TODAY + dt.timedelta(days=40))
    assert moved.shift == shifted.shift
    # shift 2 means "two months back from whichever today is live", so crossing
    # into November walks it forward to September rather than July.
    assert (moved.date_from, moved.date_to) == (
        dt.date(2026, 9, 1),
        dt.date(2026, 9, 30),
    )


# ------------------------------------------------------------------ stats --

def test_stats_trailing_year_is_not_this_calendar_year():
    # The single most common misreading of /api/stats: no year means the
    # TRAILING 365 days, which on 2026-10-19 starts in October 2025.
    trailing = StatsWindow(None)
    assert trailing.is_trailing and trailing.request() is None
    assert trailing.bounds(TODAY) == (dt.date(2025, 10, 20), TODAY)
    assert "Trailing 365" in trailing.describe(TODAY)
    assert "2025-10-20" in trailing.describe(TODAY)


def test_stats_named_year_is_a_whole_calendar_year():
    year = StatsWindow(2025)
    assert year.request() == 2025
    assert year.bounds(TODAY) == (dt.date(2025, 1, 1), dt.date(2025, 12, 31))
    assert year.describe(TODAY).startswith("2025  (")


def test_stats_ladder_walks_both_ways_from_the_year_in_progress():
    trailing = StatsWindow(None)
    assert trailing.step(1, TODAY) == trailing  # already the newest rung
    # The rung under "trailing 365 days" is THIS calendar year. The old ladder
    # stepped straight past it, so "show me the year so far" had no key.
    current = trailing.step(-1, TODAY)
    assert current == StatsWindow(2026)
    assert current.step(-1, TODAY) == StatsWindow(2025)
    # The way back is the same ladder, run the other way.
    assert StatsWindow(2025).step(1, TODAY) == StatsWindow(2026)
    assert StatsWindow(2026).step(1, TODAY) == trailing
    assert StatsWindow(2026).step(5, TODAY) == trailing


def test_the_current_year_is_named_for_the_days_it_can_answer():
    # /api/stats can only answer the current year THROUGH today; naming a window
    # the data cannot reach is the mistake this module exists to stop.
    current = StatsWindow(TODAY.year)
    assert current.bounds(TODAY) == (dt.date(2026, 1, 1), TODAY)
    assert current.describe(TODAY).startswith("2026 to date")
    # A past year stays a whole calendar year.
    assert StatsWindow(2025).bounds(TODAY) == (dt.date(2025, 1, 1), dt.date(2025, 12, 31))


# ------------------------------------------- custom-date validation stays put --

def test_a_half_typed_date_keeps_its_text_and_names_the_problem():
    assert parse_custom_date("", TODAY) == (None, "enter a date as YYYY-MM-DD")
    assert parse_custom_date("2026-13-99", TODAY)[1].startswith("'2026-13-99'")
    assert parse_custom_date("2027-01-01", TODAY)[1].endswith("is in the future")
    window, err = custom_window("2026-09-20", "2026-09-01", TODAY)
    assert window is None and "is after" in err


# --------------------------------------------- the CLI period vocabulary --

@pytest.mark.parametrize(
    "token,expect",
    [
        ("today", ("today", 1)),
        ("week", ("7d", 7)),
        ("year", ("365d", 365)),
        # No "month" row: it is the one period the resolver will not reduce to a
        # length, and it has its own test below.
        ("3days", ("rolling3", 3)),
        ("14days", ("rolling14", 14)),
        ("14", ("rolling14", 14)),
        ("7", ("7d", 7)),
    ],
)
def test_period_tokens_open_on_a_rolling_window_of_the_right_length(token, expect):
    key, days = expect
    window = window_for_period(token, TODAY)
    assert window.preset == key, window
    assert window.days == days, window
    assert window.date_to == TODAY
    assert window.request()[1] == (TODAY - dt.timedelta(days=days - 1)).isoformat()


def test_all_time_and_an_unknown_token_are_the_two_explicit_answers():
    assert window_for_period("all", TODAY).all_time
    # `tokdash tui --period` is validated by run_tui; a direct construction with
    # junk still has to land on a named window rather than on all time.
    assert window_for_period("garbage", TODAY).preset == "today"
    assert window_for_period("", TODAY).preset == "today"
    assert window_for_period(None, TODAY).preset == "today"


def test_a_period_window_steps_by_its_own_length_and_survives_midnight():
    fortnight = window_for_period("14", TODAY)
    assert fortnight.step(-1, TODAY).days == 14
    assert (fortnight.step(-1, TODAY).date_from, fortnight.step(-1, TODAY).date_to) == (
        dt.date(2026, 9, 22),
        dt.date(2026, 10, 5),
    )
    assert fortnight.step(1, TODAY) is None
    assert fortnight.redate(TODAY + dt.timedelta(days=1)).date_to == TODAY + dt.timedelta(days=1)


def test_the_month_period_takes_the_calendar_month_and_thirty_days_does_not():
    # ``month`` is the ONE period compute refuses to reduce to a length:
    # period_to_range_args opens the first of the current calendar month, so
    # --period month must show October 1..19 and light the button that names
    # those dates. A 30-day ask is still a rolling window, and lighting "This
    # Month" under September-20 dates would claim a window the numbers do not
    # show.
    month = window_for_period("month", TODAY)
    assert isinstance(month, DateWindow)
    assert month.preset == "month" and month.preset in PRESET_BY_KEY
    assert (month.date_from, month.date_to) == (dt.date(2026, 10, 1), TODAY)

    rolling = window_for_days(30, TODAY)
    assert rolling.preset not in PRESET_BY_KEY
    assert rolling.short_label == "Last 30 Days"
    assert rolling.date_from == dt.date(2026, 9, 20)


def test_window_for_days_reuses_only_identical_presets():
    assert window_for_days(1, TODAY) == window_for("today", TODAY)
    assert window_for_days(7, TODAY) == window_for("7d", TODAY)
    assert window_for_days(365, TODAY) == window_for("365d", TODAY)
    assert window_for_days(0, TODAY).days == 1  # a zero-length ask is one day
