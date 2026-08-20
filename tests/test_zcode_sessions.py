"""Tests for ZCode as a session source (sessions.py, phase 2).

Pins the token-turn vs activity split, the phase-1 accounting parity,
the event-timestamp windowing (E = COALESCE(completed_at, started_at)),
the tri-state read contract, and the activity-only active-time path.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException

from tokdash import api, sessions
from tokdash.dateutil import parse_date_range
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    ZCodeReadError,
    _build_turn,
    _public_turns,
    _repriced_turns,
    _session_active_intervals,
    _zcode_sessions,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources.coding_tools import ZCodeParser, ZCodeSnapshotError

BASE = 1_787_000_000_000

SESSION_DDL = """
CREATE TABLE session (
    id text primary key,
    parent_id text,
    directory text not null,
    title text not null,
    time_created integer not null,
    time_updated integer not null
);
"""
TURN_USAGE_DDL = """
CREATE TABLE turn_usage (
    session_id text not null,
    turn_id text not null,
    status text not null,
    started_at integer not null,
    completed_at integer,
    duration_ms integer,
    model_request_count integer not null default 0,
    model_retry_count integer not null default 0,
    tool_call_count integer not null default 0,
    error_type text,
    error_code text,
    primary key(session_id, turn_id)
);
"""
MODEL_USAGE_DDL = """
CREATE TABLE model_usage (
    id text primary key,
    session_id text not null,
    turn_id text,
    model_id text not null,
    provider_id text not null,
    status text not null,
    started_at integer not null,
    input_tokens integer not null default 0,
    output_tokens integer not null default 0,
    reasoning_tokens integer not null default 0,
    cache_creation_input_tokens integer not null default 0,
    cache_read_input_tokens integer not null default 0
);
"""


@pytest.fixture(autouse=True)
def _clean_caches():
    sessions._zcode_sessions_cache.clear()
    sessions._zcode_sessions_cache_sig = ()
    ZCodeParser._query_cache.clear()
    ZCodeParser._query_cache_sig = ()
    reload_pricing_db()
    yield
    sessions._zcode_sessions_cache.clear()
    sessions._zcode_sessions_cache_sig = ()
    ZCodeParser._query_cache.clear()
    ZCodeParser._query_cache_sig = ()
    reload_pricing_db()


def _db_path(tmp_path: Path) -> Path:
    home = tmp_path / ".zcode"
    db = home / "cli" / "db" / "db.sqlite"
    db.parent.mkdir(parents=True)
    return db


def _write(db: Path, sess=(), turns=(), models=()) -> None:
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "DROP TABLE IF EXISTS model_usage; DROP TABLE IF EXISTS turn_usage; "
        "DROP TABLE IF EXISTS session;"
        + SESSION_DDL + TURN_USAGE_DDL + MODEL_USAGE_DDL
    )
    conn.executemany(
        "INSERT INTO session (id, parent_id, directory, title, time_created, "
        "time_updated) VALUES (?,?,?,?,?,?)",
        sess,
    )
    conn.executemany(
        "INSERT INTO turn_usage (session_id, turn_id, status, started_at, "
        "completed_at, duration_ms, model_request_count, model_retry_count, "
        "tool_call_count, error_type, error_code) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        turns,
    )
    conn.executemany(
        "INSERT INTO model_usage (id, session_id, turn_id, model_id, "
        "provider_id, status, started_at, input_tokens, output_tokens, "
        "reasoning_tokens, cache_creation_input_tokens, cache_read_input_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        models,
    )
    conn.commit()
    conn.close()


def _sess(sid="sess-1", parent=None, directory="/tmp/proj", title="proj session"):
    return (sid, parent, directory, title, BASE, BASE + 9_999_999_999)


def _turn(sid="sess-1", tid="turn-1", status="completed", started=None,
          completed=None, duration=10_000, req=1, retry=0, tools=0,
          etype=None, ecode=None):
    return (sid, tid, status,
            BASE if started is None else started,
            BASE + 10_000 if completed is None else completed,
            duration, req, retry, tools, etype, ecode)


def _mu(mid="mu-1", sid="sess-1", tid="turn-1", model="GLM-5-Turbo",
        provider="builtin:zai-start-plan", started=None, inp=1000, out=100,
        reason=0, cw=0, cr=400):
    return (mid, sid, tid, model, provider, "completed",
            BASE + 100 if started is None else started,
            inp, out, reason, cw, cr)


def _load(monkeypatch, tmp_path, db: Path, since_ms=None, until_ms=None):
    monkeypatch.setenv("ZCODE_HOME", str(db.parent.parent.parent))
    return _zcode_sessions(since_ms=since_ms, until_ms=until_ms)


def test_zcode_registered():
    assert "zcode" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["zcode"] == "ZCode"


def test_mapping(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(db, sess=[_sess()], turns=[_turn()], models=[_mu()])
    raw = _load(monkeypatch, tmp_path, db)

    s = raw["sess-1"]
    assert s["display_name"] == "proj session"
    assert s["project"] == "proj"
    turn = s["turns"][0]
    # E = completed_at is the event timestamp.
    assert turn["timestamp_ms"] == BASE + 10_000
    # input 1000 is inclusive of the 400 cached: fresh 600 + cache write 0.
    assert turn["tokens_in"] == 600
    assert turn["tokens_cache"] == 400
    assert turn["tokens_out"] == 100
    assert turn["tokens_reasoning"] == 0
    assert turn["cost"] == pytest.approx(
        PricingDatabase().get_cost("GLM-5-Turbo", 600, 100, 400, 0)
    )
    assert turn["_event_key"] == "zcode:sess-1:turn-1"
    assert turn["_work_ms"] == 10_000
    # D5 turn-level fields.
    assert turn["status"] == "completed"
    assert turn["model_request_count"] == 1
    assert turn["model_retry_count"] == 0
    assert turn["tool_call_count"] == 0
    assert turn["error_type"] is None
    assert turn["error_code"] is None


def test_displayed_input_includes_cache_write(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(db, sess=[_sess()], turns=[_turn()], models=[_mu(cw=250)])
    raw = _load(monkeypatch, tmp_path, db)
    turn = raw["sess-1"]["turns"][0]
    # Displayed input = fresh + cache write; billing input stays fresh.
    assert turn["tokens_in"] == 600 + 250
    bill = turn["_bills"][0]
    assert bill["input"] == 600
    assert bill["cache_write"] == 250


def test_parity_with_usage_parser(monkeypatch, tmp_path):
    """Same rows: the session turn totals equal the phase-1 entries."""
    db = _db_path(tmp_path)
    _write(
        db,
        sess=[_sess()],
        turns=[_turn(req=2, retry=1)],
        models=[
            _mu(mid="mu-1", reason=30, cr=400, inp=1000, out=100),
            _mu(mid="mu-2", tid="turn-1", started=BASE + 500, inp=700,
                out=50, cr=0, cw=20),
        ],
    )
    monkeypatch.setenv("ZCODE_HOME", str(db.parent.parent.parent))
    raw = _zcode_sessions()
    entries = ZCodeParser(PricingDatabase()).collect(None, None)
    assert len(entries) == 2

    turn = raw["sess-1"]["turns"][0]
    assert turn["cost"] == pytest.approx(sum(e["cost"] for e in entries))
    assert turn["tokens_in"] == sum(e["input"] + e["cacheWrite"] for e in entries)
    assert turn["tokens_cache"] == sum(e["cacheRead"] for e in entries)
    assert turn["tokens_out"] == sum(e["output"] for e in entries)
    assert turn["tokens_reasoning"] == sum(e["reasoning"] for e in entries)
    # turn total = the additive displayed total of the entries.
    assert turn["tokens"] == sum(
        e["input"] + e["cacheWrite"] + e["cacheRead"] + e["output"] + e["reasoning"]
        for e in entries
    )


def test_multi_model_turn_bills_per_group(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(
        db,
        sess=[_sess()],
        turns=[_turn()],
        models=[
            _mu(mid="mu-1", model="GLM-5-Turbo", inp=1000, out=100, cr=0),
            _mu(mid="mu-2", model="GLM-5", started=BASE + 500, inp=100,
                out=10, cr=0),
        ],
    )
    raw = _load(monkeypatch, tmp_path, db)
    turn = raw["sess-1"]["turns"][0]
    assert len(turn["_bills"]) == 2
    pricing = PricingDatabase()
    assert turn["model"] == "GLM-5-Turbo"  # displayed-token majority
    assert turn["cost"] == pytest.approx(
        pricing.get_cost("GLM-5-Turbo", 1000, 100, 0, 0)
        + pricing.get_cost("GLM-5", 100, 10, 0, 0)
    )


def test_window_is_half_open_on_e(monkeypatch, tmp_path):
    S, U = BASE, BASE + 1_000_000
    db = _db_path(tmp_path)
    _write(
        db,
        sess=[
            _sess("s-cross"),   # started before S, E in-window -> present
            _sess("s-at-until"),  # E == U -> absent (half-open)
            _sess("s-before"),  # E < S -> absent
        ],
        turns=[
            _turn(sid="s-cross", tid="t1", started=S - 500_000, completed=S + 1000),
            _turn(sid="s-at-until", tid="t2", started=S + 500_000, completed=U),
            _turn(sid="s-before", tid="t3", started=S - 900_000, completed=S - 1000),
        ],
        models=[
            _mu(mid="m1", sid="s-cross", tid="t1", started=S - 400_000),
            _mu(mid="m2", sid="s-at-until", tid="t2", started=S + 600_000),
            _mu(mid="m3", sid="s-before", tid="t3", started=S - 800_000),
        ],
    )
    raw = _load(monkeypatch, tmp_path, db, since_ms=S, until_ms=U)
    assert "s-cross" in raw and raw["s-cross"]["turns"]
    # E == U is not a token turn (half-open), but its measured work
    # [U - 10_000, U] overlaps the window, so it enters as an
    # activity-only session with the next-boundary event.
    assert "s-at-until" in raw
    assert raw["s-at-until"]["turns"] == []
    assert raw["s-at-until"]["_next_event_ms"] == U
    assert raw["s-at-until"]["_next_work_ms"] == 10_000
    assert raw["s-at-until"]["_activity_intervals"] == [(U - 10_000, U)]
    assert "s-before" not in raw


def test_boundary_only_measured_turn(monkeypatch, tmp_path):
    """A long turn finishing after until_ms credits its window overlap even
    though the session has no in-window token event (set B)."""
    S_dt, U_dt = parse_date_range("2026-08-01", "2026-08-01")
    S, U = int(S_dt.timestamp() * 1000), int(U_dt.timestamp() * 1000)
    db = _db_path(tmp_path)
    _write(
        db,
        sess=[_sess("s-b")],
        turns=[_turn(sid="s-b", tid="t1", started=S + 5_000, completed=U + 2_000,
                     duration=9_000)],
    )
    raw = _load(monkeypatch, tmp_path, db, since_ms=S, until_ms=U)
    s = raw["s-b"]
    assert s["turns"] == []
    assert s["_activity_events"] == []
    assert s["_next_event_ms"] == U + 2_000
    assert s["_next_work_ms"] == 9_000
    # work [U + 2_000 - 9_000, U + 2_000] = [U - 7_000, U + 2_000]
    # clipped to [S, U) -> 7_000 ms.
    assert s["_activity_intervals"] == [(U - 7_000, U)]

    listing = get_sessions_data(
        "zcode", "range", date_from="2026-08-01", date_to="2026-08-01"
    )
    assert listing["sessions"] == []
    assert listing["summary"]["active_ms"] == 7_000
    assert listing["summary"]["active_ms_sum"] == 7_000

    # A non-overlapping measured next turn earns no raw session at all.
    _write(
        db,
        sess=[_sess("s-b")],
        turns=[_turn(sid="s-b", tid="t1", started=S + 9_500, completed=U + 2_000,
                     duration=1_000)],
    )
    raw2 = _load(monkeypatch, tmp_path, db, since_ms=S, until_ms=U)
    assert raw2 == {}


def test_activity_only_zero_token_turn(monkeypatch, tmp_path):
    """A zero-token measured turn is activity, not a token event."""
    db = _db_path(tmp_path)
    _write(
        db,
        sess=[_sess("s-a")],
        turns=[_turn(sid="s-a", tid="t1", status="error", duration=5_000,
                     tools=3, etype="tool_failed", ecode="E1")],
        models=[_mu(sid="s-a", tid="t1", inp=0, out=0, cr=0, cw=0)],
    )
    raw = _load(monkeypatch, tmp_path, db)
    s = raw["s-a"]
    assert s["turns"] == []
    assert s["_activity_events"] == [(BASE + 10_000, 5_000)]
    assert s["_activity_intervals"] == [(BASE + 5_000, BASE + 10_000)]

    listing = get_sessions_data("zcode", "all")
    assert listing["sessions"] == []
    assert listing["summary"]["active_ms"] == 5_000
    assert listing["summary"]["active_ms_sum"] == 5_000


def test_activity_mixes_with_token_turns(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(
        db,
        sess=[_sess()],
        turns=[
            _turn(sid="sess-1", tid="t1", started=BASE, completed=BASE + 10_000,
                  duration=10_000),
            _turn(sid="sess-1", tid="t2", started=BASE + 20_000,
                  completed=BASE + 25_000, duration=5_000),
        ],
        models=[
            _mu(mid="m1", tid="t1", started=BASE + 100),
            _mu(mid="m2", tid="t2", started=BASE + 20_100, inp=0, out=0, cr=0),
        ],
    )
    raw = _load(monkeypatch, tmp_path, db)
    s = raw["sess-1"]
    assert len(s["turns"]) == 1  # t2 has no billable model row
    assert s["_activity_events"] == [(BASE + 25_000, 5_000)]

    listing = get_sessions_data("zcode", "all")
    # measured [BASE, BASE+10_000] + [BASE+20_000, BASE+25_000]
    assert listing["sessions"][0]["active_ms"] == 15_000


def test_read_failure_raises_and_is_not_cached(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(db, sess=[_sess()], turns=[_turn()], models=[_mu()])

    @contextmanager
    def broken_snapshot(db_path):
        raise ZCodeSnapshotError("boom")
        yield  # unreachable

    monkeypatch.setattr(sessions, "zcode_snapshot", broken_snapshot)
    with pytest.raises(ZCodeReadError):
        _load(monkeypatch, tmp_path, db)
    assert sessions._zcode_sessions_cache == {}

    # Unpatched: the same (unchanged) files now read fine and are cached.
    monkeypatch.undo()
    monkeypatch.setenv("ZCODE_HOME", str(db.parent.parent.parent))
    raw = _zcode_sessions()
    assert raw["sess-1"]["turns"]
    assert sessions._zcode_sessions_cache


def test_corrupt_db_raises(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    db.write_bytes(b"not a sqlite database at all, just bytes. padding padding.")
    monkeypatch.setenv("ZCODE_HOME", str(db.parent.parent.parent))
    with pytest.raises(ZCodeReadError):
        _zcode_sessions()
    assert sessions._zcode_sessions_cache == {}


def test_close_failure_returns_but_does_not_cache(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(db, sess=[_sess()], turns=[_turn()], models=[_mu()])
    monkeypatch.setenv("ZCODE_HOME", str(db.parent.parent.parent))

    real_snapshot = sessions.zcode_snapshot

    @contextmanager
    def close_failing(db_path):
        with real_snapshot(db_path) as snap:
            yield snap
        snap.close_failed = True

    monkeypatch.setattr(sessions, "zcode_snapshot", close_failing)
    raw = _zcode_sessions()
    assert raw["sess-1"]["turns"]
    assert sessions._zcode_sessions_cache == {}
    # Next read (unpatched, same signature) re-reads and then caches.
    monkeypatch.undo()
    monkeypatch.setenv("ZCODE_HOME", str(db.parent.parent.parent))
    raw2 = _zcode_sessions()
    assert raw2["sess-1"]["turns"]
    assert sessions._zcode_sessions_cache


def test_api_failure_is_500_not_404(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(db, sess=[_sess()], turns=[_turn()], models=[_mu()])
    monkeypatch.setenv("ZCODE_HOME", str(db.parent.parent.parent))

    @contextmanager
    def broken_snapshot(db_path):
        raise ZCodeSnapshotError("boom")
        yield  # unreachable

    monkeypatch.setattr(sessions, "zcode_snapshot", broken_snapshot)
    with pytest.raises(HTTPException) as exc:
        api.get_sessions(tool="zcode", period="all")
    assert exc.value.status_code == 500
    with pytest.raises(HTTPException) as exc:
        api.get_session(tool="zcode", session_id="sess-1")
    assert exc.value.status_code == 500  # not a false 404

    monkeypatch.undo()
    monkeypatch.setenv("ZCODE_HOME", str(db.parent.parent.parent))
    listing = api.get_sessions(tool="zcode", period="all")
    assert listing["sessions"][0]["session_id"] == "sess-1"
    detail = api.get_session(tool="zcode", session_id="sess-1")
    turn = detail["turns"][0]
    assert "_bills" not in turn and "_event_key" not in turn
    assert turn["status"] == "completed"


def test_no_db_is_cached_empty(monkeypatch, tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    monkeypatch.setenv("ZCODE_HOME", str(home))
    assert _zcode_sessions() == {}
    assert sessions._zcode_sessions_cache  # the empty IS cached


def test_missing_turn_usage_table_is_cached_empty(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.executescript(MODEL_USAGE_DDL)
    conn.close()
    monkeypatch.setenv("ZCODE_HOME", str(db.parent.parent.parent))
    assert _zcode_sessions() == {}
    assert sessions._zcode_sessions_cache


def test_subagent_sessions_excluded(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(
        db,
        sess=[_sess("s-top"), _sess("s-child", parent="s-top")],
        turns=[
            _turn(sid="s-top", tid="t1"),
            _turn(sid="s-child", tid="t2"),
        ],
        models=[
            _mu(mid="m1", tid="t1"),
            _mu(mid="m2", sid="s-child", tid="t2"),
        ],
    )
    raw = _load(monkeypatch, tmp_path, db)
    assert "s-top" in raw
    assert "s-child" not in raw


def test_null_turn_id_rows_not_billed(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(
        db,
        sess=[_sess()],
        turns=[_turn()],
        models=[_mu(tid=None)],  # tokens, but no turn association
    )
    raw = _load(monkeypatch, tmp_path, db)
    s = raw["sess-1"]
    # The turn has no billable (session_id, turn_id)-matched row.
    assert s["turns"] == []
    assert s["_activity_events"]  # but its measured work is kept


def test_repriced_bills_and_public_turns(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(
        db,
        sess=[_sess()],
        turns=[_turn()],
        models=[
            _mu(mid="mu-1", model="GLM-5-Turbo", inp=1000, out=100, cr=0),
            _mu(mid="mu-2", model="GLM-5", started=BASE + 500, inp=100,
                out=10, cr=0),
        ],
    )
    raw = _load(monkeypatch, tmp_path, db)
    turn = raw["sess-1"]["turns"][0]
    pricing = PricingDatabase()
    expected = sum(
        pricing.get_cost(b["model"], b["input"], b["output"], b["cache_read"],
                         b["cache_write"])
        for b in turn["_bills"]
    )
    repriced = _repriced_turns([turn], pricing)
    assert repriced[0]["cost"] == pytest.approx(expected)
    assert "_bills" in repriced[0]
    public = _public_turns([repriced[0]])
    assert "_bills" not in public[0]
    assert "timestamp" in public[0] and "timestamp_ms" not in public[0]


def test_unbounded_window_skips_boundaries(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(db, sess=[_sess()], turns=[_turn()], models=[_mu()])
    raw = _load(monkeypatch, tmp_path, db)
    s = raw["sess-1"]
    assert "_next_event_ms" not in s
    assert "_prior_event_ms" not in s


def test_detail_roundtrip(monkeypatch, tmp_path):
    db = _db_path(tmp_path)
    _write(db, sess=[_sess()], turns=[_turn()], models=[_mu()])
    monkeypatch.setenv("ZCODE_HOME", str(db.parent.parent.parent))
    detail = get_session_detail("zcode", "sess-1")
    assert detail["session"]["session_id"] == "sess-1"
    assert detail["session"]["display_name"] == "proj session"
    assert detail["turns"][0]["model_request_count"] == 1
    with pytest.raises(FileNotFoundError):
        get_session_detail("zcode", "nope")

def test_missing_model_table_is_cached_empty(monkeypatch, tmp_path):
    """turn_usage without model_usage (a partially migrated schema) is a
    legitimate empty success, not a read failure: the read is cached and
    a later snapshot failure still serves the empty result."""
    db = _db_path(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.executescript(SESSION_DDL + TURN_USAGE_DDL)
    conn.execute(
        "INSERT INTO session (id, parent_id, directory, title, time_created, "
        "time_updated) VALUES (?,?,?,?,?,?)",
        ("s1", None, "/tmp/proj", "p", BASE, BASE + 1),
    )
    conn.execute(
        "INSERT INTO turn_usage (session_id, turn_id, status, started_at, "
        "completed_at, duration_ms) VALUES (?,?,?,?,?,?)",
        ("s1", "t1", "completed", BASE, BASE + 10_000, 10_000),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("ZCODE_HOME", str(db.parent.parent.parent))
    assert _zcode_sessions() == {}
    assert sessions._zcode_sessions_cache  # the empty IS cached


def test_late_store_under_stale_signature_is_skipped(monkeypatch, tmp_path):
    """A result computed under an older signature must not be stored after
    a concurrent load advanced it (the phase-1 interleaved-store failure
    mode, pinned for the session cache): it is returned for its own
    request, but the cache keeps the newer data for the same key."""
    db = _db_path(tmp_path)
    _write(
        db,
        sess=[_sess("s1")],
        turns=[_turn(sid="s1", tid="t1", started=BASE + 1_000,
                     completed=BASE + 2_000, duration=1_000)],
        models=[_mu(mid="m1", sid="s1", tid="t1", started=BASE + 1_100)],
    )
    sig_a = (sessions._zcode_db_signature(), sessions._pricing_signature())
    raw_a = _load(monkeypatch, tmp_path, db)
    assert set(raw_a) == {"s1"}

    # Collector B rewrites the source (new signature) and completes.
    _write(
        db,
        sess=[_sess("s1"), _sess("s2")],
        turns=[
            _turn(sid="s1", tid="t1", started=BASE + 1_000,
                  completed=BASE + 2_000, duration=1_000),
            _turn(sid="s2", tid="t2", started=BASE + 3_000,
                  completed=BASE + 4_000, duration=1_000),
        ],
        models=[
            _mu(mid="m1", sid="s1", tid="t1", started=BASE + 1_100),
            _mu(mid="m2", sid="s2", tid="t2", started=BASE + 3_100),
        ],
    )
    sig_b = (sessions._zcode_db_signature(), sessions._pricing_signature())
    assert sig_b != sig_a
    raw_b = _load(monkeypatch, tmp_path, db)
    assert set(raw_b) == {"s1", "s2"}

    # A's in-flight result lands late with the old signature: returned
    # for its own request, but not stored.
    stale = dict(raw_a)
    assert sessions._zcode_store(sig_a, (None, None), stale) is stale
    # The cache still serves B's data for the same key.
    assert _load(monkeypatch, tmp_path, db) == raw_b
    # Positive control: storing under the current signature does work.
    replacement = dict(raw_b)
    replacement["s9"] = {"tool": "zcode", "session_id": "s9"}
    assert sessions._zcode_store(sig_b, (None, None), replacement) is replacement
    assert _load(monkeypatch, tmp_path, db) == replacement


def test_prior_boundary_lookup_is_restricted_to_in_window_sessions(monkeypatch, tmp_path):
    """The prior-boundary lookup is restricted to set-A (in-window)
    sessions: a session present only via its next boundary (set B) gets no
    _prior_event_ms, even though it has a prior turn with measured work."""
    S, U = BASE + 60_000, BASE + 120_000
    db = _db_path(tmp_path)
    _write(
        db,
        sess=[_sess("s-in"), _sess("s-out")],
        turns=[
            # s-in (set A): prior turn + in-window token turn.
            _turn(sid="s-in", tid="t0", started=S - 15_000,
                  completed=S - 5_000, duration=10_000),
            _turn(sid="s-in", tid="t1", started=S + 1_000,
                  completed=S + 10_000, duration=9_000),
            # s-out (set B): prior turn + a next-boundary turn whose
            # measured work overlaps the window.
            _turn(sid="s-out", tid="t2", started=S - 20_000,
                  completed=S - 10_000, duration=10_000),
            _turn(sid="s-out", tid="t3", started=U - 20_000,
                  completed=U + 10_000, duration=30_000),
        ],
        models=[_mu(mid="m1", sid="s-in", tid="t1", started=S + 2_000)],
    )
    raw = _load(monkeypatch, tmp_path, db, since_ms=S, until_ms=U)
    assert "s-in" in raw and raw["s-in"]["turns"]
    assert raw["s-in"]["_prior_event_ms"] == S - 5_000
    assert raw["s-in"]["_prior_work_ms"] == 10_000
    assert "s-out" in raw
    assert raw["s-out"]["turns"] == []
    assert raw["s-out"]["_next_event_ms"] == U + 10_000
    assert "_prior_event_ms" not in raw["s-out"]
