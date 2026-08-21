from __future__ import annotations

import json
import logging
import os
import sqlite3

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import tokdash.sessions as sessions_module
import tokdash.usage_store as usage_store_module
from tokdash.activity_insights import (
    new_activity_record,
    record_structured_tool_call,
)
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, CodexParser, CodingToolsUsageTracker, _sig_cache
from tokdash.usage_store import (
    UsageEntryStore,
    build_source_signature,
    parser_code_signature,
    usage_billing_fixed,
    usage_billing_pricing,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _clear_parser_caches() -> None:
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    sessions_module._parse_codex_session_file.cache_clear()
    sessions_module._load_codex_sessions.cache_clear()
    activity_loader = getattr(sessions_module, "_load_codex_activity_records", None)
    if activity_loader is not None:
        activity_loader.cache_clear()
    sessions_module._load_codex_title_map.cache_clear()
    sessions_module._parse_claude_session_file.cache_clear()
    sessions_module._load_claude_sessions.cache_clear()
    sessions_module._load_opencode_sessions.cache_clear()
    sessions_module._parse_pi_session_file.cache_clear()
    sessions_module._load_pi_sessions.cache_clear()
    sessions_module._load_mimo_sessions.cache_clear()


def test_usage_store_syncs_and_queries_by_range(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    calls = {"count": 0}

    def parse_entries():
        calls["count"] += 1
        return [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "input": 10,
                "output": 5,
                "cacheRead": 7,
                "cacheWrite": 3,
                "reasoning": 2,
                "cost": 0.01,
                "timestamp": 1_700_000_000_000,
            },
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "input": 1,
                "output": 1,
                "cacheRead": 0,
                "cacheWrite": 0,
                "reasoning": 0,
                "cost": 0.001,
                "timestamp": 1_800_000_000_000,
            },
        ]

    sig = build_source_signature(files=[["a.jsonl", 1, 2]], pricing=[3, 4], parser={"v": 1})

    assert store.sync_source("codex", sig, parse_entries) is True
    assert store.sync_source("codex", sig, parse_entries) is False
    assert calls["count"] == 1

    entries = store.query_entries(
        sources=["codex"],
        since=datetime.fromtimestamp(1_699_999_999, timezone.utc),
        until=datetime.fromtimestamp(1_700_000_001, timezone.utc),
    )

    assert len(entries) == 1
    assert entries[0]["source"] == "codex"
    assert entries[0]["cacheWrite"] == 3
    assert entries[0]["messageCount"] == 1


def test_usage_store_replaces_source_when_signature_changes(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")

    store.sync_source(
        "claude",
        build_source_signature(files=[["old.jsonl", 1, 1]], parser={"v": 1}),
        lambda: [
            {
                "source": "claude",
                "model": "claude-sonnet-4",
                "timestamp": 1_700_000_000_000,
                "input": 10,
            }
        ],
    )
    store.sync_source(
        "claude",
        build_source_signature(files=[["new.jsonl", 2, 2]], parser={"v": 1}),
        lambda: [
            {
                "source": "claude",
                "model": "claude-sonnet-4",
                "timestamp": 1_700_000_001_000,
                "input": 20,
                "messageCount": 4,
            }
        ],
    )

    entries = store.query_entries(sources=["claude"])
    assert len(entries) == 1
    assert entries[0]["timestamp"] == 1_700_000_001_000
    assert entries[0]["input"] == 20
    assert entries[0]["messageCount"] == 4


def test_usage_store_aggregates_without_loading_raw_rows(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.sync_source(
        "codex",
        build_source_signature(files=[["codex.jsonl", 1, 1]], parser={"v": 1}),
        lambda: [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_000_000,
                "input": 10,
                "output": 5,
                "cacheRead": 7,
                "cacheWrite": 3,
                "reasoning": 2,
                "cost": 0.1,
                "messageCount": 2,
            },
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_100_000,
                "input": 20,
                "output": 10,
                "cacheRead": 0,
                "cacheWrite": 1,
                "reasoning": 4,
                "cost": 0.2,
                "messageCount": 3,
            },
        ],
    )

    data = store.aggregate_entries(sources=["codex"])

    assert data["total_tokens"] == 62
    assert data["total_messages"] == 5
    assert data["cache_hit_rate"] == round(7 / (34 + 7), 4)
    app = data["apps"]["codex"]
    assert app["tokens_in"] == 34
    assert app["tokens_cache"] == 7
    assert app["models"][0]["name"] == "openai/gpt-5.3-codex"


def test_usage_store_contribution_days_use_sql_date_window(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.sync_source(
        "claude",
        build_source_signature(files=[["claude.jsonl", 1, 1]], parser={"v": 1}),
        lambda: [
            {
                "source": "claude",
                "model": "claude-sonnet-4",
                "provider": "anthropic",
                "timestamp": 1_700_000_000_000,
                "input": 10,
                "output": 5,
                "cacheRead": 2,
                "cacheWrite": 3,
                "reasoning": 1,
                "cost": 0.1,
            },
            {
                "source": "claude",
                "model": "claude-sonnet-4",
                "provider": "anthropic",
                "timestamp": 1_800_000_000_000,
                "input": 100,
                "output": 50,
                "cacheRead": 20,
                "cacheWrite": 30,
                "reasoning": 10,
                "cost": 1.0,
            },
        ],
    )

    days = store.contribution_days(
        sources=["claude"],
        since=datetime.fromtimestamp(1_699_999_999, timezone.utc),
        until=datetime.fromtimestamp(1_700_000_001, timezone.utc),
    )

    assert len(days) == 1
    assert days[0]["totals"]["tokens"] == 21
    assert days[0]["totals"]["messages"] == 1
    assert days[0]["tokenBreakdown"] == {
        "input": 13,
        "output": 5,
        "cacheRead": 2,
        "cacheWrite": 0,
        "reasoning": 1,
    }
    assert days[0]["sources"][0]["providerId"] == "anthropic"


def test_usage_store_recompute_fallback_for_mixed_zero_and_priced_rows(tmp_path):
    """A group mixing zero-cost and priced rows must price the zero-cost share.

    Regression: the fallback used to check the *summed* cost, so a positive priced
    sum masked the free rows and the zero-cost rows stayed $0. The store path must
    agree with parse_entries_json, which recomputes each zero-cost row.
    """
    from tokdash.compute import parse_entries_json

    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    # grok-4.5 is $2 input / $6 output per 1M in the packaged pricing DB.
    rows = [
        {
            "source": "grok",
            "model": "grok-4.5",
            "provider": "xai",
            "timestamp": 1_784_900_000_000,
            "input": 1_000_000,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "reasoning": 0,
            "cost": 2.0,  # priced at ingest
            "messageCount": 1,
        },
        {
            "source": "grok",
            "model": "grok-4.5",
            "provider": "xai",
            "timestamp": 1_784_900_001_000,
            "input": 1_000_000,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "reasoning": 0,
            "cost": 0.0,  # placeholder, must be recomputed
            "messageCount": 1,
        },
    ]
    store.sync_source(
        "grok",
        build_source_signature(files=[["unified.jsonl", 1, 1]], parser={"v": 1}),
        lambda: rows,
    )

    store_data = store.aggregate_entries(sources=["grok"])
    parse_data = parse_entries_json({"entries": rows})

    store_cost = store_data["apps"]["grok"]["cost"]
    parse_cost = parse_data["apps"]["grok"]["cost"]
    assert abs(store_cost - parse_cost) < 1e-9, (
        f"store {store_cost} != parse_entries_json {parse_cost} for mixed group"
    )
    # $2 (priced) + $2 (recomputed) = $4.
    assert abs(store_cost - 4.0) < 1e-9

    # contribution_days must agree too.
    days = store.contribution_days(
        sources=["grok"],
        since=datetime.fromtimestamp(1_784_899_999, timezone.utc),
        until=datetime.fromtimestamp(1_784_900_002, timezone.utc),
    )
    assert len(days) == 1
    assert abs(days[0]["totals"]["cost"] - 4.0) < 1e-9


def test_usage_store_sync_files_replaces_only_changed_files(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    calls: list[str] = []

    def parse_file(file_sig):
        path, _mtime_ns, _size = file_sig
        calls.append(path)
        return [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_000_000 if path.endswith("a.jsonl") else 1_700_000_010_000,
                "input": 10 if path.endswith("a.jsonl") else 20,
                "output": 1,
            }
        ]

    files_v1 = (
        (str(tmp_path / "a.jsonl"), 1, 100),
        (str(tmp_path / "b.jsonl"), 1, 100),
    )
    files_v2 = (
        (str(tmp_path / "a.jsonl"), 1, 100),
        (str(tmp_path / "b.jsonl"), 2, 200),
    )

    assert store.sync_files("codex", files_v1, parser={"v": 1}, parse_file_entries=parse_file) is True
    assert calls == [files_v1[0][0], files_v1[1][0]]
    assert store.sync_files("codex", files_v1, parser={"v": 1}, parse_file_entries=parse_file) is False
    assert calls == [files_v1[0][0], files_v1[1][0]]

    assert store.sync_files("codex", files_v2, parser={"v": 1}, parse_file_entries=parse_file) is True
    assert calls == [files_v1[0][0], files_v1[1][0], files_v2[1][0]]

    data = store.aggregate_entries(sources=["codex"])
    assert data["total_tokens"] == 32
    assert data["total_messages"] == 2


def test_usage_store_codex_duplicate_key_preserves_earliest_and_promotes_survivor(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    # Deliberately make the replay sort first to prove timestamp ownership does not
    # depend on file discovery/path order.
    resumed_path = str(tmp_path / "a-resumed.jsonl")
    original_path = str(tmp_path / "z-original.jsonl")
    calls: list[str] = []

    def entry(timestamp: int, entry_id: str, input_tokens: int) -> dict:
        return {
            "source": "codex",
            "model": "gpt-5.3-codex",
            "provider": "openai",
            "timestamp": timestamp,
            "input": input_tokens,
            "output": 1,
            "entry_id": entry_id,
        }

    def parse_file(file_sig):
        path = file_sig[0]
        calls.append(path)
        if path == original_path:
            return [entry(1_700_000_000_000, "codex-token-v1:shared", 10)]
        return [
            # Restamped replay: the original row and timestamp must remain canonical.
            entry(1_700_000_100_000, "codex-token-v1:shared", 10),
            entry(1_700_000_200_000, "codex-token-v1:new", 20),
        ]

    files = ((original_path, 1, 100), (resumed_path, 1, 100))
    assert store.sync_files("codex", files, parser={"v": 1}, parse_file_entries=parse_file) is True
    rows = store.query_entries(sources=["codex"])
    assert [(row["entry_id"], row["timestamp"]) for row in rows] == [
        ("codex-token-v1:shared", 1_700_000_000_000),
        ("codex-token-v1:new", 1_700_000_200_000),
    ]

    # A normal append/change reparses only the active resumed file and still cannot
    # replace the original occurrence with its restamped copy.
    calls.clear()
    changed = ((original_path, 1, 100), (resumed_path, 2, 200))
    assert store.sync_files("codex", changed, parser={"v": 1}, parse_file_entries=parse_file) is True
    assert calls == [resumed_path]
    rows = store.query_entries(sources=["codex"])
    assert [(row["entry_id"], row["timestamp"]) for row in rows] == [
        ("codex-token-v1:shared", 1_700_000_000_000),
        ("codex-token-v1:new", 1_700_000_200_000),
    ]

    # If non-durable cleanup removes the canonical file, reparse surviving Codex
    # files so a duplicate occurrence is promoted instead of losing the usage.
    calls.clear()
    remaining = ((resumed_path, 2, 200),)
    assert store.sync_files(
        "codex",
        remaining,
        parser={"v": 1},
        parse_file_entries=parse_file,
        durable=False,
    ) is True
    assert calls == [resumed_path]
    rows = store.query_entries(sources=["codex"])
    assert [(row["entry_id"], row["timestamp"]) for row in rows] == [
        ("codex-token-v1:shared", 1_700_000_100_000),
        ("codex-token-v1:new", 1_700_000_200_000),
    ]


def test_usage_store_codex_rewrite_promotes_duplicate_from_unchanged_file(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    original_path = str(tmp_path / "original.jsonl")
    resumed_path = str(tmp_path / "resumed.jsonl")
    original_has_shared_event = True
    calls: list[str] = []

    def entry(timestamp: int, entry_id: str) -> dict:
        return {
            "source": "codex",
            "model": "gpt-5.3-codex",
            "provider": "openai",
            "timestamp": timestamp,
            "input": 10,
            "output": 1,
            "entry_id": entry_id,
        }

    def parse_file(file_sig):
        path = file_sig[0]
        calls.append(path)
        if path == original_path:
            return [entry(1_700_000_000_000, "codex-token-v1:shared")] if original_has_shared_event else []
        return [entry(1_700_000_100_000, "codex-token-v1:shared")]

    files = ((original_path, 1, 100), (resumed_path, 1, 100))
    assert store.sync_files("codex", files, parser={"v": 1}, parse_file_entries=parse_file) is True
    assert store.query_entries(sources=["codex"])[0]["timestamp"] == 1_700_000_000_000

    # Rewriting the canonical file without this event must reconsider the
    # unchanged resumed file, which still contains a later occurrence.
    calls.clear()
    original_has_shared_event = False
    changed = ((original_path, 2, 90), (resumed_path, 1, 100))
    assert store.sync_files("codex", changed, parser={"v": 1}, parse_file_entries=parse_file) is True
    assert calls == [original_path, resumed_path]
    rows = store.query_entries(sources=["codex"])
    assert [(row["entry_id"], row["timestamp"]) for row in rows] == [
        ("codex-token-v1:shared", 1_700_000_100_000),
    ]


def test_usage_store_sync_files_appends_from_safe_offset(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "a.jsonl")
    calls: list[tuple[str, int]] = []

    files_v1 = ((path, 1, 100),)
    files_v2 = ((path, 2, 160),)

    def parse_file(file_sig):
        calls.append(("full", file_sig[2]))
        return [
            {
                "source": "claude",
                "model": "claude-sonnet-4",
                "provider": "anthropic",
                "timestamp": 1_700_000_000_000,
                "input": 10,
                "output": 1,
                "entry_id": "msg-1",
            }
        ]

    def parse_tail(file_sig, start_offset):
        calls.append(("tail", start_offset))
        return (
            [
                {
                    "source": "claude",
                    "model": "claude-sonnet-4",
                    "provider": "anthropic",
                    "timestamp": 1_700_000_010_000,
                    "input": 20,
                    "output": 1,
                    "entry_id": "msg-2",
                }
            ],
            file_sig[2],
        )

    assert store.sync_files("claude", files_v1, parser={"v": 1}, parse_file_entries=parse_file) is True
    assert store.sync_files(
        "claude",
        files_v2,
        parser={"v": 1},
        parse_file_entries=parse_file,
        parse_file_tail_entries=parse_tail,
    ) is True

    assert calls == [("full", 100), ("tail", 100)]
    entries = store.query_entries(sources=["claude"])
    assert [e["entry_id"] for e in entries] == ["msg-1", "msg-2"]


def test_usage_store_durable_missing_file_keeps_rows(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "a.jsonl")
    store.sync_files(
        "codex",
        ((path, 1, 100),),
        parser={"v": 1},
        parse_file_entries=lambda _file_sig: [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_000_000,
                "input": 10,
                "output": 1,
                "entry_id": "codex-1",
            }
        ],
    )

    assert store.sync_files("codex", (), parser={"v": 1}, parse_file_entries=lambda _file_sig: [], durable=True) is True

    assert store.aggregate_entries(sources=["codex"])["total_tokens"] == 11
    status = store.status()
    assert status["files"][0]["missing_files"] == 1
    assert store.sync_files("codex", (), parser={"v": 1}, parse_file_entries=lambda _file_sig: [], durable=True) is False


def test_usage_store_non_durable_missing_file_deletes_rows(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "a.jsonl")
    store.sync_files(
        "codex",
        ((path, 1, 100),),
        parser={"v": 1},
        parse_file_entries=lambda _file_sig: [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_000_000,
                "input": 10,
                "output": 1,
                "entry_id": "codex-1",
            }
        ],
    )

    assert store.sync_files("codex", (), parser={"v": 1}, parse_file_entries=lambda _file_sig: [], durable=False) is True

    assert store.aggregate_entries(sources=["codex"])["total_tokens"] == 0


def test_usage_store_session_records_are_synced_and_retained(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "session.jsonl")
    files_v1 = ((path, 1, 100),)

    assert store.sync_session_files(
        "codex",
        files_v1,
        parser={"v": 1},
        parse_file_session=lambda _file_sig: {
            "tool": "codex",
            "session_id": "s1",
            "project": "tokdash",
            "turns": [{"turn_index": 1, "timestamp_ms": 1_700_000_000_000, "tokens": 10}],
        },
    ) is True
    assert store.sync_session_files("codex", files_v1, parser={"v": 1}, parse_file_session=lambda _file_sig: None) is False

    records = store.query_session_records("codex")
    assert len(records) == 1
    assert records[0]["session_id"] == "s1"

    assert store.sync_session_files("codex", (), parser={"v": 1}, parse_file_session=lambda _file_sig: None, durable=True) is True
    assert store.query_session_records("codex")[0]["session_id"] == "s1"


def test_session_record_date_window_filters_before_json_deserialization(monkeypatch, tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    files = tuple((str(tmp_path / f"{name}.jsonl"), index, 100) for index, name in enumerate(("old", "current", "future"), start=1))
    records = {
        files[0][0]: {
            "tool": "claude",
            "session_id": "old",
            "display_name": "OLD_SENTINEL",
            "turns": [{"timestamp_ms": 100, "tokens": 1}],
        },
        files[1][0]: {
            "tool": "claude",
            "session_id": "current",
            "display_name": "CURRENT_SENTINEL",
            "turns": [{"timestamp_ms": 250, "tokens": 2}],
        },
        files[2][0]: {
            "tool": "claude",
            "session_id": "future",
            "display_name": "FUTURE_SENTINEL",
            "turns": [{"timestamp_ms": 400, "tokens": 3}],
        },
    }
    assert store.sync_session_files(
        "claude",
        files,
        parser={"v": 1},
        parse_file_session=lambda file_sig: records[file_sig[0]],
    )

    original_loads = usage_store_module.json.loads

    def guarded_loads(value):
        assert "OLD_SENTINEL" not in value
        assert "FUTURE_SENTINEL" not in value
        return original_loads(value)

    monkeypatch.setattr(usage_store_module.json, "loads", guarded_loads)
    window = store.query_session_records("claude", since_ms=200, until_ms=300)
    assert [record["session_id"] for record in window] == ["current"]


def test_persistent_session_failure_is_logged_before_source_fallback(monkeypatch, caplog):
    monkeypatch.setattr(sessions_module, "persistent_usage_db_enabled", lambda: True)
    monkeypatch.setattr(
        sessions_module,
        "_stored_sessions_for_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broken cache")),
    )
    monkeypatch.setattr(sessions_module, "_claude_sessions", lambda: {"fallback": {}})

    with caplog.at_level(logging.WARNING, logger="tokdash.sessions"):
        result = sessions_module._raw_sessions_for_tool("claude", since_ms=100, until_ms=200)

    assert result == {"fallback": {}}
    assert "persistent session cache failed tool=claude" in caplog.text
    assert "broken cache" in caplog.text


