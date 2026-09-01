"""Tests for WorkBuddy as a session source (sessions.py).

WorkBuddy's token store is the append-only per-session transcripts
(projects/<slug>/<sessionId>.jsonl). The harness must reproduce the parser's
file set, per-row rule, cross-file call-id dedupe, and full-completion
billing so windowed session sums match WorkBuddyParser's entries.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tokdash.sessions as sessions
from test_workbuddy_parser import (
    _assistant_row,
    _fallback_usage,
    _private_keys,
    _raw_usage,
    _workbuddy_only_tracker,
    _write_transcript,
)
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    _raw_sessions_for_tool,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources.coding_tools import BaseParser, _sig_cache

# One priced model so billing drift is visible; "default-model" (the router
# alias) stays absent to pin the zero-cost path.
RATES = {
    "wb-model": {"input": 2.0, "output": 4.0, "cache_read": 0.2, "cache_write": 2.0},
}


def _local_ms(year, month, day, hour=12):
    local = datetime.now().astimezone().tzinfo
    return int(datetime(year, month, day, hour, 0, 0, tzinfo=local).timestamp() * 1000)


# Local-midnight-anchored instants so named date windows line up in any tz.
S_A = _local_ms(2026, 8, 20, 0)  # local midnight of day A (window since)
S_B = _local_ms(2026, 8, 21, 0)  # next local midnight (window until)
T_A = _local_ms(2026, 8, 20, 12)
T_B = _local_ms(2026, 8, 21, 12)
T_OLD = _local_ms(2025, 12, 1, 12)


def _setup(monkeypatch, tmp_path, root: Path) -> None:
    """Point the harness + pricing at a hermetic tree; call after writing files."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("WORKBUDDY_DATA_DIR", str(root))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    override = PricingDatabase().override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps({"version": "test", "aliases": {}, "models": RATES}),
        encoding="utf-8",
    )
    reload_pricing_db()


def _assistant_row_with_cwd(message_id, ts, model, raw_usage, cwd, usage=None):
    row = _assistant_row(message_id, ts, model, raw_usage=raw_usage, usage=usage)
    row["cwd"] = cwd
    return row


def _sid(row, session_id):
    """_assistant_row hardcodes a captured sessionId; fixtures need their own."""
    row["sessionId"] = session_id
    return row


@pytest.fixture(autouse=True)
def _clean_caches():
    # The shared workbuddy_file_signatures scan is a 5 s module-global TTL;
    # clearing it plus the per-file/aggregate caches keeps every test on a
    # fresh scan of its own unique tmp_path tree.
    sessions._parse_workbuddy_session_file.cache_clear()
    sessions._load_workbuddy_sessions.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()
    yield
    sessions._parse_workbuddy_session_file.cache_clear()
    sessions._load_workbuddy_sessions.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()


def test_registered():
    assert "workbuddy" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["workbuddy"] == "WorkBuddy"


def test_two_turn_session_fields(monkeypatch, tmp_path):
    root = tmp_path / "wb"
    (root / "projects").mkdir(parents=True)
    _write_transcript(root, "slug-a", "wb-s1", [
        {
            "type": "message", "role": "user", "id": "u1",
            "timestamp": T_A - 100000, "sessionId": "wb-s1",
            "cwd": "/work/demo-proj", "providerData": {"agent": "cli"},
        },
        {
            "type": "ai-title", "sessionId": "wb-s1",
            "timestamp": T_A - 50000, "cwd": "/work/demo-proj",
            "aiTitle": "Demo session",
        },
        _sid(_assistant_row_with_cwd("m1", T_A, "wb-model",
                                     _raw_usage(1000, 100, 400, 600, credit=1.0),
                                     "/work/demo-proj"), "wb-s1"),
        _sid(_assistant_row_with_cwd("m2", T_B, "wb-model",
                                     _raw_usage(2000, 200, 500, 1500, credit=2.0),
                                     "/work/demo-proj"), "wb-s1"),
    ])
    _setup(monkeypatch, tmp_path, root)

    data = get_sessions_data("workbuddy", "all")
    assert data["tool_label"] == "WorkBuddy"
    assert data["summary"]["session_count"] == 1
    s = data["sessions"][0]
    assert s["session_id"] == "wb-s1"
    assert s["display_name"] == "Demo session"
    assert s["project"] == "demo-proj"
    assert s["token_events"] == 2
    # Turn A: fresh 600 / cached 400 / out 100 -> 1100; turn B: 1500/500/200 -> 2200.
    assert s["tokens_in"] == 2100
    assert s["tokens_cache"] == 900
    assert s["tokens_out"] == 300
    assert s["tokens_reasoning"] == 0
    assert s["tokens"] == 3300
    pricing = PricingDatabase()
    expected_cost = (
        pricing.get_cost("wb-model", 600, 100, 400, 0)
        + pricing.get_cost("wb-model", 1500, 200, 500, 0)
    )
    assert s["cost"] == pytest.approx(expected_cost)
    assert data["summary"]["tokens"] == 3300
    assert data["summary"]["cost"] == pytest.approx(expected_cost)


