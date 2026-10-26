"""Tests for HermesParser.

Hermes keeps one row per session whose token columns cover the whole session
while naming one model, so the harness pins HERMES_HOME per test and asserts the
per-model breakdown (session_model_usage) as well as the older fallback.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokdash import compute, osinfo
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, CodingToolsUsageTracker, HermesParser, _sig_cache
from tokdash.usage_store import (
    UsageEntryStore,
    _entry_for_storage,
    build_source_signature,
    persistent_pricing_signature,
    usage_entry_cost,
)


def _create_hermes_db(db_path: Path, rows: list, usage_rows: list = None) -> None:
    """Create a minimal Hermes state.db with the given session rows and optional usage rows."""
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
    if usage_rows is not None:
        cur.execute(
            """
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                billing_provider TEXT NOT NULL DEFAULT '',
                billing_base_url TEXT NOT NULL DEFAULT '',
                billing_mode TEXT NOT NULL DEFAULT '',
                task TEXT NOT NULL DEFAULT '',
                api_call_count INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,
                actual_cost_usd REAL NOT NULL DEFAULT 0,
                cost_status TEXT,
                cost_source TEXT,
                first_seen REAL,
                last_seen REAL,
                PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
            )
            """
        )
        cur.executemany(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            usage_rows,
        )
    conn.commit()
    conn.close()


def _default_usage_row(
    session_id="sess-001",
    model="claude-3-5-sonnet",
    billing_provider="anthropic",
    billing_base_url="",
    billing_mode="",
    task="",
    api_call_count=1,
    input_tokens=1000,
    output_tokens=100,
    cache_read_tokens=0,
    cache_write_tokens=0,
    reasoning_tokens=0,
    estimated_cost_usd=0.0,
    actual_cost_usd=0.0,
    cost_status=None,
    cost_source=None,
    first_seen=None,
    last_seen=None,
):
    return (
        session_id, model, billing_provider, billing_base_url, billing_mode,
        task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
        cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
        cost_status, cost_source, first_seen, last_seen,
    )



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


def test_hermes_parser_scans_named_profiles(monkeypatch, tmp_path):
    """Named profiles under ~/.hermes/profiles/<name>/state.db count by default.

    Each profile keeps its own history (hermes never clones session state
    between profiles), so all three rows are distinct sessions.
    """
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(osinfo, "is_windows", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(hermes_dir / "state.db", [_default_row(row_id="main-1")])
    for name in ("robot1", "robot2"):
        profile = hermes_dir / "profiles" / name
        profile.mkdir(parents=True)
        _create_hermes_db(profile / "state.db", [_default_row(row_id=f"{name}-1")])
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    assert parser.search_dirs == [
        hermes_dir,
        hermes_dir / "profiles" / "robot1",
        hermes_dir / "profiles" / "robot2",
    ]
    entries = parser.collect(None, None)
    assert len(entries) == 3
    assert sum(e["input"] for e in entries) == 3 * 7000


def test_hermes_parser_profiles_under_env_home(monkeypatch, tmp_path):
    """A HERMES_HOME entry brings its own profiles — no need to list them by hand."""
    home = tmp_path / "custom-hermes"
    home.mkdir()
    _create_hermes_db(home / "state.db", [_default_row(row_id="main-1")])
    profile = home / "profiles" / "robot1"
    profile.mkdir(parents=True)
    _create_hermes_db(profile / "state.db", [_default_row(row_id="robot1-1")])

    monkeypatch.setenv("HERMES_HOME", str(home))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)
    assert {e["timestamp"] for e in entries}  # sanity: rows survived
    assert len(entries) == 2


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
    """Rows with NULL or empty model are now included (fallback path).
    They produce entries with model='unknown'."""
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
    assert len(entries) == 2
    by_id = {e["entry_id"]: e for e in entries}
    assert "hermes:good" in by_id
    assert by_id["hermes:good"]["model"] == "MiniMax-M2.7"
    assert "hermes:null-model" in by_id
    assert by_id["hermes:null-model"]["model"] == "unknown"


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


def test_hermes_parser_multi_model_session(monkeypatch, tmp_path):
    """A session with multiple models in session_model_usage should produce
    multiple entries, one per model, with correct per-model token counts."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    sess_row = _default_row(
        row_id="sess-multi",
        model="gpt-4o",
        billing_provider="openai",
        message_count=7,
        input_tokens=5000,
        output_tokens=400,
        actual_cost=0.025,
    )
    # Insert in reverse chronological order to verify first_seen sorting
    usage_rows = [
        _default_usage_row(
            session_id="sess-multi",
            model="gpt-4o",
            billing_provider="openai",
            input_tokens=2000,
            output_tokens=150,
            cache_read_tokens=50,
            cache_write_tokens=25,
            reasoning_tokens=10,
            actual_cost_usd=0.010,
            first_seen=1779395300.0,
        ),
        _default_usage_row(
            session_id="sess-multi",
            model="claude-3-5-sonnet",
            billing_provider="anthropic",
            input_tokens=3000,
            output_tokens=250,
            cache_read_tokens=100,
            cache_write_tokens=50,
            reasoning_tokens=20,
            actual_cost_usd=0.015,
            first_seen=1779395293.0,
        ),
    ]
    _create_hermes_db(hermes_dir / "state.db", [sess_row], usage_rows)

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 2
    by_model = {e["model"]: e for e in entries}
    assert "claude-3-5-sonnet" in by_model
    assert "gpt-4o" in by_model

    c = by_model["claude-3-5-sonnet"]
    assert c["source"] == "hermes"
    assert c["provider"] == "anthropic"
    assert c["input"] == 3000
    assert c["output"] == 250
    assert c["cacheRead"] == 100
    assert c["cacheWrite"] == 50
    assert c["reasoning"] == 20
    assert abs(c["cost"] - 0.015) < 1e-9
    assert c["messageCount"] == 7
    assert c["entry_id"] == "hermes:sess-multi:anthropic:claude-3-5-sonnet:recorded"

    g = by_model["gpt-4o"]
    assert g["source"] == "hermes"
    assert g["provider"] == "openai"
    assert g["input"] == 2000
    assert g["output"] == 150
    assert g["cacheRead"] == 50
    assert g["cacheWrite"] == 25
    assert g["reasoning"] == 10
    assert abs(g["cost"] - 0.010) < 1e-9
    # An explicit zero, not an omitted count: every consumer reads a row that
    # says nothing as one message, which used to inflate the session to eight.
    assert g["messageCount"] == 0
    assert g["entry_id"] == "hermes:sess-multi:openai:gpt-4o:recorded"


