from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

ACTIVITY_SCHEMA_VERSION = 1
_SPECIFICITY_RANK = {"top_level": 1, "mcp": 2}


def _nonempty(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _increment(record: dict[str, Any], key: str) -> None:
    record[key] = int(record.get(key, 0) or 0) + 1


def new_activity_record(*, is_primary: bool, has_explicit_session_id: bool) -> dict[str, Any]:
    return {
        "version": ACTIVITY_SCHEMA_VERSION,
        "is_primary": bool(is_primary),
        "has_explicit_session_id": bool(has_explicit_session_id),
        "reasoning_by_turn_id": {},
        "tool_by_call_id": {},
        "turn_records_missing_id": 0,
        "turn_records_missing_effort": 0,
        "tool_records_missing_id": 0,
    }


def record_reasoning_turn(record: dict[str, Any], *, turn_id: Any, effort: Any) -> None:
    stable_id = _nonempty(turn_id)
    normalized_effort = _nonempty(effort)
    if stable_id is None:
        _increment(record, "turn_records_missing_id")
        return
    if normalized_effort is None:
        _increment(record, "turn_records_missing_effort")
        return

    turns = record.setdefault("reasoning_by_turn_id", {})
    if not isinstance(turns, dict):
        turns = {}
        record["reasoning_by_turn_id"] = turns
    existing = turns.get(stable_id)
    if existing is None:
        turns[stable_id] = normalized_effort
    elif isinstance(existing, str) and existing != normalized_effort:
        turns[stable_id] = {"effort": None, "ambiguous": True}


def record_structured_tool_call(
    record: dict[str, Any], *, call_id: Any, name: Any, specificity: str
) -> None:
    stable_id = _nonempty(call_id)
    canonical_name = _nonempty(name)
    normalized_specificity = specificity if specificity in _SPECIFICITY_RANK else "top_level"
    rank = _SPECIFICITY_RANK[normalized_specificity]
    if stable_id is None:
        _increment(record, "tool_records_missing_id")
        return

    calls = record.setdefault("tool_by_call_id", {})
    if not isinstance(calls, dict):
        calls = {}
        record["tool_by_call_id"] = calls
    incoming = {
        "name": canonical_name,
        "specificity": normalized_specificity,
        "ambiguous": False,
    }
    existing = calls.get(stable_id)
    if not isinstance(existing, Mapping):
        calls[stable_id] = incoming
        return

    existing_rank = _SPECIFICITY_RANK.get(str(existing.get("specificity")), 0)
    if bool(existing.get("ambiguous")) and rank <= existing_rank:
        return
    if canonical_name is None:
        return
    if _nonempty(existing.get("name")) is None or rank > existing_rank:
        calls[stable_id] = incoming
        return
    if rank == existing_rank and _nonempty(existing.get("name")) != canonical_name:
        calls[stable_id] = {
            "name": None,
            "specificity": normalized_specificity,
            "ambiguous": True,
        }


def canonical_mcp_tool_name(invocation: Any) -> str | None:
    if not isinstance(invocation, Mapping):
        return None
    server = _nonempty(invocation.get("server"))
    tool = _nonempty(invocation.get("tool"))
    return f"{server}/{tool}" if server and tool else tool


def _new_merged_session() -> dict[str, Any]:
    return {
        "has_explicit_session_id": False,
        "reasoning_by_turn_id": {},
        "tool_by_call_id": {},
        "turn_records_missing_id": 0,
        "turn_records_missing_effort": 0,
        "tool_records_missing_id": 0,
    }


def _merge_reasoning_entry(target: dict[str, Any], turn_id: Any, value: Any) -> None:
    stable_id = _nonempty(turn_id)
    if stable_id is None:
        return
    turns = target["reasoning_by_turn_id"]
    if isinstance(value, Mapping) and bool(value.get("ambiguous")):
        turns[stable_id] = {"effort": None, "ambiguous": True}
        return
    effort = value.get("effort") if isinstance(value, Mapping) else value
    record_reasoning_turn(target, turn_id=stable_id, effort=effort)


def _merge_tool_entry(target: dict[str, Any], call_id: Any, value: Any) -> None:
    stable_id = _nonempty(call_id)
    if stable_id is None or not isinstance(value, Mapping):
        return
    specificity = str(value.get("specificity") or "top_level")
    calls = target["tool_by_call_id"]
    if bool(value.get("ambiguous")):
        incoming_rank = _SPECIFICITY_RANK.get(specificity, 0)
        existing = calls.get(stable_id)
        existing_rank = (
            _SPECIFICITY_RANK.get(str(existing.get("specificity")), 0)
            if isinstance(existing, Mapping)
            else 0
        )
        if incoming_rank >= existing_rank:
            calls[stable_id] = {
                "name": None,
                "specificity": specificity,
                "ambiguous": True,
            }
        return
    record_structured_tool_call(
        target,
        call_id=stable_id,
        name=value.get("name"),
        specificity=specificity,
    )


def _distribution(counter: Counter[str], value_key: str) -> list[dict[str, Any]]:
    denominator = sum(counter.values())
    if denominator <= 0:
        return []
    return [
        {
            value_key: value,
            "count": count,
            "share": round(count / denominator, 6),
        }
        for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def build_activity_insights(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    merged_sessions: dict[str, dict[str, Any]] = {}
    primary_files = 0
    files_with_session_id = 0
    legacy_unavailable_records = 0

    for row in records:
        activity = row.get("activity")
        if activity is None:
            if bool(row.get("missing")):
                legacy_unavailable_records += 1
            continue
        if not isinstance(activity, Mapping):
            continue
        if int(activity.get("version", 0) or 0) != ACTIVITY_SCHEMA_VERSION:
            continue
        if not bool(activity.get("is_primary")):
            continue

        primary_files += 1
        has_explicit_session_id = bool(activity.get("has_explicit_session_id"))
        if has_explicit_session_id:
            files_with_session_id += 1
        session_id = _nonempty(row.get("session_id"))
        if session_id is None:
            continue

        merged = merged_sessions.setdefault(session_id, _new_merged_session())
        merged["has_explicit_session_id"] = bool(
            merged["has_explicit_session_id"] or has_explicit_session_id
        )
        for counter_key in (
            "turn_records_missing_id",
            "turn_records_missing_effort",
            "tool_records_missing_id",
        ):
            merged[counter_key] += int(activity.get(counter_key, 0) or 0)

        turns = activity.get("reasoning_by_turn_id")
        if isinstance(turns, Mapping):
            for turn_id, value in turns.items():
                _merge_reasoning_entry(merged, turn_id, value)
        calls = activity.get("tool_by_call_id")
        if isinstance(calls, Mapping):
            for call_id, value in calls.items():
                _merge_tool_entry(merged, call_id, value)

    effort_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    recorded_chats = 0
    stable_turns = 0
    ambiguous_turns = 0
    reasoning_excluded_records = 0
    total_calls = 0
    named_calls = 0
    ambiguous_name_calls = 0
    tool_excluded_records = 0

    for merged in merged_sessions.values():
        if bool(merged["has_explicit_session_id"]):
            recorded_chats += 1

        turns = merged["reasoning_by_turn_id"]
        stable_turns += len(turns)
        reasoning_excluded_records += int(merged["turn_records_missing_id"]) + int(
            merged["turn_records_missing_effort"]
        )
        for value in turns.values():
            if isinstance(value, Mapping) and bool(value.get("ambiguous")):
                ambiguous_turns += 1
                continue
            effort = _nonempty(value.get("effort") if isinstance(value, Mapping) else value)
            if effort is not None:
                effort_counts[effort] += 1

        calls = merged["tool_by_call_id"]
        total_calls += len(calls)
        tool_excluded_records += int(merged["tool_records_missing_id"])
        for value in calls.values():
            if not isinstance(value, Mapping):
                ambiguous_name_calls += 1
                continue
            name = _nonempty(value.get("name"))
            if bool(value.get("ambiguous")) or name is None:
                ambiguous_name_calls += 1
                continue
            named_calls += 1
            tool_counts[name] += 1

    reasoning_distribution = _distribution(effort_counts, "effort")
    tool_distribution = _distribution(tool_counts, "name")
    known_effort_turns = sum(effort_counts.values())

    return {
        "scope": {"tool": "codex", "local": True, "primary_only": True},
        "recorded_chats": {
            "value": recorded_chats,
            "coverage": {
                "primary_files": primary_files,
                "files_with_session_id": files_with_session_id,
                "legacy_unavailable_records": legacy_unavailable_records,
            },
        },
        "reasoning": {
            "most_used": dict(reasoning_distribution[0]) if reasoning_distribution else None,
            "distribution": reasoning_distribution,
            "coverage": {
                "identified_turns": stable_turns + reasoning_excluded_records,
                "known_effort_turns": known_effort_turns,
                "ambiguous_turns": ambiguous_turns,
                "excluded_records": reasoning_excluded_records,
            },
        },
        "tools": {
            "total_calls": total_calls,
            "most_used": dict(tool_distribution[0]) if tool_distribution else None,
            "distribution": tool_distribution,
            "coverage": {
                "named_calls": named_calls,
                "ambiguous_name_calls": ambiguous_name_calls,
                "excluded_records": tool_excluded_records,
            },
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