def test_session_detail_public_turns(monkeypatch, tmp_path):
    root = tmp_path / "wb"
    (root / "projects").mkdir(parents=True)
    _write_transcript(root, "slug-a", "wb-s1", [
        _sid(_assistant_row_with_cwd("m2", T_B, "wb-model",
                                     _raw_usage(2000, 200, 500, 1500),
                                     "/work/demo-proj"), "wb-s1"),
        _sid(_assistant_row_with_cwd("m1", T_A, "wb-model",
                                     _raw_usage(1000, 100, 400, 600),
                                     "/work/demo-proj"), "wb-s1"),
    ])
    _setup(monkeypatch, tmp_path, root)

    detail = get_session_detail("workbuddy", "wb-s1")
    turns = detail["turns"]
    assert len(turns) == 2
    # Written out of order on disk; public view is in timestamp order.
    stamps = [t["timestamp"] for t in turns]
    assert stamps == sorted(stamps)
    for turn in turns:
        assert "tokens" in turn and "cost" in turn and "model" in turn
        assert "timestamp_ms" not in turn
    assert _private_keys(detail) == []


def test_date_window_clipping(monkeypatch, tmp_path):
    root = tmp_path / "wb"
    (root / "projects").mkdir(parents=True)
    _write_transcript(root, "slug-a", "wb-s1", [
        _sid(_assistant_row_with_cwd("m1", T_A, "wb-model",
                                     _raw_usage(1000, 100, 400, 600),
                                     "/work/demo-proj"), "wb-s1"),
        _sid(_assistant_row_with_cwd("m2", T_B, "wb-model",
                                     _raw_usage(2000, 200, 500, 1500),
                                     "/work/demo-proj"), "wb-s1"),
    ])
    _setup(monkeypatch, tmp_path, root)

    day_a = get_sessions_data(
        "workbuddy", "all", date_from="2026-08-20", date_to="2026-08-20")
    assert day_a["summary"]["session_count"] == 1
    assert day_a["sessions"][0]["token_events"] == 1
    assert day_a["sessions"][0]["tokens"] == 1100

    day_b = get_sessions_data(
        "workbuddy", "all", date_from="2026-08-21", date_to="2026-08-21")
    assert day_b["sessions"][0]["token_events"] == 1
    assert day_b["sessions"][0]["tokens"] == 2200

    both = get_sessions_data(
        "workbuddy", "all", date_from="2026-08-20", date_to="2026-08-21")
    assert both["sessions"][0]["token_events"] == 2
    assert both["sessions"][0]["tokens"] == 3300

    # A window before both turns lists nothing (the [since, until) clip drops
    # every turn; a zero-turn session is not listed).
    old = get_sessions_data(
        "workbuddy", "all", date_from="2025-12-01", date_to="2025-12-01")
    assert old["summary"]["session_count"] == 0


def test_cross_file_duplicate_call_id_counted_once(monkeypatch, tmp_path):
    root = tmp_path / "wb"
    (root / "projects").mkdir(parents=True)
    dup = _assistant_row_with_cwd("d" * 32, T_A, "wb-model",
                                  _raw_usage(100, 10, 0, 100), "/work/dup-proj")
    dup2 = _assistant_row_with_cwd("d" * 32, T_B, "wb-model",
                                   _raw_usage(100, 10, 0, 100), "/work/dup-proj")
    for row in (dup, dup2):
        row["sessionId"] = "wb-dup"
    _write_transcript(root, "slug-a", "wb-dup-a", [dup])
    _write_transcript(root, "slug-b", "wb-dup-b", [dup2])
    _setup(monkeypatch, tmp_path, root)

    data = get_sessions_data("workbuddy", "all")
    assert data["summary"]["session_count"] == 1
    s = data["sessions"][0]
    assert s["token_events"] == 1
    assert s["tokens"] == 110  # one of the two identical rows, not both


