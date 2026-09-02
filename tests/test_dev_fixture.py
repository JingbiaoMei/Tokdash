from __future__ import annotations

import asyncio
from contextlib import contextmanager

from tokdash import api, cli
from tokdash.dev_fixtures import dense_usage, fixture_year_days


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
