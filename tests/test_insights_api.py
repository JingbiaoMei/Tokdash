from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

import tokdash.compute as compute
import tokdash.insights as insights


# --------------------------------------------------------------------------
# D1 -- period resolution, aliases, and the observable `range` block
# --------------------------------------------------------------------------


def test_short_aliases_resolve_to_their_named_period():
    # "7d" is the token that shipped a century of data labelled as one week.
    assert compute.period_to_days("7d") == 7
    assert compute.period_to_days("2w") == 14
    assert compute.period_to_days("1y") == 365
    assert compute.period_to_days("3m") == 90
    # Case and surrounding whitespace are not a different period.
    assert compute.period_to_days(" 7D ") == 7


def test_unknown_period_still_widens_but_is_flagged():
    # The permissive fallback is retained deliberately: a 400 would break
    # callers that rely on it today. What changes is that it is now visible.
    assert compute.period_to_days("bogus") > 365 * 50
    assert compute.period_is_recognized("bogus") is False
    assert compute.period_is_recognized("week") is True
    assert compute.period_is_recognized("7d") is True
    assert compute.period_is_recognized("30") is True


def test_range_block_reports_resolution_not_the_caller_token():
    block = compute.resolve_period("7d")
    assert block["period_requested"] == "7d"
    assert block["period_resolved"] == "week"
    assert block["recognized"] is True
    assert block["days"] == 7

    unknown = compute.resolve_period("bogus")
    assert unknown["period_requested"] == "bogus"
    assert unknown["period_resolved"] == "all"
    assert unknown["recognized"] is False
    # The substitution is the whole point: a consumer can see it happened.
    assert unknown["days"] > 365 * 50


def test_resolved_period_is_a_value_a_caller_could_send_back():
    for period in ("today", "week", "7d", "2w", "month", "year", "all", "30"):
        resolved = compute.resolve_period(period)["period_resolved"]
        assert compute.period_to_days(resolved) == compute.period_to_days(period)


@pytest.mark.parametrize("period", ["today", "week", "7d", "month", "year", "30"])
def test_range_days_matches_the_inclusive_from_to_span(period):
    # `days` must never disagree with `from`/`to` -- a mismatch would be the
    # same quiet lie the block exists to remove. "month" is the sharp case: a
    # calendar month on the 1st spans one day, not the 30 its mapping implies.
    block = compute.resolve_period(period)
    start = date.fromisoformat(block["from"])
    end = date.fromisoformat(block["to"])
    assert block["days"] == (end - start).days + 1


def test_month_range_starts_on_the_first():
    block = compute.resolve_period("month")
    assert date.fromisoformat(block["from"]).day == 1


def test_custom_date_range_is_reported_as_custom():
    block = compute.resolve_period("today", "2026-01-01", "2026-01-31")
    assert block["period_resolved"] == "custom"
    assert block["from"] == "2026-01-01"
    assert block["to"] == "2026-01-31"
    assert block["days"] == 31


# --------------------------------------------------------------------------
# D3 -- streaks
# --------------------------------------------------------------------------


def _contribs(days: list[date]) -> list[dict]:
    return [{"date": day.isoformat()} for day in days]


def test_streaks_count_consecutive_days():
    today = datetime.now().astimezone().date()
    run = [today - timedelta(days=offset) for offset in range(4, -1, -1)]
    current, longest = compute.contribution_streaks(_contribs(run))
    assert current == 5
    assert longest == 5


def test_current_streak_survives_a_day_still_in_progress():
    # A streak that ran to yesterday is still current; today may simply not
    # have started yet. Treating that as a break would report 0 every morning.
    today = datetime.now().astimezone().date()
    run = [today - timedelta(days=offset) for offset in range(3, 0, -1)]
    current, longest = compute.contribution_streaks(_contribs(run))
    assert current == 3
    assert longest == 3


def test_lapsed_streak_reports_zero_but_longest_is_kept():
    today = datetime.now().astimezone().date()
    old = [today - timedelta(days=offset) for offset in (10, 9, 8)]
    current, longest = compute.contribution_streaks(_contribs(old))
    assert current == 0
    assert longest == 3


def test_streaks_ignore_gaps_and_unparseable_dates():
    today = datetime.now().astimezone().date()
    days = _contribs([today - timedelta(days=offset) for offset in (7, 6, 5, 2, 1)])
    days.append({"date": "not-a-date"})
    current, longest = compute.contribution_streaks(days)
    assert longest == 3
    assert current == 2


def test_streaks_on_empty_input():
    assert compute.contribution_streaks([]) == (0, 0)


# --------------------------------------------------------------------------
# D5 -- intensity
# --------------------------------------------------------------------------


