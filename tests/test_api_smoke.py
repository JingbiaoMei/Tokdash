import pytest


pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from tokdash.api import app


def test_api_endpoints_and_dashboard_smoke():
    client = TestClient(app)

    usage = client.get("/api/usage", params={"period": "today"}).json()
    assert "total_tokens" in usage
    assert "total_messages" in usage
    assert "comparison" in usage
    assert "openclaw_models" in usage
    assert "coding_apps" in usage

    tools = client.get("/api/tools", params={"period": "today"}).json()
    assert "apps" in tools
    assert "all_models" in tools

    for tool in ("codex", "claude", "opencode"):
        sessions = client.get("/api/sessions", params={"tool": tool, "period": "today"}).json()
        assert "sessions" in sessions
        assert "latest_session" in sessions
        assert sessions.get("tool") == tool

        latest = sessions.get("latest_session")
        if latest and latest.get("session_id"):
            detail = client.get("/api/session", params={"tool": tool, "session_id": latest["session_id"]}).json()
            assert "session" in detail
            assert "turns" in detail

    codex_sessions = client.get("/api/codex/sessions", params={"period": "today"}).json()
    assert "sessions" in codex_sessions
    assert "latest_session" in codex_sessions

    latest_codex = codex_sessions.get("latest_session")
    if latest_codex and latest_codex.get("session_id"):
        codex_detail = client.get("/api/codex/session", params={"session_id": latest_codex["session_id"]}).json()
        assert "session" in codex_detail
        assert "turns" in codex_detail

    openclaw = client.get("/api/openclaw", params={"period": "today"}).json()
    assert "models" in openclaw
    assert "contributions" in openclaw

    stats = client.get("/api/stats").json()
    assert "contributions" in stats
    assert "stats" in stats

    stats_year = client.get("/api/stats", params={"year": 2025}).json()
    assert "contributions" in stats_year
    assert "stats" in stats_year

    manifest = client.get("/manifest.webmanifest").text
    assert "Tokdash" in manifest

    sw_response = client.get("/sw.js")
    assert "no-store" in sw_response.headers["cache-control"]
    sw = sw_response.text
    assert "service worker" in sw.lower()
    assert "__TOKDASH_CACHE_NAME__" not in sw
    assert 'const CACHE_NAME = "tokdash-' in sw

    html_response = client.get("/")
    assert "no-store" in html_response.headers["cache-control"]
    html = html_response.text
    assert "Tokdash" in html
    assert "Sessions" in html

    icon_response = client.get("/static/icons/icon-192.png")
    assert icon_response.status_code == 200
    assert "no-store" in icon_response.headers["cache-control"]


def test_api_custom_date_ranges_and_validation():
    client = TestClient(app)

    usage = client.get("/api/usage", params={"date_from": "2026-04-08", "date_to": "2026-04-08"})
    assert usage.status_code == 200
    assert "comparison" in usage.json()

    sessions = client.get(
        "/api/sessions",
        params={"tool": "codex", "date_from": "2026-04-08", "date_to": "2026-04-08"},
    )
    assert sessions.status_code == 200
    assert sessions.json()["tool"] == "codex"

    missing_bound = client.get("/api/usage", params={"date_from": "2026-04-08"})
    assert missing_bound.status_code == 400
    assert "required" in missing_bound.json()["detail"]

    malformed = client.get("/api/usage", params={"date_from": "2026/04/08", "date_to": "2026-04-08"})
    assert malformed.status_code == 400
    assert "Invalid date format" in malformed.json()["detail"]

    reversed_range = client.get("/api/usage", params={"date_from": "2026-04-09", "date_to": "2026-04-08"})
    assert reversed_range.status_code == 400
    assert "on or before" in reversed_range.json()["detail"]