def test_fail_soft_bad_rows(monkeypatch, tmp_path):
    root = tmp_path / "wb"
    (root / "projects").mkdir(parents=True)
    _write_transcript(root, "slug-a", "wb-bad", [
        _sid(_assistant_row_with_cwd("ok1", T_A, "wb-model",
                                     _raw_usage(1000, 100, 0, 1000),
                                     "/work/bad-proj"), "wb-bad"),
        "{not json",
        # Zero usage: normalization returns None, row skipped.
        _assistant_row_with_cwd("zero1", T_A + 1000, "wb-model",
                                _raw_usage(0, 0, 0, 0), "/work/bad-proj"),
        # No messageId and no id: no call identity, row skipped.
        _assistant_row("none", T_A + 2000, "wb-model",
                       raw_usage=_raw_usage(100, 10, 0, 100),
                       omit_message_id=True, omit_id=True) | {"cwd": "/work/bad-proj"},
        # Timestamp 0: skipped.
        _assistant_row_with_cwd("ts0", 0, "wb-model",
                                _raw_usage(100, 10, 0, 100), "/work/bad-proj"),
        _sid(_assistant_row_with_cwd("ok2", T_B, "wb-model",
                                     _raw_usage(2000, 200, 500, 1500),
                                     "/work/bad-proj"), "wb-bad"),
    ])
    _setup(monkeypatch, tmp_path, root)

    data = get_sessions_data("workbuddy", "all")
    assert data["summary"]["session_count"] == 1
    s = data["sessions"][0]
    assert s["session_id"] == "wb-bad"
    assert s["token_events"] == 2
    assert s["tokens"] == 1100 + 2200


def test_fallback_display_and_unknown_project(monkeypatch, tmp_path):
    root = tmp_path / "wb"
    (root / "projects").mkdir(parents=True)
    # No ai-title row, no cwd on any session-bearing row.
    _write_transcript(root, "slug-a", "wb-bare", [
        _sid(_assistant_row("m1", T_A, "wb-model",
                            raw_usage=_raw_usage(100, 10, 0, 100)), "wb-bare"),
    ])
    _setup(monkeypatch, tmp_path, root)

    data = get_sessions_data("workbuddy", "all")
    assert data["summary"]["session_count"] == 1
    s = data["sessions"][0]
    assert s["session_id"] == "wb-bare"
    assert s["project"] == "unknown"
    # Fallback name derives from the session id, not the file stem.
    assert s["display_name"].startswith("wb-bare")


def test_windows_cwd_project_name(monkeypatch, tmp_path):
    """A Windows cwd read through WSL/drvfs still names the leaf directory.

    WorkBuddy records the native cwd, so a Windows install writes
    backslash paths that a POSIX Path would treat as one giant name.
    """
    root = tmp_path / "wb"
    (root / "projects").mkdir(parents=True)
    _write_transcript(root, "slug-a", "wb-win", [
        _sid(_assistant_row_with_cwd("m1", T_A, "wb-model",
                                     _raw_usage(100, 10, 0, 100),
                                     "C:\\Users\\H1937\\WorkBuddy AI"), "wb-win"),
    ])
    _setup(monkeypatch, tmp_path, root)

    s = get_sessions_data("workbuddy", "all")["sessions"][0]
    assert s["project"] == "WorkBuddy AI"
    assert s["display_name"] == "WorkBuddy AI"  # project wins the fallback