def test_intensity_spreads_active_days_across_four_ranks():
    days = [{"totals": {"tokens": n}} for n in range(1, 101)]
    compute.assign_contribution_intensity(days)
    ranks = {day["intensity"] for day in days}
    assert ranks == {1, 2, 3, 4}
    # Rank order has to follow volume order.
    assert days[0]["intensity"] == 1
    assert days[-1]["intensity"] == 4


def test_intensity_is_rank_based_not_threshold_based():
    # One enormous day must not flatten every ordinary day into the bottom
    # bucket, which is what an absolute scale would do.
    days = [{"totals": {"tokens": n}} for n in (1, 2, 3, 4, 10_000_000)]
    compute.assign_contribution_intensity(days)
    assert [day["intensity"] for day in days] == [1, 2, 3, 4, 4]


def test_intensity_zero_only_for_days_without_tokens():
    days = [{"totals": {"tokens": 0}}, {"totals": {"tokens": 5}}]
    compute.assign_contribution_intensity(days)
    assert days[0]["intensity"] == 0
    assert days[1]["intensity"] >= 1


def test_intensity_handles_an_all_empty_window():
    days = [{"totals": {"tokens": 0}}, {"totals": {"tokens": 0}}]
    compute.assign_contribution_intensity(days)
    assert [day["intensity"] for day in days] == [0, 0]


# --------------------------------------------------------------------------
# Facet selection
# --------------------------------------------------------------------------


def test_facets_default_to_the_single_scan_set():
    assert insights.parse_facets(None) == insights.DEFAULT_FACETS
    assert insights.parse_facets("  ") == insights.DEFAULT_FACETS
    # The two costly facets are opt-in.
    assert "projects" not in insights.DEFAULT_FACETS
    assert "daily" not in insights.DEFAULT_FACETS


def test_facets_are_parsed_deduped_and_order_preserved():
    assert insights.parse_facets("streaks,hourly,streaks") == ("streaks", "hourly")
    assert insights.parse_facets(" Hourly , HEATMAP ") == ("hourly", "heatmap")


def test_unknown_facet_is_refused_not_dropped():
    # Silently ignoring it renders a blank section labelled as data -- the same
    # failure mode as an unrecognised period.
    with pytest.raises(insights.UnknownFacetError) as excinfo:
        insights.parse_facets("hourly,bogus")
    assert "bogus" in str(excinfo.value)


# --------------------------------------------------------------------------
# Folds
# --------------------------------------------------------------------------


def _row(day: str, hour: int, tokens: int, *, source="codex", model="m") -> dict:
    return {
        "day": day,
        "hour": hour,
        "source": source,
        "model": model,
        "provider": "p",
        "tokens": tokens,
        "cost": 1.0,
        "messages": 2,
        "entries": 1,
    }


def test_hourly_fold_is_dense_and_reports_the_night_share():
    rows = [_row("2026-01-05", 23, 300), _row("2026-01-05", 11, 700)]
    folded = insights._fold_hourly(rows)
    assert len(folded["buckets"]) == 24
    assert folded["peak_hour"] == 11
    # 300 of 1000 tokens land inside 22:00-02:00.
    assert folded["night_share"] == pytest.approx(0.3)


def test_hourly_night_share_is_none_without_data():
    folded = insights._fold_hourly([])
    assert folded["night_share"] is None
    assert folded["peak_hour"] is None


def test_heatmap_is_a_dense_seven_by_twenty_four_grid():
    # 2026-01-05 is a Monday.
    folded = insights._fold_heatmap([_row("2026-01-05", 9, 500)])
    assert len(folded["cells"]) == 168
    assert folded["max_tokens"] == 500
    monday_nine = next(
        cell for cell in folded["cells"] if cell["weekday"] == 0 and cell["hour"] == 9
    )
    assert monday_nine["tokens"] == 500


def test_weekday_fold_uses_monday_as_zero():
    folded = insights._fold_weekday([_row("2026-01-05", 9, 500)])
    assert folded["peak_weekday"] == 0
    assert folded["buckets"][0]["name"] == "Monday"


def test_daily_fold_carries_intensity():
    rows = [_row("2026-01-05", 9, 100), _row("2026-01-06", 9, 900)]
    folded = insights._fold_daily(rows)
    assert [entry["date"] for entry in folded] == ["2026-01-05", "2026-01-06"]
    assert folded[0]["intensity"] < folded[1]["intensity"]


def test_local_day_hour_rejects_unusable_timestamps():
    assert insights._local_day_hour(None) is None
    assert insights._local_day_hour(0) is None
    assert insights._local_day_hour("nonsense") is None
    assert insights._local_day_hour(1767979285610) is not None
