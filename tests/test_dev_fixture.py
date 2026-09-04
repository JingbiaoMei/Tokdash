from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from tokdash import api, cli
from tokdash.compute import parse_entries_json
from tokdash.compute import resolve_period
from tokdash.insights import DEFAULT_FACETS
from tokdash.dev_fixtures import (
    SESSION_LABELS,
    TOOL_SPECS,
    dense_openclaw,
    dense_usage,
    fixture_year_days,
)
from tokdash.sessions import SESSION_TOOLS
from tokdash.sources.openclaw import get_session_usage
from tokdash.usage_store import model_cost_rank_key, model_rank_key


@contextmanager
def dense_fixture():
    had_value = hasattr(api.app.state, "dev_fixture")
    previous = getattr(api.app.state, "dev_fixture", "")
    had_seed = hasattr(api.app.state, "dev_fixture_seed")
    previous_seed = getattr(api.app.state, "dev_fixture_seed", 0)
    api.app.state.dev_fixture = "dense"
    api.app.state.dev_fixture_seed = 74_019_130
    try:
        yield
    finally:
        if had_value:
            api.app.state.dev_fixture = previous
        else:
            del api.app.state.dev_fixture
        if had_seed:
            api.app.state.dev_fixture_seed = previous_seed
        else:
            del api.app.state.dev_fixture_seed


def test_dense_fixture_parser_is_explicit_and_off_by_default():
    default = cli.build_parser("tokdash").parse_args(["serve"])
    fixture = cli.build_parser("tokdash").parse_args(
        ["serve", "--dev-fixture", "dense", "--dev-seed", "17"]
    )

    assert default.dev_fixture is None
    assert default.dev_seed is None
    assert fixture.dev_fixture == "dense"
    assert fixture.dev_seed == 17


def test_dense_fixture_overview_is_crowded_and_consistent():
    with dense_fixture():
        payload = api.get_usage(period="week")

    assert len(payload["by_tool"]) >= 10
    assert len(payload["apps"]) >= 10
    assert len(payload["combined_models"]) >= 12
    assert payload["total_tokens"] == sum(
        row["tokens"] for row in payload["by_tool"].values()
    )
    assert payload["range"]["days"] == 7
    assert payload["response_cache"]["status"] == "fixture"
    assert payload["fixture"] == {"name": "dense", "seed": 74_019_130}


def test_dense_fixture_seed_is_stable_and_changes_the_sample():
    range_info = api.resolve_period("week")

    first = dense_usage(range_info, seed=17)
    repeated = dense_usage(range_info, seed=17)
    different = dense_usage(range_info, seed=18)

    assert first["total_tokens"] == repeated["total_tokens"]
    assert first["by_tool"] == repeated["by_tool"]
    assert first["total_tokens"] != different["total_tokens"]


def test_dense_fixture_sessions_and_details_cover_overflow_states():
    with dense_fixture():
        sessions = api.get_sessions(
            tool="codex", period="week", include_review_sessions=True
        )
        detail = api.get_session(
            tool="codex", session_id=sessions["sessions"][0]["session_id"]
        )
        active = api.get_active_time(period="week")

    assert len(sessions["sessions"]) == 18
    assert any(len(row["project"]) > 40 for row in sessions["sessions"])
    assert len(detail["turns"]) == 48
    assert len(active["by_tool"]) >= 15
    assert all(row["active_ms"] > 0 for row in active["by_tool"].values())


def test_dense_fixture_active_time_scales_with_the_window_and_stays_inside_it():
    with dense_fixture():
        payloads = {
            period: api.get_active_time(period=period)
            for period in ("today", "week", "year")
        }

    # Read after the calls: an elapsed-time ceiling only ever grows.
    now_local = datetime.now().astimezone()
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_today_ms = int((now_local - midnight).total_seconds() * 1000)

    for period, payload in payloads.items():
        # Production merges intervals clipped to [since_ms, until_ms), so the
        # union cannot outrun the window, cannot exceed the additive agent time,
        # and cannot fall below the busiest single tool.
        assert payload["active_ms"] <= payload["range"]["days"] * 86_400_000, period
        assert payload["active_ms"] <= payload["active_ms_sum"], period
        assert payload["active_ms"] >= max(
            row["active_ms"] for row in payload["by_tool"].values()
        ), period

    assert payloads["today"]["active_ms"] <= elapsed_today_ms
    # One frozen figure for every range would make a range bug in the Overview
    # card indistinguishable from the fixture working.
    assert (
        payloads["today"]["active_ms"]
        < payloads["week"]["active_ms"]
        < payloads["year"]["active_ms"]
    )


