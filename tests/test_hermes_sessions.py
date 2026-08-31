"""Tests for Hermes as a session source (sessions.py).

Hermes keeps one mutable SQLite DB (state.db) with per-session aggregate rows.
The harness must copy the parser's six rules — DB selection (post-claim), row
selection, id dedupe, the started_at expression, bucket mapping, and cost
precedence — so parity with HermesParser holds bucket for bucket. project is
always "unknown": schema v12 records no cwd.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokdash import sessions
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    _hermes_sessions,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources.coding_tools import BaseParser, HermesParser, _sig_cache

SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    model TEXT,
    billing_provider TEXT,
    started_at REAL,
    message_count INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    title TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    role TEXT,
    content TEXT,
    timestamp REAL
);
"""


@pytest.fixture(autouse=True)
def _clean_caches():
    sessions._load_hermes_sessions.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()
    yield
    sessions._load_hermes_sessions.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()


def _home(tmp_path: Path, name="home") -> Path:
    home = tmp_path / name
    (home / "state.db").parent.mkdir(parents=True, exist_ok=True)
    return home


def _row(row_id, model, provider, started_at, inp, out, cr, cw, reason=0,
         est=None, actual=None, title=None, msg_count=1):
    return (row_id, model, provider, started_at, msg_count, inp, out, cr, cw,
            reason, est, actual, title)


def _write_db(home: Path, rows, messages=()):
    db = home / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO sessions (id, model, billing_provider, started_at, message_count,"
        " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
        " reasoning_tokens, estimated_cost_usd, actual_cost_usd, title)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO messages (id, session_id, role, content, timestamp)"
        " VALUES (?,?,?,?,?)",
        messages,
    )
    conn.commit()
    conn.close()
    return db


def _patch_env(monkeypatch, home):
    monkeypatch.setenv("HERMES_HOME", str(home))


def _dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _parser_entries(home, since_ms=None, until_ms=None):
    _patch_home_for_parser = home  # parser resolves HERMES_HOME at init
    parser = HermesParser(PricingDatabase())
    return parser.collect(_dt(since_ms) if since_ms else None,
                          _dt(until_ms) if until_ms else None)


def _turn_sums(raw, since_ms=None, until_ms=None):
    out = {"in": 0, "cache": 0, "out": 0, "reason": 0, "cost": 0.0, "ids": set()}
    for s in raw.values():
        for t in s["turns"]:
            ts = t["timestamp_ms"]
            if since_ms is not None and ts < since_ms:
                continue
            if until_ms is not None and ts >= until_ms:
                continue
            out["in"] += t["tokens_in"]
            out["cache"] += t["tokens_cache"]
            out["out"] += t["tokens_out"]
            out["reason"] += t["tokens_reasoning"]
            out["cost"] += t["cost"]
            out["ids"].add(s["session_id"])
    return out


def _entry_sums(entries):
    return {
        "in": sum(e["input"] + e["cacheWrite"] for e in entries),
        "cache": sum(e["cacheRead"] for e in entries),
        "out": sum(e["output"] for e in entries),
        "reason": sum(e["reasoning"] for e in entries),
        "cost": sum(e["cost"] for e in entries),
    }


def test_hermes_registered():
    assert "hermes" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["hermes"] == "Hermes"


