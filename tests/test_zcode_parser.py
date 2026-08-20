"""Tests for ZCodeParser (temp-dir snapshot reads of a WAL-mode source DB)."""
import shutil
import sqlite3
from pathlib import Path

import pytest

from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import CodingToolsUsageTracker, ZCodeParser

# Faithful subset of the real model_usage schema (observed on macOS, ZCode 3.7.7):
# all columns the parser selects, with the real constraints.
MODEL_USAGE_DDL = """
CREATE TABLE model_usage (
    id text primary key,
    logical_request_id text not null,
    attempt_index integer not null default 0,
    session_id text not null,
    turn_id text,
    provider_id text not null,
    model_id text not null,
    status text not null check(status in ('running', 'completed', 'error', 'cancelled')),
    started_at integer not null,
    completed_at integer,
    input_tokens integer not null default 0,
    output_tokens integer not null default 0,
    reasoning_tokens integer not null default 0,
    cache_creation_input_tokens integer not null default 0,
    cache_read_input_tokens integer not null default 0,
    provider_total_tokens integer,
    computed_total_tokens integer not null default 0,
    retry_count integer not null default 0
)
"""

INSERT = (
    "INSERT INTO model_usage (id, logical_request_id, attempt_index, session_id, "
    "provider_id, model_id, status, started_at, input_tokens, output_tokens, "
    "reasoning_tokens, cache_creation_input_tokens, cache_read_input_tokens) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _row(
    row_id="mu-1",
    logical="lr-1",
    attempt=0,
    session="sess-1",
    provider="builtin:zai-start-plan",
    model="GLM-5-Turbo",
    status="completed",
    started_at=1787161506933,
    input_tokens=16026,
    output_tokens=24,
    reasoning_tokens=0,
    cache_write=0,
    cache_read=11776,
):
    return (
        row_id, logical, attempt, session, provider, model, status, started_at,
        input_tokens, output_tokens, reasoning_tokens, cache_write, cache_read,
    )


def _create_db(db_path: Path, rows: list) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(MODEL_USAGE_DDL)
    conn.executemany(INSERT, rows)
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _clean_query_cache():
    ZCodeParser._query_cache.clear()
    ZCodeParser._query_cache_sig = ()
    yield
    ZCodeParser._query_cache.clear()
    ZCodeParser._query_cache_sig = ()


def _parser(monkeypatch, tmp_path, db_path: Path):
    monkeypatch.setenv("ZCODE_HOME", str(db_path.parent.parent.parent))
    return ZCodeParser(PricingDatabase())


def test_zcode_basic_mapping(monkeypatch, tmp_path):
    """The observed live row maps with the cached slice subtracted once."""
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(db_path, [_row()])

    entries = _parser(monkeypatch, tmp_path, db_path).collect(None, None)
    assert len(entries) == 1
    e = entries[0]
    pricing = PricingDatabase()
    # input_tokens 16026 is inclusive of the 11776 cached tokens.
    assert e["input"] == 16026 - 11776
    assert e["cacheRead"] == 11776
    assert e["cacheWrite"] == 0
    assert e["output"] == 24
    assert e["reasoning"] == 0
    assert e["model"] == "GLM-5-Turbo"
    assert e["provider"] == "builtin:zai-start-plan"
    assert e["source"] == "zcode"
    assert e["timestamp"] == 1787161506933
    assert e["entry_id"] == "zcode:mu-1"
    # Cost bills the fresh slice at the input rate and the cached slice at the
    # cache rate — never the raw inclusive input.
    assert e["cost"] == pytest.approx(
        pricing.get_cost("GLM-5-Turbo", 4250, 24, 11776, 0)
    )
    assert e["cost"] != pytest.approx(
        pricing.get_cost("GLM-5-Turbo", 16026, 24, 11776, 0)
    )


def test_zcode_reasoning_split(monkeypatch, tmp_path):
    """Displayed output/reasoning are disjoint; cost uses the full output."""
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(
        db_path,
        [_row(input_tokens=1000, output_tokens=1000, reasoning_tokens=400, cache_read=0)],
    )

    entries = _parser(monkeypatch, tmp_path, db_path).collect(None, None)
    assert len(entries) == 1
    e = entries[0]
    pricing = PricingDatabase()
    assert e["output"] == 600
    assert e["reasoning"] == 400
    # compute.py adds input + output + cacheRead + reasoning, so the displayed
    # total is 1000 (input) + 600 + 400 = the source's own input+output total.
    assert e["input"] + e["output"] + e["reasoning"] == 2000
    # Cost bills the full 1000 output tokens (reasoning at the output rate).
    assert e["cost"] == pytest.approx(pricing.get_cost("GLM-5-Turbo", 1000, 1000, 0, 0))
    assert e["cost"] != pytest.approx(pricing.get_cost("GLM-5-Turbo", 1000, 600, 0, 0))


