"""Tests for CrushParser: per-project crush.db session aggregates, seconds
timestamps (the schema comment says milliseconds but every write path is
strftime('%s','now')), last-assistant model attribution behind the
is_summary_message guard, and the comma-separated data-dir list."""
import sqlite3
from pathlib import Path

from tokdash import clientpaths
from tokdash.compute import _collect_parser_file
from tokdash.pricing import PricingDatabase
from tokdash.sources import coding_tools as ct
from tokdash.sources.coding_tools import (
    BaseParser,
    CrushParser,
    ZCodeSnapshotError,
    _crush_ts_to_ms,
    _sig_cache,
)

T0 = 1_700_000_000  # seconds

SESSIONS_DDL = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  parent_session_id TEXT,
  title TEXT,
  message_count INTEGER,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cost REAL,
  updated_at INTEGER,
  created_at INTEGER
)
"""


def _messages_ddl(with_summary: bool) -> str:
    summary = ",\n  is_summary_message INTEGER NOT NULL DEFAULT 0" if with_summary else ""
    return f"""
CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  session_id TEXT,
  role TEXT,
  parts TEXT,
  model TEXT,
  provider TEXT,
  created_at INTEGER,
  updated_at INTEGER,
  finished_at INTEGER{summary}
)
"""


def _make_db(data_dir: Path, *, with_summary=True, sessions=(), messages=()) -> Path:
    """sessions: (id, parent, prompt, completion, updated_at, cost);
    messages: (session_id, role, model, provider, created_at, is_summary)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / "crush.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(SESSIONS_DDL)
        conn.execute(_messages_ddl(with_summary))
        for sid, parent, prompt, completion, updated, cost in sessions:
            conn.execute(
                "INSERT INTO sessions (id, parent_session_id, title, message_count, "
                "prompt_tokens, completion_tokens, cost, updated_at, created_at) "
                "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (sid, parent, f"title {sid}", prompt, completion, cost, updated, T0),
            )
        for sid, role, model, provider, created, is_summary in messages:
            if with_summary:
                conn.execute(
                    "INSERT INTO messages (session_id, role, parts, model, provider, "
                    "created_at, is_summary_message) VALUES (?, ?, '', ?, ?, ?, ?)",
                    (sid, role, model, provider, created, is_summary),
                )
            else:
                conn.execute(
                    "INSERT INTO messages (session_id, role, parts, model, provider, "
                    "created_at) VALUES (?, ?, '', ?, ?, ?)",
                    (sid, role, model, provider, created),
                )
        conn.commit()
    finally:
        conn.close()
    return db


def _fresh(data_dirs, monkeypatch) -> CrushParser:
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    monkeypatch.setenv("CRUSH_DATA_DIR", ",".join(str(d) for d in data_dirs))
    return CrushParser(PricingDatabase())


def test_crush_seconds_timestamp_and_last_assistant_model(monkeypatch, tmp_path):
    d1 = tmp_path / "d1"
    _make_db(
        d1,
        sessions=[("s1", None, 500, 250, T0, 0.0)],
        messages=[
            ("s1", "assistant", "gpt-older", "openai", T0 + 1, 0),
            ("s1", "user", None, None, T0 + 2, 0),
            ("s1", "assistant", "gpt-5.5", "openai", T0 + 3, 0),
        ],
    )
    entries = _fresh([d1], monkeypatch).collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "crush"
    assert (e["input"], e["output"], e["cacheRead"], e["cacheWrite"], e["reasoning"]) == (500, 250, 0, 0, 0)
    assert e["timestamp"] == T0 * 1000
    assert e["entry_id"] == "crush:s1"
    # Mixed-model session prices at the last assistant message.
    assert e["model"] == "gpt-5.5"
    assert e["provider"] == "openai"
    assert e["cost"] == PricingDatabase().get_cost("gpt-5.5", 500, 250, 0, 0)
    assert e["cost"] > 0
    assert e["_billing"]["kind"] == "pricing"


def test_crush_millisecond_timestamp_passthrough(monkeypatch, tmp_path):
    d1 = tmp_path / "d1"
    _make_db(
        d1,
        sessions=[("s1", None, 100, 50, T0 * 1000 + 123, 0.0)],
        messages=[("s1", "assistant", "m", "p", T0, 0)],
    )
    entries = _fresh([d1], monkeypatch).collect(None, None)

    assert [e["timestamp"] for e in entries] == [T0 * 1000 + 123]


