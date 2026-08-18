from __future__ import annotations

import json

from tokdash import sessions as sessions_module
from tokdash.activity_insights import (
    build_activity_insights,
    canonical_mcp_tool_name,
    new_activity_record,
    record_reasoning_turn,
    record_structured_tool_call,
)


def _wrapped(session_id, activity, *, missing=False, file_path=None):
    return {
        "session_id": session_id,
        "file_path": file_path or f"/{session_id}.jsonl",
        "missing": missing,
        "activity": activity,
    }


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _parse(path):
    stat = path.stat()
    return sessions_module._parse_codex_session_file(
        str(path), stat.st_mtime_ns, stat.st_size, ()
    )


def test_activity_insights_merge_resumes_and_resolve_specificity():
    first = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_reasoning_turn(first, turn_id="turn-1", effort="xhigh")
    record_structured_tool_call(
        first, call_id="call-1", name="mcp_tool_call", specificity="top_level"
    )

    resumed = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_reasoning_turn(resumed, turn_id="turn-1", effort="xhigh")
    record_structured_tool_call(
        resumed, call_id="call-1", name="browser/click", specificity="mcp"
    )

    result = build_activity_insights(
        [
            _wrapped("chat-1", first, file_path="/first.jsonl"),
            _wrapped("chat-1", resumed, file_path="/resumed.jsonl"),
        ]
    )

    assert result["recorded_chats"]["value"] == 1
    assert result["reasoning"]["distribution"] == [
        {"effort": "xhigh", "count": 1, "share": 1.0}
    ]
    assert result["tools"]["total_calls"] == 1
    assert result["tools"]["distribution"] == [
        {"name": "browser/click", "count": 1, "share": 1.0}
    ]
    public_payload = json.dumps(result)
    assert "turn-1" not in public_payload
    assert "call-1" not in public_payload


def test_activity_insights_exclude_subagents_and_ambiguous_values():
    primary = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_reasoning_turn(primary, turn_id="turn-1", effort="high")
    record_reasoning_turn(primary, turn_id="turn-1", effort="xhigh")
    record_structured_tool_call(
        primary, call_id="call-1", name="exec", specificity="top_level"
    )
    record_structured_tool_call(
        primary, call_id="call-1", name="apply_patch", specificity="top_level"
    )

    subagent = new_activity_record(is_primary=False, has_explicit_session_id=True)
    record_reasoning_turn(subagent, turn_id="sub-turn", effort="high")
    record_structured_tool_call(
        subagent, call_id="sub-call", name="exec", specificity="top_level"
    )

    result = build_activity_insights(
        [
            _wrapped("chat-1", primary),
            _wrapped("subagent-1", subagent),
        ]
    )

    assert result["recorded_chats"]["value"] == 1
    assert result["reasoning"]["distribution"] == []
    assert result["reasoning"]["coverage"]["ambiguous_turns"] == 1
    assert result["tools"]["total_calls"] == 1
    assert result["tools"]["distribution"] == []
    assert result["tools"]["coverage"]["ambiguous_name_calls"] == 1


def test_activity_insights_sort_ties_and_report_missing_coverage():
    activity = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_reasoning_turn(activity, turn_id=None, effort="high")
    record_reasoning_turn(activity, turn_id="turn-2", effort=None)
    record_structured_tool_call(
        activity, call_id=None, name="exec", specificity="top_level"
    )
    for call_id, name in (("b", "zeta"), ("a", "alpha")):
        record_structured_tool_call(
            activity, call_id=call_id, name=name, specificity="top_level"
        )

    result = build_activity_insights(
        [
            _wrapped("chat-1", activity),
            _wrapped("legacy", None, missing=True),
        ]
    )

    assert [row["name"] for row in result["tools"]["distribution"]] == [
        "alpha",
        "zeta",
    ]
    assert result["reasoning"]["coverage"]["excluded_records"] == 2
    assert result["tools"]["coverage"]["excluded_records"] == 1
    assert result["recorded_chats"]["coverage"] == {
        "primary_files": 1,
        "files_with_session_id": 1,
        "legacy_unavailable_records": 1,
    }
    assert "turn-2" not in str(result)
    assert "call-1" not in str(result)


