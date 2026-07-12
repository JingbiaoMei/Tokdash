from __future__ import annotations

import json
from pathlib import Path

from tokdash.sources.quota import codex
from tokdash.sources.quota.codex import (
    collect_codex_session_snapshots,
    collect_codex_session_snapshots_incremental,
)
from tokdash.usage_store import UsageEntryStore

_BACKFILL_KEY = "quota_codex_session_backfill_done"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _count_reads(monkeypatch) -> dict:
    counter = {"n": 0}
    original = codex._read_session_bytes

    def shim(path: Path, offset: int) -> bytes:
        counter["n"] += 1
        return original(path, offset)

    monkeypatch.setattr(codex, "_read_session_bytes", shim)
    return counter


def _token_count(ts: str, primary_used: float, secondary_used: float) -> dict:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "acct_local",
                "plan_type": "prolite",
                "primary": {
                    "used_percent": primary_used,
                    "window_minutes": 300,
                    "resets_at": "2026-07-01T13:00:00Z",
                },
                "secondary": {
                    "used_percent": secondary_used,
                    "window_minutes": 10080,
                    "resets_at": 1783467600,
                },
                "credits": 3,
            },
            "info": {"model": "gpt-5"},
        },
    }


def _legacy_token_count(ts: str, primary_used: float) -> dict:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "rate_limits": {
                    "limit_id": "acct_local",
                    "plan_type": "prolite",
                    "primary": {
                        "used_percent": primary_used,
                        "window_minutes": 300,
                        "resets_at": "2026-07-01T13:00:00Z",
                    },
                }
            },
        },
    }


def test_collect_codex_session_rate_limits_downsamples_one_snapshot_per_bucket_hour(tmp_path):
    _write_jsonl(
        tmp_path / "sessions" / "2026" / "07" / "01" / "rollout-1.jsonl",
        [
            _token_count("2026-07-01T12:01:00Z", 12.5, 40.0),
            _token_count("2026-07-01T12:45:00Z", 18.0, 41.0),
            _token_count("2026-07-01T13:05:00Z", 4.0, 42.0),
            {"timestamp": "2026-07-01T13:10:00Z", "type": "event_msg", "payload": {"type": "token_count"}},
        ],
    )

    snapshots = collect_codex_session_snapshots(tmp_path / "sessions")
    by_key = {(s.bucket, s.captured_at): s for s in snapshots}

    assert len([s for s in snapshots if s.bucket == "5h"]) == 2
    assert len([s for s in snapshots if s.bucket == "7d"]) == 2
    first_primary = by_key[("5h", 1782907260)]
    assert first_primary.provider == "codex"
    assert first_primary.account == "default"
    assert first_primary.bucket_label == "5-hour window"
    assert first_primary.used_percent == 12.5
    assert first_primary.resets_at == 1782910800
    assert first_primary.plan == "prolite"
    assert first_primary.source == "codex_session"
    assert first_primary.status == "ok"
    assert first_primary.raw["rate_limits"]["credits"] == 3
    assert by_key[("5h", 1782911100)].used_percent == 4.0


def test_idle_window_nulls_phantom_resets_at(tmp_path):
    """A used_percent == 0 window's resets_at is a phantom captured_at + window_length
    reset for a rolling-window timer that hasn't actually started -- it must be nulled
    so the UI shows "reset --", mirroring how Claude already treats its idle/null
    buckets. A window with used_percent > 0 must keep its real resets_at unchanged."""
    _write_jsonl(
        tmp_path / "sessions" / "rollout-idle.jsonl",
        [_token_count("2026-07-01T12:01:00Z", 0.0, 40.0)],
    )

    snapshots = collect_codex_session_snapshots(tmp_path / "sessions")
    by_bucket = {s.bucket: s for s in snapshots}

    assert by_bucket["5h"].used_percent == 0.0
    assert by_bucket["5h"].resets_at is None
    assert by_bucket["7d"].used_percent == 40.0
    assert by_bucket["7d"].resets_at == 1783467600


def test_collect_codex_session_rate_limits_keeps_info_fallback(tmp_path):
    _write_jsonl(
        tmp_path / "sessions" / "rollout-legacy.jsonl",
        [_legacy_token_count("2026-07-01T12:01:00Z", 12.5)],
    )

    snapshots = collect_codex_session_snapshots(tmp_path / "sessions")

    assert len(snapshots) == 1
    assert snapshots[0].bucket == "5h"
    assert snapshots[0].used_percent == 12.5


def test_collect_codex_session_rate_limits_ignores_missing_or_malformed_data(tmp_path):
    _write_jsonl(
        tmp_path / "sessions" / "rollout-bad.jsonl",
        [
            {"timestamp": "not-a-date", "type": "event_msg", "payload": {"type": "token_count", "info": {}}},
            {
                "timestamp": "2026-07-01T12:01:00Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"rate_limits": {"primary": {"used_percent": None}}}},
            },
            "not json",
        ],
    )

    assert collect_codex_session_snapshots(tmp_path / "sessions") == []