def test_hermes_parser_multi_model_aggregation(monkeypatch, tmp_path):
    """Multiple session_model_usage rows for the same (session_id, model)
    but different tasks should be summed into one entry."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    sess_row = _default_row(row_id="sess-agg", model="claude-3-5-sonnet", message_count=4)
    usage_rows = [
        _default_usage_row(
            session_id="sess-agg",
            model="claude-3-5-sonnet",
            task="main-agent",
            billing_provider="anthropic",
            input_tokens=1000,
            output_tokens=100,
            cache_read_tokens=10,
            cache_write_tokens=5,
            reasoning_tokens=2,
            actual_cost_usd=0.005,
            estimated_cost_usd=0.004,
        ),
        _default_usage_row(
            session_id="sess-agg",
            model="claude-3-5-sonnet",
            task="subtask-reviewer",
            billing_provider="anthropic",
            input_tokens=2000,
            output_tokens=200,
            cache_read_tokens=20,
            cache_write_tokens=10,
            reasoning_tokens=4,
            actual_cost_usd=0.010,
            estimated_cost_usd=0.008,
        ),
    ]
    _create_hermes_db(hermes_dir / "state.db", [sess_row], usage_rows)

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert e["model"] == "claude-3-5-sonnet"
    assert e["provider"] == "anthropic"
    assert e["input"] == 3000
    assert e["output"] == 300
    assert e["cacheRead"] == 30
    assert e["cacheWrite"] == 15
    assert e["reasoning"] == 6
    assert abs(e["cost"] - 0.015) < 1e-9
    assert e["messageCount"] == 4
    assert e["entry_id"] == "hermes:sess-agg:anthropic:claude-3-5-sonnet:recorded"


def test_hermes_parser_no_session_model_usage_table(monkeypatch, tmp_path):
    """When session_model_usage table doesn't exist, fall back to current behavior (sessions table only)."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(row_id="sess-no-table", model="MiniMax-M2.7", actual_cost=0.002)],
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert e["model"] == "MiniMax-M2.7"
    assert e["entry_id"] == "hermes:sess-no-table"
    assert e["input"] == 7000
    assert e["output"] == 47
    assert abs(e["cost"] - 0.002) < 1e-9