def test_activity_insights_merge_cross_file_conflicts_and_keep_higher_specificity():
    first = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_reasoning_turn(first, turn_id="turn-1", effort="high")
    record_structured_tool_call(
        first, call_id="call-1", name="exec", specificity="top_level"
    )

    second = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_reasoning_turn(second, turn_id="turn-1", effort="xhigh")
    record_structured_tool_call(
        second, call_id="call-1", name="browser/click", specificity="mcp"
    )

    third = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_structured_tool_call(
        third, call_id="call-1", name="browser/open", specificity="mcp"
    )

    result = build_activity_insights(
        [
            _wrapped("chat-1", first, file_path="/1.jsonl"),
            _wrapped("chat-1", second, file_path="/2.jsonl"),
            _wrapped("chat-1", third, file_path="/3.jsonl"),
        ]
    )

    assert result["reasoning"]["most_used"] is None
    assert result["reasoning"]["coverage"]["identified_turns"] == 1
    assert result["reasoning"]["coverage"]["ambiguous_turns"] == 1
    assert result["tools"]["total_calls"] == 1
    assert result["tools"]["most_used"] is None
    assert result["tools"]["coverage"]["ambiguous_name_calls"] == 1


def test_tool_record_prefers_available_name_and_canonicalizes_mcp():
    activity = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_structured_tool_call(
        activity, call_id="call-1", name=None, specificity="mcp"
    )
    record_structured_tool_call(
        activity, call_id="call-1", name="exec", specificity="top_level"
    )

    result = build_activity_insights([_wrapped("chat-1", activity)])

    assert result["tools"]["distribution"] == [
        {"name": "exec", "count": 1, "share": 1.0}
    ]
    assert canonical_mcp_tool_name({"server": "browser", "tool": "click"}) == "browser/click"
    assert canonical_mcp_tool_name({"tool": "click"}) == "click"
    assert canonical_mcp_tool_name({"server": "browser"}) is None


def test_activity_fields_reject_container_values_without_retaining_content(tmp_path):
    sentinel = "SHOULD_NOT_SURVIVE"
    path = tmp_path / "privacy.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "session_meta",
                "payload": {"id": {"credential": sentinel}},
            },
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": {"credential": sentinel},
                    "effort": "high",
                },
            },
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "effort": {"credential": sentinel},
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": {"credential": sentinel},
                    "name": "exec",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": {"credential": sentinel},
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "mcp_tool_call_end",
                    "call_id": "call-2",
                    "invocation": {
                        "server": {"credential": sentinel},
                        "tool": "click",
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "mcp_tool_call_end",
                    "call_id": "call-3",
                    "invocation": {
                        "server": "browser",
                        "tool": [sentinel],
                    },
                },
            },
        ],
    )

    raw = _parse(path)
    result = build_activity_insights([_wrapped(raw["session_id"], raw["_activity"])])

    assert raw["session_id"] == "privacy"
    assert raw["_activity"]["has_explicit_session_id"] is False
    assert raw["_activity"]["turn_records_missing_id"] == 1
    assert raw["_activity"]["turn_records_missing_effort"] == 1
    assert raw["_activity"]["tool_records_missing_id"] == 1
    assert raw["_activity"]["tool_by_call_id"]["call-1"]["name"] is None
    assert raw["_activity"]["tool_by_call_id"]["call-2"]["name"] == "click"
    assert raw["_activity"]["tool_by_call_id"]["call-3"]["name"] is None
    assert result["recorded_chats"]["value"] == 0
    assert result["reasoning"]["distribution"] == []
    assert result["tools"]["total_calls"] == 3
    assert sentinel not in json.dumps(raw["_activity"])
    assert sentinel not in json.dumps(result)


def test_activity_merge_rejects_invalid_cached_specificity_without_stringifying_it():
    sentinel = "SHOULD_NOT_SURVIVE"
    activity = new_activity_record(is_primary=True, has_explicit_session_id=True)
    activity["tool_by_call_id"]["call-1"] = {
        "name": "exec",
        "specificity": {"credential": sentinel},
        "ambiguous": False,
    }

    result = build_activity_insights([_wrapped("chat-1", activity)])

    assert result["tools"]["distribution"] == [
        {"name": "exec", "count": 1, "share": 1.0}
    ]
    assert sentinel not in json.dumps(result)


