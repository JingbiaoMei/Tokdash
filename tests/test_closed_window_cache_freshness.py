"""A past date range is never answered from a snapshot taken while it was still open.

The dashboard sends every quick range as an explicit ``date_from``/``date_to`` pair and
never sends ``period``, so viewing "Today" on day D and clicking "Yesterday" on day D+1
used to build the identical response-cache key. The response cache serves a stale entry
with no upper bound on its age, so the second request was answered from the partial
mid-day snapshot the first one left behind, and only the Refresh button recomputed it.
Open windows are now pinned to the local day they were computed on, so a key without
that stamp can only have been filled after its window closed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import tokdash.api as api


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    api._clear_cache()
    yield
    api._clear_cache()


def _days_ago(days: int) -> str:
    return _date_days_ago(days).isoformat()


def _date_days_ago(days: int):
    return (datetime.now().astimezone() - timedelta(days=days)).date()


def _freeze_today(monkeypatch, days_ago: int) -> None:
    """Pin the module's one clock, which both the stamp and the open/closed test read."""
    frozen = _date_days_ago(days_ago)
    monkeypatch.setattr(api, "_local_today", lambda: frozen)


def _client() -> TestClient:
    return TestClient(api.app)


def test_a_day_viewed_as_today_is_recomputed_when_it_becomes_yesterday(monkeypatch):
    day = _days_ago(1)
    computed: list[str] = []

    def fake_usage(period, date_from, date_to):
        computed.append(date_to)
        return {"total_tokens": len(computed), "marker": f"compute-{len(computed)}"}

    monkeypatch.setattr(api, "compute_usage_with_comparison", fake_usage)

    # Day D, mid-afternoon: the dashboard's default view caches a partial day.
    _freeze_today(monkeypatch, 1)
    with _client() as client:
        first = client.get(f"/api/usage?date_from={day}&date_to={day}").json()
    assert first["marker"] == "compute-1"

    # Day D+1: the Yesterday button asks for the same pair. D is closed now, so the
    # partial snapshot must not be reused.
    _freeze_today(monkeypatch, 0)
    with _client() as client:
        second = client.get(f"/api/usage?date_from={day}&date_to={day}").json()
    assert second["marker"] == "compute-2"
    assert second["response_cache"]["served_from_cache"] is False


def test_a_closed_range_is_cached_after_it_closes(monkeypatch):
    day = _days_ago(3)
    calls: list[tuple] = []

    def fake_usage(period, date_from, date_to):
        calls.append((date_from, date_to))
        return {"total_tokens": 7}

    monkeypatch.setattr(api, "compute_usage_with_comparison", fake_usage)

    with _client() as client:
        client.get(f"/api/usage?date_from={day}&date_to={day}")
        again = client.get(f"/api/usage?date_from={day}&date_to={day}").json()

    assert len(calls) == 1, "a settled past range should still be served from cache"
    assert again["response_cache"]["served_from_cache"] is True


def test_todays_range_still_serves_and_revalidates_within_the_day(monkeypatch):
    today = _days_ago(0)
    calls: list[tuple] = []

    def fake_usage(period, date_from, date_to):
        calls.append((date_from, date_to))
        return {"total_tokens": 1}

    monkeypatch.setattr(api, "compute_usage_with_comparison", fake_usage)

    with _client() as client:
        client.get(f"/api/usage?date_from={today}&date_to={today}")
        second = client.get(f"/api/usage?date_from={today}&date_to={today}").json()

    assert len(calls) == 1
    assert second["response_cache"]["served_from_cache"] is True


def test_sessions_and_active_time_keys_follow_the_same_rule(monkeypatch):
    day = _days_ago(1)
    _freeze_today(monkeypatch, 1)
    open_session = api._session_response_cache_key("codex", "today", day, day, None)
    open_active = api._active_time_cache_key("today", day, day, None)

    _freeze_today(monkeypatch, 0)
    closed_session = api._session_response_cache_key("codex", "today", day, day, None)
    closed_active = api._active_time_cache_key("today", day, day, None)

    assert open_session != closed_session
    assert open_active != closed_active


def test_a_window_ending_today_or_later_is_open():
    assert api._usage_window_is_open(_days_ago(6), _days_ago(0))
    assert api._usage_window_is_open(_days_ago(0), _days_ago(-5))
    assert api._usage_window_is_open(None, None), "a period-only query tracks the clock"
    assert not api._usage_window_is_open(_days_ago(7), _days_ago(1))