def test_zcode_reasoning_anomaly_disjoint_fallback(monkeypatch, tmp_path):
    """If reasoning is ever reported above output (subset assumption broken
    for that row), both display and billing must treat the two as disjoint:
    no negative split, no billed/displayed mismatch."""
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(
        db_path,
        [_row(input_tokens=100, output_tokens=100, reasoning_tokens=400, cache_read=0)],
    )

    entries = _parser(monkeypatch, tmp_path, db_path).collect(None, None)
    assert len(entries) == 1
    e = entries[0]
    pricing = PricingDatabase()
    assert e["output"] == 100
    assert e["reasoning"] == 400
    # Billed output is 100 + 400, exactly the displayed output+reasoning sum.
    assert e["cost"] == pytest.approx(pricing.get_cost("GLM-5-Turbo", 100, 500, 0, 0))


def test_zcode_retries_kept_distinct(monkeypatch, tmp_path):
    """Each attempt is its own billable row; nothing dedups across attempts."""
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(
        db_path,
        [
            _row(row_id="mu-1", logical="lr-1", attempt=0),
            _row(row_id="mu-2", logical="lr-1", attempt=1),
        ],
    )

    entries = _parser(monkeypatch, tmp_path, db_path).collect(None, None)
    assert len(entries) == 2
    assert {e["entry_id"] for e in entries} == {"zcode:mu-1", "zcode:mu-2"}


def test_zcode_all_zero_row_skipped(monkeypatch, tmp_path):
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(
        db_path,
        [_row(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0)],
    )
    assert _parser(monkeypatch, tmp_path, db_path).collect(None, None) == []


def test_zcode_cancelled_request_with_tokens_kept(monkeypatch, tmp_path):
    """A cancelled request that already burned prompt tokens still bills."""
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(
        db_path,
        [_row(status="cancelled", input_tokens=5000, output_tokens=0, cache_read=0)],
    )
    entries = _parser(monkeypatch, tmp_path, db_path).collect(None, None)
    assert len(entries) == 1
    assert entries[0]["input"] == 5000


def test_zcode_empty_model_skipped(monkeypatch, tmp_path):
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(
        db_path,
        [_row(model=""), _row(row_id="mu-2")],
    )
    entries = _parser(monkeypatch, tmp_path, db_path).collect(None, None)
    assert len(entries) == 1
    assert entries[0]["entry_id"] == "zcode:mu-2"


def test_zcode_date_window_half_open(monkeypatch, tmp_path):
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    base = 1787161506000
    _create_db(
        db_path,
        [
            _row(row_id="before", started_at=base - 1),
            _row(row_id="at_start", started_at=base),
            _row(row_id="at_end", started_at=base + 60_000),
            _row(row_id="after", started_at=base + 60_000 + 1),
        ],
    )

    from datetime import datetime, timezone

    since = datetime.fromtimestamp(base / 1000, tz=timezone.utc)
    until = datetime.fromtimestamp((base + 60_000) / 1000, tz=timezone.utc)
    entries = _parser(monkeypatch, tmp_path, db_path).collect(since, until)
    assert {e["entry_id"] for e in entries} == {"zcode:at_start"}


def test_zcode_default_home_path(monkeypatch, tmp_path):
    """Without ZCODE_HOME the DB is read from ~/.zcode/cli/db/db.sqlite."""
    monkeypatch.delenv("ZCODE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".zcode" / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(db_path, [_row()])

    parser = ZCodeParser(PricingDatabase())
    assert parser.db_path == db_path
    assert len(parser.collect(None, None)) == 1


def test_zcode_missing_db(monkeypatch, tmp_path):
    monkeypatch.setenv("ZCODE_HOME", str(tmp_path / "nowhere"))
    assert ZCodeParser(PricingDatabase()).collect(None, None) == []


def test_zcode_wal_freshness(monkeypatch, tmp_path):
    """Rows that land in the WAL after the first read are picked up: the
    signature covers -wal (and -shm), not just the main file."""
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)

    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.executescript(MODEL_USAGE_DDL)
    writer.execute(
        "INSERT INTO model_usage (id, logical_request_id, session_id, provider_id, "
        "model_id, status, started_at, input_tokens, output_tokens) "
        "VALUES ('mu-1','lr-1','s','p','GLM-5-Turbo','completed',1787161506000,100,10)"
    )
    writer.commit()
    try:
        monkeypatch.setenv("ZCODE_HOME", str(home))
        parser = ZCodeParser(PricingDatabase())
        assert len(parser.collect(None, None)) == 1

        # Second row goes into the WAL (writer connection stays open, no
        # checkpoint), changing the -wal size/mtime the signature covers.
        writer.execute(
            "INSERT INTO model_usage (id, logical_request_id, session_id, provider_id, "
            "model_id, status, started_at, input_tokens, output_tokens) "
            "VALUES ('mu-2','lr-2','s','p','GLM-5-Turbo','completed',1787161507000,200,20)"
        )
        writer.commit()
        assert len(parser.collect(None, None)) == 2
    finally:
        writer.close()


