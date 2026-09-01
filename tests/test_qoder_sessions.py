"""Tests for Qoder IDE as a session source (sessions.py).

Qoder IDE's token store is the single WAL-mode SQLite DB (local.db).
The harness reads the same chat_message rows through the same
zcode_snapshot and shared QoderIdeParser._row_buckets as the usage
parser, so windowed session sums match the parser's entries.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from test_qoder_ide_parser import CHAT_MESSAGE_DDL, CN_ROOT, INTL_ROOT, DB_SUFFIX

from tokdash import api, sessions
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    QoderReadError,
    _qoder_sessions,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources.coding_tools import BaseParser, QoderIdeParser, ZCodeSnapshotError

BASE = 1_787_000_000_000  # ~2026-08-17T03:53Z

SESSION_DDL = """
CREATE TABLE chat_session (
    session_id varchar(64) primary key,
    session_title varchar(256),
    project_uri varchar(512)
)
"""

# One priced model so cost drift is visible; "auto" stays absent on purpose.
RATES = {"gpt-x": {"input": 2.0, "output": 4.0, "cache_read": 0.2, "cache_write": 2.0}}


@pytest.fixture(autouse=True)
def _clean_caches():
    sessions._qoder_sessions_cache.clear()
    sessions._qoder_sessions_cache_sig = ()
    QoderIdeParser._query_cache.clear()
    QoderIdeParser._query_cache_sig = ()
    BaseParser._entry_cache.clear()
    reload_pricing_db()
    yield
    sessions._qoder_sessions_cache.clear()
    sessions._qoder_sessions_cache_sig = ()
    QoderIdeParser._query_cache.clear()
    QoderIdeParser._query_cache_sig = ()
    BaseParser._entry_cache.clear()
    reload_pricing_db()


def _setup(monkeypatch, tmp_path, root: Path) -> None:
    """Point QODER_IDE_DATA_DIR at root with a known pricing override."""
    monkeypatch.setenv("QODER_IDE_DATA_DIR", str(root))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    override = PricingDatabase().override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps({"version": "test", "aliases": {}, "models": RATES}),
        encoding="utf-8",
    )
    reload_pricing_db()


def _msg(row_id, session_id, token_info, gmt=BASE, role="assistant", model_key=""):
    model_info = json.dumps({"model_key": model_key}) if model_key else ""
    return (row_id, session_id, "req-" + row_id, role, token_info, model_info, gmt)


def _ti(prompt, completion, cached=0):
    return json.dumps({
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "max_input_tokens": 180000,
    })


def _make_db(root: Path, message_rows, session_rows=()) -> Path:
    db = root / DB_SUFFIX
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(CHAT_MESSAGE_DDL)
        conn.execute(SESSION_DDL)
        conn.executemany(
            "INSERT INTO chat_message (id, session_id, request_id, role, "
            "token_info, model_info, gmt_create) VALUES (?, ?, ?, ?, ?, ?, ?)",
            message_rows,
        )
        conn.executemany(
            "INSERT INTO chat_session (session_id, session_title, project_uri) "
            "VALUES (?, ?, ?)",
            session_rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_cn_fixture_full_window(monkeypatch, tmp_path):
    """The real CN capture through the None-window (bind-default) path."""
    _setup(monkeypatch, tmp_path, CN_ROOT)
    data = get_sessions_data("qoder", "all")
    assert data["tool_label"] == "Qoder IDE"
    assert data["summary"]["session_count"] == 1
    s = data["sessions"][0]
    assert s["session_id"] == "task-3b44336653874aa9991b.session.execution"
    assert s["token_events"] == 60
    assert s["tokens_in"] == 2_319_522
    assert s["tokens_cache"] == 0
    assert s["tokens_out"] == 10_808
    assert s["tokens"] == 2_330_330
    assert s["model"] == "auto"
    assert s["cost"] == 0.0
    # The fixture DB carries no chat_session table: orphan naming applies.
    assert s["project"] == "unknown"
    assert data["summary"]["tokens"] == 2_330_330
    assert data["summary"]["cost"] == 0.0


def test_row_mapping_intl(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, INTL_ROOT)
    sid = "db96962b-bfda-402f-8728-ff48bb27217c"
    raw = _qoder_sessions()
    turn = raw[sid]["turns"][0]
    assert turn["tokens_in"] == 17_553
    assert turn["tokens_out"] == 115
    assert turn["tokens_cache"] == 0
    assert turn["tokens_reasoning"] == 0
    assert turn["model"] == "auto"
    assert turn["_bill"]["rule"] == "fresh-input"
    assert turn["_bill"]["input"] == 17_553
    assert turn["_bill"]["output"] == 115
    detail = get_session_detail("qoder", sid)
    public = detail["turns"][0]
    assert "_bill" not in public and "_event_key" not in public
    assert public["timestamp_ms" if "timestamp_ms" in public else "timestamp"]


def test_cached_slice_in_prompt(monkeypatch, tmp_path):
    root = tmp_path / "qoder"
    _make_db(root, [
        _msg("r1", "s1", _ti(1000, 50, 400), BASE, model_key="gpt-x"),
        # Torn row: cached > prompt clamps to prompt, input never negative.
        _msg("r2", "s1", _ti(500, 20, 600), BASE + 1000, model_key="gpt-x"),
    ])
    _setup(monkeypatch, tmp_path, root)

    raw = _qoder_sessions()
    t1, t2 = raw["s1"]["turns"]
    assert (t1["tokens_in"], t1["tokens_cache"], t1["tokens_out"], t1["tokens"]) == (600, 400, 50, 1050)
    assert (t2["tokens_in"], t2["tokens_cache"], t2["tokens_out"], t2["tokens"]) == (0, 500, 20, 520)
    pricing = PricingDatabase()
    expected = (
        pricing.get_cost("gpt-x", 600, 50, 400, 0)
        + pricing.get_cost("gpt-x", 0, 20, 500, 0)
    )
    assert raw["s1"]["turns"][0]["cost"] == pytest.approx(
        pricing.get_cost("gpt-x", 600, 50, 400, 0))
    assert raw["s1"]["turns"][1]["cost"] == pytest.approx(
        pricing.get_cost("gpt-x", 0, 20, 500, 0))
    assert sum(t["cost"] for t in raw["s1"]["turns"]) == pytest.approx(expected)


def test_bad_rows_skipped(monkeypatch, tmp_path):
    root = tmp_path / "qoder"
    _make_db(root, [
        _msg("zero", "s1", _ti(0, 0, 0), BASE),
        _msg("notjson", "s1", "{not json", BASE + 1000),
        _msg("nondict", "s1", json.dumps([1, 2, 3]), BASE + 2000),
        # prompt 0 / cached 5 clamps to 0 -> all-zero -> skipped.
        _msg("clamp0", "s1", _ti(0, 0, 5), BASE + 3000),
        _msg("good", "s1", _ti(100, 10), BASE + 4000),
    ])
    _setup(monkeypatch, tmp_path, root)

    raw = _qoder_sessions()
    assert set(raw) == {"s1"}
    turns = raw["s1"]["turns"]
    assert len(turns) == 1
    assert turns[0]["_event_key"] == "qoder:good"
    assert turns[0]["tokens"] == 110


def test_window_half_open(monkeypatch, tmp_path):
    root = tmp_path / "qoder"
    S, U = BASE, BASE + 1000
    _make_db(root, [
        _msg("before", "s1", _ti(100, 10), S - 1),
        _msg("at_s", "s1", _ti(100, 10), S),
        _msg("before_u", "s1", _ti(100, 10), U - 1),
        _msg("at_u", "s1", _ti(100, 10), U),
    ])
    _setup(monkeypatch, tmp_path, root)

    raw = _qoder_sessions(since_ms=S, until_ms=U)
    keys = [t["_event_key"] for t in raw["s1"]["turns"]]
    assert keys == ["qoder:at_s", "qoder:before_u"]
    assert sum(t["tokens"] for t in raw["s1"]["turns"]) == 220

    # The "all" window (None, None) binds 0 / 9999999999999: every row.
    raw_all = _qoder_sessions()
    assert sum(t["tokens"] for t in raw_all["s1"]["turns"]) == 440


def test_parity_with_usage_parser(monkeypatch, tmp_path):
    """Windowed per-session sums equal the parser's entry sums."""
    # The real fixture DBs (one session each), full window.
    for root in (CN_ROOT, INTL_ROOT):
        _setup(monkeypatch, tmp_path, root)
        raw = _qoder_sessions()
        entries = QoderIdeParser(PricingDatabase()).collect(None, None)
        assert raw  # every fixture row bills
        for sid, session in raw.items():
            turns = session["turns"]
            assert sum(t["tokens_in"] for t in turns) == sum(e["input"] for e in entries)
            assert sum(t["tokens_cache"] for t in turns) == sum(e["cacheRead"] for e in entries)
            assert sum(t["tokens_out"] for t in turns) == sum(e["output"] for e in entries)
            assert sum(t["tokens"] for t in turns) == sum(
                e["input"] + e["cacheRead"] + e["output"] for e in entries)
            assert sum(t["cost"] for t in turns) == pytest.approx(
                sum(e["cost"] for e in entries))

    # Synthetic windowing on a two-session tree.
    root = tmp_path / "qoder-parity"
    S, U = BASE + 10_000, BASE + 20_000
    rows = [
        _msg("p1a", "s1", _ti(100, 10), S - 1, model_key="gpt-x"),
        _msg("p1b", "s1", _ti(200, 20, 80), S, model_key="gpt-x"),
        _msg("p1c", "s1", _ti(300, 30, 100), U - 1, model_key="gpt-x"),
        _msg("p2a", "s2", _ti(400, 40), U, "assistant"),
        _msg("p2b", "s2", _ti(50, 5), BASE + 99_000),
    ]
    _make_db(root, rows)
    _setup(monkeypatch, tmp_path, root)

    row_session = {"p1a": "s1", "p1b": "s1", "p1c": "s1", "p2a": "s2", "p2b": "s2"}
    windows = ((None, None), (S, U), (S + 1, U - 1))
    for since_ms, until_ms in windows:
        raw = _qoder_sessions(since_ms=since_ms, until_ms=until_ms)
        lo = 0 if since_ms is None else since_ms
        hi = 9_999_999_999_999 if until_ms is None else until_ms
        entries = [
            e for e in QoderIdeParser(PricingDatabase()).collect(None, None)
            if lo <= e["timestamp"] < hi
        ]
        by_session: dict[str, list] = {}
        for e in entries:
            rid = e["entry_id"].split(":", 1)[1]
            by_session.setdefault(row_session[rid], []).append(e)
        assert set(raw) == set(by_session)
        for sid, evs in by_session.items():
            turns = raw[sid]["turns"]
            assert sum(t["tokens_in"] for t in turns) == sum(e["input"] for e in evs), (since_ms, until_ms, sid)
            assert sum(t["tokens_cache"] for t in turns) == sum(e["cacheRead"] for e in evs), (since_ms, until_ms, sid)
            assert sum(t["tokens_out"] for t in turns) == sum(e["output"] for e in evs), (since_ms, until_ms, sid)
            assert sum(t["tokens"] for t in turns) == sum(
                e["input"] + e["cacheRead"] + e["output"] for e in evs), (since_ms, until_ms, sid)
            assert sum(t["cost"] for t in turns) == pytest.approx(
                sum(e["cost"] for e in evs)), (since_ms, until_ms, sid)