def test_dense_fixture_active_time_answers_the_review_toggle():
    with dense_fixture():
        included = api.get_active_time(period="week", include_review_sessions=True)
        excluded = api.get_active_time(period="week", include_review_sessions=False)

    # The payload has to describe the request that produced it: the dashboard
    # sends this flag whenever its toggle is set.
    assert included["include_review_sessions"] is True
    assert excluded["include_review_sessions"] is False
    # Review sessions are Codex-only, and dropping them drops their intervals.
    codex_on = included["by_tool"]["codex"]
    codex_off = excluded["by_tool"]["codex"]
    assert codex_off["active_ms"] < codex_on["active_ms"]
    assert codex_off["session_count"] < codex_on["session_count"]
    assert excluded["active_ms"] < included["active_ms"]


def test_dense_fixture_active_time_ignores_a_window_that_has_not_happened():
    today = datetime.now().astimezone().date()
    with dense_fixture():
        payload = api.get_active_time(
            date_from=(today - timedelta(days=1)).isoformat(),
            date_to=(today + timedelta(days=120)).isoformat(),
        )

    # 122 nominal days, two of which have happened. Active time is measured from
    # logged events, and the rest of that window has not produced any.
    assert payload["range"]["days"] > 100
    assert payload["active_ms"] <= 2 * 86_400_000


@pytest.mark.parametrize(
    "days_before, days_after",
    [(29, 0), (1, 120)],  # a closed past window, and one running into the future
)
def test_dense_fixture_openclaw_grid_stays_inside_the_requested_window(
    days_before, days_after
):
    today = datetime.now().astimezone().date()
    # Resolved once and passed in: a today-anchored window moves as the test runs.
    range_info = resolve_period(
        "custom",
        (today - timedelta(days=days_before)).isoformat(),
        (today + timedelta(days=days_after)).isoformat(),
    )
    payload = dense_openclaw(range_info, seed=17)
    dates = [day["date"] for day in payload["contributions"]]

    assert dates == sorted(dates)
    assert len(dates) == len(set(dates))
    # Anchoring the walk-back on today instead of on `from` put the whole grid
    # months before the window whenever `to` ran past today.
    assert range_info["from"] <= dates[0]
    assert dates[-1] <= min(range_info["to"], today.isoformat())
    assert payload["total_tokens"] == sum(
        day["totals"]["tokens"] for day in payload["contributions"]
    )


def test_dense_fixture_stats_fill_a_leap_year_and_include_multi_source_days():
    with dense_fixture():
        payload = api.get_stats(year=2024)

    assert len(payload["contributions"]) == fixture_year_days(2024)
    assert len(payload["contributions"]) == 366
    assert max(len(day["sources"]) for day in payload["contributions"]) == 5
    assert payload["meta"] == {
        "source": "dev-fixture",
        "synthetic": True,
        "seed": 74_019_130,
    }


def test_dense_fixture_quota_has_multiple_providers_and_chart_series():
    with dense_fixture():
        quota = api.get_quota()
        history = api.get_quota_history(granularity="hour")
        refreshed = api.refresh_quota()

    assert len(quota["providers"]) == 7
    assert all(
        len(provider["buckets"]) == 2 for provider in quota["providers"].values()
    )
    assert len(history["series"]) == 8
    assert all(len(series["points"]) == 72 for series in history["series"])
    assert refreshed == {
        "snapshots": 14,
        "inserted": 0,
        "fixture": "dense",
        "seed": 74_019_130,
    }


def test_dense_fixture_rejects_all_mutating_requests():
    request = type("FixtureRequest", (), {"method": "PUT", "app": api.app})()

    async def unexpected_handler(_request):
        raise AssertionError("fixture write reached its route handler")

    with dense_fixture():
        response = asyncio.run(api._write_guard(request, unexpected_handler))

    assert response.status_code == 409


