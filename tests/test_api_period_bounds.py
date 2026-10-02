"""#146: Unbounded numeric period values must not cause 500s or leak internal exception text.

Values outside [1, ALL_TIME_DAYS] must return uniform 400 with 'period out of range'
across all period-accepting endpoints, while unknown named strings fall back to all-time
with 200 OK per API contract. Compute-level period helpers clamp numeric values to
prevent OverflowError in date arithmetic.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tokdash import api
from tokdash import compute


@pytest.fixture
def client():
    return TestClient(api.app, raise_server_exceptions=False)


PERIOD_ENDPOINTS = [
    ("usage", {}),
    ("openclaw", {}),
    ("tools", {}),
    ("sessions", {"tool": "codex"}),
    ("codex/sessions", {}),
    ("active-time", {}),
    ("insights", {}),
]

BAD_PERIODS = [
    "9" * 400,
    "9999999",
    "9999999d",
    "0",
    "-5",
    "0d",
    "-7d",
    "+0",
    "-0",
]


@pytest.mark.parametrize("ep,extra_params", PERIOD_ENDPOINTS)
@pytest.mark.parametrize("bad_period", BAD_PERIODS)
def test_endpoints_reject_out_of_range_period_with_400(client, ep, extra_params, bad_period):
    params = {"period": bad_period, **extra_params}
    r = client.get(f"/api/{ep}", params=params)
    assert r.status_code == 400, f"/api/{ep}?period={bad_period[:20]} returned {r.status_code}: {r.text}"
    assert r.json() == {"detail": "period out of range"}
    assert "OverflowError" not in r.text
    assert "date value out of range" not in r.text
    assert "Python int too large to convert to C int" not in r.text


@pytest.mark.parametrize("ep,extra_params", PERIOD_ENDPOINTS)
@pytest.mark.parametrize(
    "good_period",
    ["today", "week", "month", "year", "all", "3days", "14days", "7d", "2w", "30", "365", "36500"],
)
def test_endpoints_accept_valid_periods(client, ep, extra_params, good_period):
    params = {"period": good_period, **extra_params}
    r = client.get(f"/api/{ep}", params=params)
    assert r.status_code == 200, f"/api/{ep}?period={good_period} returned {r.status_code}: {r.text}"


@pytest.mark.parametrize("ep,extra_params", PERIOD_ENDPOINTS)
@pytest.mark.parametrize("unknown_period", ["bogus", "zzz", "unknown_period_name"])
def test_endpoints_accept_unrecognized_named_periods_as_all_time(client, ep, extra_params, unknown_period):
    # API spec: unknown string tokens resolve to all-time with 200 OK (recognized=False in range block)
    params = {"period": unknown_period, **extra_params}
    r = client.get(f"/api/{ep}", params=params)
    assert r.status_code == 200, f"/api/{ep}?period={unknown_period} returned {r.status_code}: {r.text}"


def test_compute_period_to_days_clamping():
    assert compute.period_to_days("9" * 400) == compute.ALL_TIME_DAYS
    assert compute.period_to_days("9999999") == compute.ALL_TIME_DAYS
    assert compute.period_to_days("9999999d") == compute.ALL_TIME_DAYS
    assert compute.period_to_days("0") == 1
    assert compute.period_to_days("-5") == 1
    assert compute.period_to_days("30") == 30
    assert compute.period_to_days("7d") == 7


def test_compute_date_arithmetic_does_not_overflow_on_huge_input():
    args = compute.period_to_range_args("9" * 400)
    assert args[0] == "--since" and args[2] == "--until"

    since, until = compute._current_period_range("9" * 400)
    assert since < until

    prev_since, prev_until = compute.previous_period_range("9" * 400)
    assert prev_since < prev_until


def test_compute_period_is_recognized_bounds():
    assert compute.period_is_recognized("30") is True
    assert compute.period_is_recognized("7d") is True
    assert compute.period_is_recognized("week") is True
    assert compute.period_is_recognized("36500") is True
    assert compute.period_is_recognized("bogus") is False
    assert compute.period_is_recognized("9" * 400) is False
    assert compute.period_is_recognized("9999999") is False
    assert compute.period_is_recognized("0") is False
    assert compute.period_is_recognized("-5") is False


def test_validate_period_raises_value_error_only_on_out_of_range_numbers():
    for bad in ["9" * 400, "9999999", "9999999d", "0", "-5", "0d", "-7d"]:
        with pytest.raises(ValueError, match="period out of range"):
            compute.validate_period(bad)

    for good in ["today", "week", "month", "year", "all", "7d", "2w", "30", "365", "36500", "bogus", "zzz"]:
        compute.validate_period(good)