def test_hermes_parser_multi_model_cost_precedence(monkeypatch, tmp_path):
    """Cost precedence should work per-model: actual > estimated > pricing DB."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    sess_row = _default_row(row_id="sess-cost-prec", model="kimi-k2.5")
    usage_rows = [
        # Model 1: actual > 0 and estimated > 0 -> uses actual
        _default_usage_row(
            session_id="sess-cost-prec",
            model="gpt-4o",
            billing_provider="openai",
            input_tokens=1000,
            output_tokens=100,
            actual_cost_usd=0.02,
            estimated_cost_usd=0.05,
        ),
        # Model 2: actual == 0, estimated > 0 -> uses estimated
        _default_usage_row(
            session_id="sess-cost-prec",
            model="claude-3-5-sonnet",
            billing_provider="anthropic",
            input_tokens=1000,
            output_tokens=100,
            actual_cost_usd=0.0,
            estimated_cost_usd=0.03,
        ),
        # Model 3: actual == 0, estimated == 0 -> uses pricing DB
        _default_usage_row(
            session_id="sess-cost-prec",
            model="kimi-k2.5",
            billing_provider="moonshotai",
            input_tokens=1000,
            output_tokens=100,
            actual_cost_usd=0.0,
            estimated_cost_usd=0.0,
        ),
    ]
    _create_hermes_db(hermes_dir / "state.db", [sess_row], usage_rows)

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 3
    by_model = {e["model"]: e for e in entries}

    # Model 1: actual cost took precedence over estimated
    assert abs(by_model["gpt-4o"]["cost"] - 0.02) < 1e-9

    # Model 2: estimated cost used when actual is 0
    assert abs(by_model["claude-3-5-sonnet"]["cost"] - 0.03) < 1e-9

    # Model 3: pricing DB used when both actual and estimated are 0
    assert by_model["kimi-k2.5"]["cost"] > 0


def test_hermes_parser_multi_model_mixed_cost_aggregation(monkeypatch, tmp_path):
    """One row with actual_cost > 0 and another with actual_cost = 0 for the
    same model: the unpriced tokens are priced on their own, and a rate edit
    still reaches them."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    sess_row = _default_row(row_id="sess-mixed", model="kimi-k2.5", message_count=3)
    usage_rows = [
        _default_usage_row(
            session_id="sess-mixed", model="kimi-k2.5", task="main",
            input_tokens=100, output_tokens=10,
            actual_cost_usd=0.005, estimated_cost_usd=0.004,
        ),
        _default_usage_row(
            session_id="sess-mixed", model="kimi-k2.5", task="background_review",
            input_tokens=1000, output_tokens=100,
            actual_cost_usd=0.0, estimated_cost_usd=0.0,
        ),
    ]
    _create_hermes_db(hermes_dir / "state.db", [sess_row], usage_rows)
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = HermesParser(PricingDatabase())
    entries = parser.collect(None, None)

    # The recorded charge and the repricable tokens are separate rows, so
    # neither freezes the other.
    assert [e["_billing"]["kind"] for e in entries] == ["fixed", "pricing"]
    assert entries[0]["cost"] == pytest.approx(0.005)
    assert (entries[0]["input"], entries[0]["output"]) == (100, 10)
    assert (entries[1]["input"], entries[1]["output"]) == (1000, 100)
    assert entries[1]["cost"] > 0.0
    # Tokens counted once, messages counted once.
    assert sum(e["input"] for e in entries) == 1100
    assert sum(e["output"] for e in entries) == 110
    assert [e["messageCount"] for e in entries] == [3, 0]