def test_zcode_snapshot_open_failure_skips(monkeypatch, tmp_path):
    """A snapshot open failure skips the source with a single attempt and
    no fallback: the only connection attempt targets the private temp-dir
    copy, never the source path, and _open_snapshot returns None."""
    import tokdash.sources.coding_tools as ct

    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(db_path, [_row()])
    monkeypatch.setenv("ZCODE_HOME", str(home))

    calls = []

    def fake_connect(*args, **kwargs):
        calls.append((args, kwargs))
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(ct.sqlite3, "connect", fake_connect)

    created = []

    def fake_mkdtemp(suffix=None, prefix=None, dir=None):
        d = tmp_path / f"zcode-snap-{len(created)}"
        d.mkdir()
        created.append(d)
        return str(d)

    monkeypatch.setattr(ct.tempfile, "mkdtemp", fake_mkdtemp)
    parser = ZCodeParser(PricingDatabase())

    assert parser._open_snapshot() is None
    assert len(calls) == 1
    (conn_arg,), _ = calls[0]
    assert conn_arg != str(db_path)
    assert Path(conn_arg).name == "db.sqlite"

    assert parser.collect(None, None) == []
    # The collect-level attempt is also a single open of the private copy;
    # there is no second attempt in another mode against the source.
    assert len(calls) == 2
    assert calls[1][0][0] != str(db_path)
    # Both failed attempts' temp dirs are removed, not leaked.
    assert len(created) == 2
    assert not any(d.exists() for d in created)


def test_zcode_failed_read_not_cached(monkeypatch, tmp_path):
    """A failed read is not cached: the next collect retries and returns the
    rows once the read recovers, even though the file signatures are
    unchanged."""
    import tokdash.sources.coding_tools as ct

    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(db_path, [_row()])
    monkeypatch.setenv("ZCODE_HOME", str(home))

    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        raise sqlite3.OperationalError("simulated transient read failure")

    parser = ZCodeParser(PricingDatabase())
    ct.sqlite3.connect = failing_connect
    try:
        assert parser.collect(None, None) == []
    finally:
        ct.sqlite3.connect = real_connect

    # Same files on disk, same signature: the empty result must not have
    # been cached, so this collect re-reads and succeeds.
    assert len(parser.collect(None, None)) == 1


class _FlakyCursor:
    """Wraps a real cursor; fails the first sqlite_master query exactly
    once. sqlite3.Cursor is an immutable C type, so the wrapper forwards
    everything else instead of shadowing methods."""

    def __init__(self, cur, flaky):
        self._cur = cur
        self._flaky = flaky

    def execute(self, sql, *params, **kw):
        if not self._flaky["probe_failed"] and "sqlite_master" in sql:
            self._flaky["probe_failed"] = True
            raise sqlite3.OperationalError("simulated probe failure")
        return self._cur.execute(sql, *params, **kw)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _FlakyConnection:
    """Wraps a real connection; every cursor() fails the first
    sqlite_master query exactly once. Attribute writes (row_factory) and
    reads (close) forward to the wrapped connection."""

    def __init__(self, conn, flaky):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_flaky", flaky)

    def cursor(self):
        return _FlakyCursor(self._conn.cursor(), self._flaky)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


def test_zcode_table_probe_failure_not_cached(monkeypatch, tmp_path):
    """A transient sqlite_master probe error is a failed read, not an
    "absent table": the empty result is not cached, and the next collect
    retries and returns the rows even though the file signatures are
    unchanged."""
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(db_path, [_row()])
    monkeypatch.setenv("ZCODE_HOME", str(home))

    parser = ZCodeParser(PricingDatabase())
    real_open = parser._open_snapshot
    # One flaky state shared across collects: the first probe fails, the
    # retry's probe succeeds.
    flaky = {"probe_failed": False}

    def flaky_open():
        conn, tmpdir = real_open()
        return _FlakyConnection(conn, flaky), tmpdir

    monkeypatch.setattr(parser, "_open_snapshot", flaky_open)

    assert parser.collect(None, None) == []
    # Same files on disk, same signature: the probe failure must not have
    # been cached as an "absent table" empty success.
    assert len(parser.collect(None, None)) == 1


def test_zcode_registered_in_tracker():
    tracker = CodingToolsUsageTracker()
    assert "zcode" in tracker.parsers
    assert isinstance(tracker.parsers["zcode"], ZCodeParser)