def test_activity_insights_ignore_unknown_schema_and_missing_session_identity():
    unsupported = new_activity_record(is_primary=True, has_explicit_session_id=True)
    unsupported["version"] = 999
    anonymous = new_activity_record(is_primary=True, has_explicit_session_id=False)

    result = build_activity_insights(
        [
            _wrapped("unsupported", unsupported),
            _wrapped("", anonymous),
        ]
    )

    assert result["recorded_chats"]["value"] == 0
    assert result["recorded_chats"]["coverage"] == {
        "primary_files": 1,
        "files_with_session_id": 0,
        "legacy_unavailable_records": 0,
    }


def test_codex_parser_collects_activity_without_private_payload_content(tmp_path):
    path = tmp_path / "root.jsonl"
    _write_jsonl(
        path,
        [
            {"type": "session_meta", "payload": {"id": "chat-1"}},
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "effort": "xhigh",
                    "model": "gpt-5.3-codex",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "exec",
                    "arguments": "SECRET-ARGUMENT",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "mcp_tool_call_end",
                    "call_id": "call-1",
                    "invocation": {"server": "browser", "tool": "click"},
                    "result": "SECRET-RESULT",
                },
            },
        ],
    )

    raw = _parse(path)

    assert raw["session_id"] == "chat-1"
    assert raw["turns"] == []
    assert raw["_activity"]["is_primary"] is True
    assert raw["_activity"]["reasoning_by_turn_id"]["turn-1"] == "xhigh"
    assert raw["_activity"]["tool_by_call_id"]["call-1"]["name"] == "browser/click"
    assert "SECRET" not in json.dumps(raw["_activity"])


def test_codex_parser_marks_empty_subagent_from_first_session_meta(tmp_path):
    path = tmp_path / "subagent.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "sub-1",
                    "source": {
                        "subagent": {
                            "thread_spawn": {"parent_thread_id": "root-1"}
                        }
                    },
                },
            },
            {
                "type": "turn_context",
                "payload": {"turn_id": "turn-1", "effort": "high"},
            },
        ],
    )

    assert _parse(path) is None


def test_codex_parser_excludes_guardian_activity_from_primary_metrics(tmp_path):
    path = tmp_path / "guardian.jsonl"
    _write_jsonl(
        path,
        [
            {
                "timestamp": "2026-08-03T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "guardian-1",
                    "source": {"subagent": {"other": "guardian"}},
                },
            },
            {
                "timestamp": "2026-08-03T10:00:01Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "effort": "low",
                    "model": "codex-auto-review",
                },
            },
            {
                "timestamp": "2026-08-03T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "exec_command",
                },
            },
            {
                "timestamp": "2026-08-03T10:00:03Z",
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
            },
        ],
    )

    raw = _parse(path)
    result = build_activity_insights([_wrapped("guardian-1", raw["_activity"])])

    assert raw["is_review_session"] is True
    assert raw["_activity"]["is_primary"] is False
    assert result["recorded_chats"]["value"] == 0
    assert result["reasoning"]["distribution"] == []
    assert result["tools"]["total_calls"] == 0


def test_empty_primary_activity_record_stays_out_of_session_loaders(tmp_path):
    raw = {
        "tool": "codex",
        "session_id": "empty",
        "project": "unknown",
        "turns": [],
        "_activity": new_activity_record(
            is_primary=True, has_explicit_session_id=True
        ),
    }

    assert sessions_module._session_records_to_raw_sessions("codex", [raw]) == {}

    path = tmp_path / "empty.jsonl"
    _write_jsonl(path, [{"type": "session_meta", "payload": {"id": "empty"}}])
    stat = path.stat()
    assert sessions_module._load_codex_sessions(
        ((str(path), stat.st_mtime_ns, stat.st_size),), ()
    ) == {}