def test_session_activity_is_stored_separately_from_session_raw_json(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "session.jsonl")
    activity = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_structured_tool_call(
        activity, call_id="opaque", name="exec", specificity="top_level"
    )

    assert store.sync_session_files(
        "codex",
        ((path, 1, 100),),
        parser={"v": 2},
        parse_file_session=lambda _sig: {
            "tool": "codex",
            "session_id": "chat-1",
            "display_name": "RAW_SENTINEL",
            "turns": [],
            "_activity": activity,
        },
    )

    with sqlite3.connect(store.path) as conn:
        raw_json, activity_json = conn.execute(
            "SELECT raw_json, activity_json FROM session_records"
        ).fetchone()
    assert "_activity" not in raw_json
    assert "RAW_SENTINEL" in raw_json
    assert json.loads(activity_json)["tool_by_call_id"]["opaque"]["name"] == "exec"
    assert store.query_session_activity_records("codex") == [
        {
            "session_id": "chat-1",
            "file_path": path,
            "missing": False,
            "activity": activity,
        }
    ]


def test_session_activity_query_never_deserializes_raw_json(monkeypatch, tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "session.jsonl")
    activity = new_activity_record(is_primary=True, has_explicit_session_id=True)
    store.sync_session_files(
        "codex",
        ((path, 1, 100),),
        parser={"v": 2},
        parse_file_session=lambda _sig: {
            "tool": "codex",
            "session_id": "chat-1",
            "display_name": "RAW_SENTINEL",
            "turns": [],
            "_activity": activity,
        },
    )
    original_loads = usage_store_module.json.loads

    def guarded_loads(value):
        assert "RAW_SENTINEL" not in value
        return original_loads(value)

    monkeypatch.setattr(usage_store_module.json, "loads", guarded_loads)

    assert store.query_session_activity_records("codex")[0]["activity"] == activity


def test_schema_five_migrates_activity_column_without_losing_session_rows(tmp_path):
    db_path = tmp_path / "usage.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '5')")
        conn.execute(
            """
            CREATE TABLE session_records (
                tool TEXT NOT NULL,
                session_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                safe_offset INTEGER NOT NULL DEFAULT 0,
                missing INTEGER NOT NULL DEFAULT 0,
                signature TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (tool, file_path, session_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO session_records(
                tool, session_id, file_path, mtime_ns, size, safe_offset,
                missing, signature, updated_at_ms, raw_json
            ) VALUES ('codex', 'legacy', '/missing.jsonl', 1, 2, 2, 1, 'old', 3, ?)
            """,
            (json.dumps({"session_id": "legacy", "turns": [{"timestamp_ms": 123, "tokens": 7}]}),),
        )
        conn.commit()

    store = UsageEntryStore(db_path)

    assert store.query_session_records("codex")[0]["session_id"] == "legacy"
    assert store.query_session_activity_records("codex") == [
        {
            "session_id": "legacy",
            "file_path": "/missing.jsonl",
            "missing": True,
            "activity": None,
        }
    ]
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(session_records)").fetchall()
        }
        schema_version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        raw_json = conn.execute(
            "SELECT raw_json FROM session_records WHERE session_id = 'legacy'"
        ).fetchone()[0]
    assert {"activity_json", "started_at_ms", "last_seen_at_ms"}.issubset(columns)
    # The point is that a v5 database is migrated forward, not which number is
    # current; pinning the literal makes every later schema bump fail here.
    assert schema_version == str(usage_store_module.SCHEMA_VERSION)
    assert json.loads(raw_json)["turns"][0]["tokens"] == 7
    with sqlite3.connect(db_path) as conn:
        bounds = conn.execute(
            "SELECT started_at_ms, last_seen_at_ms FROM session_records WHERE session_id = 'legacy'"
        ).fetchone()
    assert bounds == (123, 123)


def test_session_activity_sync_parses_only_changed_files(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "session.jsonl")
    calls = {"count": 0}

    def parse(_sig):
        calls["count"] += 1
        return {
            "tool": "codex",
            "session_id": "chat-1",
            "turns": [],
            "_activity": new_activity_record(
                is_primary=True, has_explicit_session_id=True
            ),
        }

    files = ((path, 1, 100),)
    assert store.sync_session_files(
        "codex", files, parser={"v": 2}, parse_file_session=parse
    )
    assert not store.sync_session_files(
        "codex", files, parser={"v": 2}, parse_file_session=parse
    )
    assert calls["count"] == 1

    assert store.sync_session_files(
        "codex", ((path, 2, 101),), parser={"v": 2}, parse_file_session=parse
    )
    assert calls["count"] == 2


def test_usage_store_session_file_can_emit_multiple_records(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "opencode.db")
    files_v1 = ((path, 1, 100),)

    assert store.sync_session_files(
        "opencode",
        files_v1,
        parser={"v": 1},
        parse_file_session=lambda _file_sig: [
            {"tool": "opencode", "session_id": "s1", "project": "a", "turns": []},
            {"tool": "opencode", "session_id": "s2", "project": "b", "turns": []},
        ],
    ) is True

    records = store.query_session_records("opencode")
    assert [record["session_id"] for record in records] == ["s1", "s2"]
    status = store.status()
    assert status["sessions"][0]["tool"] == "opencode"
    assert status["sessions"][0]["sessions"] == 2


def test_usage_store_repair_recomputes_derived_counts(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.sync_source(
        "codex",
        build_source_signature(files=[["a.jsonl", 1, 1]], parser={"v": 1}),
        lambda: [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_000_000,
                "input": 10,
            }
        ],
    )

    with store._connect() as conn:
        conn.execute("UPDATE source_state SET entry_count = 999 WHERE source = 'codex'")
        conn.commit()

    result = store.repair()

    assert result["ok"] is True
    assert "recomputed source_state.entry_count" in result["actions"]
    status = store.status()
    assert status["sources"][0]["entry_count"] == 1


def test_coding_tool_parsers_declare_sync_capabilities():
    tracker = CodingToolsUsageTracker()
    modes = {name: parser.sync_capability.mode for name, parser in tracker.parsers.items()}

    assert modes["opencode"] == "source_native_db"
    assert modes["mimo"] == "source_native_db"
    assert modes["codex"] == "file_replace"
    assert modes["claude"] == "file_replace"
    assert modes["antigravity_cli"] == "file_replace"
    assert modes["copilot_cli"] == "source_replace"
    assert tracker.parsers["gemini_cli"].sync_capability.append_jsonl is True
    assert tracker.parsers["kimi"].sync_capability.append_jsonl is True
    assert modes["workbuddy"] == "file_replace"
    assert tracker.parsers["workbuddy"].sync_capability.append_jsonl is True
    assert tracker.parsers["opencode"].sync_capability.session_store is False


def test_parser_code_signature_unwraps_lru_cache_functions():
    @lru_cache(maxsize=1)
    def parser_fn():
        return "ok"

    signature = parser_code_signature(parser_fn)

    assert signature["object"].endswith(".parser_fn")


def test_session_file_parser_signatures_are_explicit_and_v159_compatible(monkeypatch):
    codex = sessions_module._session_file_parser_signature(
        "_parse_codex_session_file"
    )
    claude = sessions_module._session_file_parser_signature(
        "_parse_claude_session_file"
    )

    # Both original parsers have left the frozen v1.5.9 compat token behind:
    # Claude at version 2 (turns carry _stream_id), Codex at version 2 (thread_spawn
    # replay turns are keyed to the parent session and raws carry
    # _subagent_parent_id). Their old rows must be reparsed, not reused.
    assert codex == {
        "object": "tokdash.sessions._parse_codex_session_file",
        "version": sessions_module._SESSION_FILE_PARSER_VERSIONS["_parse_codex_session_file"],
    }
    assert codex["version"] > 1
    assert claude == {
        "object": "tokdash.sessions._parse_claude_session_file",
        "version": sessions_module._SESSION_FILE_PARSER_VERSIONS["_parse_claude_session_file"],
    }
    assert claude["version"] > 1

    # Unrelated sessions.py changes used to alter both signatures because the
    # entire module was hashed. The persistent parser token no longer consults
    # that module hash.
    monkeypatch.setattr(
        sessions_module,
        "parser_code_signature",
        lambda _obj: {"object": "changed", "content_sha1": "changed"},
    )
    assert sessions_module._session_file_parser_signature(
        "_parse_codex_session_file"
    ) == codex
    assert sessions_module._session_file_parser_signature(
        "_parse_claude_session_file"
    ) == claude


def _priced_codex_signature(pricing):
    """The signature shape builds wrote while cost was baked into a row.

    Derived from the current one so the parser-identity keys stay in step; only
    the pricing/cost-basis half is historical.
    """
    signature = sessions_module._codex_session_parser_signature()
    signature.pop("cost_basis", None)
    signature["pricing"] = pricing
    return signature


