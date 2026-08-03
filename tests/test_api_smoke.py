import os
import asyncio

import pytest


pytest.importorskip("fastapi")

import tokdash.api as api


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _static_middleware_cache_control(path: str) -> str:
    messages = []

    async def dummy_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    middleware = api.NoCacheMiddleware(dummy_app)
    asyncio.run(middleware({"type": "http", "path": path}, receive, send))
    response_start = next(message for message in messages if message["type"] == "http.response.start")
    headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in response_start["headers"]}
    return headers["cache-control"]


@pytest.fixture(autouse=True)
def _reset_api_cache(monkeypatch):
    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    api._clear_cache()
    with api._cache_guard:
        api._key_locks.clear()
    yield
    api._clear_cache()
    with api._cache_guard:
        api._key_locks.clear()


@pytest.fixture
def synthetic_api_data(monkeypatch):
    """Keep default API smoke tests hermetic and cheap.

    The real local-log walk is useful as an integration/stress check, but it can
    reparse large session histories and compete with the installed dashboard
    service. The local FastAPI/AnyIO stack deadlocks on synchronous route
    handlers under TestClient/ASGITransport, so these tests call handlers
    directly and keep ASGI/static middleware coverage narrow and explicit.
    """

    def fake_usage(period, date_from, date_to):
        return {
            "period": period,
            "date_from": date_from,
            "date_to": date_to,
            "total_tokens": 123,
            "total_messages": 4,
            "comparison": {"previous_total_tokens": 100},
            "openclaw_models": [],
            "coding_apps": [],
        }

    def fake_tools(period):
        return {"apps": [], "all_models": [], "period": period}

    def fake_openclaw(period):
        return {"models": [], "contributions": [], "period": period}

    def fake_stats(year):
        return {"contributions": [], "stats": {"year": year}}

    def fake_sessions(tool, period, date_from=None, date_to=None, include_review_sessions=None):
        return {
            "tool": tool.strip().lower(),
            "period": period,
            "date_from": date_from,
            "date_to": date_to,
            "include_review_sessions": include_review_sessions,
            "sessions": [{"session_id": "session-1"}],
            "latest_session": {"session_id": "session-1"},
        }

    def fake_session_detail(tool, session_id):
        return {"session": {"tool": tool, "session_id": session_id}, "turns": []}

    def fake_codex_sessions(period, include_review_sessions=None):
        return {
            "tool": "codex",
            "period": period,
            "include_review_sessions": include_review_sessions,
            "sessions": [{"session_id": "codex-session-1"}],
            "latest_session": {"session_id": "codex-session-1"},
        }

    def fake_codex_session_detail(session_id):
        return {"session": {"tool": "codex", "session_id": session_id}, "turns": []}

    def fake_activity_insights():
        return {
            "scope": {"tool": "codex", "local": True, "primary_only": True},
            "recorded_chats": {
                "value": 3,
                "coverage": {
                    "primary_files": 3,
                    "files_with_session_id": 3,
                    "legacy_unavailable_records": 0,
                },
            },
            "reasoning": {
                "most_used": {"effort": "xhigh", "count": 2, "share": 1.0},
                "distribution": [
                    {"effort": "xhigh", "count": 2, "share": 1.0}
                ],
                "coverage": {
                    "identified_turns": 2,
                    "known_effort_turns": 2,
                    "ambiguous_turns": 0,
                    "excluded_records": 0,
                },
            },
            "tools": {
                "total_calls": 8,
                "most_used": {"name": "exec", "count": 5, "share": 0.625},
                "distribution": [
                    {"name": "exec", "count": 5, "share": 0.625},
                    {"name": "apply_patch", "count": 3, "share": 0.375},
                ],
                "coverage": {
                    "named_calls": 8,
                    "ambiguous_name_calls": 0,
                    "excluded_records": 0,
                },
            },
            "timestamp": "2026-08-03T12:00:00+00:00",
        }

    monkeypatch.setattr(api, "compute_usage_with_comparison", fake_usage)
    monkeypatch.setattr(api, "get_tools_data", fake_tools)
    monkeypatch.setattr(api, "get_openclaw_data", fake_openclaw)
    monkeypatch.setattr(api, "compute_stats", fake_stats)
    monkeypatch.setattr(api, "get_sessions_data", fake_sessions)
    monkeypatch.setattr(api, "get_session_detail", fake_session_detail)
    monkeypatch.setattr(api, "get_codex_sessions_data", fake_codex_sessions)
    monkeypatch.setattr(api, "get_codex_session_detail", fake_codex_session_detail)
    monkeypatch.setattr(api, "get_codex_activity_insights", fake_activity_insights)


