"""Issue #170: copied Claude message ids select the same snapshot in both views."""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from tokdash import clientpaths, compute
from tokdash.onboard.paths import pricing_db_override_path
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, ClaudeParser, CodingToolsUsageTracker, _sig_cache
from tokdash.usage_store import UsageEntryStore


@pytest.fixture(autouse=True)
def _isolated_claude(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    _clear()
    yield
    _clear()


def _clear():
    _sig_cache.clear()
    BaseParser._entry_cache.clear()


def _row(message_id="shared", *, buckets=(100, 5, 0, 0), second=0, model="claude-sonnet-4.5"):
    return {
        "type": "assistant",
        "timestamp": f"2026-09-19T12:00:{second:02d}Z",
        "message": {
            "role": "assistant", "id": message_id, "model": model,
            "usage": dict(zip(("input_tokens", "output_tokens", "cache_read_input_tokens",
                               "cache_creation_input_tokens"), buckets)),
        },
    }


def _write(name, rows):
    path = clientpaths.claude_config_dir() / "projects" / "project" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({**row, "sessionId": name}) + "\n" for row in rows), encoding="utf-8")
    _clear()
    return path


def _stored():
    tracker = CodingToolsUsageTracker()
    tracker.parsers = {"claude": ClaudeParser(tracker.pricing_db)}
    store, _ = compute._sync_usage_store(tracker)
    return store.query_entries(sources=["claude"])


def _assert_parity(expected=None):
    live = ClaudeParser(PricingDatabase()).collect(None, None)
    stored = _stored()
    fields = ("entry_id", "input", "output", "cacheRead", "cacheWrite", "timestamp", "model", "cost")
    def normalize(entries):
        return sorted(tuple(entry[field] for field in fields) for entry in entries)

    assert normalize(stored) == normalize(live)
    assert all("_dedup" not in entry and "_billing" not in entry for entry in stored)
    if expected is not None:
        assert {entry["entry_id"]: (entry["input"], entry["output"], entry["cacheRead"], entry["cacheWrite"])
                for entry in stored} == expected
    return stored


def _owner(key="claude:shared"):
    with sqlite3.connect(clientpaths.usage_db_path()) as conn:
        return Path(conn.execute(
            "SELECT file_path FROM usage_entries WHERE source = 'claude' AND entry_key = ?", (key,)
        ).fetchone()[0]).stem


@pytest.mark.parametrize("largest", ["aaaa", "bbbb"])
@pytest.mark.parametrize("first", ["aaaa", "bbbb"])
def test_largest_snapshot_wins_in_either_sync_order(largest, first):
    small, full = _row(buckets=(100, 1, 0, 0)), _row(buckets=(100, 50, 0, 0), second=5)
    _write(first, [full if first == largest else small])
    _assert_parity()
    other = "bbbb" if first == "aaaa" else "aaaa"
    _write(other, [full if other == largest else small])
    _assert_parity({"claude:shared": (100, 50, 0, 0)})
    assert _owner() == largest


@pytest.mark.parametrize("durable", ["0", "1"])
@pytest.mark.parametrize("delete", ["aaaa", "bbbb"])
def test_deleting_either_identical_copy_keeps_the_survivor(monkeypatch, durable, delete):
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", durable)
    paths = {name: _write(name, [_row()]) for name in ("aaaa", "bbbb")}
    _assert_parity({"claude:shared": (100, 5, 0, 0)})
    assert _owner() == "aaaa"
    paths[delete].unlink()
    _clear()
    _assert_parity({"claude:shared": (100, 5, 0, 0)})
    assert _owner() == ("bbbb" if durable == "0" and delete == "aaaa" else "aaaa")


@pytest.mark.parametrize("durable", ["0", "1"])
def test_rewriting_owner_without_shared_message_promotes_unchanged_copy(monkeypatch, durable):
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", durable)
    _write("aaaa", [_row(buckets=(100, 50, 0, 0))])
    _write("bbbb", [_row()])
    _assert_parity()
    _write("aaaa", [_row("new", buckets=(7, 5, 0, 0))])
    _assert_parity({"claude:shared": (100, 5, 0, 0), "claude:new": (7, 5, 0, 0)})
    assert _owner() == "bbbb"


