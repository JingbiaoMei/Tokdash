from __future__ import annotations

import json
import sqlite3

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import tokdash.sessions as sessions_module
from tokdash.sources.coding_tools import BaseParser, CodingToolsUsageTracker, _sig_cache
from tokdash.usage_store import UsageEntryStore, build_source_signature, parser_code_signature


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _clear_parser_caches() -> None:
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    sessions_module._parse_codex_session_file.cache_clear()
    sessions_module._load_codex_sessions.cache_clear()
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
    assert tracker.parsers["opencode"].sync_capability.session_store is False


def test_parser_code_signature_unwraps_lru_cache_functions():
    @lru_cache(maxsize=1)
    def parser_fn():
        return "ok"

    signature = parser_code_signature(parser_fn)

    assert signature["object"].endswith(".parser_fn")


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


def test_codex_stored_session_duplicate_policy_matches_live_loader():
    records = [
        {"tool": "codex", "session_id": "dup", "project": "old", "turns": [{"tokens": 10}]},
        {"tool": "codex", "session_id": "dup", "project": "new", "turns": [{"tokens": 20}]},
    ]

    result = sessions_module._session_records_to_raw_sessions("codex", records)

    assert result["dup"]["project"] == "new"
    assert result["dup"]["turns"] == [{"tokens": 20}]


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

    expected = records[0]
    expected = sessions_module._merge_raw_session(expected, records[1])
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
                        cost=0.0,
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
                        cost=0.0,
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