# --- Issue #238 follow-ups: providers, billing halves, message counts -------


class _Rates:
    """Pricing this file owns, so every cost assertion below is exact.

    Rates are per million tokens and every bucket prices at the same rate, so a
    model's cost is its token count times the rate.
    """

    def __init__(self, rates: dict) -> None:
        self.rates = dict(rates)

    def get_cost(self, model, input_tokens, output_tokens, cache_read=0, cache_write=0):
        rate = self.rates.get(str(model))
        if rate is None:
            return 0.0
        tokens = (input_tokens or 0) + (output_tokens or 0) + (cache_read or 0) + (cache_write or 0)
        return tokens * rate / 1_000_000

    def with_rate(self, model: str, rate: float) -> "_Rates":
        return _Rates({**self.rates, model: rate})


def _entries(pricing) -> list:
    """Parse the Hermes home the test just pinned, with the given rates."""
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    return HermesParser(pricing).collect(None, None)


def test_hermes_parser_keeps_the_recorded_billing_provider(monkeypatch, tmp_path):
    """One model reached through two providers is two bills.

    The breakdown records which provider actually billed each model. Dropping it
    and re-guessing from the model name turned a proxy charge into a vendor
    charge (Issue #238 review).
    """
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(row_id="sess-proxy", model="gpt-4o", billing_provider="proxy",
                      message_count=2)],
        [
            _default_usage_row(session_id="sess-proxy", model="gpt-4o",
                               billing_provider="proxy", input_tokens=1000, output_tokens=100),
            _default_usage_row(session_id="sess-proxy", model="gpt-4o",
                               billing_provider="openai", input_tokens=1000, output_tokens=100,
                               first_seen=1779395300.0),
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))

    entries = _entries(_Rates({"proxy/gpt-4o": 25.0, "openai/gpt-4o": 2.0}))

    assert len(entries) == 2
    by_provider = {e["provider"]: e for e in entries}
    assert set(by_provider) == {"proxy", "openai"}
    assert abs(by_provider["proxy"]["cost"] - 0.0275) < 1e-9
    assert abs(by_provider["openai"]["cost"] - 0.0022) < 1e-9
    # Provider-qualified key first, bare model second, in that order.
    assert by_provider["proxy"]["_billing"]["models"] == ["proxy/gpt-4o", "gpt-4o"]
    assert len({e["entry_id"] for e in entries}) == 2


