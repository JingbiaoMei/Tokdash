from __future__ import annotations

from datetime import datetime
from datetime import timedelta, timezone

import tokdash.compute as compute
import tokdash.sessions as sessions


def test_get_session_data_month_vs_numeric_days(monkeypatch):
    calls: list[tuple] = []

    def fake_month():
        calls.append(("month",))
        return {"range": "month"}

    def fake_days(days: int):
        calls.append(("days", days))
        return {"range": f"{days}d"}

    monkeypatch.setattr(compute, "get_session_usage_month", fake_month)
    monkeypatch.setattr(compute, "get_session_usage_days", fake_days)

    assert compute.get_session_data("month") == {"range": "month"}
    assert compute.get_session_data("30") == {"range": "30d"}
    assert compute.get_session_data("week") == {"range": "7d"}

    assert calls == [("month",), ("days", 30), ("days", 7)]


def test_period_to_days_named_periods_do_not_collapse_to_today():
    # Regression: "all"/"year" (and any unrecognised named period) previously fell
    # through to 1 day, silently truncating ?period=all to today and looking like a
    # massive undercount. They must map to a wide window instead.
    assert compute.period_to_days("year") == 365
    assert compute.period_to_days("all") > 365 * 50
    assert compute.period_to_days("bogus") > 365 * 50
    # Sanity: the known short periods are unchanged.
    assert compute.period_to_days("today") == 1
    assert compute.period_to_days("week") == 7
    assert compute.period_to_days("14days") == 14


def test_sessions_period_mapping_matches_compute():
    # /api/sessions and /api/usage must agree on what named periods mean.
    # sessions._period_to_days previously had its own copy that mapped
    # year/all/unknown to today; it now delegates to compute.period_to_days.
    for period in ("today", "week", "month", "year", "all", "14days", "90", "bogus"):
        assert sessions._period_to_days(period) == compute.period_to_days(period)


def test_period_to_range_args_all_spans_many_years():
    args = compute.period_to_range_args("all")
    assert args[:1] == ["--since"]
    since = datetime.strptime(args[1], "%Y-%m-%d").date()
    until = datetime.strptime(args[3], "%Y-%m-%d").date()
    # The window should reach back far enough to cover any real transcript history.
    assert (until - since).days > 365 * 50


def test_period_to_range_args_month_is_calendar_month():
    args = compute.period_to_range_args("month")
    assert args[:1] == ["--since"]
    assert args[2:3] == ["--until"]

    since = datetime.strptime(args[1], "%Y-%m-%d").date()
    until = datetime.strptime(args[3], "%Y-%m-%d").date()

    now_local = datetime.now().astimezone()

    assert since == now_local.replace(day=1).date()
    assert until == now_local.date()


def test_previous_period_range_today_uses_full_yesterday(monkeypatch):
    current_since = datetime(2026, 3, 31, 0, 0, tzinfo=timezone.utc)
    current_until = datetime(2026, 3, 31, 8, 20, tzinfo=timezone.utc)

    monkeypatch.setattr(compute, "_current_period_range", lambda period: (current_since, current_until))

    prev_since, prev_until = compute.previous_period_range("today")

    assert prev_since == current_since - timedelta(days=1)
    assert prev_until == current_since


def test_previous_period_range_three_days_uses_full_previous_three_days(monkeypatch):
    current_since = datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)
    current_until = datetime(2026, 3, 31, 8, 20, tzinfo=timezone.utc)

    monkeypatch.setattr(compute, "_current_period_range", lambda period: (current_since, current_until))

    prev_since, prev_until = compute.previous_period_range("3days")

    assert prev_since == datetime(2026, 3, 26, 0, 0, tzinfo=timezone.utc)
    assert prev_until == current_since


def test_previous_period_range_month_uses_full_previous_calendar_month(monkeypatch):
    current_since = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
    current_until = datetime(2026, 3, 31, 8, 20, tzinfo=timezone.utc)

    monkeypatch.setattr(compute, "_current_period_range", lambda period: (current_since, current_until))

    prev_since, prev_until = compute.previous_period_range("month")

    assert prev_since == datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    assert prev_until == current_since
