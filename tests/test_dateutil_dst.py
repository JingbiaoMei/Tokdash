"""Tests for DST-aware date range resolution and dateutil helpers (Issue #145)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest

from tokdash.compute import _date_range_from_args
from tokdash.dateutil import (
    get_configured_timezone,
    local_midnight,
    parse_date_range,
    reset_cached_timezone,
)
from tokdash.sources.openclaw import get_usage_for_year


@pytest.fixture(autouse=True)
def clean_tz_state(monkeypatch):
    monkeypatch.delenv("TOKDASH_TZ", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    reset_cached_timezone()
    yield
    reset_cached_timezone()


def test_dst_resolution_america_new_york_cross_phase():
    """Verify that January (EST, -5) and September (EDT, -4) get date-specific offsets."""
    ny = ZoneInfo("America/New_York")

    # Winter date (EST, UTC-5)
    s_jan, u_jan = parse_date_range("2026-01-05", "2026-01-05", tz=ny)
    assert s_jan.astimezone(timezone.utc) == datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc)
    assert u_jan.astimezone(timezone.utc) == datetime(2026, 1, 6, 5, 0, tzinfo=timezone.utc)
    assert s_jan.utcoffset() == timedelta(hours=-5)

    # Summer date (EDT, UTC-4)
    s_sep, u_sep = parse_date_range("2026-09-15", "2026-09-15", tz=ny)
    assert s_sep.astimezone(timezone.utc) == datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc)
    assert u_sep.astimezone(timezone.utc) == datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)
    assert s_sep.utcoffset() == timedelta(hours=-4)


def test_dst_boundary_event_capture_issue_145():
    """Verify the exact reproduction scenario from Issue #145: late-night events are captured."""
    ny = ZoneInfo("America/New_York")

    s, u = parse_date_range("2026-01-05", "2026-01-05", tz=ny)
    # Event at 11:30 PM Jan 5 local time in New York (EST)
    ev = datetime(2026, 1, 5, 23, 30, tzinfo=ny).astimezone(timezone.utc)

    # True local-day bounds in UTC
    lo = datetime(2026, 1, 5, tzinfo=ny).astimezone(timezone.utc)
    hi = datetime(2026, 1, 6, tzinfo=ny).astimezone(timezone.utc)

    assert lo <= ev < hi, "Sanity check: true window captures event"
    assert s <= ev < u, "Fix verification: returned window must capture late-night event"


def test_dst_transition_spanning_range():
    """A range crossing the spring-forward DST transition resolves each bound correctly."""
    ny = ZoneInfo("America/New_York")
    # US DST in 2026 begins on Sunday, March 8:
    # March 5 is EST (UTC-5), March 15 is EDT (UTC-4)
    s, u = parse_date_range("2026-03-05", "2026-03-15", tz=ny)

    # March 5 starts at 05:00 UTC (EST)
    assert s.astimezone(timezone.utc) == datetime(2026, 3, 5, 5, 0, tzinfo=timezone.utc)
    # until is start of March 16: 04:00 UTC (EDT)
    assert u.astimezone(timezone.utc) == datetime(2026, 3, 16, 4, 0, tzinfo=timezone.utc)


def test_dst_resolution_europe_london():
    """Verify GMT (UTC+0) in winter and BST (UTC+1) in summer."""
    lon = ZoneInfo("Europe/London")

    s_jan, u_jan = parse_date_range("2026-01-10", "2026-01-10", tz=lon)
    assert s_jan.astimezone(timezone.utc) == datetime(2026, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert s_jan.utcoffset() == timedelta(0)

    s_jul, u_jul = parse_date_range("2026-07-10", "2026-07-10", tz=lon)
    assert s_jul.astimezone(timezone.utc) == datetime(2026, 7, 9, 23, 0, tzinfo=timezone.utc)
    assert s_jul.utcoffset() == timedelta(hours=1)


def test_dst_resolution_southern_hemisphere():
    """Verify southern hemisphere zone (Australia/Sydney) where Jan is DST and Jul is standard."""
    syd = ZoneInfo("Australia/Sydney")

    # January: AEDT (UTC+11)
    s_jan, u_jan = parse_date_range("2026-01-15", "2026-01-15", tz=syd)
    assert s_jan.astimezone(timezone.utc) == datetime(2026, 1, 14, 13, 0, tzinfo=timezone.utc)
    assert s_jan.utcoffset() == timedelta(hours=11)

    # July: AEST (UTC+10)
    s_jul, u_jul = parse_date_range("2026-07-15", "2026-07-15", tz=syd)
    assert s_jul.astimezone(timezone.utc) == datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)
    assert s_jul.utcoffset() == timedelta(hours=10)