def test_hermes_parser_falls_back_to_the_bare_model_and_keeps_the_order(tmp_path, monkeypatch):
    """A recorded provider that the pricing file does not know still prices on
    the bare name, and the ordered candidates travel with the row so a provider
    rate added later shadows it on the reprice."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(row_id="sess-fallback", model="gpt-4o", billing_provider="proxy")],
        [_default_usage_row(session_id="sess-fallback", model="gpt-4o",
                            billing_provider="proxy", input_tokens=1000, output_tokens=100)],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))

    rates = _Rates({"gpt-4o": 2.0})
    entries = _entries(rates)
    assert len(entries) == 1
    assert entries[0]["provider"] == "proxy"
    assert abs(entries[0]["cost"] - 0.0022) < 1e-9

    repriced = usage_entry_cost(entries[0]["_billing"], rates.with_rate("proxy/gpt-4o", 25.0))
    assert repriced == pytest.approx(0.0275)


def test_hermes_parser_infers_provider_only_when_none_is_recorded(monkeypatch, tmp_path):
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(row_id="sess-infer", model="claude-3-5-sonnet")],
        [_default_usage_row(session_id="sess-infer", model="claude-3-5-sonnet",
                            billing_provider="")],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))

    entries = _entries(_Rates({"anthropic/claude-3-5-sonnet": 2.0}))
    assert [e["provider"] for e in entries] == ["anthropic"]


def test_hermes_parser_breakdown_without_billing_provider_column(monkeypatch, tmp_path):
    """An older breakdown table has no provider column; per-model attribution
    still works and the provider comes from the model name."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    hermes_dir.mkdir(parents=True, exist_ok=True)
    db_path = hermes_dir / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, billing_provider TEXT,"
        " started_at REAL, message_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,"
        " cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,"
        " estimated_cost_usd REAL, actual_cost_usd REAL)"
    )
    conn.execute(
        "CREATE TABLE session_model_usage (session_id TEXT NOT NULL, model TEXT NOT NULL,"
        " task TEXT NOT NULL DEFAULT '', input_tokens INTEGER NOT NULL DEFAULT 0,"
        " output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,"
        " cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,"
        " estimated_cost_usd REAL NOT NULL DEFAULT 0, actual_cost_usd REAL NOT NULL DEFAULT 0,"
        " first_seen REAL, PRIMARY KEY (session_id, model, task))"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('sess-old','gpt-4o','openai',1779395293.0,4,3000,300,0,0,0,0,0)"
    )
    conn.executemany(
        "INSERT INTO session_model_usage (session_id, model, task, input_tokens, output_tokens,"
        " first_seen) VALUES (?,?,?,?,?,?)",
        [("sess-old", "gpt-4o", "main", 2000, 200, 1779395300.0),
         ("sess-old", "claude-3-5-sonnet", "aux", 1000, 100, 1779395293.0)],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))

    entries = _entries(_Rates({
        "openai/gpt-4o": 2.0, "gpt-4o": 2.0,
        "anthropic/claude-3-5-sonnet": 2.0, "claude-3-5-sonnet": 2.0,
    }))

    assert {e["model"]: e["provider"] for e in entries} == {
        "gpt-4o": "openai", "claude-3-5-sonnet": "anthropic",
    }
    assert sum(e["input"] for e in entries) == 3000
    assert [e["messageCount"] for e in entries] == [4, 0]