def test_serve_fixture_skips_real_background_daemons(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        cli, "_start_usage_db_sync_daemon", lambda: calls.append("usage")
    )
    monkeypatch.setattr(cli, "_start_quota_poll_daemon", lambda: calls.append("quota"))
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *args, **kwargs: calls.append(str(api.app.state.dev_fixture)),
    )
    monkeypatch.setattr(cli, "_has_display", lambda: False)

    cli.serve(
        "127.0.0.1",
        55423,
        "warning",
        open_browser=False,
        dev_fixture="dense",
        dev_seed=17,
    )

    assert calls == ["dense"]
    assert not hasattr(api.app.state, "dev_fixture")
    assert not hasattr(api.app.state, "dev_fixture_seed")


# --- Follow-up coverage: the gaps between what the docs promise and what the
# --- code enforces (PR #63 review).


@pytest.mark.parametrize(
    "argv",
    [
        ["export", "--dev-fixture", "dense"],
        ["export", "--dev-seed", "5"],
        ["quota", "--dev-fixture", "dense", "--dev-seed", "5"],
        ["db", "--dev-fixture", "dense"],
        ["version", "--dev-fixture", "dense"],
    ],
)
def test_dev_fixture_flags_are_refused_outside_serve(argv, capsys):
    """Only serve honors these, so only serve may accept them.

    They sit on the flat top-level parser, so every one of these used to parse
    cleanly and then run against the user's REAL data with no warning.
    """
    with pytest.raises(SystemExit):
        cli.cli(argv)

    assert "only supported by `serve`" in capsys.readouterr().err


def test_dev_seed_still_requires_a_fixture_on_serve(capsys):
    with pytest.raises(SystemExit):
        cli.cli(["serve", "--dev-seed", "5"])

    assert "--dev-seed requires --dev-fixture" in capsys.readouterr().err


def test_dense_fixture_session_tools_match_production():
    """SESSION_LABELS is the real session-tool set, not an arbitrary subset.

    Active Time covers 17 tools and Overview covers 12 because usage sources and
    session tools genuinely differ (cursor and gemini_cli report tokens but ship
    no transcripts). Pinning the session half here keeps that difference
    deliberate instead of drifting.
    """
    assert set(SESSION_LABELS) == set(SESSION_TOOLS)
    assert {tool for tool, _label, _models in TOOL_SPECS} - set(SESSION_LABELS) == {
        "cursor",
        "gemini_cli",
    }


def test_dense_fixture_model_arrays_use_the_shared_rank_keys():
    """The fixture must not re-implement the #61 ordering contract."""
    payload = dense_usage(api.resolve_period("year"), seed=17)

    for key in ("combined_models", "coding_models"):
        rows = payload[key]
        assert rows == sorted(rows, key=model_rank_key), key

    assert payload["top_models"] == payload["combined_models"][:5]
    assert (
        payload["top_models_by_cost"]
        == sorted(payload["combined_models"], key=model_cost_rank_key)[:5]
    )
    for app_row in payload["apps"].values():
        assert app_row["models"] == sorted(app_row["models"], key=model_rank_key)


def test_dense_fixture_surfaces_session_error_states():
    """404 and 400 are UI states the fixture exists to exercise."""
    with dense_fixture():
        with pytest.raises(HTTPException) as unknown_session:
            api.get_session(tool="codex", session_id="no-such-session")
        with pytest.raises(HTTPException) as unknown_tool:
            api.get_sessions(tool="not-a-real-tool", period="week")
        with pytest.raises(HTTPException) as unknown_detail_tool:
            api.get_session(tool="not-a-real-tool", session_id="x")

    assert unknown_session.value.status_code == 404
    assert unknown_tool.value.status_code == 400
    assert "Unsupported session tool" in str(unknown_tool.value.detail)
    assert unknown_detail_tool.value.status_code == 400


def test_dense_fixture_stats_never_fabricate_future_days():
    """`compute_stats` builds days from real usage, so it cannot emit the future."""
    today = datetime.now().astimezone().date()

    with dense_fixture():
        current_year = api.get_stats(year=today.year)
        future_year = api.get_stats(year=today.year + 4)
        rolling = api.get_stats()

    assert current_year["contributions"][-1]["date"] == today.isoformat()
    assert all(day["date"] <= today.isoformat() for day in current_year["contributions"])
    assert future_year["contributions"] == []
    assert all(day["date"] <= today.isoformat() for day in rolling["contributions"])


