"""Tests for DeepSeek Harness (dsh) as a session source in sessions.py."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokdash import sessions
from tokdash.pricing import PricingDatabase
from tokdash.sessions import SESSION_TOOLS, get_session_detail, get_sessions_data, reload_pricing_db

# Fixed past instants so ``period="all"`` and date windows behave predictably.
DAY1_MS = int(datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
DAY2_MS = int(datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _header(session_id, cwd="/work/proj", **overrides):
    header = {"type": "session", "version": 0, "id": session_id, "createdAt": DAY1_MS, "cwd": cwd}
    header.update(overrides)
    return header


def _assistant_message(seq, turn, step, usage, ts_ms, model="deepseek-v4-flash"):
    return {
        "type": "assistant/message",
        "seq": seq,
        "time": ts_ms,
        "data": {
            "turn": turn,
            "step": step,
            "message": {
                "id": f"a{seq}",
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "source": {"kind": "model", "provider": "deepseek", "model": model},
            },
            "usage": usage,
        },
    }


def _write_session(home: Path, session_id: str, rows) -> Path:
    path = home / "sessions" / "--work-proj--" / session_id / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolated_dsh_home(monkeypatch, tmp_path):
    home = tmp_path / "dsh-home"
    monkeypatch.setenv("DSH_HOME", str(home))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    reload_pricing_db()
    yield home
    reload_pricing_db()


def test_dsh_is_a_session_tool():
    assert "dsh" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["dsh"] == "DeepSeek Harness"
    get_sessions_data("dsh", "all")  # no DSH_HOME dir: empty source, no error
    with pytest.raises(ValueError):
        get_sessions_data("not_a_tool", "all")


# --- case 10: title, project, fallback title, model, date windows --------------


def test_session_mapping_title_project_and_model(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_session(
        home,
        "session-abc",
        [
            _header("session-abc"),
            {
                "type": "session/title",
                "seq": 5,
                "time": DAY1_MS + 5000,
                "data": {"title": "Refactor the parser", "messageSeqs": [2], "source": {"kind": "provider", "provider": "builtin"}},
            },
            _assistant_message(3, 0, 0, {"inputTokens": 100, "outputTokens": 12, "cacheReadTokens": 50, "cacheWriteTokens": 20}, DAY1_MS),
        ],
    )
    data = get_sessions_data("dsh", "all")
    assert data["tool_label"] == "DeepSeek Harness"
    assert len(data["sessions"]) == 1
    row = data["sessions"][0]
    assert row["session_id"] == "session-abc"
    assert row["display_name"] == "Refactor the parser"
    assert row["project"] == "proj"
    assert row["model"] == "deepseek-v4-flash"
    assert row["tokens_in"] == 120  # inputTokens + cacheWriteTokens
    assert row["tokens_cache"] == 50
    assert row["tokens_out"] == 12
    assert row["tokens_reasoning"] == 0
    assert row["is_review_session"] is False

    detail = get_session_detail("dsh", "session-abc")
    assert detail["session"]["display_name"] == "Refactor the parser"
    turn = detail["turns"][0]
    assert turn["tokens_in"] == 120
    # Private billing internals never leave the API.
    assert "_bill" not in turn and "_event_key" not in turn


def test_display_name_fallback_chain(_isolated_dsh_home):
    home = _isolated_dsh_home
    # No session/title: the first user-message preview wins.
    _write_session(
        home,
        "session-preview",
        [
            _header("session-preview"),
            {
                "type": "user/message",
                "seq": 1,
                "time": DAY1_MS,
                "data": {"id": "u1", "role": "user", "content": [{"type": "text", "text": "fix the flaky test"}], "source": {"kind": "user"}},
            },
            _assistant_message(2, 0, 0, {"inputTokens": 10, "outputTokens": 5}, DAY1_MS + 1000),
        ],
    )
    # No title and no user message: the project/id fallback.
    _write_session(
        home,
        "session-bare",
        [
            _header("session-bare"),
            _assistant_message(1, 0, 0, {"inputTokens": 10, "outputTokens": 5}, DAY1_MS + 2000),
        ],
    )
    by_id = {row["session_id"]: row for row in get_sessions_data("dsh", "all")["sessions"]}
    assert by_id["session-preview"]["display_name"] == "fix the flaky test"
    assert by_id["session-bare"]["display_name"] == "proj"


def test_date_window_filters_turns(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_session(
        home,
        "session-abc",
        [
            _header("session-abc"),
            _assistant_message(1, 0, 0, {"inputTokens": 10, "outputTokens": 5}, DAY1_MS),
            _assistant_message(2, 1, 0, {"inputTokens": 20, "outputTokens": 6}, DAY2_MS),
        ],
    )
    assert len(get_sessions_data("dsh", "all")["sessions"]) == 1
    window = get_sessions_data("dsh", "all", date_from="2026-06-02", date_to="2026-06-02")
    assert len(window["sessions"]) == 1
    assert window["sessions"][0]["token_events"] == 1
    assert window["sessions"][0]["tokens_out"] == 6
    empty = get_sessions_data("dsh", "all", date_from="2026-07-01", date_to="2026-07-01")
    assert empty["sessions"] == []


# --- case 7/14: split-cache-write billing and repricing without a reparse -------


def _write_pricing_override(rates: dict) -> None:
    path = PricingDatabase().override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": "test", "aliases": {}, "models": rates}), encoding="utf-8")
    reload_pricing_db()


def _seed_session(home: Path) -> None:
    _write_session(
        home,
        "session-abc",
        [
            _header("session-abc"),
            _assistant_message(
                1, 0, 0,
                {"inputTokens": 100, "outputTokens": 10, "cacheReadTokens": 1000, "cacheWriteTokens": 50},
                DAY1_MS,
            ),
        ],
    )


def test_split_cache_write_repricing_without_reparse(_isolated_dsh_home, monkeypatch):
    """Stored dsh rows carry price-neutral billing inputs: a rate edit reprices
    them on read instead of reparsing the log, and cache write bills at its own
    rate (split-cache-write, not folded into the input rate)."""
    home = _isolated_dsh_home
    _seed_session(home)
    _write_pricing_override(
        {
            "deepseek-v4-flash": {
                "provider": "deepseek",
                "input": 2.0,
                "output": 10.0,
                "cache_read": 0.2,
                "cache_write": 4.0,
                "unit": "per_million_tokens",
            }
        }
    )

    counts = {"parse": 0}
    original = sessions._parse_dsh_session_file

    def counting(*args, **kwargs):
        counts["parse"] += 1
        return original(*args, **kwargs)

    counting.cache_clear = original.cache_clear
    counting.cache_info = original.cache_info
    monkeypatch.setattr(sessions, "_parse_dsh_session_file", counting)

    first = get_sessions_data("dsh", "all")["sessions"][0]["cost"]
    # (100*2 + 10*10 + 1000*0.2 + 50*4) / 1e6 — cache write at its own rate.
    assert first == pytest.approx(0.0007)
    assert counts["parse"] >= 1

    counts["parse"] = 0
    _write_pricing_override(
        {
            "deepseek-v4-flash": {
                "provider": "deepseek",
                "input": 4.0,
                "output": 20.0,
                "cache_read": 0.4,
                "cache_write": 8.0,
                "unit": "per_million_tokens",
            }
        }
    )
    second = get_sessions_data("dsh", "all")["sessions"][0]["cost"]
    assert second == pytest.approx(0.0014)
    assert counts["parse"] == 0  # repriced from the stored row, not the log


# --- case 15: API surface and frontend registry --------------------------------


def test_api_routes_serve_dsh(_isolated_dsh_home, monkeypatch):
    from tokdash import api

    home = _isolated_dsh_home
    _seed_session(home)
    # Keep the cross-tool active-time rollup to dsh only; the other tools would
    # scan this machine's real logs.
    monkeypatch.setattr(sessions, "SESSION_TOOLS", ("dsh",))

    listing = api.get_sessions(tool="dsh", period="all")
    assert listing["tool"] == "dsh"
    assert listing["sessions"][0]["session_id"] == "session-abc"

    detail = api.get_session(tool="dsh", session_id="session-abc")
    assert detail["turns"]

    active = api.get_active_time(period="all", refresh=True)
    assert "dsh" in active["by_tool"]
    assert active["by_tool"]["dsh"]["session_count"] == 1


def test_cli_sync_primes_dsh_session_records(_isolated_dsh_home, monkeypatch):
    """`tokdash sync` warms session records for every tool whose session reads
    route through the persistent store (sessions._raw_sessions_for_tool)."""
    from tokdash import cli

    called = []
    monkeypatch.setattr("tokdash.compute._sync_usage_store", lambda tracker: None)
    monkeypatch.setattr("tokdash.sources.openclaw.get_usage_for_days", lambda days: {})
    monkeypatch.setattr(
        "tokdash.sessions.get_sessions_data",
        lambda tool, period: called.append(tool) or {"sessions": []},
    )
    cli._sync_usage_database()
    assert called == ["codex", "claude", "kimi", "dsh", "reasonix"]


def test_frontend_session_registry_includes_dsh():
    index = Path(sessions.__file__).parent / "static" / "index.html"
    source = index.read_text(encoding="utf-8")
    assert "'codex', 'claude', 'opencode', 'pi_agent', 'mimo', 'kimi', 'dsh', 'reasonix'" in source
    assert "kimi: null, dsh: null, reasonix: null, combined: null" in source
    assert 'updateSessionPanel("dsh", lastSessionsResponses.dsh);' in source
    assert 'initSortHeaders("dsh", renderSessionsTab);' in source
    assert "dsh: { ...DEFAULT_SORT }," in source
    assert "dsh: 'DeepSeek Harness'," in source
    assert "dshSessions: 'DeepSeek Harness Sessions'," in source
    assert 'id="dshSessionsTable"' in source
    brand = source.split("const TOOL_BRAND_META = Object.freeze({", 1)[1].split("});", 1)[0]
    assert "dsh:" in brand and "fallback: 'D'" in brand
    assert "dsh: { icon: '/static/icons/agents/dsh.svg'" in brand
    assert (index.parent / "icons" / "agents" / "dsh.svg").is_file()