def test_api_endpoints_and_dashboard_smoke(synthetic_api_data):
    usage = api.get_usage(period="today")
    assert "total_tokens" in usage
    assert "total_messages" in usage
    assert "comparison" in usage
    assert "openclaw_models" in usage
    assert "coding_apps" in usage

    tools = api.get_tools(period="today")
    assert "apps" in tools
    assert "all_models" in tools

    for tool in ("codex", "claude", "opencode", "pi_agent"):
        sessions = api.get_sessions(tool=tool, period="today")
        assert "sessions" in sessions
        assert "latest_session" in sessions
        assert sessions.get("tool") == tool

        latest = sessions.get("latest_session")
        if latest and latest.get("session_id"):
            detail = api.get_session(tool=tool, session_id=latest["session_id"])
            assert "session" in detail
            assert "turns" in detail

    codex_sessions = api.get_codex_sessions(period="today")
    assert "sessions" in codex_sessions
    assert "latest_session" in codex_sessions
    assert api.get_codex_sessions(period="today", include_review_sessions=False)["include_review_sessions"] is False
    assert api.get_codex_sessions(period="today", include_review_sessions=True)["include_review_sessions"] is True
    assert (
        api.get_sessions(tool="codex", period="today", include_review_sessions=False)["include_review_sessions"]
        is False
    )
    assert api.get_sessions(tool="codex", period="today", include_review_sessions=True)["include_review_sessions"] is True

    latest_codex = codex_sessions.get("latest_session")
    if latest_codex and latest_codex.get("session_id"):
        codex_detail = api.get_codex_session(session_id=latest_codex["session_id"])
        assert "session" in codex_detail
        assert "turns" in codex_detail

    openclaw = api.get_openclaw(period="today")
    assert "models" in openclaw
    assert "contributions" in openclaw

    stats = api.get_stats()
    assert "contributions" in stats
    assert "stats" in stats

    stats_year = api.get_stats(year=2025)
    assert "contributions" in stats_year
    assert "stats" in stats_year

    activity = api.get_activity_insights()
    assert activity["scope"]["primary_only"] is True
    assert activity["recorded_chats"]["value"] == 3

    manifest = (api.STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8")
    assert "Tokdash" in manifest

    sw = api._render_service_worker("")
    assert "service worker" in sw.lower()
    assert "__TOKDASH_CACHE_NAME__" not in sw
    assert "__TOKDASH_BASE_PATH__" not in sw
    assert 'const CACHE_NAME = "tokdash-' in sw

    html = api._render_dashboard_html("")
    assert "Tokdash" in html
    assert "Sessions" in html
    assert 'data-tab="quota"' in html
    assert 'id="quota-content"' in html
    assert "/api/quota" in html
    assert "/api/quota/refresh" in html
    assert "/api/quota/consent" in html
    assert "resetCredits" in html
    assert "renderQuotaBucketGroups" in html
    assert "return 'paper';" in html
    assert "updateBadge" in html
    assert "initUpdateNotice" in html
    assert "tokdash update" in html
    assert "/api/update-check" in html
    assert "__TOKDASH_BASE_PATH__" not in html

    icon_path = api.STATIC_DIR / "icons" / "icon-192.png"
    assert icon_path.exists()
    assert "no-store" in _static_middleware_cache_control("/static/icons/icon-192.png")


def test_public_base_path_rendering():
    assert api._normalize_public_base_path("tokdash/") == "/tokdash"
    assert api._normalize_public_base_path("/") == ""

    html = api._render_dashboard_html("/tokdash")
    assert 'const configured = "/tokdash";' in html
    assert 'configured.startsWith("/")' in html

    manifest = api._render_manifest("/tokdash")
    assert '"start_url":"/tokdash/"' in manifest
    assert '"/tokdash/static/icons/icon-192.png"' in manifest

    sw = api._render_service_worker("/tokdash")
    assert 'const BASE_PATH = "/tokdash";' in sw
    assert "__TOKDASH_BASE_PATH__" not in sw


def test_dashboard_refresh_status_copy_and_auto_success_reset():
    html = api._render_dashboard_html("")

    assert "refreshFailed: 'Failed — retry'" in html
    assert "refreshFailed: '失败，请重试'" in html
    assert "Refresh browser" not in html
    assert "刷新浏览器" not in html
    assert "setRefreshUiState('idle');" in html


def test_api_custom_date_ranges_and_validation(synthetic_api_data):
    usage = api.get_usage(date_from="2026-04-08", date_to="2026-04-08")
    assert "comparison" in usage

    sessions = api.get_sessions(tool="codex", date_from="2026-04-08", date_to="2026-04-08")
    assert sessions["tool"] == "codex"

    with pytest.raises(api.HTTPException) as excinfo:
        api.get_usage(date_from="2026-04-08")
    assert excinfo.value.status_code == 400
    assert "required" in excinfo.value.detail

    with pytest.raises(api.HTTPException) as excinfo:
        api.get_usage(date_from="2026/04/08", date_to="2026-04-08")
    assert excinfo.value.status_code == 400
    assert "Invalid date format" in excinfo.value.detail

    with pytest.raises(api.HTTPException) as excinfo:
        api.get_usage(date_from="2026-04-09", date_to="2026-04-08")
    assert excinfo.value.status_code == 400
    assert "on or before" in excinfo.value.detail


def test_usage_refresh_param_forces_recompute_and_reports_cache_metadata(monkeypatch):
    calls = []

    def fake_usage(period, date_from, date_to):
        calls.append((period, date_from, date_to))
        return {"total_tokens": len(calls), "timestamp": f"ts-{len(calls)}"}

    monkeypatch.setattr(api, "compute_usage_with_comparison", fake_usage)

    first = api.get_usage(period="today")
    cached = api.get_usage(period="today")
    refreshed = api.get_usage(period="today", refresh=True)

    assert first["total_tokens"] == 1
    assert first["response_cache"]["status"] == "recomputed"
    assert first["response_cache"]["served_from_cache"] is False
    assert cached["total_tokens"] == 1
    assert cached["response_cache"]["status"] == "hit"
    assert cached["response_cache"]["served_from_cache"] is True
    assert refreshed["total_tokens"] == 2
    assert refreshed["response_cache"]["status"] == "recomputed"
    assert refreshed["response_cache"]["served_from_cache"] is False
    assert calls == [("today", None, None), ("today", None, None)]


def test_activity_insights_endpoint_shape(synthetic_api_data):
    result = api.get_activity_insights()

    assert result["scope"] == {
        "tool": "codex",
        "local": True,
        "primary_only": True,
    }
    assert result["recorded_chats"]["value"] == 3
    assert result["reasoning"]["most_used"]["effort"] == "xhigh"
    assert result["tools"]["total_calls"] == 8
    assert result["tools"]["distribution"][0]["name"] == "exec"


def test_activity_insights_uses_own_versioned_cache_key(monkeypatch):
    captured = {}
    expected = {"scope": {"tool": "codex"}}

    def fake_cached_route(route_name, cache_key, fetch_fn, **_kwargs):
        captured["route_name"] = route_name
        captured["cache_key"] = cache_key
        captured["fetch_fn"] = fetch_fn
        return expected

    def fake_activity():
        return expected

    monkeypatch.setattr(api, "_cached_route", fake_cached_route)
    monkeypatch.setattr(api, "get_codex_activity_insights", fake_activity)

    assert api.get_activity_insights() is expected
    assert captured == {
        "route_name": "/api/activity-insights",
        "cache_key": "activity_insights_v1",
        "fetch_fn": fake_activity,
    }


def test_activity_insights_failure_does_not_change_stats(monkeypatch):
    expected = {"stats": {"total_tokens": 10}, "contributions": []}
    monkeypatch.setattr(api, "compute_stats", lambda _year=None: expected)
    monkeypatch.setattr(
        api,
        "get_codex_activity_insights",
        lambda: (_ for _ in ()).throw(RuntimeError("activity unavailable")),
    )

    with pytest.raises(api.HTTPException) as exc:
        api.get_activity_insights()
    assert exc.value.status_code == 500
    assert api.get_stats() == expected


@pytest.mark.skipif(
    not _enabled("TOKDASH_RUN_REAL_API_SMOKE"),
    reason="set TOKDASH_RUN_REAL_API_SMOKE=1 to walk real local logs; this is intentionally heavy",
)
def test_api_endpoints_against_real_local_logs():
    """Opt-in integration/stress check for the real parser stack."""
    usage = api.get_usage(period="today")
    assert "total_tokens" in usage
    assert "total_messages" in usage
    assert "comparison" in usage

    stats = api.get_stats()
    assert "contributions" in stats
    assert "stats" in stats
