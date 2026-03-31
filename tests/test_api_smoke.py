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

    manifest = client.get("/manifest.webmanifest").text
    assert "Tokdash" in manifest

    sw = client.get("/sw.js").text
    assert "service worker" in sw.lower()

    html = client.get("/").text
    assert "Tokdash" in html
    assert "Sessions" in html