def test_v159_session_signature_upgrade_resigns_without_reparse(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    session_path = str(tmp_path / "session.jsonl")
    files = ((session_path, 123, 456),)
    # The fast path applies only while the packaged pricing still holds v1.5.9's
    # content, so drive that condition rather than assert it against whatever is
    # shipped today. The constant is frozen at those bytes on purpose: see
    # test_v159_rows_are_not_reused_once_the_packaged_pricing_changes.
    content_pricing = sessions_module._V159_BASELINE_PRICING_CONTENT_SIGNATURE

    # This is the v1.5.9 shape. A wheel reinstall changes its packaged path or
    # mtime even when the pricing bytes and parsed session output are identical.
    legacy_pricing = (
        (
            "/old/site-packages/tokdash/pricing_db.json",
            111,
            sessions_module._V159_BASELINE_PRICING_RAW_SIZE,
        ),
        (str(tmp_path / "pricing_db.json"), 0, ""),
    )
    legacy_parser = _priced_codex_signature(legacy_pricing)
    content_parser = _priced_codex_signature(content_pricing)
    calls = 0

    def parse(_file_sig):
        nonlocal calls
        calls += 1
        return {
            "session_id": "session-1",
            "turns": [{"timestamp_ms": 123, "tokens": 1}],
        }

    assert store.sync_session_files(
        "codex", files, parser=legacy_parser, parse_file_session=parse
    )
    assert calls == 1

    # The format migration updates the stored signature but reuses raw_json.
    assert store.sync_session_files(
        "codex",
        files,
        parser=content_parser,
        parse_file_session=parse,
        signature_compatible=sessions_module._session_signature_compatible,
    )
    assert calls == 1
    assert not store.sync_session_files(
        "codex",
        files,
        parser=content_parser,
        parse_file_session=parse,
        signature_compatible=sessions_module._session_signature_compatible,
    )
    assert calls == 1

    # A real pricing-content change remains an invalidation and must reparse.
    changed_pricing = (*content_pricing[:3], "different-content")
    assert store.sync_session_files(
        "codex",
        files,
        parser=_priced_codex_signature(changed_pricing),
        parse_file_session=parse,
        signature_compatible=sessions_module._session_signature_compatible,
    )
    assert calls == 2


def test_priced_rows_move_onto_the_price_neutral_shape_without_reparsing(tmp_path):
    """The migration itself must not reread the corpus.

    Rows written while pricing was part of the signature are resigned in place:
    their cost is recomputed on read from the billing inputs they carry, or
    reconstructed from their totals when they predate those, so whichever rates
    they were written under stop mattering.
    """
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    session_path = str(tmp_path / "session.jsonl")
    files = ((session_path, 123, 456),)
    calls = 0

    def parse(_file_sig):
        nonlocal calls
        calls += 1
        return {"session_id": "session-1", "turns": [{"timestamp_ms": 123, "tokens": 1}]}

    assert store.sync_session_files(
        "codex",
        files,
        parser=_priced_codex_signature(sessions_module._session_pricing_content_signature()),
        parse_file_session=parse,
    )
    assert calls == 1

    assert store.sync_session_files(
        "codex",
        files,
        parser=sessions_module._codex_session_parser_signature(),
        parse_file_session=parse,
        signature_compatible=sessions_module._session_signature_compatible,
    )
    assert calls == 1, "the move to price-neutral rows must not reparse"

    # Settled: the same shape twice is a no-op.
    assert not store.sync_session_files(
        "codex",
        files,
        parser=sessions_module._codex_session_parser_signature(),
        parse_file_session=parse,
        signature_compatible=sessions_module._session_signature_compatible,
    )
    assert calls == 1

    # Pricing is no longer part of the signature at all, so an edit cannot
    # invalidate the row however it is spelled.
    assert not store.sync_session_files(
        "codex",
        files,
        parser=sessions_module._codex_session_parser_signature(),
        parse_file_session=parse,
        signature_compatible=sessions_module._session_signature_compatible,
    )
    assert calls == 1


def test_a_priced_row_from_a_different_parser_is_not_resigned(tmp_path, monkeypatch):
    """Dropping pricing from the signature must not weaken the parser check."""
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    session_path = str(tmp_path / "session.jsonl")
    files = ((session_path, 123, 456),)
    calls = 0

    def parse(_file_sig):
        nonlocal calls
        calls += 1
        return {"session_id": "session-1", "turns": [{"timestamp_ms": 123, "tokens": 1}]}

    store.sync_session_files(
        "codex",
        files,
        parser=_priced_codex_signature(sessions_module._session_pricing_content_signature()),
        parse_file_session=parse,
    )
    assert calls == 1

    bumped = dict(sessions_module._SESSION_FILE_PARSER_VERSIONS)
    bumped["_parse_codex_session_file"] += 1
    monkeypatch.setattr(sessions_module, "_SESSION_FILE_PARSER_VERSIONS", bumped)
    assert store.sync_session_files(
        "codex",
        files,
        parser=sessions_module._codex_session_parser_signature(),
        parse_file_session=parse,
        signature_compatible=sessions_module._session_signature_compatible,
    )
    assert calls == 2, "a different parser must reparse, price-neutral or not"


def test_price_neutral_rows_are_rebuilt_for_a_build_that_prices_at_parse_time(tmp_path):
    """Downgrading to a build with baked-in costs must not reuse these rows."""
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    session_path = str(tmp_path / "session.jsonl")
    files = ((session_path, 123, 456),)
    calls = 0

    def parse(_file_sig):
        nonlocal calls
        calls += 1
        return {"session_id": "session-1", "turns": [{"timestamp_ms": 123, "tokens": 1}]}

    store.sync_session_files(
        "codex",
        files,
        parser=sessions_module._codex_session_parser_signature(),
        parse_file_session=parse,
    )
    assert calls == 1

    assert store.sync_session_files(
        "codex",
        files,
        parser=_priced_codex_signature(sessions_module._session_pricing_content_signature()),
        parse_file_session=parse,
        signature_compatible=sessions_module._session_signature_compatible,
    )
    assert calls == 2


def test_a_storage_format_bump_rebuilds_price_neutral_rows(tmp_path, monkeypatch):
    """_SESSION_COST_BASIS is the versioned migration handle."""
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    session_path = str(tmp_path / "session.jsonl")
    files = ((session_path, 123, 456),)
    calls = 0

    def parse(_file_sig):
        nonlocal calls
        calls += 1
        return {"session_id": "session-1", "turns": [{"timestamp_ms": 123, "tokens": 1}]}

    store.sync_session_files(
        "codex",
        files,
        parser=sessions_module._codex_session_parser_signature(),
        parse_file_session=parse,
    )
    assert calls == 1

    monkeypatch.setattr(sessions_module, "_SESSION_COST_BASIS", "priced-on-read-v2")
    assert store.sync_session_files(
        "codex",
        files,
        parser=sessions_module._codex_session_parser_signature(),
        parse_file_session=parse,
        signature_compatible=sessions_module._session_signature_compatible,
    )
    assert calls == 2


def test_v159_rows_are_not_reused_once_the_packaged_pricing_changes(tmp_path):
    """A pricing update ends the v1.5.9 fast path, and must.

    The baseline constant is frozen at the pricing content v1.5.9 shipped. Once a
    model is added or a rate changes, the packaged file no longer matches it and
    those rows reparse. Re-pointing the constant at the newer file would instead
    re-sign rows whose costs were computed at the old rates as if they matched
    the new ones.
    """
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    session_path = str(tmp_path / "session.jsonl")
    files = ((session_path, 123, 456),)
    legacy_pricing = (
        (
            "/old/site-packages/tokdash/pricing_db.json",
            111,
            sessions_module._V159_BASELINE_PRICING_RAW_SIZE,
        ),
        (str(tmp_path / "pricing_db.json"), 0, ""),
    )
    baseline = sessions_module._V159_BASELINE_PRICING_CONTENT_SIGNATURE
    updated = (*baseline[:2], baseline[2] + 190, "a-later-pricing-db")

    # Signatures reach this check as JSON, i.e. lists all the way down.
    def as_json(value):
        return json.loads(json.dumps(value))

    assert sessions_module._legacy_pricing_signature_matches_content(
        as_json(legacy_pricing), as_json(baseline)
    )
    assert not sessions_module._legacy_pricing_signature_matches_content(
        as_json(legacy_pricing), as_json(updated)
    )

    calls = 0

    def parse(_file_sig):
        nonlocal calls
        calls += 1
        return {"session_id": "session-1", "turns": [{"timestamp_ms": 123, "tokens": 1}]}

    assert store.sync_session_files(
        "codex",
        files,
        parser=_priced_codex_signature(legacy_pricing),
        parse_file_session=parse,
    )
    assert calls == 1

    assert store.sync_session_files(
        "codex",
        files,
        parser=_priced_codex_signature(updated),
        parse_file_session=parse,
        signature_compatible=sessions_module._session_signature_compatible,
    )
    assert calls == 2, "rows priced at v1.5.9 rates must reparse once pricing changes"


def _load_fn_from_module_file(path: Path, module_name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module.parser_fn


def test_parser_code_signature_is_content_based_not_mtime_based(tmp_path):
    module_path = tmp_path / "fake_parser.py"
    module_path.write_text("def parser_fn():\n    return 'v1'\n", encoding="utf-8")
    fn = _load_fn_from_module_file(module_path, "fake_parser")
    sig_v1 = parser_code_signature(fn)

    # A reinstall/upgrade (e.g. `pipx upgrade`) restamps file mtimes without
    # changing content; the signature must survive so the persistent parse
    # cache is not invalidated and the next dashboard load stays fast.
    st = module_path.stat()
    os.utime(module_path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert parser_code_signature(fn) == sig_v1
    restamped_mtime_ns = module_path.stat().st_mtime_ns

    # A real parser code change must still bust the cache.
    module_path.write_text("def parser_fn():\n    return 'v2'\n", encoding="utf-8")
    st = module_path.stat()
    os.utime(module_path, ns=(st.st_atime_ns, restamped_mtime_ns + 5_000_000_000))
    fn_v2 = _load_fn_from_module_file(module_path, "fake_parser")
    sig_v2 = parser_code_signature(fn_v2)
    assert sig_v2["object"] == sig_v1["object"]
    assert sig_v2["content_sha1"] != sig_v1["content_sha1"]

    relocated_path = tmp_path / "relocated" / "fake_parser.py"
    relocated_path.parent.mkdir()
    relocated_path.write_bytes(module_path.read_bytes())
    relocated_fn = _load_fn_from_module_file(relocated_path, "fake_parser")
    assert parser_code_signature(relocated_fn) == sig_v2


def _codex_session_rows(
    session_id: str,
    *,
    review: bool = False,
    thread_name: str = "",
    user_message: str = "",
) -> list[dict]:
    source = {"subagent": {"other": "guardian"}} if review else "cli"
    rows = [
        {
            "timestamp": "2026-06-19T10:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": "/work/tokdash",
                "source": source,
                "model_provider": "openai",
            },
        },
        {
            "timestamp": "2026-06-19T10:00:01.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.3-codex", "cwd": "/work/tokdash"},
        },
    ]
    if thread_name:
        rows.append(
            {
                "timestamp": "2026-06-19T10:00:02.000Z",
                "type": "event_msg",
                "payload": {"type": "thread_name_updated", "thread_id": session_id, "thread_name": thread_name},
            }
        )
    if user_message:
        rows.append(
            {
                "timestamp": "2026-06-19T10:00:02.500Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": user_message},
            }
        )
    rows.append(
        {
            "timestamp": "2026-06-19T10:00:03.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 11,
                        "cached_input_tokens": 2,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 3,
                    }
                },
            },
        }
    )
    return rows


def test_codex_guardian_sessions_are_hidden_from_session_view_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("TOKDASH_INCLUDE_CODEX_GUARDIAN", raising=False)
    _clear_parser_caches()
    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "06" / "19"
    _write_jsonl(codex_dir / "normal.jsonl", _codex_session_rows("normal-session"))
    _write_jsonl(codex_dir / "review.jsonl", _codex_session_rows("review-session", review=True))

    hidden = sessions_module.get_sessions_data("codex", "today", "2026-06-19", "2026-06-19")
    shown = sessions_module.get_sessions_data(
        "codex",
        "today",
        "2026-06-19",
        "2026-06-19",
        include_review_sessions=True,
    )

    assert [row["session_id"] for row in hidden["sessions"]] == ["normal-session"]
    assert {row["session_id"] for row in shown["sessions"]} == {"normal-session", "review-session"}
    assert next(row for row in shown["sessions"] if row["session_id"] == "review-session")["is_review_session"] is True

    tracker = CodingToolsUsageTracker()
    codex_entries = tracker.parsers["codex"].collect()
    assert len(codex_entries) == 2


def _codex_token_count_row(ts: str, tokens_in: int, tokens_cache: int, tokens_out: int, tokens_reasoning: int) -> dict:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": tokens_in,
                    "cached_input_tokens": tokens_cache,
                    "output_tokens": tokens_out,
                    "reasoning_output_tokens": tokens_reasoning,
                }
            },
        },
    }


def _codex_token_count_row_with_total(
    ts: str,
    *,
    last_input: int,
    last_cache: int,
    last_output: int,
    last_reasoning: int,
    total_input: int,
    total_cache: int,
    total_output: int,
    total_reasoning: int,
) -> dict:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total_input,
                    "cached_input_tokens": total_cache,
                    "output_tokens": total_output,
                    "reasoning_output_tokens": total_reasoning,
                    "total_tokens": total_input + total_output,
                },
                "last_token_usage": {
                    "input_tokens": last_input,
                    "cached_input_tokens": last_cache,
                    "output_tokens": last_output,
                    "reasoning_output_tokens": last_reasoning,
                    "total_tokens": last_input + last_output,
                },
            },
        },
    }