def test_mapping(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _write_db(home, [
        _row("h-1", "deepseek-chat", "deepseek", 1779395293.727557,
             12597, 758, 47033, 28387, 0, est=0.0, actual=None,
             title="Debug the pipeline"),
    ])
    _patch_env(monkeypatch, home)
    raw = _hermes_sessions()
    assert set(raw) == {"h-1"}
    s = raw["h-1"]
    assert s["tool"] == "hermes"
    assert s["display_name"] == "Debug the pipeline"
    assert s["project"] == "unknown"
    turn = s["turns"][0]
    # tokens_in folds cache_write (compute.py reporting semantics).
    assert turn["tokens_in"] == 12597 + 28387
    assert turn["tokens_cache"] == 47033
    assert turn["tokens_out"] == 758
    assert turn["tokens_reasoning"] == 0
    assert turn["tokens"] == 12597 + 28387 + 47033 + 758
    assert turn["model"] == "deepseek-chat"
    assert turn["timestamp_ms"] == 1779395293727
    assert turn["_event_key"] == "hermes:h-1"
    bill = turn["_bill"]
    assert bill["rule"] == "split-cache-write"
    assert bill["model"] == "deepseek/deepseek-chat"
    assert "fixed" not in bill
    assert turn["cost"] == pytest.approx(
        PricingDatabase().get_cost("deepseek/deepseek-chat", 12597, 758, 47033, 28387)
    )


def test_cost_precedence(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _write_db(home, [
        # actual beats estimated and pricing.
        _row("h-actual", "deepseek-chat", "deepseek", 1779395293.0,
             100, 10, 0, 0, est=0.5, actual=0.75),
        # estimated beats pricing.
        _row("h-est", "deepseek-chat", "deepseek", 1779395300.0,
             100, 10, 0, 0, est=0.25, actual=None),
        # recorded zeros fall through to the pricing DB.
        _row("h-zero", "deepseek-chat", "deepseek", 1779395310.0,
             100, 10, 0, 0, est=0.0, actual=0.0),
        # zero tokens but a positive actual cost: kept.
        _row("h-cost-only", "deepseek-chat", "deepseek", 1779395320.0,
             0, 0, 0, 0, est=None, actual=0.42),
        # all zero and no cost: skipped entirely.
        _row("h-skip", "deepseek-chat", "deepseek", 1779395330.0,
             0, 0, 0, 0, est=None, actual=None),
    ])
    _patch_env(monkeypatch, home)
    raw = _hermes_sessions()
    assert set(raw) == {"h-actual", "h-est", "h-zero", "h-cost-only"}
    turns = {sid: raw[sid]["turns"][0] for sid in raw}
    assert turns["h-actual"]["cost"] == pytest.approx(0.75)
    assert turns["h-actual"]["_bill"]["fixed"] == 0.75
    assert turns["h-est"]["cost"] == pytest.approx(0.25)
    assert turns["h-est"]["_bill"]["fixed"] == 0.25
    assert turns["h-zero"]["cost"] == pytest.approx(
        PricingDatabase().get_cost("deepseek/deepseek-chat", 100, 10, 0, 0)
    )
    assert "fixed" not in turns["h-zero"]["_bill"]
    assert turns["h-cost-only"]["tokens"] == 0
    assert turns["h-cost-only"]["cost"] == pytest.approx(0.42)


def _parity_fixture(home: Path):
    """One row per branch: provider set/unset, seconds and ms timestamps,
    title set/NULL (with a user message for the preview), zero-token+cost,
    model='' and NULL-model rows the WHERE clause must drop."""
    _write_db(
        home,
        [
            _row("p-1", "deepseek-chat", "deepseek", 1779395293.727557,
                 12597, 758, 47033, 28387, title="Priced row"),
            # No billing_provider: _infer_provider("gpt-5") -> openai.
            _row("p-2", "gpt-5", None, 1779395300.0,
                 500, 50, 100, 25, title="Inferred provider"),
            # started_at already in epoch ms.
            _row("p-3", "claude-opus-4", "anthropic", 1779400000123.0,
                 300, 30, 0, 0, title=None),
            # Zero tokens + positive estimated cost: kept.
            _row("p-4", "deepseek-chat", "deepseek", 1779400010.0,
                 0, 0, 0, 0, est=0.9, title="Cost only"),
            _row("p-5", "", "deepseek", 1779400020.0, 10, 1, 0, 0),
            _row("p-6", None, "deepseek", 1779400030.0, 1, 1, 0, 0),
        ],
        messages=[
            # First user message for the title-less p-3 (and a user row that
            # must not shadow a real title, for p-1).
            (1, "p-1", "user", "ignore me, p-1 has a title", 1779395294.0),
            (2, "p-3", "user", "First prompt of p3", 1779400001.0),
        ],
    )


def test_parity_with_usage_parser(monkeypatch, tmp_path):
    """The load-bearing property: bucket sums equal HermesParser's for the
    full window and a half-open sub-window, and both sides list the same
    session ids."""
    home = _home(tmp_path)
    _parity_fixture(home)
    _patch_env(monkeypatch, home)

    windows = [(None, None), (1779395300000, 1779400010000)]
    for since_ms, until_ms in windows:
        entries = _parser_entries(home, since_ms, until_ms)
        p = _entry_sums(entries)
        h = _turn_sums(_hermes_sessions(), since_ms, until_ms)
        assert h["in"] == p["in"], (since_ms, until_ms)
        assert h["cache"] == p["cache"], (since_ms, until_ms)
        assert h["out"] == p["out"], (since_ms, until_ms)
        assert h["reason"] == p["reason"], (since_ms, until_ms)
        assert h["cost"] == pytest.approx(p["cost"], abs=1e-12), (since_ms, until_ms)

    raw = _hermes_sessions()
    # WHERE drops the model='' and NULL rows; each surviving row keeps one turn.
    assert {s["session_id"] for s in raw.values()} == {"p-1", "p-2", "p-3", "p-4"}
    assert raw["p-1"]["display_name"] == "Priced row"  # title beats preview
    assert raw["p-3"]["display_name"] == "First prompt of p3"  # NULL title -> preview
    # The ms-branch timestamp: 1779400000123.0 seconds is > 1e12 -> used as ms.
    assert raw["p-3"]["turns"][0]["timestamp_ms"] == 1779400000123
    # Inferred provider on the billing record.
    assert raw["p-2"]["turns"][0]["_bill"]["model"] == "openai/gpt-5"


def test_named_profiles_scanned(monkeypatch, tmp_path):
    """Sessions from ~/.hermes/profiles/<name>/state.db surface in the Sessions
    tab without HERMES_HOME, and the parser bills exactly the same rows."""
    home = _home(tmp_path)
    _write_db(home, [_row("main-1", "deepseek-chat", "deepseek", 1779395293.0,
                          100, 10, 0, 0, title="main")])
    for name, sid in (("robot1", "r1-1"), ("robot2", "r2-1")):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        _write_db(profile, [_row(sid, "deepseek-chat", "deepseek", 1779395294.0,
                                 200, 20, 0, 0, title=name)])
    _patch_env(monkeypatch, home)

    raw = _hermes_sessions()
    assert set(raw) == {"main-1", "r1-1", "r2-1"}
    assert raw["r1-1"]["display_name"] == "robot1"
    # Parity: Overview bills the same tokens the Sessions tab lists.
    assert _entry_sums(_parser_entries(home))["out"] == _turn_sums(raw)["out"] == 50


def test_named_profiles_ids_are_disjoint_not_deduped(monkeypatch, tmp_path):
    """Profiles never share session history, so the row-id dedup that guards
    multiple HERMES_HOME dirs must not collapse two profiles' distinct rows."""
    home = _home(tmp_path)
    _write_db(home, [])
    for name, sid in (("robot1", "20260521_212809_aaaaaa"),
                      ("robot2", "20260521_212809_bbbbbb")):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        _write_db(profile, [_row(sid, "deepseek-chat", "deepseek", 1779395294.0,
                                 5000, 500, 0, 0)])
    _patch_env(monkeypatch, home)

    raw = _hermes_sessions()
    assert len(raw) == 2
    assert _turn_sums(raw)["out"] == 1000


def test_dedup_first_db_wins(monkeypatch, tmp_path):
    home_a = _home(tmp_path, "homeA")
    home_b = _home(tmp_path, "homeB")
    _write_db(home_a, [_row("dup-1", "deepseek-chat", "deepseek", 1779395293.0,
                            111, 11, 0, 0, title="first dir")])
    _write_db(home_b, [_row("dup-1", "deepseek-chat", "deepseek", 1779395293.0,
                            222, 22, 0, 0, title="second dir")])
    monkeypatch.setenv("HERMES_HOME", f"{home_a},{home_b}")
    raw = _hermes_sessions()
    assert set(raw) == {"dup-1"}
    # The first search dir's values win — as in the parser.
    assert raw["dup-1"]["turns"][0]["tokens_out"] == 11
    assert len(_parser_entries(home_a)) == 1


def test_dedup_zero_row_first_dir_claims_id(monkeypatch, tmp_path):
    """Parity regression: the parser claims a session id *before* the zero-row
    skip (seen_ids at first sight). If the first dir's copy is an all-zero row
    with no cost, the second dir's real row is suppressed on BOTH sides — a
    harness that only remembered ids it kept would list the real row in
    Sessions while Overview bills nothing (reproduced as 0 vs 88775 tokens)."""
    home_a = _home(tmp_path, "homeA")
    home_b = _home(tmp_path, "homeB")
    _write_db(home_a, [_row("dup-zero", "deepseek-chat", "deepseek", 1779395293.0,
                            0, 0, 0, 0, est=None, actual=None)])
    _write_db(home_b, [_row("dup-zero", "deepseek-chat", "deepseek", 1779395294.0,
                            123, 45, 6, 7, title="real row")])
    monkeypatch.setenv("HERMES_HOME", f"{home_a},{home_b}")
    # Parser: the zero row claimed the id, so the real row never surfaces.
    assert _parser_entries(home_a) == []
    # Harness must agree: nothing in Sessions either.
    assert _hermes_sessions() == {}


def test_window_half_open(monkeypatch, tmp_path):
    # Bounds at local midnights so parse_date_range(day, day) reproduces
    # [S, U) exactly (dateutil parses in the local timezone).
    from datetime import timedelta
    local = datetime.now().astimezone().tzinfo
    day = datetime(2026, 5, 22, 0, 0, 0, tzinfo=local)
    S = int(day.timestamp() * 1000)
    U = int((day + timedelta(days=1)).timestamp() * 1000)
    home = _home(tmp_path)
    _write_db(home, [
        _row("w-since", "deepseek-chat", "deepseek", S / 1000, 10, 1, 0, 0),
        _row("w-until", "deepseek-chat", "deepseek", U / 1000, 10, 1, 0, 0),
        _row("w-before", "deepseek-chat", "deepseek", (S - 1000) / 1000, 10, 1, 0, 0),
    ])
    _patch_env(monkeypatch, home)
    raw = _hermes_sessions()
    listed = _turn_sums(raw, since_ms=S, until_ms=U)
    assert listed["ids"] == {"w-since"}
    # The public API windows the same way: the range day [S, U).
    listing = get_sessions_data(
        "hermes", "range",
        date_from=datetime.fromtimestamp(S / 1000, tz=local).strftime("%Y-%m-%d"),
        date_to=datetime.fromtimestamp(S / 1000, tz=local).strftime("%Y-%m-%d"),
    )
    assert [s["session_id"] for s in listing["sessions"]] == ["w-since"]


def test_missing_dir_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "does-not-exist"))
    assert _hermes_sessions() == {}
    assert get_sessions_data("hermes", "all")["sessions"] == []


def test_corrupt_db_empty(monkeypatch, tmp_path):
    home = _home(tmp_path)
    (home / "state.db").write_bytes(b"garbage, not a sqlite database. padding.")
    _patch_env(monkeypatch, home)
    assert _hermes_sessions() == {}
    assert get_sessions_data("hermes", "all")["sessions"] == []


def test_api_endpoints(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _write_db(home, [
        _row("h-api", "deepseek-chat", "deepseek", 1779395293.0,
             100, 10, 5, 1, title="API session"),
    ])
    _patch_env(monkeypatch, home)
    listing = get_sessions_data("hermes", "all")
    assert listing["tool_label"] == "Hermes"
    assert listing["sessions"][0]["session_id"] == "h-api"
    detail = get_session_detail("hermes", "h-api")
    turn = detail["turns"][0]
    assert turn["tokens_in"] == 101
    assert "_bill" not in turn and "_event_key" not in turn
    with pytest.raises(ValueError):
        get_sessions_data("not_a_tool", "all")