def test_dense_fixture_review_sessions_follow_the_configured_default(monkeypatch):
    """An unset flag resolves through the real default, and the payload says so."""
    monkeypatch.delenv("TOKDASH_INCLUDE_CODEX_GUARDIAN", raising=False)
    with dense_fixture():
        off = api.get_sessions(tool="codex", period="week")

    assert off["include_review_sessions"] is False
    assert not any(row["is_review_session"] for row in off["sessions"])

    monkeypatch.setenv("TOKDASH_INCLUDE_CODEX_GUARDIAN", "1")
    with dense_fixture():
        on = api.get_sessions(tool="codex", period="week")

    assert on["include_review_sessions"] is True
    assert any(row["is_review_session"] for row in on["sessions"])


def test_dense_fixture_insights_answers_without_touching_real_history(monkeypatch):
    """/api/insights is the endpoint the Report tab reads (facets added in #59).

    Fixture mode used to answer this route with a 409, which kept real history
    out but left the widest contract in the app unexercised. It now answers from
    the fixture, so the invariant is restated as a positive: the production
    aggregation must never run while a fixture is active.
    """

    def never(*_args, **_kwargs):
        raise AssertionError("compute_insights ran while a fixture was active")

    monkeypatch.setattr(api, "compute_insights", never)
    with dense_fixture():
        payload = api.get_insights(period="month")

    assert payload["fixture"] == {"name": "dense", "seed": 74_019_130}
    assert payload["facets"] == list(DEFAULT_FACETS)
    assert payload["totals"]["tokens"] > 0
    assert payload["range"]["period_resolved"] == "month"
    assert payload["range"]["to"] <= datetime.now().astimezone().date().isoformat()


def test_dense_fixture_insights_accepts_an_explicit_short_range():
    """`/api/insights` takes date_from/date_to directly, which is a shorter window
    than any the Report tab can ask for. A one-day window used to 500 for about
    one seed in eight, because the day draw could leave no day to put the tokens
    on; the route answering is the whole assertion here.
    """
    with dense_fixture():
        payload = api.get_insights(
            period="all",
            date_from="2026-05-05",
            date_to="2026-05-05",
            facets="daily,streaks,firsts,projects",
        )

    assert payload["range"]["from"] == "2026-05-05"
    assert payload["range"]["days"] == 1
    assert len(payload["daily"]) == 1
    assert payload["totals"]["tokens"] > 0
    assert payload["streaks"]["active_days"] == 1


def test_dense_fixture_insights_honours_facet_selection_and_name_anonymisation():
    with dense_fixture():
        picked = api.get_insights(period="week", facets="streaks,firsts")
        anonymous = api.get_insights(period="week", facets="projects", include_project_names=False)

    assert picked["facets"] == ["streaks", "firsts"]
    assert set(picked) >= {"streaks", "firsts"}
    assert "hourly" not in picked and "projects" not in picked

    assert anonymous["projects"]["names_included"] is False
    names = [row["project"] for row in anonymous["projects"]["projects"]]
    assert names and all(name.startswith("project-") for name in names)
    assert anonymous["projects"]["projects"][0]["tokens"] >= anonymous["projects"]["projects"][-1]["tokens"]

    with pytest.raises(HTTPException) as refused:
        api.get_insights(period="week", facets="nope")
    assert refused.value.status_code == 400


def test_dense_fixture_project_cost_follows_its_token_share():
    """The ranked rows hold about 86% of the window's tokens, because production
    always has a gap the project facet cannot see. Handing them 100% of its cost
    put a sixth too much money on the podium's "Top project" tile, which is the
    figure a fixture screenshot exists to show.
    """
    with dense_fixture():
        payload = api.get_insights(period="month", facets="projects")

    projects = payload["projects"]
    rows = projects["projects"]
    unattributed = projects["unattributed"]
    total_cost = payload["totals"]["cost"]
    total_tokens = payload["totals"]["tokens"]
    assert total_cost > 0 and total_tokens > 0

    row_cost = sum(row["cost"] for row in rows)
    row_tokens = sum(row["tokens"] for row in rows)
    # Same three-way split as the tokens: the rows, the facet's own unattributed
    # bucket, and the share the facet never sees at all.
    assert row_cost / total_cost == pytest.approx(row_tokens / total_tokens, abs=0.01)
    assert unattributed["cost"] > 0, "the unattributed bucket carries tokens, so it carries cost"
    assert row_cost + unattributed["cost"] < total_cost, "the invisible gap keeps its share"