def test_codex_subagent_thread_spawn_replay_is_skipped(monkeypatch, tmp_path):
    """Codex MultiAgent V2 `thread_spawn` subagent rollout files replay the parent
    thread's entire `session_meta` + `token_count` history under the parent's session
    ID. Both parsers must skip `token_count` events whose current session ID differs
    from the file's own (first-`session_meta`) session ID, so the replay inflates
    neither the Overview tab nor the Sessions tab (where it would otherwise clobber
    the parent session's real turns)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    parent_id = "019f5168-1796-7532-97b4-6570dc76a98d"
    sub_id = "019f524d-0461-7a13-8c1e-6570dc76a98e"

    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "11"

    parent_rows = [
        {
            "timestamp": "2026-07-11T14:40:04.000Z",
            "type": "session_meta",
            "payload": {"id": parent_id, "cwd": "/work/tokdash", "source": "vscode", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-07-11T14:40:05.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row("2026-07-11T14:40:10.000Z", 100, 10, 20, 5),
        _codex_token_count_row("2026-07-11T14:41:10.000Z", 101, 10, 20, 5),
        _codex_token_count_row("2026-07-11T14:42:10.000Z", 102, 10, 20, 5),
    ]
    # N = 3 real token_count events belonging to the parent's own session ID.
    parent_turn_count = 3

    subagent_rows = [
        # Own session_meta carries the thread_spawn marker distinguishing it from a
        # guardian (`source.subagent.other == "guardian"`) session.
        {
            "timestamp": "2026-07-11T18:50:06.000Z",
            "type": "session_meta",
            "payload": {
                "id": sub_id,
                "cwd": "/work/tokdash",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "depth": 1,
                            "agent_path": "/root/fix-bug",
                            "agent_nickname": "worker",
                            "agent_role": None,
                        }
                    }
                },
                "model_provider": "openai",
            },
        },
        # Replayed parent session_meta (same id as parent_rows[0]) + turn_context.
        {
            "timestamp": "2026-07-11T18:50:07.000Z",
            "type": "session_meta",
            "payload": {"id": parent_id, "cwd": "/work/tokdash", "source": "vscode", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-07-11T18:50:08.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        # Replayed copies of the parent's token_count events (timestamp-shifted, same
        # fingerprints) attributed to the parent's session ID, not the subagent's own.
        _codex_token_count_row("2026-07-11T18:50:20.000Z", 100, 10, 20, 5),
        _codex_token_count_row("2026-07-11T18:50:21.000Z", 101, 10, 20, 5),
        _codex_token_count_row("2026-07-11T18:50:22.000Z", 102, 10, 20, 5),
    ]

    _write_jsonl(codex_dir / "rollout-parent.jsonl", parent_rows)
    _write_jsonl(codex_dir / "rollout-subagent.jsonl", subagent_rows)

    # --- Overview tab: CodexParser._parse_all must emit exactly N entries, none of
    # which come from the replayed subagent file. ---
    parser = CodexParser(PricingDatabase())
    entries = parser._parse_all()
    assert len(entries) == parent_turn_count
    assert parser.replay_events_skipped == 3   # the 3 replayed parent events were skipped
    parent_file_str = str(codex_dir / "rollout-parent.jsonl")
    assert all(entry["entry_id"].startswith(f"{parent_file_str}:") for entry in entries)

    # --- Sessions tab: the subagent file yields zero own-session turns -> None (so it
    # can no longer overwrite/clobber the parent's real session in _load_codex_sessions).
    sub_path = codex_dir / "rollout-subagent.jsonl"
    sub_stat = sub_path.stat()
    sub_raw = sessions_module._parse_codex_session_file(str(sub_path), sub_stat.st_mtime_ns, sub_stat.st_size, ())
    assert sub_raw is None

    parent_path = codex_dir / "rollout-parent.jsonl"
    parent_stat = parent_path.stat()
    parent_raw = sessions_module._parse_codex_session_file(
        str(parent_path), parent_stat.st_mtime_ns, parent_stat.st_size, ()
    )
    assert parent_raw is not None
    assert parent_raw["session_id"] == parent_id
    assert len(parent_raw["turns"]) == parent_turn_count

    # --- Guardrail: a primary file with multiple session_meta lines that all carry the
    # SAME id (e.g. a resumed/continued session) must keep all of its events - the skip
    # must not trigger on same-ID session_meta repeats. ---
    same_id = "same-id-primary-session"
    # Isolated under a separate HOME so CodexParser's rglob over ~/.codex/sessions
    # doesn't also pick up the parent/subagent files written above.
    guardrail_home = tmp_path / "guardrail-home"
    same_id_dir = guardrail_home / ".codex" / "sessions" / "2026" / "07" / "12"
    same_id_rows = [
        {
            "timestamp": "2026-07-12T09:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": same_id, "cwd": "/work/tokdash", "source": "cli", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-07-12T09:00:01.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row("2026-07-12T09:00:02.000Z", 50, 5, 10, 2),
        # A second session_meta line with the SAME id (e.g. resumed session), followed
        # by another real token_count event that must not be dropped.
        {
            "timestamp": "2026-07-12T09:05:00.000Z",
            "type": "session_meta",
            "payload": {"id": same_id, "cwd": "/work/tokdash", "source": "cli", "model_provider": "openai"},
        },
        _codex_token_count_row("2026-07-12T09:05:02.000Z", 60, 6, 12, 3),
    ]
    _write_jsonl(same_id_dir / "rollout-same-id.jsonl", same_id_rows)

    monkeypatch.setenv("HOME", str(guardrail_home))
    monkeypatch.setattr(Path, "home", lambda: guardrail_home)
    _clear_parser_caches()
    same_id_parser = CodexParser(PricingDatabase())
    same_id_entries = same_id_parser._parse_all()
    assert len(same_id_entries) == 2

    same_id_path = same_id_dir / "rollout-same-id.jsonl"
    same_id_stat = same_id_path.stat()
    same_id_raw = sessions_module._parse_codex_session_file(
        str(same_id_path), same_id_stat.st_mtime_ns, same_id_stat.st_size, ()
    )
    assert same_id_raw is not None
    assert len(same_id_raw["turns"]) == 2


def _codex_fork_usage_rows():
    """Cumulative usage snapshots for a parent thread and a forked subagent that
    continues the same counters after the fork point."""
    usage_1 = dict(last_input=100, last_cache=10, last_output=20, last_reasoning=5,
                   total_input=100, total_cache=10, total_output=20, total_reasoning=5)
    usage_2 = dict(last_input=110, last_cache=11, last_output=21, last_reasoning=6,
                   total_input=210, total_cache=21, total_output=41, total_reasoning=11)
    usage_3 = dict(last_input=120, last_cache=12, last_output=22, last_reasoning=7,
                   total_input=330, total_cache=33, total_output=63, total_reasoning=18)
    own_1 = dict(last_input=130, last_cache=13, last_output=23, last_reasoning=8,
                 total_input=460, total_cache=46, total_output=86, total_reasoning=26)
    own_2 = dict(last_input=140, last_cache=14, last_output=24, last_reasoning=9,
                 total_input=600, total_cache=60, total_output=110, total_reasoning=35)
    return usage_1, usage_2, usage_3, own_1, own_2


def test_codex_subagent_single_meta_fork_replay_is_deduplicated(monkeypatch, tmp_path):
    """Codex 0.146+ writes thread_spawn fork files with ONE session_meta (the child's
    own id, parent declared via thread_spawn/forked_from_id) and replays the parent's
    token_count prefix BEFORE the child's first turn_context. The parent-id gate never
    fires (current id never flips) and the session-scoped stable key hashes the copies
    under the child id, so both dedup mechanisms miss them. The replayed prefix must be
    keyed to the parent session so it collapses against the parent file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    parent_id = "019f8024-8014-7202-aa03-9f039f67a648"
    child_id = "019f80ad-f48c-7361-a358-532ff84dcba9"
    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "20"
    usage_1, usage_2, usage_3, own_1, own_2 = _codex_fork_usage_rows()

    parent_rows = [
        {
            "timestamp": "2026-07-20T16:28:24.000Z",
            "type": "session_meta",
            "payload": {"id": parent_id, "cwd": "/work/tokdash", "source": "vscode", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-07-20T16:28:25.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total("2026-07-20T16:30:10.000Z", **usage_1),
        _codex_token_count_row_with_total("2026-07-20T16:31:10.000Z", **usage_2),
        _codex_token_count_row_with_total("2026-07-20T16:32:10.000Z", **usage_3),
    ]
    child_rows = [
        {
            "timestamp": "2026-07-20T18:58:31.000Z",
            "type": "session_meta",
            "payload": {
                "id": child_id,
                "cwd": "/work/tokdash",
                "forked_from_id": parent_id,
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "depth": 1,
                            "agent_path": "/root/fix-bug",
                            "agent_nickname": "worker",
                            "agent_role": None,
                        }
                    }
                },
                "model_provider": "openai",
            },
        },
        # Replayed parent prefix: restamped copies under the child's own session id,
        # before any turn_context.
        _codex_token_count_row_with_total("2026-07-20T18:58:32.000Z", **usage_1),
        _codex_token_count_row_with_total("2026-07-20T18:58:32.001Z", **usage_2),
        _codex_token_count_row_with_total("2026-07-20T18:58:32.002Z", **usage_3),
        # The child's first turn_context ends the replay prefix.
        {
            "timestamp": "2026-07-20T18:58:33.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total("2026-07-20T19:00:10.000Z", **own_1),
        _codex_token_count_row_with_total("2026-07-20T19:01:10.000Z", **own_2),
    ]

    # Child sorts first: the parent file must still win each shared key on timestamp.
    child_path = codex_dir / "rollout-1-child.jsonl"
    parent_path = codex_dir / "rollout-2-parent.jsonl"
    _write_jsonl(child_path, child_rows)
    _write_jsonl(parent_path, parent_rows)

    parser = CodexParser(PricingDatabase())
    entries = parser._parse_all()
    assert len(entries) == 5
    assert parser.replay_events_skipped == 3
    expected_timestamps = [
        sessions_module._parse_iso_to_ms("2026-07-20T16:30:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-20T16:31:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-20T16:32:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-20T19:00:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-20T19:01:10.000Z"),
    ]
    assert [entry["timestamp"] for entry in entries] == expected_timestamps
    assert all(entry["entry_id"].startswith("codex-token-v1:") for entry in entries)
    # The parent's turn_context model wins; no phantom default model from the replay.
    assert all(entry["model"] == "gpt-5.6-sol" for entry in entries)

    signatures = tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        for path in (child_path, parent_path)
    )
    sessions = sessions_module._load_codex_sessions(signatures, ())
    assert set(sessions) == {parent_id, child_id}
    assert len(sessions[parent_id]["turns"]) == 3
    # Cross-session dedup: the child session keeps only its own two turns.
    assert sessions[child_id]["_subagent_parent_id"] == parent_id
    assert [turn["timestamp_ms"] for turn in sessions[child_id]["turns"]] == expected_timestamps[3:]

    child_raw = sessions_module._parse_codex_session_file(
        str(child_path), child_path.stat().st_mtime_ns, child_path.stat().st_size, ()
    )
    parent_raw = sessions_module._parse_codex_session_file(
        str(parent_path), parent_path.stat().st_mtime_ns, parent_path.stat().st_size, ()
    )
    assert child_raw is not None and len(child_raw["turns"]) == 5
    stored = sessions_module._session_records_to_raw_sessions("codex", [child_raw, parent_raw])
    assert len(stored[parent_id]["turns"]) == 3
    assert len(stored[child_id]["turns"]) == 2


