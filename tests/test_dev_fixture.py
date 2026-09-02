from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from tokdash import api, cli
from tokdash.compute import parse_entries_json
from tokdash.dev_fixtures import (
    SESSION_LABELS,
    TOOL_SPECS,
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


def test_dense_fixture_insights_refuses_instead_of_serving_real_history():
    """/api/insights is the external report endpoint added by #59."""
    with dense_fixture():
        with pytest.raises(HTTPException) as refused:
            api.get_insights(period="year")

    assert refused.value.status_code == 409
    assert "not synthesized" in str(refused.value.detail)


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