def test_rewriting_owner_with_smaller_snapshot_promotes_unchanged_copy():
    _write("aaaa", [_row(buckets=(100, 50, 0, 0))])
    _write("bbbb", [_row(buckets=(100, 30, 0, 0))])
    _assert_parity()
    _write("aaaa", [_row()])
    _assert_parity({"claude:shared": (100, 30, 0, 0)})
    assert _owner() == "bbbb"


def test_changed_nonowner_can_overtake_the_previous_winner():
    _write("aaaa", [_row(buckets=(100, 50, 0, 0))])
    _write("bbbb", [_row()])
    _assert_parity()
    _write("bbbb", [_row(buckets=(100, 60, 0, 0))])
    _assert_parity({"claude:shared": (100, 60, 0, 0)})
    assert _owner() == "bbbb"


def test_equal_total_reclassification_follows_path_order_even_after_earlier_file_changes():
    _write("bbbb", [_row(buckets=(0, 5, 100, 0), second=1)])
    _assert_parity()
    _write("aaaa", [_row()])
    _assert_parity({"claude:shared": (0, 5, 100, 0)})
    assert _owner() == "bbbb"
    _write("aaaa", [_row(), _row("unrelated")])
    _assert_parity()
    assert _owner() == "bbbb"


def test_multiple_copies_keep_first_identical_split_after_last_reclassification():
    _write("aaaa", [_row()])
    _write("bbbb", [_row(buckets=(0, 5, 100, 0), second=1)])
    _write("cccc", [_row(second=2)])
    _write("dddd", [_row(second=3)])
    _assert_parity({"claude:shared": (100, 5, 0, 0)})
    assert _owner() == "cccc"
    _write("bbbb", [_row(buckets=(0, 5, 0, 100), second=1)])
    _assert_parity()
    assert _owner() == "cccc"


def test_file_local_reclassification_back_to_earlier_split_keeps_later_timestamp():
    _write("aaaa", [_row()])
    _write("bbbb", [_row(buckets=(0, 5, 100, 0), second=1), _row(second=2), _row(second=3)])
    rows = _assert_parity({"claude:shared": (100, 5, 0, 0)})
    assert rows[0]["timestamp"] % 60_000 == 2_000
    assert _owner() == "bbbb"


def test_incremental_update_does_not_reparse_unchanged_candidates(monkeypatch):
    _write("aaaa", [_row()])
    _write("bbbb", [_row(buckets=(100, 30, 0, 0))])
    _assert_parity()
    _write("bbbb", [_row("new")])
    original = compute._collect_parser_file
    calls = []

    def collect(parser, sig, **kwargs):
        calls.append(Path(sig[0]).stem)
        return original(parser, sig, **kwargs)

    monkeypatch.setattr(compute, "_collect_parser_file", collect)
    _assert_parity()
    assert calls == ["bbbb"]
    calls.clear()
    _stored()
    assert calls == []


def test_durable_largest_candidate_survives_other_file_rewrites(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "1")
    archived = _write("aaaa", [_row(buckets=(100, 50, 0, 0))])
    _write("bbbb", [_row()])
    _assert_parity()
    archived.unlink()
    _clear()
    assert _stored()[0]["output"] == 50
    _write("bbbb", [_row(), _row("new")])
    assert {row["entry_id"]: row["output"] for row in _stored()} == {"claude:shared": 50, "claude:new": 5}
    assert _owner() == "aaaa"


def test_parser_upgrade_rebuilds_last_writer_owned_rows():
    _write("aaaa", [_row(buckets=(100, 50, 0, 0))])
    _write("bbbb", [_row()])
    # Recreate the old per-file, last-writer sync and its parser signature.
    parser = ClaudeParser(PricingDatabase())
    signature = {**parser.persistent_parser_signature(), "version": 3}
    UsageEntryStore().sync_files(
        "claude", parser._file_signatures(), parser=signature,
        parse_file_entries=lambda sig: compute._collect_parser_file(parser, sig),
    )
    assert _owner() == "bbbb"
    _assert_parity({"claude:shared": (100, 50, 0, 0)})
    assert _owner() == "aaaa"