def test_project_from_repo_or_path_separators():
    """The backslash split is shared by every harness, not just WorkBuddy.

    _project_from_repo_or_path serves all of SESSION_TOOLS, so the cases
    below pin what changed and what did not for the tools that record a
    POSIX cwd or a repo URL.
    """
    project_of = sessions._project_from_repo_or_path
    assert project_of(None, "C:\\Users\\H1937\\WorkBuddy AI") == "WorkBuddy AI"
    assert project_of(None, "\\\\server\\share\\proj") == "proj"  # UNC path
    assert project_of(None, "C:\\Users\\H1937\\proj\\") == "proj"  # trailing sep
    # POSIX paths and repo URLs are untouched.
    assert project_of(None, "/work/demo-proj") == "demo-proj"
    assert project_of("https://github.com/acme/tokdash.git", "/work/other") == "tokdash"
    # Known behaviour change: a POSIX directory whose NAME contains a
    # literal backslash now splits on it. Rare, and accepted so that the
    # far more common Windows cwd resolves.
    assert project_of(None, "/work/odd\\name") == "name"


def test_multi_root_whitespace(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    root_a = tmp_path / "wb-a"
    root_b = tmp_path / "wb-b"
    _write_transcript(root_a, "slug-a", "wb-r1", [
        _sid(_assistant_row("a" * 32, T_A, "wb-model",
                            raw_usage=_raw_usage(1000, 100, 400, 600)), "wb-r1"),
    ])
    _write_transcript(root_b, "slug-b", "wb-r2", [
        _sid(_assistant_row("b" * 32, T_B, "wb-model",
                            raw_usage=_raw_usage(2000, 200, 0, 2000)), "wb-r2"),
    ])
    monkeypatch.setenv("WORKBUDDY_DATA_DIR", f"{root_a} , {root_b}")
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    override = PricingDatabase().override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps({"version": "test", "aliases": {}, "models": RATES}),
        encoding="utf-8",
    )
    reload_pricing_db()

    data = get_sessions_data("workbuddy", "all")
    ids = {s["session_id"] for s in data["sessions"]}
    assert ids == {"wb-r1", "wb-r2"}
    assert data["summary"]["tokens"] == 1100 + 2200


def test_reasoning_split_billed_as_completion(monkeypatch, tmp_path):
    root = tmp_path / "wb"
    (root / "projects").mkdir(parents=True)
    _write_transcript(root, "slug-a", "wb-reason", [
        _sid(_assistant_row_with_cwd("m1", T_A, "wb-model",
                                     _raw_usage(1000, 100, 400, 600, reasoning=50),
                                     "/work/reason-proj"), "wb-reason"),
    ])
    _setup(monkeypatch, tmp_path, root)

    data = get_sessions_data("workbuddy", "all")
    s = data["sessions"][0]
    # completion 100 = output 50 + reasoning 50; tokens unchanged by the split.
    assert s["tokens_out"] == 50
    assert s["tokens_reasoning"] == 50
    assert s["tokens_in"] == 600
    assert s["tokens_cache"] == 400
    assert s["tokens"] == 1100
    # Cost is priced on the FULL completion (100), not the displayed 50.
    pricing = PricingDatabase()
    assert s["cost"] == pytest.approx(pricing.get_cost("wb-model", 600, 100, 400, 0))


def test_unpriced_router_alias_zero_cost(monkeypatch, tmp_path):
    root = tmp_path / "wb"
    (root / "projects").mkdir(parents=True)
    _write_transcript(root, "slug-a", "wb-auto", [
        _sid(_assistant_row("m1", T_A, "default-model",
                            raw_usage=_raw_usage(34417, 128, 12288, 22129, credit=2.12)),
             "wb-auto"),
    ])
    _setup(monkeypatch, tmp_path, root)

    data = get_sessions_data("workbuddy", "all")
    s = data["sessions"][0]
    assert s["model"] == "default-model"
    assert s["tokens"] == 34417 + 128
    assert s["cost"] == 0.0


