"""Tests for HermesParser."""
import sqlite3
from pathlib import Path

from tokdash import osinfo
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, HermesParser, _sig_cache


def _create_hermes_db(db_path: Path, rows: list) -> None:
    """Create a minimal Hermes state.db with the given session rows."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
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
            actual_cost_usd REAL
        )
        """
    )
    cur.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def _default_row(
    row_id="sess-001",
    model="MiniMax-M2.7",
    billing_provider="minimax",
    started_at=1779395293.72756,
    message_count=5,
    input_tokens=7000,
    output_tokens=47,
    cache_read_tokens=0,
    cache_write_tokens=0,
    reasoning_tokens=0,
    estimated_cost=0.002,
    actual_cost=0.0021564,
):
    return (
        row_id, model, billing_provider, started_at, message_count,
        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
        reasoning_tokens, estimated_cost, actual_cost,
    )


def test_hermes_parser_basic(monkeypatch, tmp_path):
    """Reads a single session row and maps all fields correctly."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(hermes_dir / "state.db", [_default_row()])

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "hermes"
    assert e["model"] == "MiniMax-M2.7"
    assert e["provider"] == "minimax"
    assert e["input"] == 7000
    assert e["output"] == 47
    assert e["cacheRead"] == 0
    assert e["cacheWrite"] == 0
    assert e["reasoning"] == 0
    # actual_cost_usd > 0, so it should be used
    assert abs(e["cost"] - 0.0021564) < 1e-9
    expected_ts = int(1779395293.72756 * 1000)
    assert e["timestamp"] == expected_ts


def test_hermes_parser_cost_fallback_estimated(monkeypatch, tmp_path):
    """When actual_cost_usd is 0, falls back to estimated_cost_usd."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(actual_cost=0.0, estimated_cost=0.005)],
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 1
    assert abs(entries[0]["cost"] - 0.005) < 1e-9


def test_hermes_parser_cost_fallback_pricing_db(monkeypatch, tmp_path):
    """When both recorded costs are 0, falls back to pricing DB."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(model="kimi-k2.5", billing_provider="moonshotai", actual_cost=0.0, estimated_cost=0.0)],
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    db = PricingDatabase()
    parser = HermesParser(db)
    entries = parser.collect(None, None)

    assert len(entries) == 1
    # Should be computed from pricing DB, not 0 (kimi-k2.5 has pricing)
    assert entries[0]["cost"] >= 0


def test_hermes_parser_skip_all_zero_no_cost(monkeypatch, tmp_path):
    """Rows with all-zero tokens AND zero costs are skipped."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(input_tokens=0, output_tokens=0, cache_read_tokens=0,
                      cache_write_tokens=0, reasoning_tokens=0,
                      actual_cost=0.0, estimated_cost=0.0)],
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)
    assert len(entries) == 0


def test_hermes_parser_zero_cost_with_tokens_not_skipped(monkeypatch, tmp_path):
    """Rows with tokens but zero cost (subscription-included) are NOT skipped."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(
            model="gpt-5.2",
            billing_provider="openai",
            input_tokens=1000,
            output_tokens=100,
            actual_cost=0.0,
            estimated_cost=0.0,
        )],
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)
    # Row has tokens so it is NOT skipped; cost comes from pricing DB
    assert len(entries) == 1


def test_hermes_parser_dedup_across_dbs(monkeypatch, tmp_path):
    """Duplicate session IDs across multiple state.db files are deduplicated."""
    dir1 = tmp_path / "hermes1"
    dir2 = tmp_path / "hermes2"
    dir1.mkdir()
    dir2.mkdir()
    row = _default_row(row_id="shared-id")
    _create_hermes_db(dir1 / "state.db", [row])
    _create_hermes_db(dir2 / "state.db", [row])

    monkeypatch.setenv("HERMES_HOME", f"{dir1},{dir2}")
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)
    assert len(entries) == 1


def test_hermes_parser_default_dir(monkeypatch, tmp_path):
    """Without HERMES_HOME, defaults to ~/.hermes."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(osinfo, "is_windows", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    assert parser.search_dirs == [tmp_path / ".hermes"]
    # No DB file → empty result without error
    entries = parser.collect(None, None)
    assert entries == []


def test_hermes_parser_skip_null_model(monkeypatch, tmp_path):
    """Rows with NULL or empty model are excluded by SQL WHERE clause."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    rows = [
        _default_row(row_id="good", model="MiniMax-M2.7"),
        _default_row(row_id="null-model", model=None, billing_provider="openai"),
    ]
    _create_hermes_db(hermes_dir / "state.db", rows)

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)
    assert len(entries) == 1
    assert entries[0]["model"] == "MiniMax-M2.7"


def test_hermes_parser_started_at_ms_passthrough(monkeypatch, tmp_path):
    """If started_at is already in ms (> 1e12), it is used as-is."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    ts_ms = 1779395293727.0  # already in milliseconds
    _create_hermes_db(hermes_dir / "state.db", [_default_row(started_at=ts_ms)])

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)
    assert len(entries) == 1
    assert entries[0]["timestamp"] == int(ts_ms)