def test_hermes_parser_splits_recorded_charge_from_repriced_tokens(monkeypatch, tmp_path):
    """A recorded charge and Tokdash-priced tokens in one model group stay
    separate, so a rate edit reaches the half that owns them."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(row_id="sess-mixed2", model="kimi-k2.5", billing_provider="moonshotai",
                      message_count=3)],
        [
            _default_usage_row(session_id="sess-mixed2", model="kimi-k2.5",
                               billing_provider="moonshotai", task="main",
                               input_tokens=100, output_tokens=10, actual_cost_usd=0.005),
            _default_usage_row(session_id="sess-mixed2", model="kimi-k2.5",
                               billing_provider="moonshotai", task="background_review",
                               input_tokens=1000, output_tokens=100),
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))

    rates = _Rates({"moonshotai/kimi-k2.5": 2.0, "kimi-k2.5": 2.0})
    entries = _entries(rates)

    assert [e["_billing"]["kind"] for e in entries] == ["fixed", "pricing"]
    assert entries[0]["cost"] == pytest.approx(0.005)
    assert entries[1]["cost"] == pytest.approx(0.0022)
    # Tokens of both halves, counted once, and only one row carries the count.
    assert (sum(e["input"] for e in entries), sum(e["output"] for e in entries)) == (1100, 110)
    assert [e["messageCount"] for e in entries] == [3, 0]

    edited = rates.with_rate("moonshotai/kimi-k2.5", 20.0)
    repriced = [usage_entry_cost(e["_billing"], edited) for e in entries]
    assert repriced == [pytest.approx(0.005), pytest.approx(0.022)]


def test_hermes_message_count_survives_both_consumer_paths(monkeypatch, tmp_path):
    """An explicit zero stays zero; an absent count still means one message.

    Hermes splits a session across models, so the extra rows are marked zero.
    Every consumer defaults an omitted count to one message, which is what turned
    a seven-message session into eight.
    """
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(row_id="sess-msg", model="gpt-4o", billing_provider="openai",
                      message_count=7)],
        [
            _default_usage_row(session_id="sess-msg", model="gpt-4o",
                               billing_provider="openai", input_tokens=1000, output_tokens=100),
            _default_usage_row(session_id="sess-msg", model="claude-3-5-sonnet",
                               billing_provider="anthropic", input_tokens=1000, output_tokens=100,
                               first_seen=1779395300.0),
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    entries = _entries(_Rates({
        "openai/gpt-4o": 2.0, "gpt-4o": 2.0,
        "anthropic/claude-3-5-sonnet": 2.0, "claude-3-5-sonnet": 2.0,
    }))

    assert [e["messageCount"] for e in entries] == [7, 0]
    assert compute.parse_entries_json({"entries": entries})["total_messages"] == 7
    assert sum(_entry_for_storage(e)["messageCount"] for e in entries) == 7

    # The default belongs to rows that say nothing, not to Hermes' second model.
    silent = {k: v for k, v in entries[1].items() if k != "messageCount"}
    assert compute.parse_entries_json({"entries": [silent]})["total_messages"] == 1
    assert _entry_for_storage(silent)["messageCount"] == 1


def test_hermes_message_count_lands_on_a_row_that_reports_usage(monkeypatch, tmp_path):
    """A row that recorded a charge but no tokens should not be the one that
    carries the session's messages."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(row_id="sess-late", model="gpt-4o", billing_provider="openai",
                      message_count=6)],
        [
            _default_usage_row(session_id="sess-late", model="gpt-4o",
                               billing_provider="openai", input_tokens=0, output_tokens=0,
                               actual_cost_usd=0.01, first_seen=1779395293.0),
            _default_usage_row(session_id="sess-late", model="gpt-4o",
                               billing_provider="openai", task="main",
                               input_tokens=1000, output_tokens=100,
                               first_seen=1779395300.0),
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    entries = _entries(_Rates({"openai/gpt-4o": 2.0, "gpt-4o": 2.0}))

    # The cost-only row comes first (it was seen first) and carries nothing.
    assert [e["_billing"]["kind"] for e in entries] == ["fixed", "pricing"]
    assert [e["messageCount"] for e in entries] == [0, 6]
    assert sum(_entry_for_storage(e)["messageCount"] for e in entries) == 6


@pytest.mark.parametrize("backend", ["disabled", "stored", "corrupt"])
@pytest.mark.parametrize("charge_group", ["same", "other-model", "other-provider", "cost-only"])
def test_hermes_tokenless_charge_survives_overview_aggregation(
    monkeypatch, tmp_path, backend, charge_group,
):
    """Splitting a fixed charge from priced tokens must preserve the whole bill."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    charged_model = "claude-3-5-sonnet" if charge_group == "other-model" else "gpt-4o"
    charged_provider = "proxy" if charge_group == "other-provider" else "openai"
    usage_rows = [
        _default_usage_row(
            session_id="sess-charge", model=charged_model, billing_provider=charged_provider,
            input_tokens=0, output_tokens=0, actual_cost_usd=0.01,
            first_seen=1779395293.0,
        ),
    ]
    if charge_group != "cost-only":
        usage_rows.append(_default_usage_row(
            session_id="sess-charge", model="gpt-4o", billing_provider="openai", task="main",
            input_tokens=1000, output_tokens=100, first_seen=1779395300.0,
        ))
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(row_id="sess-charge", model="gpt-4o", billing_provider="openai",
                      message_count=6)],
        usage_rows,
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0" if backend == "disabled" else "1")
    store_path = tmp_path / "usage.sqlite3"
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(store_path))
    if backend == "corrupt":
        store_path.write_bytes(b"not a sqlite database")
    _write_rates({"openai/gpt-4o": 2.0, "gpt-4o": 2.0})
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    tracker = CodingToolsUsageTracker()
    tracker.parsers = {"hermes": HermesParser(tracker.pricing_db)}
    tracker._dir_conflicts = []
    monkeypatch.setattr(compute, "CodingToolsUsageTracker", lambda: tracker)

    since = datetime.fromtimestamp(1779395293.0, timezone.utc) - timedelta(hours=1)
    result = compute.get_tools_data_for_range(since, since + timedelta(hours=2))

    expected_tokens = 0 if charge_group == "cost-only" else 1100
    expected_cost = 0.01 if charge_group == "cost-only" else 0.0122
    assert result["source_errors"] == []
    assert result["total_cost"] == pytest.approx(expected_cost)
    assert result["total_tokens"] == expected_tokens
    assert result["total_messages"] == 6
    app = result["apps"]["hermes"]
    assert (app["tokens"], app["messages"]) == (expected_tokens, 6)
    assert app["cost"] == pytest.approx(expected_cost)
    assert sum(row["cost"] for row in result["all_models"]) == pytest.approx(expected_cost)
    assert sum(row["cost"] for row in app["models"]) == pytest.approx(expected_cost)
    charged_name = f"{charged_provider}/{charged_model}"
    charged = next(row for row in app["models"] if row["name"] == charged_name)
    assert charged["cost"] == pytest.approx(0.0122 if charge_group == "same" else 0.01)
    if charge_group != "same":
        assert charged["tokens"] == 0
    buckets = result["sparkline"]["buckets"]
    assert sum(row["cost"] for row in buckets) == pytest.approx(expected_cost)
    assert sum(row["tokens"] for row in buckets) == expected_tokens
    assert sum(row["messages"] for row in buckets) == 6


def test_hermes_stored_rows_reprice_only_the_priced_half(monkeypatch, tmp_path):
    """Persistence/reload: the recorded row never moves, the priced row follows
    the new rates, and neither is reparsed out of the source database."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    _create_hermes_db(
        hermes_dir / "state.db",
        [_default_row(row_id="sess-store", model="kimi-k2.5", billing_provider="moonshotai",
                      message_count=3)],
        [
            _default_usage_row(session_id="sess-store", model="kimi-k2.5",
                               billing_provider="moonshotai", task="main",
                               input_tokens=100, output_tokens=10, actual_cost_usd=0.005),
            _default_usage_row(session_id="sess-store", model="kimi-k2.5",
                               billing_provider="moonshotai", task="aux",
                               input_tokens=1000, output_tokens=100),
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    _write_rates({"moonshotai/kimi-k2.5": 2.0, "kimi-k2.5": 2.0})

    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    signature = build_source_signature(files=[("state.db", 1, 2)], parser={"object": "hermes", "version": 3})

    parses = {"count": 0}
    original = HermesParser._parse_all

    def counting(self):
        parses["count"] += 1
        return original(self)

    monkeypatch.setattr(HermesParser, "_parse_all", counting)
    pricing = PricingDatabase()
    store.sync_source(
        "hermes",
        signature,
        lambda: HermesParser(pricing).collect(None, None),
        pricing_identity=persistent_pricing_signature(pricing),
    )

    rows = sorted(store.query_entries(sources=["hermes"]), key=lambda row: row["cost"])
    assert [row["cost"] for row in rows] == [pytest.approx(0.0022), pytest.approx(0.005)]
    assert parses["count"] == 1
    assert sorted(_message_counts(store)) == [0, 3]

    # A rate edit: the priced half follows, the recorded half does not.
    _write_rates({"moonshotai/kimi-k2.5": 20.0, "kimi-k2.5": 2.0})
    edited = PricingDatabase()
    assert store.apply_pricing(persistent_pricing_signature(edited), edited) is True

    rows = sorted(store.query_entries(sources=["hermes"]), key=lambda row: row["cost"])
    assert [row["cost"] for row in rows] == [pytest.approx(0.005), pytest.approx(0.022)]
    assert store.aggregate_entries(sources=["hermes"])["total_cost"] == pytest.approx(0.027)
    assert parses["count"] == 1, "repricing must not read the source database"
    assert sorted(_message_counts(store)) == [0, 3]


def _write_rates(rates: dict) -> None:
    """Pin pricing to rates this file owns (the override replaces the baseline)."""
    path = PricingDatabase().override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "version": "test",
            "aliases": {},
            "models": {
                name: {"input": rate, "output": rate, "cache_read": rate, "cache_write": rate}
                for name, rate in rates.items()
            },
        }),
        encoding="utf-8",
    )


def _message_counts(store) -> list:
    return [int(row.get("messageCount") or 0) for row in store.query_entries(sources=["hermes"])]