def test_incremental_append_advances_watermark_and_yields_only_new(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    sessions = tmp_path / "sessions"
    rollout = sessions / "rollout-1.jsonl"
    _write_jsonl(rollout, [_token_count("2026-07-01T12:01:00Z", 12.5, 40.0)])

    first = collect_codex_session_snapshots_incremental(store, sessions)
    assert {s.bucket for s in first} == {"5h", "7d"}
    assert [s.used_percent for s in first if s.bucket == "5h"] == [12.5]

    _append_jsonl(rollout, [_token_count("2026-07-01T13:05:00Z", 20.0, 41.0)])
    second = collect_codex_session_snapshots_incremental(store, sessions)
    assert [s.used_percent for s in second if s.bucket == "5h"] == [20.0]

    # Nothing changed since the last cycle -> no new snapshots.
    assert collect_codex_session_snapshots_incremental(store, sessions) == []


def test_incremental_unchanged_files_read_zero_bytes(tmp_path, monkeypatch):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    sessions = tmp_path / "sessions"
    _write_jsonl(sessions / "rollout-1.jsonl", [_token_count("2026-07-01T12:01:00Z", 12.5, 40.0)])

    collect_codex_session_snapshots_incremental(store, sessions)  # backfill reads once

    reads = _count_reads(monkeypatch)
    result = collect_codex_session_snapshots_incremental(store, sessions)

    assert result == []
    assert reads["n"] == 0


def test_incremental_partial_trailing_line_parsed_next_cycle(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    sessions = tmp_path / "sessions"
    rollout = sessions / "rollout-1.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    complete = json.dumps(_token_count("2026-07-01T12:01:00Z", 12.5, 40.0))
    partial = json.dumps(_token_count("2026-07-01T13:05:00Z", 20.0, 41.0))
    # A complete line, then a partial trailing line with NO newline (Codex mid-write).
    rollout.write_text(complete + "\n" + partial, encoding="utf-8")

    first = collect_codex_session_snapshots_incremental(store, sessions)
    assert [s.used_percent for s in first if s.bucket == "5h"] == [12.5]  # partial not parsed yet

    # The partial line is completed (newline arrives).
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    second = collect_codex_session_snapshots_incremental(store, sessions)
    assert [s.used_percent for s in second if s.bucket == "5h"] == [20.0]


def test_incremental_shrunken_file_is_reread_whole(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    sessions = tmp_path / "sessions"
    rollout = sessions / "rollout-1.jsonl"
    _write_jsonl(
        rollout,
        [
            _token_count("2026-07-01T12:01:00Z", 50.0, 40.0),
            _token_count("2026-07-01T12:45:00Z", 60.0, 41.0),
        ],
    )
    collect_codex_session_snapshots_incremental(store, sessions)

    # Rewrite the file smaller (size < stored safe_offset) -> watermark dropped, whole re-read.
    _write_jsonl(rollout, [_token_count("2026-07-01T14:00:00Z", 5.0, 6.0)])
    second = collect_codex_session_snapshots_incremental(store, sessions)

    assert [s.used_percent for s in second if s.bucket == "5h"] == [5.0]


def test_incremental_discovers_new_files(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    sessions = tmp_path / "sessions"
    _write_jsonl(sessions / "rollout-1.jsonl", [_token_count("2026-07-01T12:01:00Z", 10.0, 20.0)])
    collect_codex_session_snapshots_incremental(store, sessions)

    _write_jsonl(sessions / "rollout-2.jsonl", [_token_count("2026-07-01T13:01:00Z", 30.0, 40.0)])
    second = collect_codex_session_snapshots_incremental(store, sessions)

    assert [s.used_percent for s in second if s.bucket == "5h"] == [30.0]


def test_incremental_backfill_once_survives_process_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "usage.sqlite3"
    sessions = tmp_path / "sessions"
    _write_jsonl(sessions / "rollout-1.jsonl", [_token_count("2026-07-01T12:01:00Z", 10.0, 20.0)])

    first = collect_codex_session_snapshots_incremental(UsageEntryStore(db_path), sessions)
    assert first  # backfill produced snapshots
    assert UsageEntryStore(db_path).quota_meta_get(_BACKFILL_KEY) == "1"

    # A fresh store instance (new process) with no file changes must read nothing.
    reads = _count_reads(monkeypatch)
    second = collect_codex_session_snapshots_incremental(UsageEntryStore(db_path), sessions)

    assert second == []
    assert reads["n"] == 0


def test_incremental_collector_persists_snapshots_atomically_with_watermarks(tmp_path):
    # Snapshots, their file watermarks, and the backfill-done flag land in ONE transaction,
    # so watermarks can never outrun the stored rows: a failed insert rolls all three back
    # and the next cycle re-reads the same bytes instead of skipping them forever.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    sessions = tmp_path / "sessions"
    _write_jsonl(sessions / "rollout-1.jsonl", [_token_count("2026-07-01T12:01:00Z", 12.5, 40.0)])

    snapshots = collect_codex_session_snapshots_incremental(store, sessions)

    assert snapshots
    assert store.status()["quota_snapshots"] == len(snapshots)
    assert store.quota_meta_get(_BACKFILL_KEY) == "1"