def test_codex_parser_deduplicates_attempts_and_reports_missing_ids(tmp_path):
    path = tmp_path / "calls.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "chat-1"}}]
    rows.extend(
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "exec",
                    "status": status,
                },
            }
            for status in ("in_progress", "completed")
        ]
    )
    rows.extend(
        [
            {
                "type": "event_msg",
                "payload": {
                    "type": "mcp_tool_call_end",
                    "call_id": "call-1",
                    "invocation": {"server": "browser", "tool": "click"},
                    "status": "failed",
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "tool_search_call", "id": "call-2"},
            },
            {
                "type": "response_item",
                "payload": {"type": "web_search_call", "call_id": "call-3"},
            },
            {
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "id": "call-4"},
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call", "name": "missing-id"},
            },
            {
                "type": "turn_context",
                "payload": {"effort": "high"},
            },
            {
                "type": "turn_context",
                "payload": {"turn_id": "turn-without-effort"},
            },
        ]
    )
    _write_jsonl(path, rows)

    raw = _parse(path)
    result = build_activity_insights([_wrapped("chat-1", raw["_activity"])])

    assert result["tools"]["total_calls"] == 4
    assert result["tools"]["coverage"] == {
        "named_calls": 3,
        "ambiguous_name_calls": 1,
        "excluded_records": 1,
    }
    assert [row["name"] for row in result["tools"]["distribution"]] == [
        "browser/click",
        "tool_search",
        "web_search",
    ]
    assert result["reasoning"]["coverage"]["excluded_records"] == 2


def _clear_codex_activity_caches():
    sessions_module._parse_codex_session_file.cache_clear()
    sessions_module._load_codex_sessions.cache_clear()
    loader = getattr(sessions_module, "_load_codex_activity_records", None)
    if loader is not None:
        loader.cache_clear()


def _install_counting_codex_parser(monkeypatch):
    original = sessions_module._parse_codex_session_file
    calls = {"count": 0}

    def counting_parser(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        sessions_module, "_parse_codex_session_file", counting_parser
    )
    return calls


def test_codex_activity_warm_persistent_load_does_not_reparse(
    monkeypatch, tmp_path
):
    session_root = tmp_path / "sessions"
    _write_jsonl(
        session_root / "chat.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "chat-1"}},
            {
                "type": "turn_context",
                "payload": {"turn_id": "turn-1", "effort": "high"},
            },
        ],
    )
    db_path = tmp_path / "tokdash" / "usage.sqlite3"
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db_path))
    monkeypatch.setattr(
        sessions_module.clientpaths, "codex_sessions_dir", lambda: session_root
    )
    monkeypatch.setattr(
        sessions_module.clientpaths, "codex_archived_sessions_dir",
        lambda: tmp_path / "archived_sessions",
    )
    _clear_codex_activity_caches()
    calls = _install_counting_codex_parser(monkeypatch)

    first = sessions_module.get_codex_activity_insights()
    first_count = calls["count"]
    second = sessions_module.get_codex_activity_insights()

    assert first["recorded_chats"]["value"] == 1
    assert second["reasoning"]["most_used"]["effort"] == "high"
    assert first_count == 1
    assert calls["count"] == first_count
    assert db_path.exists()


def test_codex_activity_warm_store_disabled_load_does_not_reparse(
    monkeypatch, tmp_path
):
    session_root = tmp_path / "sessions"
    _write_jsonl(
        session_root / "chat.jsonl",
        [{"type": "session_meta", "payload": {"id": "chat-1"}}],
    )
    db_path = tmp_path / "tokdash" / "usage.sqlite3"
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db_path))
    monkeypatch.setattr(
        sessions_module.clientpaths, "codex_sessions_dir", lambda: session_root
    )
    monkeypatch.setattr(
        sessions_module.clientpaths, "codex_archived_sessions_dir",
        lambda: tmp_path / "archived_sessions",
    )
    _clear_codex_activity_caches()
    calls = _install_counting_codex_parser(monkeypatch)

    first = sessions_module.get_codex_activity_insights()
    first_count = calls["count"]
    second = sessions_module.get_codex_activity_insights()

    assert first["recorded_chats"]["value"] == 1
    assert second["recorded_chats"]["value"] == 1
    assert first_count == 1
    assert calls["count"] == first_count
    assert not db_path.exists()