def test_missing_db_and_missing_table(monkeypatch, tmp_path):
    root = tmp_path / "qoder-empty"
    root.mkdir()
    _setup(monkeypatch, tmp_path, root)

    # No DB file: legitimate empty, cached.
    assert _qoder_sessions() == {}
    assert sessions._qoder_sessions_cache

    # DB present but no chat_message table: legitimate empty, cached.
    db = root / DB_SUFFIX
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE other (x integer)")
    conn.commit()
    conn.close()
    sessions._qoder_sessions_cache.clear()
    sessions._qoder_sessions_cache_sig = ()
    assert _qoder_sessions() == {}
    assert sessions._qoder_sessions_cache


def test_corrupt_db_raises(monkeypatch, tmp_path):
    root = tmp_path / "qoder"
    db = root / DB_SUFFIX
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a sqlite database at all, just bytes. padding padding.")
    _setup(monkeypatch, tmp_path, root)

    with pytest.raises(QoderReadError):
        _qoder_sessions()
    assert sessions._qoder_sessions_cache == {}
    # No silent {} at the public layer either.
    with pytest.raises(QoderReadError):
        get_sessions_data("qoder", "all")


def test_close_failure_returns_but_does_not_cache(monkeypatch, tmp_path):
    root = tmp_path / "qoder"
    _make_db(root, [_msg("r1", "s1", _ti(100, 10))])
    _setup(monkeypatch, tmp_path, root)

    real_snapshot = sessions.zcode_snapshot

    @contextmanager
    def close_failing(db_path):
        with real_snapshot(db_path) as snap:
            yield snap
        snap.close_failed = True

    monkeypatch.setattr(sessions, "zcode_snapshot", close_failing)
    raw = _qoder_sessions()
    assert raw["s1"]["turns"]
    assert sessions._qoder_sessions_cache == {}
    monkeypatch.undo()
    monkeypatch.setenv("QODER_IDE_DATA_DIR", str(root))
    raw2 = _qoder_sessions()
    assert raw2["s1"]["turns"]
    assert sessions._qoder_sessions_cache


