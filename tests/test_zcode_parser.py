"""Tests for ZCodeParser (read-only SQLite, WAL-mode source DB)."""
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


def test_zcode_reasoning_sibling_guard(monkeypatch, tmp_path):
    """If reasoning is ever a sibling of output (inclusion unverified), the
    split must not go negative."""
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
    assert e["output"] == 0
    assert e["reasoning"] == 400
    assert e["input"] + e["output"] + e["cacheRead"] + e["reasoning"] >= 0


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


def test_zcode_registered_in_tracker():
    tracker = CodingToolsUsageTracker()
    assert "zcode" in tracker.parsers
    assert isinstance(tracker.parsers["zcode"], ZCodeParser)