def test_codex_subagent_single_meta_fork_replay_survives_without_parent(monkeypatch, tmp_path):
    """If the parent file was never indexed (archived/deleted), the replayed prefix
    must survive under the child file — counted once, never silently dropped."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    parent_id = "019f8024-8014-7202-aa03-9f039f67a648"
    child_id = "019f80ad-f48c-7361-a358-532ff84dcba9"
    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "20"
    usage_1, usage_2, usage_3, own_1, own_2 = _codex_fork_usage_rows()

    child_rows = [
        {
            "timestamp": "2026-07-20T18:58:31.000Z",
            "type": "session_meta",
            "payload": {
                "id": child_id,
                "cwd": "/work/tokdash",
                "forked_from_id": parent_id,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": 1}}},
                "model_provider": "openai",
            },
        },
        _codex_token_count_row_with_total("2026-07-20T18:58:32.000Z", **usage_1),
        _codex_token_count_row_with_total("2026-07-20T18:58:32.001Z", **usage_2),
        _codex_token_count_row_with_total("2026-07-20T18:58:32.002Z", **usage_3),
        {
            "timestamp": "2026-07-20T18:58:33.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total("2026-07-20T19:00:10.000Z", **own_1),
        _codex_token_count_row_with_total("2026-07-20T19:01:10.000Z", **own_2),
    ]
    child_path = codex_dir / "rollout-child.jsonl"
    _write_jsonl(child_path, child_rows)

    parser = CodexParser(PricingDatabase())
    entries = parser._parse_all()
    assert len(entries) == 5
    assert parser.replay_events_skipped == 0
    # Orphaned replay rows predate the file's first model signal; they must be
    # backfilled with the file's own model, not the placeholder default.
    assert all(entry["model"] == "gpt-5.6-sol" for entry in entries)

    signatures = ((str(child_path), child_path.stat().st_mtime_ns, child_path.stat().st_size),)
    sessions = sessions_module._load_codex_sessions(signatures, ())
    assert set(sessions) == {child_id}
    assert len(sessions[child_id]["turns"]) == 5
    assert all(turn["model"] == "gpt-5.6-sol" for turn in sessions[child_id]["turns"])


def test_codex_explicit_default_model_selection_survives_backfill(monkeypatch, tmp_path):
    """CODEX_DEFAULT_MODEL is a real, selectable model. A file that starts on
    another model and later switches to an explicit gpt-5.3-codex turn_context
    must keep those rows attributed to gpt-5.3-codex — only rows written before
    ANY model signal (the placeholder flag) may be backfilled to the file's
    first observed model. Observed on real logs: 5 files switch into
    gpt-5.3-codex mid-file; a name-equality backfill relabeled 2,999 of their
    explicit rows to the earlier model."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    session_id = "019c3028-f83c-7842-86ab-4c02428a841f"
    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "02" / "05"
    rows = [
        {
            "timestamp": "2026-02-05T23:35:17.000Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/work/tokdash", "model_provider": "openai"},
        },
        # No model signal yet: placeholder rows.
        _codex_token_count_row_with_total(
            "2026-02-05T23:35:18.000Z",
            last_input=100, last_cache=10, last_output=20, last_reasoning=5,
            total_input=100, total_cache=10, total_output=20, total_reasoning=5,
        ),
        {
            "timestamp": "2026-02-05T23:36:00.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.2-codex", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total(
            "2026-02-05T23:36:10.000Z",
            last_input=110, last_cache=11, last_output=21, last_reasoning=6,
            total_input=210, total_cache=21, total_output=41, total_reasoning=11,
        ),
        # Explicit switch into the placeholder's own name.
        {
            "timestamp": "2026-02-05T23:40:00.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.3-codex", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total(
            "2026-02-05T23:40:10.000Z",
            last_input=120, last_cache=12, last_output=22, last_reasoning=7,
            total_input=330, total_cache=33, total_output=63, total_reasoning=18,
        ),
    ]
    path = codex_dir / "rollout-switch.jsonl"
    _write_jsonl(path, rows)

    entries = CodexParser(PricingDatabase())._parse_all()
    assert [entry["model"] for entry in entries] == ["gpt-5.2-codex", "gpt-5.2-codex", "gpt-5.3-codex"]

    signatures = ((str(path), path.stat().st_mtime_ns, path.stat().st_size),)
    sessions = sessions_module._load_codex_sessions(signatures, ())
    turn_models = [turn["model"] for turn in sessions[session_id]["turns"]]
    assert turn_models == ["gpt-5.2-codex", "gpt-5.2-codex", "gpt-5.3-codex"]
    assert all("_model_placeholder" not in turn for turn in sessions[session_id]["turns"])
    bill_models = [turn["_bill"]["model"] for turn in sessions[session_id]["turns"]]
    assert bill_models == ["openai/gpt-5.2-codex", "openai/gpt-5.2-codex", "openai/gpt-5.3-codex"]


def test_codex_cross_file_replacement_still_backfills(monkeypatch, tmp_path):
    """A duplicate with an earlier timestamp replaces the slot owned by a
    previously parsed file (index before the current file's first append). The
    replacement's placeholder marker must still be resolved by the current
    file's own model signal — not left as the default model with a leaked flag."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "01" / "02"
    usage = dict(last_input=100, last_cache=10, last_output=20, last_reasoning=5,
                 total_input=100, total_cache=10, total_output=20, total_reasoning=5)

    # File 1 (sorts first): owns the event-key slot with a LATER timestamp.
    _write_jsonl(codex_dir / "rollout-1-first.jsonl", [
        {
            "timestamp": "2026-01-02T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "s1", "cwd": "/work/tokdash", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-01-02T00:00:01.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.5", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total("2026-01-02T00:00:10.000Z", **usage),
    ])
    # File 2: same session scope and same usage snapshot with an EARLIER
    # timestamp, written before its own first model signal. It replaces file 1's
    # slot while still carrying the placeholder marker.
    _write_jsonl(codex_dir / "rollout-2-second.jsonl", [
        {
            "timestamp": "2026-01-01T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "s1", "cwd": "/work/tokdash", "model_provider": "openai"},
        },
        _codex_token_count_row_with_total("2026-01-01T00:00:05.000Z", **usage),
        {
            "timestamp": "2026-01-01T00:01:00.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
    ])

    entries = CodexParser(PricingDatabase())._parse_all()
    assert len(entries) == 1
    assert entries[0]["model"] == "gpt-5.6-sol"
    assert "_model_placeholder" not in entries[0]


def test_codex_file_without_any_model_signal_is_unknown(monkeypatch, tmp_path):
    """A file with no turn_context, no thread_settings_applied, and no provenance
    model must label its rows explicitly unknown (issue #23) — token counts kept,
    cost $0 — rather than fabricating usage under the default model."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "01" / "03"
    usage = dict(last_input=100, last_cache=10, last_output=20, last_reasoning=5,
                 total_input=100, total_cache=10, total_output=20, total_reasoning=5)
    _write_jsonl(codex_dir / "rollout-nosignal.jsonl", [
        {
            "timestamp": "2026-01-03T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "nosignal-1", "cwd": "/work/tokdash", "model_provider": "openai"},
        },
        _codex_token_count_row_with_total("2026-01-03T00:00:05.000Z", **usage),
    ])

    entries = CodexParser(PricingDatabase())._parse_all()
    assert len(entries) == 1
    assert entries[0]["model"] == "unknown"
    assert entries[0]["cost"] == 0.0
    assert "_model_placeholder" not in entries[0]

    path = codex_dir / "rollout-nosignal.jsonl"
    raw = sessions_module._parse_codex_session_file(
        str(path), path.stat().st_mtime_ns, path.stat().st_size, ()
    )
    assert raw is not None
    assert [turn["model"] for turn in raw["turns"]] == ["unknown"]
    assert raw["turns"][0]["_bill"]["model"] == "unknown"
    assert raw["turns"][0]["cost"] == 0.0
    assert all("_model_placeholder" not in turn for turn in raw["turns"])


def test_codex_orphan_sibling_forks_keep_one_replay_copy(monkeypatch, tmp_path):
    """Two sibling forks of a parent that was never indexed would each retain an
    identical parent-scoped copy of the replay prefix. Overview's global event-key
    index keeps one; Sessions must do the same — the earliest sibling keeps the
    prefix, later siblings drop it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    parent_id = "019f8024-8014-7202-aa03-9f039f67a648"
    child_a = "019f80ad-f48c-7361-a358-532ff84dcba9"
    child_b = "019f80ae-1c1c-7dd3-86e5-a40140bdcb41"
    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "20"
    usage_1, usage_2, usage_3, own_1, own_2 = _codex_fork_usage_rows()

    def fork_rows(child_id, fork_ts, own_ts):
        return [
            {
                "timestamp": f"{fork_ts}.000Z",
                "type": "session_meta",
                "payload": {
                    "id": child_id,
                    "cwd": "/work/tokdash",
                    "forked_from_id": parent_id,
                    "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": 1}}},
                    "model_provider": "openai",
                },
            },
            _codex_token_count_row_with_total(f"{fork_ts}.001Z", **usage_1),
            _codex_token_count_row_with_total(f"{fork_ts}.002Z", **usage_2),
            _codex_token_count_row_with_total(f"{fork_ts}.003Z", **usage_3),
            {
                "timestamp": f"{fork_ts}.004Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
            },
            _codex_token_count_row_with_total(f"{own_ts}.000Z", **own_1),
            _codex_token_count_row_with_total(f"{own_ts}.001Z", **own_2),
        ]

    # child_b forked earlier, so its restamped replay copy is the survivor.
    _write_jsonl(codex_dir / "rollout-1-a.jsonl", fork_rows(child_a, "2026-07-20T18:58:31", "2026-07-20T19:00:10"))
    _write_jsonl(codex_dir / "rollout-2-b.jsonl", fork_rows(child_b, "2026-07-20T18:00:00", "2026-07-20T18:30:10"))

    signatures = tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(codex_dir.glob("*.jsonl"))
    )
    sessions = sessions_module._load_codex_sessions(signatures, ())
    assert set(sessions) == {child_a, child_b}
    assert len(sessions[child_b]["turns"]) == 5   # earliest sibling keeps the prefix
    assert [
        turn["timestamp_ms"] for turn in sessions[child_a]["turns"]
    ] == [
        sessions_module._parse_iso_to_ms("2026-07-20T19:00:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-20T19:00:10.001Z"),
    ]


def test_codex_archived_sessions_root_is_scanned(monkeypatch, tmp_path):
    """Issue #23: Codex archives rollouts to ~/.codex/archived_sessions. Both the
    Overview parser and the Sessions signature scan must include that root, and a
    file present in BOTH roots (archived copy not yet removed) must collapse via
    the stable event key rather than double-count."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    usage = dict(last_input=100, last_cache=10, last_output=20, last_reasoning=5,
                 total_input=100, total_cache=10, total_output=20, total_reasoning=5)
    live_dir = tmp_path / ".codex" / "sessions" / "2026" / "08" / "01"
    archived_dir = tmp_path / ".codex" / "archived_sessions" / "2026" / "07" / "01"

    archived_only_rows = [
        {
            "timestamp": "2026-07-01T08:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "archived-only", "cwd": "/work/tokdash", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-07-01T08:00:01.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total("2026-07-01T08:00:10.000Z", **usage),
    ]
    _write_jsonl(archived_dir / "rollout-archived.jsonl", archived_only_rows)
    # Same content also still present under sessions/ (copy not yet cleaned up).
    _write_jsonl(live_dir / "rollout-live-copy.jsonl", archived_only_rows)

    entries = CodexParser(PricingDatabase())._parse_all()
    assert len(entries) == 1  # identical snapshots in both roots collapse by key
    assert entries[0]["model"] == "gpt-5.6-sol"

    signatures = sessions_module._codex_file_signatures()
    assert len(signatures) == 2  # both roots are scanned
    sessions = sessions_module._load_codex_sessions(signatures, ())
    assert set(sessions) == {"archived-only"}
    assert len(sessions["archived-only"]["turns"]) == 1  # merged, deduped by key


def test_codex_missing_parent_records_still_dedupe_replay(monkeypatch, tmp_path):
    """A durable store keeps a session's rows (missing=1) after its file is deleted.
    Those rows still prove the parent was indexed: the unbounded parent lookup must
    include them or a deleted parent resurrects the child's replay prefix."""
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    parent_path = tmp_path / "rollout-parent.jsonl"
    parent_raw = {
        "tool": "codex",
        "session_id": "parent-1",
        "turns": [{"turn_index": 1, "timestamp_ms": 100, "_event_key": "k1"}],
    }
    assert store.sync_session_files(
        "codex",
        ((str(parent_path), 1, 100),),
        parser={"v": 2},
        parse_file_session=lambda _sig: parent_raw,
        durable=True,
    )
    # Parent file deleted: durable sync marks the record missing instead of deleting.
    assert store.sync_session_files(
        "codex",
        (),
        parser={"v": 2},
        parse_file_session=lambda _sig: None,
        durable=True,
    )
    records = store.query_session_records_by_ids("codex", {"parent-1"})
    assert len(records) == 1
    assert records[0]["turns"][0]["_event_key"] == "k1"


def test_codex_session_meta_provenance_model_is_an_early_model_source(monkeypatch, tmp_path):
    """Issue #23 reported the selected model nested under session_meta
    .base_instructions.provenance.model. No real log through Codex 0.147.0 carries
    that key (unconfirmed shape), but the lookup stays so rows never bill under
    the placeholder default if a future build adds it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "08" / "18"
    usage_1 = dict(last_input=100, last_cache=10, last_output=20, last_reasoning=5,
                   total_input=100, total_cache=10, total_output=20, total_reasoning=5)
    rows = [
        {
            "timestamp": "2026-08-18T10:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": "provenance-session",
                "cwd": "/work/tokdash",
                "source": "cli",
                "model_provider": "openai",
                "base_instructions": {"text": "...", "provenance": {"model": "gpt-5.6-terra"}},
            },
        },
        # No turn_context or thread_settings_applied: provenance is the only signal.
        _codex_token_count_row_with_total("2026-08-18T10:01:10.000Z", **usage_1),
    ]
    _write_jsonl(codex_dir / "rollout-provenance.jsonl", rows)

    parser = CodexParser(PricingDatabase())
    entries = parser._parse_all()
    assert len(entries) == 1
    assert entries[0]["model"] == "gpt-5.6-terra"

    path = codex_dir / "rollout-provenance.jsonl"
    raw = sessions_module._parse_codex_session_file(
        str(path), path.stat().st_mtime_ns, path.stat().st_size, ()
    )
    assert raw is not None
    assert [turn["model"] for turn in raw["turns"]] == ["gpt-5.6-terra"]


def test_codex_forked_from_id_only_file_gets_replay_dedup(monkeypatch, tmp_path):
    """Ancestry declared only via top-level forked_from_id (no thread_spawn marker —
    user /fork, exec forks) replays the parent prefix the same way and must get the
    same rekeying. Without the thread_spawn marker the session stays primary."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    parent_id = "019f8024-8014-7202-aa03-9f039f67a648"
    child_id = "019f90ad-f48c-7361-a358-532ff84dcba9"
    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "21"
    usage_1, usage_2, usage_3, own_1, own_2 = _codex_fork_usage_rows()

    parent_rows = [
        {
            "timestamp": "2026-07-21T08:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": parent_id, "cwd": "/work/tokdash", "source": "vscode", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-07-21T08:00:01.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total("2026-07-21T08:01:10.000Z", **usage_1),
        _codex_token_count_row_with_total("2026-07-21T08:02:10.000Z", **usage_2),
        _codex_token_count_row_with_total("2026-07-21T08:03:10.000Z", **usage_3),
    ]
    child_rows = [
        {
            "timestamp": "2026-07-21T12:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": child_id,
                "cwd": "/work/tokdash",
                "forked_from_id": parent_id,
                "source": "cli",
                "model_provider": "openai",
            },
        },
        _codex_token_count_row_with_total("2026-07-21T12:00:01.000Z", **usage_1),
        _codex_token_count_row_with_total("2026-07-21T12:00:02.000Z", **usage_2),
        _codex_token_count_row_with_total("2026-07-21T12:00:03.000Z", **usage_3),
        {
            "timestamp": "2026-07-21T12:00:04.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total("2026-07-21T12:10:10.000Z", **own_1),
        _codex_token_count_row_with_total("2026-07-21T12:11:10.000Z", **own_2),
    ]
    _write_jsonl(codex_dir / "rollout-1-parent.jsonl", parent_rows)
    _write_jsonl(codex_dir / "rollout-2-child.jsonl", child_rows)

    parser = CodexParser(PricingDatabase())
    entries = parser._parse_all()
    assert len(entries) == 5
    assert parser.replay_events_skipped == 3

    signatures = tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(codex_dir.glob("*.jsonl"))
    )
    sessions = sessions_module._load_codex_sessions(signatures, ())
    assert set(sessions) == {parent_id, child_id}
    assert len(sessions[parent_id]["turns"]) == 3
    assert len(sessions[child_id]["turns"]) == 2

    # No thread_spawn marker: the fork is not a subagent for activity purposes.
    child_path = codex_dir / "rollout-2-child.jsonl"
    raw = sessions_module._parse_codex_session_file(
        str(child_path), child_path.stat().st_mtime_ns, child_path.stat().st_size, ()
    )
    assert raw is not None
    assert raw["_activity"]["is_primary"] is True


def test_codex_subagent_replay_gate_keeps_own_turns_with_stable_keys(monkeypatch, tmp_path):
    """Codex 0.144 shape: the child file replays the parent's session_meta, so the
    current session id stays the parent's for the rest of the file — including the
    subagent's OWN turns after the replay. When those events carry cumulative state,
    the stable key already dedups the replay against the parent file; hard-skipping
    every parent-attributed event loses the subagent's genuine work. Only rows without
    a stable key may still hit the source-shape fallback."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    parent_id = "019f8024-8014-7202-aa03-9f039f67a648"
    child_id = "019f8084-9931-7403-b9ff-6f631cb5703f"
    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "20"
    usage_1, usage_2, usage_3, own_1, own_2 = _codex_fork_usage_rows()

    parent_rows = [
        {
            "timestamp": "2026-07-20T16:28:24.000Z",
            "type": "session_meta",
            "payload": {"id": parent_id, "cwd": "/work/tokdash", "source": "vscode", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-07-20T16:28:25.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total("2026-07-20T16:30:10.000Z", **usage_1),
        _codex_token_count_row_with_total("2026-07-20T16:31:10.000Z", **usage_2),
        _codex_token_count_row_with_total("2026-07-20T16:32:10.000Z", **usage_3),
    ]
    child_rows = [
        {
            "timestamp": "2026-07-20T18:13:21.000Z",
            "type": "session_meta",
            "payload": {
                "id": child_id,
                "cwd": "/work/tokdash",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": 1}}},
                "model_provider": "openai",
            },
        },
        # Replayed parent session_meta flips the current session id to the parent's.
        {
            "timestamp": "2026-07-20T18:13:21.001Z",
            "type": "session_meta",
            "payload": {"id": parent_id, "cwd": "/work/tokdash", "source": "vscode", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-07-20T18:13:21.002Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total("2026-07-20T18:13:22.000Z", **usage_1),
        _codex_token_count_row_with_total("2026-07-20T18:13:22.001Z", **usage_2),
        _codex_token_count_row_with_total("2026-07-20T18:13:22.002Z", **usage_3),
        # Genuine subagent work continues under the parent's session id (no second
        # child session_meta is ever written).
        _codex_token_count_row_with_total("2026-07-20T18:20:10.000Z", **own_1),
        _codex_token_count_row_with_total("2026-07-20T18:21:10.000Z", **own_2),
    ]
    _write_jsonl(codex_dir / "rollout-1-parent.jsonl", parent_rows)
    _write_jsonl(codex_dir / "rollout-2-child.jsonl", child_rows)

    parser = CodexParser(PricingDatabase())
    entries = parser._parse_all()
    assert len(entries) == 5
    assert parser.replay_events_skipped == 3
    expected_timestamps = [
        sessions_module._parse_iso_to_ms("2026-07-20T16:30:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-20T16:31:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-20T16:32:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-20T18:20:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-20T18:21:10.000Z"),
    ]
    assert [entry["timestamp"] for entry in entries] == expected_timestamps

    # Sessions tab: the child file merges into the parent session; the replay turns
    # dedup by event key and the subagent's own turns survive there.
    signatures = tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(codex_dir.glob("*.jsonl"))
    )
    sessions = sessions_module._load_codex_sessions(signatures, ())
    assert set(sessions) == {parent_id}
    assert [turn["timestamp_ms"] for turn in sessions[parent_id]["turns"]] == expected_timestamps


def test_codex_windowed_stored_read_dedupes_subagent_replay(monkeypatch, tmp_path):
    """A windowed stored read only loads sessions touching the window. The forked
    subagent file keeps its own session id and its replayed prefix is restamped
    into the window, so the parent session is absent from the result. The replay
    dedup must still find the parent's keys in the store, unbounded by the window
    — otherwise Sessions disagrees with Overview exactly when the window hides
    the parent."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    parent_id = "019f8024-8014-7202-aa03-9f039f67a648"
    child_id = "019f80ad-f48c-7361-a358-532ff84dcba9"
    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "20"
    usage_1, usage_2, usage_3, own_1, own_2 = _codex_fork_usage_rows()

    # Parent ran on day 1 only.
    parent_rows = [
        {
            "timestamp": "2026-07-20T16:28:24.000Z",
            "type": "session_meta",
            "payload": {"id": parent_id, "cwd": "/work/tokdash", "source": "vscode", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-07-20T16:28:25.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total("2026-07-20T16:30:10.000Z", **usage_1),
        _codex_token_count_row_with_total("2026-07-20T16:31:10.000Z", **usage_2),
        _codex_token_count_row_with_total("2026-07-20T16:32:10.000Z", **usage_3),
    ]
    # Forked on day 2; the replayed prefix is restamped to day 2, inside the window.
    child_rows = [
        {
            "timestamp": "2026-07-21T09:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": child_id,
                "cwd": "/work/tokdash",
                "forked_from_id": parent_id,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": 1}}},
                "model_provider": "openai",
            },
        },
        _codex_token_count_row_with_total("2026-07-21T09:00:01.000Z", **usage_1),
        _codex_token_count_row_with_total("2026-07-21T09:00:02.000Z", **usage_2),
        _codex_token_count_row_with_total("2026-07-21T09:00:03.000Z", **usage_3),
        {
            "timestamp": "2026-07-21T09:00:04.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row_with_total("2026-07-21T09:10:10.000Z", **own_1),
        _codex_token_count_row_with_total("2026-07-21T09:11:10.000Z", **own_2),
    ]
    _write_jsonl(codex_dir / "rollout-1-parent.jsonl", parent_rows)
    _write_jsonl(codex_dir / "rollout-2-child.jsonl", child_rows)

    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    monkeypatch.setattr(sessions_module, "UsageEntryStore", lambda: store)

    day2_start = sessions_module._parse_iso_to_ms("2026-07-21T00:00:00.000Z")
    day3_start = sessions_module._parse_iso_to_ms("2026-07-22T00:00:00.000Z")

    windowed = sessions_module._stored_sessions_for_tool(
        "codex", since_ms=day2_start, until_ms=day3_start
    )
    # The parent's files are entirely outside the window, so its session is not
    # in the result — but the child must still lose the replayed prefix.
    assert parent_id not in windowed
    assert set(windowed) == {child_id}
    assert [
        turn["timestamp_ms"] for turn in windowed[child_id]["turns"]
    ] == [
        sessions_module._parse_iso_to_ms("2026-07-21T09:10:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-21T09:11:10.000Z"),
    ]

    # Unwindowed reads see the parent and dedup against it directly.
    unbounded = sessions_module._stored_sessions_for_tool("codex")
    assert set(unbounded) == {parent_id, child_id}
    assert len(unbounded[parent_id]["turns"]) == 3
    assert len(unbounded[child_id]["turns"]) == 2


def test_codex_thread_settings_applied_is_an_early_model_source(monkeypatch, tmp_path):
    """Newer Codex emits thread_settings_applied before the first turn_context.
    Rows that follow it (and rows before any model signal, via backfill) must use
    that model instead of the placeholder default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "08" / "12"
    usage_1 = dict(last_input=100, last_cache=10, last_output=20, last_reasoning=5,
                   total_input=100, total_cache=10, total_output=20, total_reasoning=5)
    rows = [
        {
            "timestamp": "2026-08-12T11:25:16.000Z",
            "type": "session_meta",
            "payload": {"id": "settings-session", "cwd": "/work/tokdash", "source": "cli", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-08-12T11:25:16.100Z",
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_settings": {"model": "gpt-5.6-sol", "model_provider_id": "openai"},
            },
        },
        # No turn_context in this file at all: thread_settings_applied is the only
        # model signal and must win over the placeholder default.
        _codex_token_count_row_with_total("2026-08-12T11:26:10.000Z", **usage_1),
    ]
    _write_jsonl(codex_dir / "rollout-settings.jsonl", rows)

    parser = CodexParser(PricingDatabase())
    entries = parser._parse_all()
    assert len(entries) == 1
    assert entries[0]["model"] == "gpt-5.6-sol"
    assert entries[0]["provider"] == "openai"

    path = codex_dir / "rollout-settings.jsonl"
    raw = sessions_module._parse_codex_session_file(
        str(path), path.stat().st_mtime_ns, path.stat().st_size, ()
    )
    assert raw is not None
    assert [turn["model"] for turn in raw["turns"]] == ["gpt-5.6-sol"]


def test_codex_primary_resume_replay_is_deduplicated_across_files(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    logical_id = "019f6a43-3863-7892-b67b-c8b45093b547"
    resume_file_id = "019fb22c-5baa-7052-a593-feec9cf2d74d"
    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "30"

    usage_1 = dict(
        last_input=100,
        last_cache=10,
        last_output=20,
        last_reasoning=5,
        total_input=100,
        total_cache=10,
        total_output=20,
        total_reasoning=5,
    )
    usage_2 = dict(
        last_input=110,
        last_cache=11,
        last_output=21,
        last_reasoning=6,
        total_input=210,
        total_cache=21,
        total_output=41,
        total_reasoning=11,
    )
    usage_3 = dict(
        last_input=120,
        last_cache=12,
        last_output=22,
        last_reasoning=7,
        total_input=330,
        total_cache=33,
        total_output=63,
        total_reasoning=18,
    )

    parent_rows = [
        {
            "timestamp": "2026-07-16T09:32:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": logical_id,
                "cwd": "/work/PersonalMemoryQA",
                "source": "vscode",
                "model_provider": "openai",
            },
        },
        {
            "timestamp": "2026-07-16T09:32:01.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.3-codex", "cwd": "/work/PersonalMemoryQA"},
        },
        _codex_token_count_row_with_total("2026-07-16T09:32:10.000Z", **usage_1),
        _codex_token_count_row_with_total("2026-07-16T09:33:10.000Z", **usage_2),
    ]
    replayed_usage_1 = _codex_token_count_row_with_total("2026-07-30T08:38:00.000Z", **usage_1)
    replayed_usage_2 = _codex_token_count_row_with_total("2026-07-30T08:38:00.001Z", **usage_2)
    for replayed in (replayed_usage_1, replayed_usage_2):
        # Codex 0.146 adds this explicit zero while replaying snapshots written by
        # older versions that omitted the field.
        replayed["payload"]["info"]["total_token_usage"]["cache_write_input_tokens"] = 0
        replayed["payload"]["info"]["last_token_usage"]["cache_write_input_tokens"] = 0
    resumed_rows = [
        {
            "timestamp": "2026-07-30T08:37:59.000Z",
            "type": "session_meta",
            "payload": {
                "id": resume_file_id,
                "cwd": "/work/PersonalMemoryQA",
                "source": "vscode",
                "model_provider": "openai",
            },
        },
        {
            "timestamp": "2026-07-30T08:37:59.001Z",
            "type": "session_meta",
            "payload": {
                "id": logical_id,
                "cwd": "/work/PersonalMemoryQA",
                "source": "vscode",
                "model_provider": "openai",
            },
        },
        {
            "timestamp": "2026-07-30T08:37:59.002Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.3-codex", "cwd": "/work/PersonalMemoryQA"},
        },
        {
            "timestamp": "2026-07-30T08:37:59.003Z",
            "type": "event_msg",
            "payload": {
                "type": "thread_name_updated",
                "thread_id": logical_id,
                "thread_name": "Investigate PersonalMemory token usage",
            },
        },
        # Restamped copies of the original usage snapshots.
        replayed_usage_1,
        replayed_usage_2,
        # Genuine resumed work remains under the old logical session id.
        _codex_token_count_row_with_total("2026-07-30T08:38:11.000Z", **usage_3),
    ]

    # Replay sorts first to ensure both live parsing and session merging choose
    # the earliest occurrence by timestamp, not discovery order.
    resumed_path = codex_dir / "rollout-1-resumed.jsonl"
    parent_path = codex_dir / "rollout-2-parent.jsonl"
    _write_jsonl(parent_path, parent_rows)
    _write_jsonl(resumed_path, resumed_rows)

    parser = CodexParser(PricingDatabase())
    entries = parser._parse_all()
    assert len(entries) == 3
    assert parser.replay_events_skipped == 2
    expected_timestamps = [
        sessions_module._parse_iso_to_ms("2026-07-16T09:32:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-16T09:33:10.000Z"),
        sessions_module._parse_iso_to_ms("2026-07-30T08:38:11.000Z"),
    ]
    assert [entry["timestamp"] for entry in entries] == expected_timestamps
    assert all(entry["entry_id"].startswith("codex-token-v1:") for entry in entries)

    signatures = tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        for path in (resumed_path, parent_path)
    )
    sessions = sessions_module._load_codex_sessions(signatures, ())
    assert set(sessions) == {logical_id}
    assert sessions[logical_id]["display_name"] == "Investigate PersonalMemory token usage"
    assert sessions[logical_id]["_display_name_explicit"] is True
    assert len(sessions[logical_id]["turns"]) == 3
    assert [turn["timestamp_ms"] for turn in sessions[logical_id]["turns"]] == expected_timestamps

    parent_raw = sessions_module._parse_codex_session_file(
        str(parent_path), parent_path.stat().st_mtime_ns, parent_path.stat().st_size, ()
    )
    resumed_raw = sessions_module._parse_codex_session_file(
        str(resumed_path), resumed_path.stat().st_mtime_ns, resumed_path.stat().st_size, ()
    )
    assert parent_raw["_display_name_explicit"] is False
    assert resumed_raw["_display_name_explicit"] is True
    stored = sessions_module._session_records_to_raw_sessions("codex", [resumed_raw, parent_raw])
    assert stored[logical_id]["display_name"] == "Investigate PersonalMemory token usage"
    assert len(stored[logical_id]["turns"]) == 3


def test_codex_event_identity_is_scoped_to_current_session_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "30"
    shared_usage = dict(
        last_input=100,
        last_cache=10,
        last_output=20,
        last_reasoning=5,
        total_input=100,
        total_cache=10,
        total_output=20,
        total_reasoning=5,
    )
    rows = [
        {
            "timestamp": "2026-07-30T09:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "before-compaction", "source": "vscode", "model_provider": "openai"},
        },
        _codex_token_count_row_with_total("2026-07-30T09:00:01.000Z", **shared_usage),
        {
            "timestamp": "2026-07-30T09:01:00.000Z",
            "type": "session_meta",
            "payload": {"id": "after-compaction", "source": "vscode", "model_provider": "openai"},
        },
        # Identical counters under a different logical id are not a replay collision.
        _codex_token_count_row_with_total("2026-07-30T09:01:01.000Z", **shared_usage),
    ]
    _write_jsonl(codex_dir / "rollout-compaction.jsonl", rows)

    parser = CodexParser(PricingDatabase())
    entries = parser._parse_all()
    assert len(entries) == 2
    assert entries[0]["entry_id"] != entries[1]["entry_id"]
    assert parser.replay_events_skipped == 0

    path = codex_dir / "rollout-compaction.jsonl"
    raw = sessions_module._parse_codex_session_file(str(path), path.stat().st_mtime_ns, path.stat().st_size, ())
    assert raw is not None
    assert len(raw["turns"]) == 2


def test_codex_partial_usage_snapshots_fall_back_without_deduplicating(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "30"
    rows = [
        {
            "timestamp": "2026-07-30T10:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "partial-session", "source": "cli", "model_provider": "openai"},
        },
        # Without cumulative state, identical per-call amounts are ambiguous. Keep
        # both rather than risk silently dropping a genuine call.
        _codex_token_count_row("2026-07-30T10:00:01.000Z", 100, 10, 20, 5),
        _codex_token_count_row("2026-07-30T10:00:02.000Z", 100, 10, 20, 5),
    ]
    _write_jsonl(codex_dir / "rollout-partial.jsonl", rows)

    parser = CodexParser(PricingDatabase())
    entries = parser._parse_all()
    assert len(entries) == 2
    assert parser.replay_events_skipped == 0
    assert entries[0]["entry_id"] != entries[1]["entry_id"]

    path = codex_dir / "rollout-partial.jsonl"
    raw = sessions_module._parse_codex_session_file(str(path), path.stat().st_mtime_ns, path.stat().st_size, ())
    assert raw is not None
    assert len(raw["turns"]) == 2


def test_codex_primary_session_id_change_is_not_skipped(monkeypatch, tmp_path):
    """The source-shape fallback is gated on positive `thread_spawn` detection.
    A primary file whose id changes mid-file must keep real events; stable identity
    also scopes full snapshots by current logical id (covered above)."""
    primary_home = tmp_path / "primary-home"
    primary_dir = primary_home / ".codex" / "sessions" / "2026" / "07" / "13"

    id_a = "session-id-a"
    id_b = "session-id-b"

    primary_rows = [
        {
            "timestamp": "2026-07-13T09:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": id_a, "cwd": "/work/tokdash", "source": "vscode", "model_provider": "openai"},
        },
        {
            "timestamp": "2026-07-13T09:00:01.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": "/work/tokdash"},
        },
        _codex_token_count_row("2026-07-13T09:00:02.000Z", 50, 5, 10, 2),
        # session_meta.id CHANGES mid-file (e.g. compaction/fork) - no thread_spawn marker
        # anywhere in this file, so it must never be treated as a subagent rollout.
        {
            "timestamp": "2026-07-13T09:05:00.000Z",
            "type": "session_meta",
            "payload": {"id": id_b, "cwd": "/work/tokdash", "source": "vscode", "model_provider": "openai"},
        },
        _codex_token_count_row("2026-07-13T09:05:02.000Z", 60, 6, 12, 3),
    ]
    _write_jsonl(primary_dir / "rollout-primary.jsonl", primary_rows)

    monkeypatch.setenv("HOME", str(primary_home))
    monkeypatch.setattr(Path, "home", lambda: primary_home)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _clear_parser_caches()

    parser = CodexParser(PricingDatabase())
    entries = parser._parse_all()
    assert len(entries) == 2
    assert parser.replay_events_skipped == 0

    primary_path = primary_dir / "rollout-primary.jsonl"
    primary_stat = primary_path.stat()
    primary_raw = sessions_module._parse_codex_session_file(
        str(primary_path), primary_stat.st_mtime_ns, primary_stat.st_size, ()
    )
    assert primary_raw is not None
    assert len(primary_raw["turns"]) == 2


def test_codex_sessions_echo_effective_review_default(monkeypatch, tmp_path):
    """The response echoes the effective review-session default so the dashboard
    toggle can adopt the server's TOKDASH_INCLUDE_CODEX_GUARDIAN default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "06" / "19"
    _write_jsonl(codex_dir / "normal.jsonl", _codex_session_rows("normal-session"))

    def effective(env_value, param):
        if env_value is None:
            monkeypatch.delenv("TOKDASH_INCLUDE_CODEX_GUARDIAN", raising=False)
        else:
            monkeypatch.setenv("TOKDASH_INCLUDE_CODEX_GUARDIAN", env_value)
        _clear_parser_caches()
        return sessions_module.get_sessions_data(
            "codex", "today", "2026-06-19", "2026-06-19", include_review_sessions=param
        )["include_review_sessions"]

    # Explicit param wins over the env default.
    assert effective(None, True) is True
    assert effective("1", False) is False
    # When the param is omitted, the env default decides.
    assert effective(None, None) is False
    assert effective("1", None) is True


def test_session_display_name_fallbacks(monkeypatch, tmp_path):
    _clear_parser_caches()

    codex_file = tmp_path / "codex.jsonl"
    _write_jsonl(codex_file, _codex_session_rows("codex-session", thread_name="Fix busy refresh"))
    stat = codex_file.stat()
    codex_raw = sessions_module._parse_codex_session_file(str(codex_file), stat.st_mtime_ns, stat.st_size, ())
    assert codex_raw["display_name"] == "Fix busy refresh"
    assert sessions_module._summarize_session(codex_raw)["display_name"] == "Fix busy refresh"

    codex_context_file = tmp_path / "codex-context.jsonl"
    _write_jsonl(
        codex_context_file,
        _codex_session_rows(
            "codex-context-session",
            user_message="# Context from my IDE setup:\n\n## Active file: data/README.md",
        ),
    )
    stat = codex_context_file.stat()
    codex_context_raw = sessions_module._parse_codex_session_file(
        str(codex_context_file), stat.st_mtime_ns, stat.st_size, ()
    )
    assert codex_context_raw["display_name"] == "tokdash"

    claude_file = tmp_path / "claude.jsonl"
    _write_jsonl(
        claude_file,
        [
            {"type": "ai-title", "sessionId": "claude-session", "aiTitle": "Draft older title"},
            {"type": "custom-title", "sessionId": "claude-session", "customTitle": "Polish sessions"},
            {
                "type": "assistant",
                "sessionId": "claude-session",
                "cwd": "/work/tokdash",
                "timestamp": "2026-06-19T10:00:00.000Z",
                "message": {
                    "role": "assistant",
                    "id": "m1",
                    "model": "claude-sonnet-4",
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                },
            },
        ],
    )
    stat = claude_file.stat()
    claude_raw = sessions_module._parse_claude_session_file(str(claude_file), stat.st_mtime_ns, stat.st_size, ())
    assert claude_raw["display_name"] == "Polish sessions"


def test_codex_session_display_name_uses_state_db_title(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _clear_parser_caches()

    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "06" / "19"
    _write_jsonl(
        codex_dir / "codex.jsonl",
        _codex_session_rows(
            "codex-session",
            user_message="# Context from my IDE setup:\n\n## Active file: data/README.md",
        ),
    )

    state_db = tmp_path / ".codex" / "state_5.sqlite"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_db))
    try:
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', "
            "preview TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO threads (id, title, preview, first_user_message) VALUES (?, ?, ?, ?)",
            ("codex-session", "Implement real Codex titles", "fallback preview", "fallback first user"),
        )
        conn.commit()
    finally:
        conn.close()

    data = sessions_module.get_sessions_data("codex", "today", "2026-06-19", "2026-06-19")

    assert data["sessions"][0]["display_name"] == "Implement real Codex titles"


def test_pi_agent_sessions_data_and_detail(monkeypatch, tmp_path):
    pi_root = tmp_path / "pi-sessions"
    monkeypatch.setenv("PI_AGENT_DIR", str(pi_root))
    _clear_parser_caches()

    _write_jsonl(
        pi_root / "direct.jsonl",
        [
            {"type": "session", "id": "pi-direct", "cwd": "/tmp/direct-project", "timestamp": "2026-06-19T09:00:00.000Z"},
            {"type": "model_change", "provider": "minimax-cn", "modelId": "MiniMax-M2.7"},
            {
                "type": "message",
                "id": "u1",
                "timestamp": "2026-06-19T09:30:00.000Z",
                "message": {"role": "user", "content": "Investigate Pi session titles"},
            },
            {
                "type": "message",
                "id": "a1",
                "timestamp": "2026-06-19T10:00:00.000Z",
                "message": {
                    "role": "assistant",
                    "usage": {"input": 5, "cacheWrite": 2, "cacheRead": 3, "output": 4, "totalTokens": 14},
                },
            },
        ],
    )
    _write_jsonl(
        pi_root / "nested" / "2026-06-19T10-00-00-000Z_pi-named.jsonl",
        [
            {"type": "session", "id": "pi-named", "cwd": "/work/tokdash", "timestamp": "2026-06-19T10:00:00.000Z"},
            {"type": "session_info", "name": "Plan Pi support"},
            {
                "type": "message",
                "id": "b1",
                "timestamp": "2026-06-19T11:00:00.000Z",
                "message": {
                    "role": "assistant",
                    "provider": "openai",
                    "model": "gpt-5.3-codex",
                    "usage": {"input": 7, "cacheWrite": 1, "cacheRead": 2, "output": 6, "cost": {"total": 0.25}},
                },
            },
        ],
    )

    data = sessions_module.get_sessions_data("pi_agent", "today", "2026-06-19", "2026-06-19")
    rows = {row["session_id"]: row for row in data["sessions"]}

    assert set(rows) == {"pi-direct", "pi-named"}
    assert rows["pi-direct"]["display_name"] == "Investigate Pi session titles"
    assert rows["pi-direct"]["tokens_in"] == 7
    assert rows["pi-direct"]["tokens_cache"] == 3
    assert rows["pi-direct"]["tokens_out"] == 4
    assert rows["pi-named"]["display_name"] == "Plan Pi support"
    assert rows["pi-named"]["cost"] == 0.25

    detail = sessions_module.get_session_detail("pi_agent", "pi-named")
    assert detail["session"]["display_name"] == "Plan Pi support"
    assert detail["turns"][0]["tokens"] == 16


def test_codex_stored_session_records_merge_instead_of_overwriting():
    records = [
        {
            "tool": "codex",
            "session_id": "dup",
            "project": "old",
            "display_name": "old",
            "_display_name_explicit": False,
            "turns": [{"tokens": 10}],
        },
        {
            "tool": "codex",
            "session_id": "dup",
            "project": "new",
            "display_name": "Investigate replay counting",
            "_display_name_explicit": True,
            "turns": [{"tokens": 20}],
        },
    ]

    result = sessions_module._session_records_to_raw_sessions("codex", records)

    assert result["dup"]["project"] == "old"
    assert result["dup"]["display_name"] == "Investigate replay counting"
    assert result["dup"]["_display_name_explicit"] is True
    assert [turn["tokens"] for turn in result["dup"]["turns"]] == [10, 20]
    assert [turn["turn_index"] for turn in result["dup"]["turns"]] == [1, 2]


def test_claude_stored_session_records_merge_in_one_pass_matches_legacy_semantics():
    records = [
        {
            "tool": "claude",
            "session_id": "shared",
            "project": "unknown",
            "turns": [
                {"turn_index": 1, "timestamp_ms": 1000, "model": "claude", "tokens_in": 1, "tokens_cache": 2, "tokens_out": 3, "tokens_reasoning": 0, "cost": 0.01},
                {"turn_index": 2, "timestamp_ms": 3000, "model": "claude", "tokens_in": 10, "tokens_cache": 0, "tokens_out": 1, "tokens_reasoning": 0, "cost": 0.02},
            ],
        },
        {
            "tool": "claude",
            "session_id": "shared",
            "project": "tokdash",
            "turns": [
                {"turn_index": 1, "timestamp_ms": 1000, "model": "claude", "tokens_in": 1, "tokens_cache": 2, "tokens_out": 3, "tokens_reasoning": 0, "cost": 0.01},
                {"turn_index": 2, "timestamp_ms": 2000, "model": "claude", "tokens_in": 4, "tokens_cache": 5, "tokens_out": 6, "tokens_reasoning": 0, "cost": 0.03},
            ],
        },
    ]

    # Reading prices the rows, so price both sides: what is under test here is
    # the merge, not the rates.
    expected = dict(records[0], turns=sessions_module._repriced_turns(records[0]["turns"]))
    expected = sessions_module._merge_raw_session(
        expected, dict(records[1], turns=sessions_module._repriced_turns(records[1]["turns"]))
    )
    result = sessions_module._session_records_to_raw_sessions("claude", records)

    assert result["shared"] == expected


def test_claude_stored_session_merge_documents_same_timestamp_tie_behavior():
    records = [
        {
            "tool": "claude",
            "session_id": "shared",
            "project": "",
            "turns": [
                {"turn_index": 2, "timestamp_ms": 1000, "model": "claude", "tokens_in": 2, "tokens_cache": 0, "tokens_out": 0, "tokens_reasoning": 0, "cost": 0.02}
            ],
        },
        {
            "tool": "claude",
            "session_id": "shared",
            "project": "tokdash",
            "turns": [
                {"turn_index": 1, "timestamp_ms": 1000, "model": "claude", "tokens_in": 1, "tokens_cache": 0, "tokens_out": 0, "tokens_reasoning": 0, "cost": 0.01}
            ],
        },
        {
            "tool": "claude",
            "session_id": "shared",
            "project": "ignored",
            "turns": [
                {"turn_index": 3, "timestamp_ms": 1000, "model": "claude", "tokens_in": 3, "tokens_cache": 0, "tokens_out": 0, "tokens_reasoning": 0, "cost": 0.03}
            ],
        },
    ]

    result = sessions_module._session_records_to_raw_sessions("claude", records)["shared"]

    assert result["project"] == "tokdash"
    assert [turn["tokens_in"] for turn in result["turns"]] == [1, 2, 3]
    assert [turn["turn_index"] for turn in result["turns"]] == [1, 2, 3]
    assert sum(turn["tokens_in"] for turn in result["turns"]) == 6


def _create_opencode_session_db(db_path: Path) -> tuple[tuple[str, int, int], ...]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT);
            CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, title TEXT, slug TEXT);
            CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
            """
        )
        conn.execute("INSERT INTO project(id, worktree) VALUES('p1', '/workspace/tokdash')")
        conn.execute("INSERT INTO project(id, worktree) VALUES('p2', '/workspace/other')")
        conn.execute("INSERT INTO session(id, project_id, directory, title, slug) VALUES('s1', 'p1', '/tmp/fallback', 'OpenCode title', 'open-slug')")
        conn.execute("INSERT INTO session(id, project_id, directory, title, slug) VALUES('s2', 'p2', '/tmp/other', '', 'other-slug')")
        messages = [
            (
                "before",
                "s1",
                900,
                {
                    "role": "assistant",
                    "modelID": "glm-5.2",
                    "providerID": "zai",
                    "tokens": {"input": 1, "output": 2, "reasoning": 0, "cache": {"write": 0, "read": 0}},
                },
            ),
            (
                "at_since",
                "s1",
                1000,
                {
                    "role": "assistant",
                    "modelID": "glm-5.2",
                    "providerID": "zai",
                    "tokens": {"input": 2, "output": 1, "reasoning": 0, "cache": {"write": 0, "read": 0}},
                },
            ),
            (
                "inside",
                "s1",
                1500,
                {
                    "role": "assistant",
                    "modelID": "glm-5.2",
                    "providerID": "zai",
                    "path": {"cwd": "/ignored/cwd"},
                    "tokens": {"input": 10, "output": 5, "reasoning": 6, "cache": {"write": 3, "read": 4}},
                },
            ),
            (
                "user",
                "s1",
                1600,
                {
                    "role": "user",
                    "modelID": "glm-5.2",
                    "providerID": "zai",
                    "tokens": {"input": 100, "output": 100, "cache": {"write": 0, "read": 0}},
                },
            ),
            (
                "at_until",
                "s1",
                2000,
                {
                    "role": "assistant",
                    "modelID": "glm-5.2",
                    "providerID": "zai",
                    "tokens": {"input": 7, "output": 1, "reasoning": 0, "cache": {"write": 0, "read": 0}},
                },
            ),
            (
                "other_inside",
                "s2",
                1500,
                {
                    "role": "assistant",
                    "modelID": "glm-5.2",
                    "providerID": "zai",
                    "tokens": {"input": 4, "output": 2, "reasoning": 0, "cache": {"write": 1, "read": 0}},
                },
            ),
        ]
        conn.executemany(
            "INSERT INTO message(id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            [(msg_id, session_id, ts, json.dumps(data)) for msg_id, session_id, ts, data in messages],
        )
        conn.execute(
            "INSERT INTO message(id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("malformed", "s1", 1700, "{not valid json"),
        )
        conn.commit()
    finally:
        conn.close()

    stat = db_path.stat()
    return ((str(db_path), stat.st_mtime_ns, stat.st_size),)


def _add_mimo_external_import(db_path: Path, message_ids: list[str]) -> tuple[tuple[str, int, int], ...]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE external_import(
                source TEXT NOT NULL,
                source_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_mtime INTEGER NOT NULL,
                time_imported INTEGER NOT NULL,
                message_ids TEXT,
                PRIMARY KEY(source, source_key)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO external_import(
                source, source_key, session_id, source_path, source_mtime, time_imported, message_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("cc", "claude-session", "s1", "/home/howard/.claude/projects/session.jsonl", 1, 2, json.dumps(message_ids)),
        )
        conn.commit()
    finally:
        conn.close()

    stat = db_path.stat()
    return ((str(db_path), stat.st_mtime_ns, stat.st_size),)


def test_opencode_session_loaders_use_sql_window_and_match_raw_json_fallback(tmp_path):
    db_path = tmp_path / "opencode.db"
    signature = _create_opencode_session_db(db_path)

    sessions_module._load_opencode_sessions.cache_clear()
    scalar = sessions_module._load_opencode_sessions(signature, (), 1000, 2000)
    raw = sessions_module._load_opencode_sessions_raw_json(db_path, since_ms=1000, until_ms=2000)
    all_rows = sessions_module._load_opencode_sessions_raw_json(db_path)

    assert scalar == raw
    assert set(scalar) == {"s1", "s2"}
    assert len(all_rows["s1"]["turns"]) == 4
    assert len(scalar["s1"]["turns"]) == 2
    assert [turn["timestamp_ms"] for turn in scalar["s1"]["turns"]] == [1000, 1500]
    turn = next(turn for turn in scalar["s1"]["turns"] if turn["timestamp_ms"] == 1500)
    assert scalar["s1"]["project"] == "tokdash"
    assert scalar["s1"]["display_name"] == "OpenCode title"
    assert scalar["s2"]["project"] == "other"
    assert scalar["s2"]["display_name"] == "other-slug"
    assert turn["tokens_in"] == 13
    assert turn["tokens_cache"] == 4
    assert turn["tokens_out"] == 5
    assert turn["tokens_reasoning"] == 6
    assert turn["tokens"] == 28


def test_opencode_loader_falls_back_to_raw_json_when_scalar_query_fails(monkeypatch, tmp_path):
    db_path = tmp_path / "opencode.db"
    signature = _create_opencode_session_db(db_path)

    def fail_scalar(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such function: json_extract")

    monkeypatch.setattr(sessions_module, "_load_opencode_sessions_scalar", fail_scalar)
    sessions_module._load_opencode_sessions.cache_clear()

    result = sessions_module._load_opencode_sessions(signature, (), 1000, 2000)

    assert set(result) == {"s1", "s2"}
    assert [turn["timestamp_ms"] for turn in result["s1"]["turns"]] == [1000, 1500]
    assert len(result["s2"]["turns"]) == 1


def test_get_sessions_data_passes_period_window_to_opencode_loader(monkeypatch):
    captured = {}

    def fake_opencode_sessions(*, since_ms=None, until_ms=None):
        captured["since_ms"] = since_ms
        captured["until_ms"] = until_ms
        return {
            "s1": {
                "tool": "opencode",
                "session_id": "s1",
                "project": "tokdash",
                "turns": [
                    sessions_module._build_turn(
                        turn_index=1,
                        timestamp_ms=int(since_ms or 0),
                        model="model",
                        tokens_in=1,
                        tokens_cache=0,
                        tokens_out=1,
                        tokens_reasoning=0,
                        bill=sessions_module._billing_record("model", "fresh-input"),
                    )
                ],
            }
        }

    monkeypatch.setattr(sessions_module, "_opencode_sessions", fake_opencode_sessions)

    result = sessions_module.get_sessions_data("opencode", "today")

    assert captured["since_ms"] is not None
    assert captured["until_ms"] is not None
    assert captured["since_ms"] < captured["until_ms"]
    assert result["summary"]["session_count"] == 1


def test_get_sessions_data_passes_period_window_to_mimo_loader(monkeypatch):
    captured = {}

    def fake_mimo_sessions(*, since_ms=None, until_ms=None):
        captured["since_ms"] = since_ms
        captured["until_ms"] = until_ms
        return {
            "s1": {
                "tool": "mimo",
                "session_id": "s1",
                "project": "tokdash",
                "turns": [
                    sessions_module._build_turn(
                        turn_index=1,
                        timestamp_ms=int(since_ms or 0),
                        model="model",
                        tokens_in=1,
                        tokens_cache=0,
                        tokens_out=1,
                        tokens_reasoning=0,
                        bill=sessions_module._billing_record("model", "fresh-input"),
                    )
                ],
            }
        }

    monkeypatch.setattr(sessions_module, "_mimo_sessions", fake_mimo_sessions)

    result = sessions_module.get_sessions_data("mimo", "today")

    assert captured["since_ms"] is not None
    assert captured["until_ms"] is not None
    assert captured["since_ms"] < captured["until_ms"]
    assert result["summary"]["session_count"] == 1


def test_opencode_signatures_include_wal_and_shm(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    opencode_dir = tmp_path / ".local" / "share" / "opencode"
    opencode_dir.mkdir(parents=True)
    for name in ("opencode.db", "opencode.db-wal", "opencode.db-shm"):
        (opencode_dir / name).write_text(name, encoding="utf-8")

    tracker = CodingToolsUsageTracker()
    signatures = tracker.parsers["opencode"]._file_signatures()

    assert {Path(path).name for path, _mtime, _size in signatures} == {
        "opencode.db",
        "opencode.db-wal",
        "opencode.db-shm",
    }
    assert {Path(path).name for path, _mtime, _size in sessions_module._opencode_db_signature()} == {
        "opencode.db",
        "opencode.db-wal",
        "opencode.db-shm",
    }


def test_mimo_signatures_include_wal_and_shm(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mimo_dir = tmp_path / ".local" / "share" / "mimocode"
    mimo_dir.mkdir(parents=True)
    for name in ("mimocode.db", "mimocode.db-wal", "mimocode.db-shm"):
        (mimo_dir / name).write_text(name, encoding="utf-8")

    tracker = CodingToolsUsageTracker()
    signatures = tracker.parsers["mimo"]._file_signatures()

    assert {Path(path).name for path, _mtime, _size in signatures} == {
        "mimocode.db",
        "mimocode.db-wal",
        "mimocode.db-shm",
    }
    assert {Path(path).name for path, _mtime, _size in sessions_module._mimo_db_signature()} == {
        "mimocode.db",
        "mimocode.db-wal",
        "mimocode.db-shm",
    }


def test_mimo_session_loader_uses_sql_window_and_project_worktree(tmp_path):
    db_path = tmp_path / "mimocode.db"
    signature = _create_opencode_session_db(db_path)

    sessions_module._load_mimo_sessions.cache_clear()
    result = sessions_module._load_mimo_sessions(signature, (), 1000, 2000)

    assert set(result) == {"s1", "s2"}
    assert len(result["s1"]["turns"]) == 2
    assert [turn["timestamp_ms"] for turn in result["s1"]["turns"]] == [1000, 1500]
    assert result["s1"]["project"] == "tokdash"
    assert result["s1"]["display_name"] == "OpenCode title"
    assert result["s2"]["project"] == "other"
    assert result["s2"]["display_name"] == "other-slug"


def test_mimo_session_loaders_exclude_external_import_messages(tmp_path):
    db_path = tmp_path / "mimocode.db"
    _create_opencode_session_db(db_path)
    signature = _add_mimo_external_import(db_path, ["at_since", "inside"])

    sessions_module._load_mimo_sessions.cache_clear()
    result = sessions_module._load_mimo_sessions(signature, (), 1000, 2000)
    raw = sessions_module._load_mimo_sessions_raw_json(db_path, since_ms=1000, until_ms=2000)

    assert result == raw
    assert set(result) == {"s2"}
    assert [turn["timestamp_ms"] for turn in result["s2"]["turns"]] == [1500]
    assert len(result["s2"]["turns"]) == 1


def test_mimo_parser_collect_uses_sql_window(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_dir = tmp_path / ".local" / "share" / "mimocode"
    db_dir.mkdir(parents=True)
    _create_opencode_session_db(db_dir / "mimocode.db")

    tracker = CodingToolsUsageTracker()
    parser = tracker.parsers["mimo"]

    window_entries = parser.collect(
        datetime.fromtimestamp(1, timezone.utc),
        datetime.fromtimestamp(2, timezone.utc),
    )
    all_entries = parser.collect(None, None)

    assert [entry["timestamp"] for entry in window_entries] == [1000, 1500, 1500]
    assert len(window_entries) == 3
    assert len(all_entries) == 5


def test_mimo_parser_collect_excludes_external_import_messages(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_dir = tmp_path / ".local" / "share" / "mimocode"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "mimocode.db"
    _create_opencode_session_db(db_path)
    _add_mimo_external_import(db_path, ["at_since", "inside"])

    tracker = CodingToolsUsageTracker()
    parser = tracker.parsers["mimo"]

    window_entries = parser.collect(
        datetime.fromtimestamp(1, timezone.utc),
        datetime.fromtimestamp(2, timezone.utc),
    )
    all_entries = parser.collect(None, None)

    assert [entry["entry_id"] for entry in window_entries] == ["mimo:other_inside"]
    assert len(window_entries) == 1
    assert len(all_entries) == 3


def test_query_session_records_whole_sessions_keeps_sibling_files(tmp_path):
    """A session's out-of-window files come back when whole_sessions is set.

    One session spans several rows (Kimi agents, resumed Codex/Claude logs), and
    the row that carries its name may sit outside the requested window.
    """
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    files = (
        ("/logs/main.jsonl", 1, 10),
        ("/logs/agent-0.jsonl", 1, 10),
        ("/logs/other.jsonl", 1, 10),
    )
    records = {
        "/logs/main.jsonl": {
            "tool": "kimi",
            "session_id": "s1",
            "display_name": "main prompt",
            "turns": [{"timestamp_ms": 100, "tokens": 5}],
        },
        "/logs/agent-0.jsonl": {
            "tool": "kimi",
            "session_id": "s1",
            "display_name": "subagent preamble",
            "turns": [{"timestamp_ms": 500, "tokens": 1}],
        },
        "/logs/other.jsonl": {
            "tool": "kimi",
            "session_id": "s2",
            "display_name": "unrelated",
            "turns": [{"timestamp_ms": 900, "tokens": 1}],
        },
    }
    assert store.sync_session_files(
        "kimi",
        files,
        parser={"v": 1},
        parse_file_session=lambda file_sig: records[file_sig[0]],
    )

    windowed = store.query_session_records("kimi", since_ms=400, until_ms=600)
    assert [record["display_name"] for record in windowed] == ["subagent preamble"]

    whole = store.query_session_records("kimi", since_ms=400, until_ms=600, whole_sessions=True)
    assert sorted(record["display_name"] for record in whole) == ["main prompt", "subagent preamble"]
    # Sessions that never touch the window stay out.
    assert all(record["session_id"] == "s1" for record in whole)

    unwindowed = store.query_session_records("kimi", whole_sessions=True)
    assert len(unwindowed) == 3
# ---------------------------------------------------------------------------
# cost_authoritative (schema v9) -- generic usage-store invariants
# ---------------------------------------------------------------------------


def _v8_usage_db(db_path: Path) -> None:
    """A usage DB left at schema v8: billing provenance present, no cost_authoritative."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '8')")
        conn.execute(
            """
            CREATE TABLE usage_entries (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                file_path TEXT NOT NULL DEFAULT '',
                entry_key TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                timestamp INTEGER NOT NULL,
                input INTEGER NOT NULL DEFAULT 0,
                output INTEGER NOT NULL DEFAULT 0,
                cache_read INTEGER NOT NULL DEFAULT 0,
                cache_write INTEGER NOT NULL DEFAULT 0,
                reasoning INTEGER NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 1,
                raw_json TEXT NOT NULL,
                billing_json TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.commit()


def test_usage_store_v9_adds_cost_authoritative_column(tmp_path):
    db_path = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(db_path)
    assert store.source_signature("missing") is None
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(usage_entries)").fetchall()
        }
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert "cost_authoritative" in columns
    assert usage_store_module.SCHEMA_VERSION == 9
    assert version == "9"


def test_v9_migration_backfills_fixed_billing_rows(tmp_path):
    """v8 -> v9 marks fixed-billing rows authoritative, pricing rows unpriced.

    Includes the deliberate change: a pre-v9 fixed row with cost = 0 was
    repriced by the aggregate unpriced-bucket; after migration it stays 0.0.
    """
    db_path = tmp_path / "usage.sqlite3"
    _v8_usage_db(db_path)
    rows = [
        ("fixed", "m1", 0.5, json.dumps(usage_billing_fixed(0.5))),
        ("fixed-zero", "grok-4.5", 0.0, json.dumps(usage_billing_fixed(0.0))),
        (
            "pricing",
            "m1",
            0.0,
            json.dumps(usage_billing_pricing(["m1"], input_tokens=10, output_tokens=5)),
        ),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO usage_entries (
                source, entry_key, model, timestamp, input, output, cost,
                raw_json, billing_json
            ) VALUES ('pi', ?, ?, 1700000000000, 10, 5, ?, ?, ?)
            """,
            [
                (
                    key,
                    model,
                    cost,
                    json.dumps(
                        {
                            "source": "pi",
                            "model": model,
                            "timestamp": 1700000000000,
                            "input": 10,
                            "output": 5,
                            "cost": cost,
                        }
                    ),
                    billing,
                )
                for key, model, cost, billing in rows
            ],
        )
        conn.commit()

    store = UsageEntryStore(db_path)
    # Opening the store runs the v8 -> v9 migration.
    assert store.source_signature("pi") is None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        flags = {
            row["entry_key"]: row["cost_authoritative"]
            for row in conn.execute(
                "SELECT entry_key, cost_authoritative FROM usage_entries"
            ).fetchall()
        }
    assert flags == {"fixed": 1, "fixed-zero": 1, "pricing": 0}
    assert len(store.query_entries()) == 3

    data = store.aggregate_entries(sources=["pi"])
    # The fixed-zero grok row keeps its recorded 0.0 instead of being priced
    # from its token buckets like the pre-v9 unpriced bucket would do.
    grok = next(m for m in data["apps"]["pi"]["models"] if m["name"] == "grok-4.5")
    assert grok["cost"] == 0.0
    assert grok["tokens_in"] == 10
    m1 = next(m for m in data["apps"]["pi"]["models"] if m["name"] == "m1")
    # fixed 0.5 + the pricing row's unpriced model (no recompute price)
    assert m1["cost"] == 0.5