def test_crush_ts_to_ms_guards():
    assert _crush_ts_to_ms(1_700_000_000) == 1_700_000_000_000
    assert _crush_ts_to_ms(1_700_000_000_123) == 1_700_000_000_123
    assert _crush_ts_to_ms(0) is None
    assert _crush_ts_to_ms(-5) is None
    assert _crush_ts_to_ms("garbage") is None
    assert _crush_ts_to_ms(None) is None


def test_crush_counts_sub_agent_sessions_too(monkeypatch, tmp_path):
    """Crush folds only cost into a parent session
    (coordinator.updateParentSessionCost), never tokens, so a sub-agent's
    tokens exist in the child row alone: filtering to parent_session_id IS
    NULL would drop them. Its own stats queries filter that way because
    they count sessions, not tokens."""
    d1 = tmp_path / "d1"
    _make_db(
        d1,
        sessions=[
            ("s1", None, 100, 50, T0, 0.0),
            ("child", "s1", 999, 111, T0, 0.0),  # sub-agent: tokens live here only
            ("s_zero", None, 0, 0, T0, 0.0),     # nothing to count
            ("s_nomsg", None, 70, 30, T0, 0.0),  # no messages -> unknown model
        ],
        messages=[
            ("s1", "assistant", "m1", "p1", T0, 0),
            ("child", "assistant", "m_child", "p1", T0, 0),
        ],
    )
    entries = _fresh([d1], monkeypatch).collect(None, None)

    assert {(e["entry_id"], e["model"]) for e in entries} == {
        ("crush:s1", "m1"),
        ("crush:child", "m_child"),
        ("crush:s_nomsg", "unknown"),
    }
    child = next(e for e in entries if e["entry_id"] == "crush:child")
    assert (child["input"], child["output"]) == (999, 111)
    nomsg = next(e for e in entries if e["entry_id"] == "crush:s_nomsg")
    assert nomsg["provider"] == "unknown"
    assert nomsg["cost"] == 0.0


def test_crush_latest_summary_message_is_ignored(monkeypatch, tmp_path):
    d1 = tmp_path / "d1"
    _make_db(
        d1,
        sessions=[("s1", None, 100, 50, T0, 0.0)],
        messages=[
            ("s1", "assistant", "real-model", "p", T0 + 1, 0),
            ("s1", "assistant", "summary-model", "p", T0 + 2, 1),
        ],
    )
    entries = _fresh([d1], monkeypatch).collect(None, None)

    assert [e["model"] for e in entries] == ["real-model"]


def test_crush_pre_migration_db_without_summary_column(monkeypatch, tmp_path):
    """The PRAGMA guard drops the predicate when the column is absent, so a
    DB no post-2025-08-10 build has opened still parses."""
    d1 = tmp_path / "d1"
    _make_db(
        d1,
        with_summary=False,
        sessions=[("s1", None, 100, 50, T0, 0.0)],
        messages=[("s1", "assistant", "m1", "p1", T0, 0)],
    )
    entries = _fresh([d1], monkeypatch).collect(None, None)

    assert [(e["entry_id"], e["model"]) for e in entries] == [("crush:s1", "m1")]


def test_crush_env_unset_has_no_rows(monkeypatch, tmp_path):
    d1 = tmp_path / "d1"
    _make_db(
        d1,
        sessions=[("s1", None, 100, 50, T0, 0.0)],
        messages=[("s1", "assistant", "m", "p", T0, 0)],
    )
    monkeypatch.delenv("CRUSH_DATA_DIR", raising=False)
    parser = CrushParser(PricingDatabase())
    assert parser.data_dirs == []
    assert parser.collect(None, None) == []


def test_crush_dirs_without_db_are_filtered(monkeypatch, tmp_path):
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d2.mkdir(parents=True)  # no crush.db
    _make_db(
        d1,
        sessions=[("s1", None, 100, 50, T0, 0.0)],
        messages=[("s1", "assistant", "m1", "p1", T0, 0)],
    )
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    monkeypatch.setenv("CRUSH_DATA_DIR", f" {d1} , {d2} ")
    parser = CrushParser(PricingDatabase())
    assert parser.data_dirs == [d1]

    entries = parser.collect(None, None)
    assert [e["entry_id"] for e in entries] == ["crush:s1"]