def test_dense_fixture_tools_route_matches_the_real_producer_shape():
    real = parse_entries_json(
        {
            "entries": [
                {
                    "source": "codex",
                    "model": "gpt-5.6-sol",
                    "provider": "openai",
                    "input": 10,
                    "output": 5,
                    "cacheRead": 3,
                }
            ]
        }
    )

    with dense_fixture():
        fixture = api.get_tools(period="week")

    assert set(fixture) == set(real) | {"source_errors", "period", "range", "timestamp"}
    assert set(fixture["all_models"][0]) == set(real["all_models"][0])
    assert set(next(iter(fixture["apps"].values()))) == set(
        next(iter(real["apps"].values()))
    )
    assert fixture["total_tokens"] == sum(
        row["tokens"] for row in fixture["all_models"]
    )


def test_dense_fixture_openclaw_route_matches_the_real_producer_shape():
    real = get_session_usage(
        [],
        since_date=datetime(2020, 1, 1, tzinfo=timezone.utc),
        until_date=datetime.now(timezone.utc),
    )

    with dense_fixture():
        fixture = api.get_openclaw(period="week")

    assert set(fixture) == set(real) | {"period", "range", "timestamp"}
    # `models` is a mapping keyed by model name here -- unlike every other model
    # list in the API -- and a fixture that returned a list would hide that.
    assert isinstance(fixture["models"], dict)
    assert all("name" not in row for row in fixture["models"].values())
    assert fixture["total_tokens"] == sum(
        row["tokens"] for row in fixture["models"].values()
    )


def test_dense_fixture_openclaw_totals_match_its_own_contributions():
    with dense_fixture():
        for period in ("today", "week", "month"):
            payload = api.get_openclaw(period=period)
            overview = api.get_usage(period=period)
            days = payload["contributions"]

            # The header, the day grid and the Overview slice are three views of
            # one window. The real producer folds a single window-filtered pass
            # into all of them -- and the store-backed path reads them out of one
            # snapshot -- so a consumer may take a total from one and a chart
            # from another.
            assert payload["total_tokens"] == sum(
                day["totals"]["tokens"] for day in days
            ), period
            assert (
                payload["total_tokens"] == overview["by_tool"]["openclaw"]["tokens"]
            ), period
            assert payload["total_messages"] == sum(
                day["totals"]["messages"] for day in days
            ), period
            assert payload["total_cost"] == pytest.approx(
                sum(day["totals"]["cost"] for day in days), abs=1e-4
            ), period

            assert days, period
            for day in days:
                breakdown = day["tokenBreakdown"]
                assert (
                    breakdown["input"] + breakdown["output"] + breakdown["cacheRead"]
                    == day["totals"]["tokens"]
                )
                day_messages = sum(row["messages"] for row in day["sources"])
                assert day_messages == day["totals"]["messages"]


def test_dense_fixture_pricing_db_never_discloses_the_user_override():
    with dense_fixture():
        payload = api.get_pricing_db()

    assert payload["source"] == "dev-fixture"
    assert str(api._pricing_override_path()) not in payload["path"]
    # The packaged baseline is part of the install, not user data.
    assert payload["data"]
    assert payload["baseline_path"] == str(api.PRICING_DB_PATH)


def test_dense_fixture_codex_routes_do_not_read_rollout_files(monkeypatch):
    """The /api/codex/* pair reads transcripts off disk in the real path."""

    def explode(*_args, **_kwargs):
        raise AssertionError("fixture mode reached the real Codex reader")

    monkeypatch.setattr(api, "get_codex_sessions_data", explode)
    monkeypatch.setattr(api, "get_codex_session_detail", explode)

    with dense_fixture():
        listing = api.get_codex_sessions(period="week", include_review_sessions=True)
        detail = api.get_codex_session(
            session_id=listing["sessions"][0]["session_id"]
        )

    assert listing["tool"] == "codex"
    assert len(listing["sessions"]) == 18
    assert detail["turns"]