def test_zcode_no_sidecar_writes_in_source_dir(monkeypatch, tmp_path):
    """Reading must not create db.sqlite-shm (or anything else) in the
    source directory: SQLite creates a missing -shm even for a ?mode=ro
    open of a WAL database, so the parser reads a temp-dir snapshot. The
    fixture is the crash-recovery state - db + wal, no -shm - with the row
    living only in the WAL, which also proves the WAL was read."""
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)

    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.executescript(MODEL_USAGE_DDL)
    writer.execute(INSERT, _row())
    writer.commit()
    # Crash-recovery state: copy db + wal (NOT -shm) to the live location
    # while the writer still holds the uncheckpointed WAL, then close it.
    live_dir = tmp_path / "live" / "cli" / "db"
    live_dir.mkdir(parents=True)
    shutil.copy2(db_path, live_dir / "db.sqlite")
    shutil.copy2(db_path.parent / "db.sqlite-wal", live_dir / "db.sqlite-wal")
    writer.close()  # checkpoints and deletes the ORIGINAL wal/shm only

    monkeypatch.setenv("ZCODE_HOME", str(tmp_path / "live"))
    before = sorted(p.name for p in live_dir.iterdir())
    assert before == ["db.sqlite", "db.sqlite-wal"]

    import tokdash.sources.coding_tools as ct

    created = []

    def fake_mkdtemp(suffix=None, prefix=None, dir=None):
        d = tmp_path / f"zcode-snap-{len(created)}"
        d.mkdir()
        created.append(d)
        return str(d)

    monkeypatch.setattr(ct.tempfile, "mkdtemp", fake_mkdtemp)

    entries = ZCodeParser(PricingDatabase()).collect(None, None)
    assert len(entries) == 1  # the WAL-only row was read from the snapshot
    # No sidecar (or any) file appears in the source directory, and the
    # exact temp-dir snapshot is removed afterwards.
    assert sorted(p.name for p in live_dir.iterdir()) == before
    assert len(created) == 1
    assert not created[0].exists()


def test_zcode_stale_read_not_stored_after_signature_change(monkeypatch, tmp_path):
    """A result fetched under an older file signature (its query in flight
    while a concurrent collect advanced the signature) is returned for that
    request but NOT stored under the new signature - no indefinite stale
    cache."""
    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(db_path, [_row()])
    monkeypatch.setenv("ZCODE_HOME", str(home))
    parser = ZCodeParser(PricingDatabase())

    real_open = parser._open_snapshot
    state = {"intercepted": False}

    def interleaved_open():
        # A's snapshot, taken before the source change below.
        snap = real_open()
        if not state["intercepted"]:
            state["intercepted"] = True
            # B: the source gains a row (new signature) and a full collect
            # runs to completion, advancing _query_cache_sig and caching
            # the fresh data.
            db_path.unlink()
            _create_db(db_path, [_row(), _row(row_id="mu-2", logical="lr-2", attempt=1)])
            assert len(parser.collect(None, None)) == 2
        return snap

    monkeypatch.setattr(parser, "_open_snapshot", interleaved_open)

    # A finishes its (stale) query and must skip the store.
    assert len(parser.collect(None, None)) == 1
    # C: the cache hit must serve B's fresh data, not A's stale store.
    assert len(parser.collect(None, None)) == 2


def test_zcode_snapshot_retries_when_source_changes_mid_copy(monkeypatch, tmp_path):
    """If the source db is replaced between the two copies (ZCode
    checkpointing or rewriting mid-copy), the attempt would mix two
    generations, so the db/-wal signatures are re-checked after copying
    and the attempt is dropped and re-copied: the result is the complete
    new generation, never a torn mix."""
    import tokdash.sources.coding_tools as ct

    home = tmp_path / ".zcode"
    db_path = home / "cli" / "db" / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    _create_db(db_path, [_row()])
    monkeypatch.setenv("ZCODE_HOME", str(home))

    real_copy2 = ct.shutil.copy2
    state = {"fired": False}

    def sneaky_copy2(src, dst, *args, **kwargs):
        result = real_copy2(src, dst, *args, **kwargs)
        if not state["fired"] and Path(dst).name == "db.sqlite":
            state["fired"] = True
            # The source is replaced mid-copy: the copied db is an older
            # generation than the signature taken after the copy.
            db_path.unlink()
            _create_db(db_path, [_row(), _row(row_id="mu-2", logical="lr-2", attempt=1)])
        return result

    monkeypatch.setattr(ct.shutil, "copy2", sneaky_copy2)
    entries = ZCodeParser(PricingDatabase()).collect(None, None)
    # First attempt is torn and retried; the stored result is the
    # complete second generation.
    assert len(entries) == 2
    assert {e["entry_id"] for e in entries} == {"zcode:mu-1", "zcode:mu-2"}