def test_orphan_message_and_naming(monkeypatch, tmp_path):
    root = tmp_path / "qoder"
    long_title = "T" * 200
    _make_db(root, [
        _msg("r1", "s-named", _ti(100, 10), BASE),
        _msg("r2", "s-orphan", _ti(200, 20), BASE + 1000),
        _msg("r3", "s-long", _ti(300, 30), BASE + 2000),
    ], [
        ("s-named", "My  Project", "/work/ai_anime_videos"),
        ("s-long", long_title, "/work/x"),
    ])
    _setup(monkeypatch, tmp_path, root)

    by_id = {s["session_id"]: s for s in get_sessions_data("qoder", "all")["sessions"]}
    named = by_id["s-named"]
    assert named["display_name"] == "My Project"  # whitespace collapsed
    assert named["project"] == "ai_anime_videos"  # final path element
    orphan = by_id["s-orphan"]
    assert orphan["project"] == "unknown"
    assert orphan["display_name"] == "s-orphan"  # short-id fallback
    assert orphan["token_events"] == 1
    assert orphan["tokens"] == 220
    long = by_id["s-long"]
    assert len(long["display_name"]) == sessions.DISPLAY_NAME_MAX_CHARS
    assert long["display_name"].endswith("…")


def test_session_less_row_bills_under_synthetic_id(monkeypatch, tmp_path):
    """A row with no session_id still reaches Sessions, not just Overview.

    QoderIdeParser bills every row whose token_info parses; the loader must
    not drop the session-less ones or the same tokens would appear in
    Overview and vanish from Sessions. Each becomes its own synthetic
    session so per-row parity is exact.
    """
    root = tmp_path / "qoder-sessionless"
    _make_db(root, [
        _msg("r1", "s1", _ti(100, 10), BASE, model_key="gpt-x"),
        _msg("r2", "", _ti(200, 20, 50), BASE + 1000, model_key="gpt-x"),
        # A long row id too: real chat_message ids are not two characters.
        _msg("row-3b44336653874aa9", "   ", _ti(300, 30), BASE + 2000, model_key="gpt-x"),
    ], [("s1", "Named", "/work/p")])
    _setup(monkeypatch, tmp_path, root)

    raw = _qoder_sessions()
    assert set(raw) == {"s1", "qoder-orphan:r2", "qoder-orphan:row-3b44336653874aa9"}
    orphan = raw["qoder-orphan:r2"]
    assert orphan["project"] == "unknown"
    assert len(orphan["turns"]) == 1
    turn = orphan["turns"][0]
    assert (turn["tokens_in"], turn["tokens_cache"], turn["tokens_out"]) == (150, 50, 20)
    assert turn["_event_key"] == "qoder:r2"

    # Rendered names, not raw ones: every synthetic id starts with the same
    # 8 characters, so the short-id fallback would show each orphan as
    # "qoder-or". Each is named by its own row instead.
    rendered = {row["session_id"]: row["display_name"]
                for row in get_sessions_data("qoder", "all")["sessions"]}
    assert rendered["s1"] == "Named"
    assert rendered["qoder-orphan:r2"] == "r2"
    assert rendered["qoder-orphan:row-3b44336653874aa9"] == "row-3b44"

    # Overview and Sessions bill the same totals: the divergence this
    # synthetic id exists to prevent.
    entries = QoderIdeParser(PricingDatabase()).collect(None, None)
    turns = [t for session in raw.values() for t in session["turns"]]
    assert len(turns) == len(entries) == 3
    assert sum(t["tokens_in"] for t in turns) == sum(e["input"] for e in entries)
    assert sum(t["tokens_cache"] for t in turns) == sum(e["cacheRead"] for e in entries)
    assert sum(t["tokens_out"] for t in turns) == sum(e["output"] for e in entries)
    assert sum(t["cost"] for t in turns) == pytest.approx(
        sum(e["cost"] for e in entries))


