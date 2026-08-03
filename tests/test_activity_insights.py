from __future__ import annotations

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