def test_sync_paths_populate_cost_authoritative_from_billing(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    ts = 1_700_000_000_000
    entries = [
        {
            "source": "pi",
            "model": "m1",
            "timestamp": ts,
            "input": 10,
            "output": 5,
            "cost": 0.5,
            "_billing": usage_billing_fixed(0.5),
        },
        {
            "source": "pi",
            "model": "m1",
            "timestamp": ts + 1000,
            "input": 10,
            "output": 5,
            "cost": 0.0,
            "_billing": usage_billing_pricing(["m1"], input_tokens=10, output_tokens=5),
        },
        {
            "source": "pi",
            "model": "m1",
            "timestamp": ts + 2000,
            "input": 10,
            "output": 5,
            "cost": 0.25,
        },
    ]
    store.sync_source(
        "pi",
        build_source_signature(files=[["a.jsonl", 1, 1]], parser={"v": 1}),
        lambda: entries,
    )
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        flags = {
            row["timestamp"]: row["cost_authoritative"]
            for row in conn.execute(
                "SELECT timestamp, cost_authoritative FROM usage_entries WHERE source = 'pi'"
            ).fetchall()
        }
    assert flags == {ts: 1, ts + 1000: 0, ts + 2000: 0}

    # The file_replace path (INSERT OR REPLACE) populates the same column.
    store.sync_files(
        "pi2",
        [("b.jsonl", 1, 1)],
        parser={"v": 1},
        parse_file_entries=lambda file_sig: [dict(e, source="pi2") for e in entries],
    )
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        flags = {
            row["timestamp"]: row["cost_authoritative"]
            for row in conn.execute(
                "SELECT timestamp, cost_authoritative FROM usage_entries WHERE source = 'pi2'"
            ).fetchall()
        }
    assert flags == {ts: 1, ts + 1000: 0, ts + 2000: 0}


def test_sync_files_codex_upsert_updates_cost_authoritative(tmp_path):
    """DO UPDATE SET must carry cost_authoritative when a copy wins the upsert."""
    store = UsageEntryStore(tmp_path / "usage.sqlite3")

    def make_entry(ts_ms, billing, cost):
        return {
            "source": "codex",
            "entry_id": "k1",
            "model": "m",
            "timestamp": ts_ms,
            "input": 1,
            "output": 1,
            "cost": cost,
            "_billing": billing,
        }

    store.sync_files(
        "codex",
        [("a.jsonl", 1, 1)],
        parser={"v": 1},
        parse_file_entries=lambda file_sig: [
            make_entry(2_000_000_000_000, usage_billing_pricing(["m"], input_tokens=1, output_tokens=1), 0.0)
        ],
    )
    # b.jsonl's earlier-timestamp copy of k1 wins the conflict upsert.
    store.sync_files(
        "codex",
        [("a.jsonl", 1, 1), ("b.jsonl", 2, 2)],
        parser={"v": 1},
        parse_file_entries=lambda file_sig: [
            make_entry(1_999_999_999_000, usage_billing_fixed(0.1), 0.1)
        ],
    )
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT cost_authoritative, cost, file_path
            FROM usage_entries
            WHERE source = 'codex' AND entry_key = 'k1'
            """
        ).fetchone()
    assert row is not None
    assert row["cost_authoritative"] == 1
    assert row["cost"] == 0.1
    assert row["file_path"] == "b.jsonl"


def test_aggregate_paths_keep_authoritative_free_rows_out_of_recompute(tmp_path):
    """A group mixing priced, authoritative-free and unpriced rows.

    Only the unpriced (non-authoritative) zero row is repriced; the recorded
    zero stays 0.0 on both the Overview aggregate and the Stats contribution
    query.
    """
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    ts = 1_784_900_000_000

    def row(ts_ms, cost, billing, marker=False):
        entry = {
            "source": "grok",
            "model": "grok-4.5",
            "provider": "xai",
            "timestamp": ts_ms,
            "input": 1_000_000,
            "output": 0,
            "cost": cost,
            "messageCount": 1,
            "_billing": billing,
        }
        if marker:
            entry["costAuthoritative"] = True
        return entry

    rows = [
        row(ts, 2.0, usage_billing_pricing(["grok-4.5"], input_tokens=1_000_000)),
        row(ts + 1000, 0.0, usage_billing_fixed(0.0), marker=True),
        row(ts + 2000, 0.0, usage_billing_pricing(["grok-4.5"], input_tokens=1_000_000)),
    ]
    store.sync_source(
        "grok",
        build_source_signature(files=[["unified.jsonl", 1, 1]], parser={"v": 1}),
        lambda: rows,
    )

    data = store.aggregate_entries(sources=["grok"])
    # 2.0 priced + 0.0 authoritative + 2.0 recomputed.
    assert abs(data["apps"]["grok"]["cost"] - 4.0) < 1e-9
    # All three rows' tokens still count.
    assert data["apps"]["grok"]["tokens_in"] == 3_000_000

    days = store.contribution_days(
        sources=["grok"],
        since=datetime.fromtimestamp(1_784_899_999, timezone.utc),
        until=datetime.fromtimestamp(1_784_900_003, timezone.utc),
    )
    assert len(days) == 1
    assert abs(days[0]["totals"]["cost"] - 4.0) < 1e-9


def test_parse_entries_json_accepts_authoritative_zero_cost():
    from tokdash.compute import parse_entries_json

    # grok-4.5 is $2 input per 1M in the packaged pricing DB: an unmarked
    # zero-cost row recomputes to $2, an authoritative one stays at 0.0.
    entries = [
        {
            "source": "grok",
            "model": "grok-4.5",
            "provider": "xai",
            "timestamp": 1_784_900_000_000,
            "input": 1_000_000,
            "output": 0,
            "cost": 0.0,
            "costAuthoritative": True,
            "messageCount": 1,
        },
        {
            "source": "grok",
            "model": "grok-4.5",
            "provider": "xai",
            "timestamp": 1_784_900_001_000,
            "input": 1_000_000,
            "output": 0,
            "cost": 0.0,
            "messageCount": 1,
        },
    ]
    data = parse_entries_json({"entries": entries})
    assert abs(data["apps"]["grok"]["cost"] - 2.0) < 1e-9