def test_fixed_offset_timezone_asia_kolkata():
    """Fixed-offset zone without DST resolves identically year-round."""
    cal = ZoneInfo("Asia/Kolkata")

    s_jan, u_jan = parse_date_range("2026-01-01", "2026-01-01", tz=cal)
    s_jul, u_jul = parse_date_range("2026-07-01", "2026-07-01", tz=cal)

    assert s_jan.utcoffset() == timedelta(hours=5, minutes=30)
    assert s_jul.utcoffset() == timedelta(hours=5, minutes=30)


def test_env_var_tokdash_tz(monkeypatch):
    """Setting TOKDASH_TZ resolves date ranges in the specified zone."""
    monkeypatch.setenv("TOKDASH_TZ", "America/New_York")
    assert get_configured_timezone() is not None

    s, u = parse_date_range("2026-01-05", "2026-01-05")
    assert s.astimezone(timezone.utc) == datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc)
    assert u.astimezone(timezone.utc) == datetime(2026, 1, 6, 5, 0, tzinfo=timezone.utc)


def test_pre_1970_fallback_does_not_crash():
    """Pre-1970 dates resolve safely without raising OSError on Windows CRT."""
    dt_pre = datetime(1960, 5, 10, 0, 0)
    res = local_midnight(dt_pre)
    assert res.tzinfo is not None
    assert res.year == 1960

    s, u = parse_date_range("1965-06-01", "1965-06-05")
    assert s.year == 1965
    assert u.year == 1965
    assert s < u


def test_invalid_and_inverted_date_ranges():
    """Validation errors on bad inputs and inverted date spans."""
    with pytest.raises(ValueError, match="date_from must be on or before date_to"):
        parse_date_range("2026-01-10", "2026-01-09")

    with pytest.raises(ValueError):
        parse_date_range("not-a-date", "2026-01-09")

    with pytest.raises(ValueError):
        parse_date_range("2026-02-30", "2026-03-01")


def test_compute_date_range_from_args_uses_local_midnight(monkeypatch):
    """_date_range_from_args resolves --since and --until with local_midnight."""
    monkeypatch.setenv("TOKDASH_TZ", "America/New_York")
    since, until = _date_range_from_args(["--since", "2026-01-05", "--until", "2026-01-05"])
    assert since is not None and until is not None
    assert since.astimezone(timezone.utc) == datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc)
    assert until.astimezone(timezone.utc) == datetime(2026, 1, 6, 5, 0, tzinfo=timezone.utc)


def test_openclaw_get_usage_for_year_local_midnight(monkeypatch):
    """openclaw.get_usage_for_year resolves start and end with local_midnight."""
    monkeypatch.setenv("TOKDASH_TZ", "America/New_York")
    # Mock openclaw agent session glob to return empty so get_usage_for_year returns empty dict
    monkeypatch.setattr("tokdash.sources.openclaw.openclaw_agent_sessions_glob", lambda: "nonexistent/*")
    data = get_usage_for_year(2026)
    assert isinstance(data, dict)


def test_dst_spring_forward_23_hour_day():
    """On spring-forward day (March 8, 2026 in NY), the single local day window is 23 hours."""
    ny = ZoneInfo("America/New_York")
    s, u = parse_date_range("2026-03-08", "2026-03-08", tz=ny)
    assert s.astimezone(timezone.utc) == datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc)
    assert u.astimezone(timezone.utc) == datetime(2026, 3, 9, 4, 0, tzinfo=timezone.utc)
    delta = u.astimezone(timezone.utc) - s.astimezone(timezone.utc)
    assert delta == timedelta(hours=23)


def test_dst_fall_back_25_hour_day():
    """On fall-back day (November 1, 2026 in NY), the single local day window is 25 hours."""
    ny = ZoneInfo("America/New_York")
    s, u = parse_date_range("2026-11-01", "2026-11-01", tz=ny)
    assert s.astimezone(timezone.utc) == datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
    assert u.astimezone(timezone.utc) == datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    delta = u.astimezone(timezone.utc) - s.astimezone(timezone.utc)
    assert delta == timedelta(hours=25)


def test_local_midnight_aware_datetime_passthrough():
    """Aware datetimes passed to local_midnight are returned or converted to target tz."""
    ny = ZoneInfo("America/New_York")
    aware_dt = datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc)

    # Without explicit tz parameter: returned directly
    res = local_midnight(aware_dt)
    assert res == aware_dt

    # With target tz parameter: converted to target timezone
    res_ny = local_midnight(aware_dt, tz=ny)
    assert res_ny == aware_dt
    assert res_ny.tzinfo == ny
    assert res_ny.hour == 0  # 05:00 UTC is 00:00 EST


def test_detect_system_zoneinfo_resolution():
    """Verify system timezone detection returns a valid timezone on standard systems."""
    from tokdash.dateutil import _detect_system_zoneinfo, get_system_timezone

    detected = _detect_system_zoneinfo()
    cached = get_system_timezone()
    assert detected == cached