def test_a_malformed_end_date_is_treated_as_open():
    """An unparseable date may only cost a recompute, never serve a partial window."""
    assert api._usage_window_is_open("2026-01-01", "not-a-date")


def test_a_past_year_of_stats_is_cached_but_the_current_year_is_day_scoped(monkeypatch):
    calls: list = []

    def fake_stats(year):
        calls.append(year)
        return {"total_tokens": 0, "year": year}

    monkeypatch.setattr(api, "compute_stats", fake_stats)
    last_year = datetime.now().astimezone().year - 1

    with _client() as client:
        client.get(f"/api/stats?year={last_year}")
        client.get(f"/api/stats?year={last_year}")
    assert calls == [last_year]

    _freeze_today(monkeypatch, 1)
    with _client() as client:
        client.get("/api/stats")
    _freeze_today(monkeypatch, 0)
    with _client() as client:
        client.get("/api/stats")
    assert calls == [last_year, None, None], "the rolling window must recompute the next day"


def test_an_unpadded_past_date_is_closed(monkeypatch):
    """``strptime`` accepts an unpadded ``2026-9-1``, which sorts AFTER ``2026-10-30``
    as a string. Comparing the raw strings would call a settled September window open
    all through October, recomputing it on every request."""
    monkeypatch.setattr(api, "_local_today", lambda: date(2026, 10, 30))
    assert not api._usage_window_is_open("2026-9-1", "2026-9-1")
    assert not api._usage_window_is_open("2026-09-01", "2026-09-01")
    # A padded date on the same day still reads as open, so the padding is all that changed.
    assert api._usage_window_is_open("2026-10-30", "2026-11-2")


# The dashboard's quick ranges all resolve to a date pair, and several of them produce a
# pair that was ALREADY requested on an earlier day while its last day was still running:
# "Last week" repeats the span a "Last 7 days" view covered, "Last month" repeats the
# "This month" pair as viewed on the final day of that month, "Last year" repeats
# "This year" as viewed on 31 December, and the custom picker can re-select any of them.
# Each is the same defect as the Yesterday button, so each must recompute once closed.
@pytest.mark.parametrize(
    "label, viewed_days_ago, start_days_ago",
    [
        ("yesterday after today", 1, 1),
        ("last week after last 7 days", 1, 7),
        ("two weeks", 2, 15),
        ("last month after this month", 2, 32),
        ("last year after this year", 3, 370),
    ],
)
def test_a_range_viewed_on_its_final_day_is_recomputed_once_closed(
    monkeypatch, label, viewed_days_ago, start_days_ago
):
    date_from = _days_ago(start_days_ago)
    date_to = _days_ago(viewed_days_ago)
    computed: list[str] = []

    def fake_usage(period, df, dt):
        computed.append(f"compute-{len(computed) + 1}")
        return {"marker": computed[-1]}

    monkeypatch.setattr(api, "compute_usage_with_comparison", fake_usage)

    # Viewed while the last day of the range was still accruing usage.
    _freeze_today(monkeypatch, viewed_days_ago)
    with _client() as client:
        first = client.get(f"/api/usage?date_from={date_from}&date_to={date_to}").json()
    assert first["marker"] == "compute-1", label

    # The same pair requested after the range closed must not reuse that partial view.
    _freeze_today(monkeypatch, 0)
    with _client() as client:
        second = client.get(f"/api/usage?date_from={date_from}&date_to={date_to}").json()
    assert second["marker"] == "compute-2", label
    assert second["response_cache"]["served_from_cache"] is False, label


def test_a_rolling_range_keeps_its_own_key_per_day(monkeypatch):
    """"Last 7 days" covers a different pair each day, so the two must not share a key."""
    today_pair = (_days_ago(6), _days_ago(0))
    yesterday_pair = (_days_ago(7), _days_ago(1))
    assert api._window_cache_key("usage_today", *today_pair) != api._window_cache_key(
        "usage_today", *yesterday_pair
    )


def test_a_multi_day_range_ending_today_is_open(monkeypatch):
    _freeze_today(monkeypatch, 0)
    assert api._usage_window_is_open(_days_ago(27), _days_ago(0))
    assert api._usage_window_is_open(_days_ago(364), _days_ago(0))
    assert not api._usage_window_is_open(_days_ago(27), _days_ago(1))