def test_crush_file_signatures_fold_sidecars_into_one_entry(monkeypatch, tmp_path):
    """file_replace sync parses once per signature entry
    (compute._collect_parser_file), and each Crush parse copies the DB
    through the WAL snapshot helper, so a sidecar must move the signature
    without adding an entry."""
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    db1 = _make_db(d1, sessions=[("s1", None, 10, 5, T0, 0.0)], messages=[])
    db2 = _make_db(d2, sessions=[("s2", None, 20, 5, T0, 0.0)], messages=[])
    parser = _fresh([d1, d2], monkeypatch)
    before = parser._file_signatures()
    assert [s[0] for s in before] == [str(db1), str(db2)]

    Path(str(db1) + "-wal").write_bytes(b"x" * 64)
    Path(str(db1) + "-shm").write_bytes(b"y" * 32)
    _sig_cache.clear()
    after = parser._file_signatures()
    assert [s[0] for s in after] == [str(db1), str(db2)]
    assert after[0] != before[0]
    assert after[1] == before[1]


def test_crush_parse_all_honours_an_injected_file_scope(monkeypatch, tmp_path):
    """file_replace sync reparses one changed file at a time by injecting a
    single signature; _parse_all must read that DB only, not every dir."""
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    db1 = _make_db(
        d1,
        sessions=[("s1", None, 10, 5, T0, 0.0)],
        messages=[("s1", "assistant", "m1", "p1", T0, 0)],
    )
    _make_db(
        d2,
        sessions=[("s2", None, 20, 5, T0, 0.0)],
        messages=[("s2", "assistant", "m2", "p2", T0, 0)],
    )
    parser = _fresh([d1, d2], monkeypatch)
    assert sorted(e["entry_id"] for e in parser._parse_all()) == ["crush:s1", "crush:s2"]

    sig = next(s for s in parser._file_signatures() if s[0] == str(db1))
    assert [e["entry_id"] for e in _collect_parser_file(parser, sig)] == ["crush:s1"]


def test_crush_recorded_cost_is_ignored(monkeypatch, tmp_path):
    d1 = tmp_path / "d1"
    _make_db(
        d1,
        sessions=[("s1", None, 1_000_000, 0, T0, 999.0)],
        messages=[("s1", "assistant", "gpt-5.5", "openai", T0, 0)],
    )
    entries = _fresh([d1], monkeypatch).collect(None, None)

    assert entries[0]["cost"] == PricingDatabase().get_cost("gpt-5.5", 1_000_000, 0, 0, 0)


def test_crush_one_db_failure_keeps_the_others(monkeypatch, tmp_path):
    da, db = tmp_path / "a", tmp_path / "b"
    _make_db(
        da,
        sessions=[("sa", None, 10, 5, T0, 0.0)],
        messages=[("sa", "assistant", "ma", "pa", T0, 0)],
    )
    _make_db(
        db,
        sessions=[("sb", None, 20, 5, T0, 0.0)],
        messages=[("sb", "assistant", "mb", "pb", T0, 0)],
    )
    real_snapshot = ct.zcode_snapshot

    def flaky_snapshot(db_path):
        if db_path.parent.name == "a":
            raise ZCodeSnapshotError("locked")
        return real_snapshot(db_path)

    monkeypatch.setattr(ct, "zcode_snapshot", flaky_snapshot)
    parser = _fresh([da, db], monkeypatch)

    assert [e["entry_id"] for e in parser._parse_all()] == ["crush:sb"]


def test_crush_all_dbs_failing_is_an_empty_parse(monkeypatch, tmp_path):
    d1 = tmp_path / "d1"
    _make_db(
        d1,
        sessions=[("s1", None, 100, 50, T0, 0.0)],
        messages=[("s1", "assistant", "m", "p", T0, 0)],
    )

    def broken_snapshot(db_path):
        raise ZCodeSnapshotError("locked")

    monkeypatch.setattr(ct, "zcode_snapshot", broken_snapshot)
    parser = _fresh([d1], monkeypatch)

    assert parser._parse_all() == []
    assert parser.collect(None, None) == []