def test_schema_upgrade_retains_missing_durable_candidate(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "1")
    archived = _write("aaaa", [_row(buckets=(100, 50, 0, 0))])
    _write("bbbb", [_row()])
    _assert_parity()
    with sqlite3.connect(clientpaths.usage_db_path()) as conn:
        conn.execute("DROP TABLE usage_entry_candidates")
        conn.execute("UPDATE meta SET value = '13' WHERE key = 'schema_version'")
        # A real v13 file has the old parse signature, triggering the rebuild.
        for path, signature in conn.execute("SELECT path, signature FROM file_state").fetchall():
            old = json.loads(signature)
            old["parser"]["version"] = 3
            conn.execute("UPDATE file_state SET signature = ? WHERE path = ?", (json.dumps(old), path))
    archived.unlink()
    _clear()
    assert _stored()[0]["output"] == 50
    _write("bbbb", [_row(), _row("new")])
    assert {row["entry_id"]: row["output"] for row in _stored()} == {"claude:shared": 50, "claude:new": 5}


def test_removing_a_nonowning_reclassification_can_change_the_owner(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "0")
    _write("aaaa", [_row()])
    middle = _write("bbbb", [_row(buckets=(0, 5, 100, 0), second=1)])
    _write("cccc", [_row(second=2)])
    _assert_parity()
    assert _owner() == "cccc"
    middle.unlink()
    _clear()
    _assert_parity()
    assert _owner() == "aaaa"


def test_promoted_candidate_uses_current_pricing(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "0")
    override = pricing_db_override_path()
    override.parent.mkdir(parents=True, exist_ok=True)

    def rates(input_rate):
        override.write_text(json.dumps({"models": {
            "claude-sonnet-4.5": {"input": input_rate, "output": 2, "unit": "per_million_tokens"}
        }}), encoding="utf-8")

    rates(1)
    original = _write("aaaa", [_row(buckets=(100, 50, 0, 0))])
    _write("bbbb", [_row()])
    _assert_parity()
    rates(9)
    _clear()
    _assert_parity()
    original.unlink()
    _clear()
    rows = _assert_parity()
    assert rows[0]["cost"] == pytest.approx((100 * 9 + 5 * 2) / 1_000_000)


def test_promoted_candidate_uses_the_syncs_pricing_snapshot(monkeypatch):
    """Disk rates can change after the tracker captures its pricing identity."""
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "0")
    original = _write("aaaa", [_row(buckets=(100, 50, 0, 0))])
    _write("bbbb", [_row()])
    _assert_parity()
    original.unlink()
    _clear()
    tracker = CodingToolsUsageTracker()
    tracker.parsers = {"claude": ClaudeParser(tracker.pricing_db)}
    expected = tracker.pricing_db.get_cost("claude-sonnet-4.5", 100, 5, 0, 0)
    override = pricing_db_override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(json.dumps({"models": {"claude-sonnet-4.5": {"input": 999, "output": 999}}}),
                        encoding="utf-8")
    store, _ = compute._sync_usage_store(tracker)
    # Read the committed row directly: query_entries intentionally repairs a
    # stale pricing identity against the newly written disk rates.
    with sqlite3.connect(store.path) as conn:
        cost = conn.execute("SELECT cost FROM usage_entries WHERE entry_key = 'claude:shared'").fetchone()[0]
    assert cost == expected


def test_file_local_reduction_matches_original_line_by_line_streaming_rule(monkeypatch):
    """Independent oracle protects against both views sharing the same wrong fold."""
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "0")
    rng = random.Random(170)
    splits = [(100, 5, 0, 0), (0, 5, 100, 0), (0, 5, 0, 100),
              (100, 50, 0, 0), (0, 50, 100, 0), (50, 5, 0, 0)]
    files = {}
    for name in ("cccc", "aaaa", "dddd", "bbbb"):
        rows = [_row(f"msg-{message}", buckets=rng.choice(splits), second=line)
                for message in range(20) for line in range(12)]
        _write(name, rows)
        files[name] = rows
        expected = {}
        for path in sorted(files):
            for row in files[path]:
                message_id = f"claude:{row['message']['id']}"
                buckets = tuple(row["message"]["usage"].values())
                previous = expected.get(message_id)
                if previous is None or sum(buckets) > sum(previous[0]) or (
                    sum(buckets) == sum(previous[0]) and buckets != previous[0]
                ):
                    expected[message_id] = (buckets, int(datetime.fromisoformat(
                        row["timestamp"].replace("Z", "+00:00")).timestamp() * 1000))
        stored = _assert_parity()
        assert {entry["entry_id"]: ((entry["input"], entry["output"], entry["cacheRead"], entry["cacheWrite"]),
                                    entry["timestamp"]) for entry in stored} == expected