def test_parity_with_overview_parser(monkeypatch, tmp_path):
    """Windowed session sums equal the parser's entry sums for the same window.

    The persistent store, when enabled, serves Overview from stored rows — a
    different code path than the live parser. Disable it so both sides of the
    comparison are live parses; with the store on this test would measure
    ingestion state, not the harness.
    """
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    root = tmp_path / "wb-parity"
    (root / "projects").mkdir(parents=True)
    _write_transcript(root, "slug-p1", "wb-p1", [
        _sid(_assistant_row_with_cwd("p1a", T_OLD, "wb-model",
                                     _raw_usage(100, 10, 0, 100), "/work/p1"), "wb-p1"),
        _sid(_assistant_row_with_cwd("p1b", T_A, "wb-model",
                                     _raw_usage(1000, 100, 400, 600), "/work/p1"), "wb-p1"),
        _sid(_assistant_row_with_cwd("p1c", T_A + 60000, "wb-model",
                                     _raw_usage(100, 10, 0, 100, reasoning=5),
                                     "/work/p1"), "wb-p1"),
    ])
    _write_transcript(root, "slug-p2", "wb-p2", [
        _sid(_assistant_row_with_cwd("p2a", T_B, "wb-model", None,
                                     "/work/p2",
                                     usage=_fallback_usage(34417, 128, 12288)),
             "wb-p2"),
    ])
    _write_transcript(root, "slug-p3", "wb-p3", [
        "{not json",
        _sid(_assistant_row("p3a", T_B + 1000, "wb-model",
                            raw_usage=_raw_usage(500, 50, 100, 400),
                            omit_message_id=True, omit_id=True), "wb-p3"),
        _sid(_assistant_row_with_cwd("p3b", T_B + 2000, "wb-model",
                                     _raw_usage(200, 20, 0, 200), "/work/p3"), "wb-p3"),
    ])
    _setup(monkeypatch, tmp_path, root)

    local = datetime.now().astimezone().tzinfo
    tracker = _workbuddy_only_tracker()
    windows = (
        (dict(period="all"), {"wb-p1", "wb-p2", "wb-p3"}),
        (dict(period="all", date_from="2026-08-20", date_to="2026-08-20"), {"wb-p1"}),
        (dict(period="all", date_from="2026-08-20", date_to="2026-08-21"),
         {"wb-p1", "wb-p2", "wb-p3"}),
        (dict(period="all", date_from="2025-12-01", date_to="2025-12-01"), {"wb-p1"}),
    )
    for window, expected_ids in windows:
        data = get_sessions_data("workbuddy", **window)
        if "date_from" in window:
            since = datetime.strptime(window["date_from"], "%Y-%m-%d").replace(tzinfo=local)
            until = (datetime.strptime(window["date_to"], "%Y-%m-%d").replace(tzinfo=local)
                     + timedelta(days=1))
        else:
            since = until = None
        tracker.collect(since, until)
        entries = [e for e in tracker.entries if e["source"] == "workbuddy"]
        expected_tokens = sum(
            e["input"] + e["output"] + e["cacheRead"] + e["cacheWrite"] + e["reasoning"]
            for e in entries
        )
        expected_cost = sum(e["cost"] for e in entries)
        assert data["summary"]["tokens"] == expected_tokens, window
        assert data["summary"]["cost"] == pytest.approx(expected_cost), window
        assert {s["session_id"] for s in data["sessions"]} == expected_ids, window


def test_no_root_empty_view(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    reload_pricing_db()

    data = get_sessions_data("workbuddy", "all")
    assert data["sessions"] == []
    assert data["summary"]["session_count"] == 0


def test_registration_smoke(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    reload_pricing_db()

    assert "workbuddy" in SESSION_TOOLS
    assert "workbuddy" in sessions.TOOL_LABELS
    raw = _raw_sessions_for_tool("workbuddy")
    assert raw == {}


def test_frontend_session_registry_includes_workbuddy():
    index = Path(sessions.__file__).parent / "static" / "index.html"
    source = index.read_text(encoding="utf-8")
    assert "'antigravity_cli', 'cline', 'workbuddy'" in source
    assert "cline: null, workbuddy: null, qoder: null, combined: null" in source
    assert 'updateSessionPanel("workbuddy", lastSessionsResponses.workbuddy);' in source
    assert 'initSortHeaders("workbuddy", renderSessionsTab);' in source
    assert "workbuddy: { ...DEFAULT_SORT }," in source
    assert "workbuddySessions: 'WorkBuddy Sessions'," in source
    assert "workbuddySessions: 'WorkBuddy \u4f1a\u8bdd'," in source
    assert 'id="workbuddySessionsTable"' in source
    assert 'data-panel-details="workbuddy"' in source
    brand = source.split("const TOOL_BRAND_META = Object.freeze({", 1)[1].split("});", 1)[0]
    assert "workbuddy:" in brand