def test_cache_invalidates_on_write(monkeypatch, tmp_path):
    root = tmp_path / "qoder"
    _make_db(root, [_msg("r1", "s1", _ti(100, 10))])
    _setup(monkeypatch, tmp_path, root)

    first = _qoder_sessions()
    assert sessions._qoder_sessions_cache[(None, None)] is first
    assert _qoder_sessions() is first  # dict hit: zero file I/O

    # Append a row: the file signature changes, the cache must miss.
    db = root / DB_SUFFIX
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO chat_message (id, session_id, request_id, role, "
        "token_info, model_info, gmt_create) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("r2", "s1", "req-r2", "assistant", _ti(100, 10), "", BASE + 5000),
    )
    conn.commit()
    conn.close()
    st = db.stat()
    os.utime(str(db), ns=(st.st_mtime_ns + 10_000_000, st.st_mtime_ns + 10_000_000))

    third = _qoder_sessions()
    assert third is not first
    assert sum(t["tokens"] for t in third["s1"]["turns"]) == 220


def test_registration_and_api(monkeypatch, tmp_path):
    assert "qoder" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["qoder"] == "Qoder IDE"

    root = tmp_path / "qoder"
    _make_db(root, [_msg("r1", "s1", _ti(100, 10))], [("s1", "Named", "/work/p")])
    _setup(monkeypatch, tmp_path, root)

    # Broken-read check first: a successful _qoder_sessions read would be
    # cached under the same signature and shadow the failure, and a route
    # response cache hit would shadow it at the API layer.
    @contextmanager
    def broken_snapshot(db_path):
        raise ZCodeSnapshotError("boom")
        yield  # unreachable

    monkeypatch.setattr(sessions, "zcode_snapshot", broken_snapshot)
    with pytest.raises(HTTPException) as exc:
        api.get_sessions(tool="qoder", period="all")
    assert exc.value.status_code == 500
    with pytest.raises(HTTPException) as exc:
        api.get_session(tool="qoder", session_id="s1")
    assert exc.value.status_code == 500

    monkeypatch.undo()
    monkeypatch.setenv("QODER_IDE_DATA_DIR", str(root))
    data = get_sessions_data("qoder", "all")
    assert data["sessions"][0]["session_id"] == "s1"
    assert data["sessions"][0]["display_name"] == "Named"
    detail = get_session_detail("qoder", "s1")
    assert detail["turns"][0]["tokens"] == 110
    listing = api.get_sessions(tool="qoder", period="all")
    assert listing["sessions"][0]["session_id"] == "s1"
    detail_api = api.get_session(tool="qoder", session_id="s1")
    assert detail_api["turns"][0]["model"] == "auto"
    assert "_bill" not in detail_api["turns"][0]


def test_frontend_session_registry_includes_qoder():
    index = Path(sessions.__file__).parent / "static" / "index.html"
    source = index.read_text(encoding="utf-8")
    assert "'workbuddy', 'qoder'" in source
    assert "workbuddy: null, qoder: null, combined: null" in source
    assert 'updateSessionPanel("qoder", lastSessionsResponses.qoder);' in source
    assert 'initSortHeaders("qoder", renderSessionsTab);' in source
    assert "qoder: { ...DEFAULT_SORT }," in source
    assert "qoderSessions: 'Qoder IDE Sessions'," in source
    assert "qoderSessions: 'Qoder IDE \u4f1a\u8bdd'," in source
    assert 'id="qoderSessionsTable"' in source
    assert 'data-panel-details="qoder"' in source
