from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .dateutil import parse_date_range
from .pricing import PricingDatabase


SESSION_TOOLS = ("codex", "claude", "opencode")
TOOL_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "opencode": "OpenCode",
}

_PRICING_DB = PricingDatabase()


def reload_pricing_db() -> None:
    """Reload session pricing and clear parsed session caches."""
    _PRICING_DB.load()
    _parse_codex_session_file.cache_clear()
    _load_codex_sessions.cache_clear()
    _parse_claude_session_file.cache_clear()
    _load_claude_sessions.cache_clear()
    _load_opencode_sessions.cache_clear()


def _period_to_days(period: str) -> int:
    try:
        return max(1, int(period))
    except ValueError:
        mapping = {
            "today": 1,
            "3days": 3,
            "week": 7,
            "14days": 14,
            "month": 30,
            "90": 90,
            "365": 365,
        }
        return mapping.get(period, 1)


def _period_range(period: str) -> tuple[int, int]:
    """Return [since_ms, until_ms) in local time."""
    now_local = datetime.now().astimezone()
    local_tz = now_local.tzinfo or timezone.utc

    if period == "month":
        since = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        until = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return _dt_to_ms(since.astimezone(timezone.utc)), _dt_to_ms(until.astimezone(timezone.utc))

    days = _period_to_days(period)
    if days == 1:
        since = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        until = since + timedelta(days=1)
        return _dt_to_ms(since.astimezone(timezone.utc)), _dt_to_ms(until.astimezone(timezone.utc))

    end_date = now_local.date()
    start_date = end_date - timedelta(days=days - 1)
    since = datetime.combine(start_date, datetime.min.time(), tzinfo=local_tz).astimezone(timezone.utc)
    until = datetime.combine(end_date, datetime.min.time(), tzinfo=local_tz).astimezone(timezone.utc) + timedelta(days=1)
    return _dt_to_ms(since), _dt_to_ms(until)


def _dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _parse_iso_to_ms(value: Any) -> Optional[int]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return _dt_to_ms(dt.astimezone(timezone.utc))


def _project_from_repo_or_path(repo_url: Optional[str], path: Optional[str]) -> str:
    if repo_url:
        name = repo_url.rstrip("/").split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return name
    if path:
        name = Path(path).name
        if name:
            return name
    return "unknown"


def _build_turn(
    turn_index: int,
    timestamp_ms: int,
    model: str,
    tokens_in: int,
    tokens_cache: int,
    tokens_out: int,
    tokens_reasoning: int,
    cost: float,
) -> Dict[str, Any]:
    total_tokens = tokens_in + tokens_cache + tokens_out + tokens_reasoning
    return {
        "turn_index": turn_index,
        "timestamp_ms": int(timestamp_ms),
        "model": model or "unknown",
        "tokens_in": int(tokens_in),
        "tokens_cache": int(tokens_cache),
        "tokens_out": int(tokens_out),
        "tokens_reasoning": int(tokens_reasoning),
        "tokens": int(total_tokens),
        "cost": float(cost or 0.0),
    }


def _summarize_session(
    raw: Dict[str, Any],
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    turns = []
    for turn in raw.get("turns", []):
        ts_ms = int(turn.get("timestamp_ms", 0) or 0)
        if since_ms is not None and ts_ms < since_ms:
            continue
        if until_ms is not None and ts_ms >= until_ms:
            continue
        turns.append(turn)

    if not turns:
        return None

    turns.sort(key=lambda item: int(item.get("timestamp_ms", 0) or 0))
    tokens_in = sum(int(turn.get("tokens_in", 0) or 0) for turn in turns)
    tokens_cache = sum(int(turn.get("tokens_cache", 0) or 0) for turn in turns)
    tokens_out = sum(int(turn.get("tokens_out", 0) or 0) for turn in turns)
    tokens_reasoning = sum(int(turn.get("tokens_reasoning", 0) or 0) for turn in turns)
    total_tokens = sum(int(turn.get("tokens", 0) or 0) for turn in turns)
    total_cost = sum(float(turn.get("cost", 0.0) or 0.0) for turn in turns)

    per_model_tokens: Dict[str, int] = {}
    for turn in turns:
        model = str(turn.get("model") or "unknown")
        per_model_tokens[model] = per_model_tokens.get(model, 0) + int(turn.get("tokens", 0) or 0)
    top_model = max(per_model_tokens.items(), key=lambda item: item[1])[0] if per_model_tokens else "unknown"

    started_at_ms = int(turns[0].get("timestamp_ms", 0) or 0)
    last_seen_at_ms = int(turns[-1].get("timestamp_ms", 0) or 0)

    return {
        "tool": raw.get("tool", "unknown"),
        "session_id": raw.get("session_id", "unknown"),
        "project": raw.get("project", "unknown"),
        "model": top_model,
        "token_events": len(turns),
        "tokens_in": tokens_in,
        "tokens_cache": tokens_cache,
        "tokens_out": tokens_out,
        "tokens_reasoning": tokens_reasoning,
        "tokens": total_tokens,
        "cache_ratio": (tokens_cache / total_tokens) if total_tokens > 0 else 0.0,
        "cost": total_cost,
        "started_at": _ms_to_iso(started_at_ms),
        "last_seen_at": _ms_to_iso(last_seen_at_ms),
    }


def _public_turns(turns: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    result = []
    for turn in turns:
        row = dict(turn)
        row["timestamp"] = _ms_to_iso(int(row.pop("timestamp_ms", 0) or 0))
        result.append(row)
    return result


def _merge_raw_session(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    merged = {
        "tool": existing.get("tool") or new.get("tool") or "unknown",
        "session_id": existing.get("session_id") or new.get("session_id") or "unknown",
        "project": existing.get("project") if existing.get("project") != "unknown" else new.get("project", "unknown"),
        "turns": [],
    }

    seen = set()
    merged_turns = []
    for turn in list(existing.get("turns", [])) + list(new.get("turns", [])):
        key = (
            int(turn.get("timestamp_ms", 0) or 0),
            str(turn.get("model") or "unknown"),
            int(turn.get("tokens_in", 0) or 0),
            int(turn.get("tokens_cache", 0) or 0),
            int(turn.get("tokens_out", 0) or 0),
            int(turn.get("tokens_reasoning", 0) or 0),
            round(float(turn.get("cost", 0.0) or 0.0), 8),
        )
        if key in seen:
            continue
        seen.add(key)
        merged_turns.append(dict(turn))

    merged_turns.sort(key=lambda item: (int(item.get("timestamp_ms", 0) or 0), int(item.get("turn_index", 0) or 0)))
    for index, turn in enumerate(merged_turns, start=1):
        turn["turn_index"] = index

    merged["turns"] = merged_turns
    return merged


def _file_signature(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return str(path), int(stat.st_mtime_ns), int(stat.st_size)


def _iter_file_signatures(root: Path) -> tuple[tuple[str, int, int], ...]:
    if not root.exists():
        return ()
    items = []
    for path in root.rglob("*.jsonl"):
        try:
            items.append(_file_signature(path))
        except FileNotFoundError:
            continue
    items.sort(key=lambda item: item[0])
    return tuple(items)


@lru_cache(maxsize=512)
def _parse_codex_session_file(path_str: str, _mtime_ns: int, _size: int) -> Optional[Dict[str, Any]]:
    session_path = Path(path_str)
    if not session_path.exists():
        return None

    session_id = session_path.stem
    current_model = "gpt-5.3-codex"
    current_provider = "openai"
    cwd = ""
    repo_url = ""
    turns = []
    turn_index = 0

    with session_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except Exception:
                continue

            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            obj_type = obj.get("type")

            if obj_type == "session_meta":
                session_id = str(payload.get("id") or session_id)
                cwd = str(payload.get("cwd") or cwd)
                repo_url = str(((payload.get("git") or {}).get("repository_url")) or repo_url)
                if payload.get("model_provider"):
                    current_provider = str(payload.get("model_provider"))
                continue

            if obj_type == "turn_context":
                current_model = str(payload.get("model") or current_model)
                cwd = str(payload.get("cwd") or cwd)
                continue

            if obj_type != "event_msg" or payload.get("type") != "token_count":
                continue

            timestamp_ms = _parse_iso_to_ms(obj.get("timestamp"))
            if timestamp_ms is None:
                continue

            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
            if not usage:
                continue

            input_total = int(usage.get("input_tokens", 0) or 0)
            cache_read = int(usage.get("cached_input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            reasoning_tokens = int(usage.get("reasoning_output_tokens", 0) or 0)
            input_tokens = max(0, input_total - cache_read)
            total_tokens = input_tokens + cache_read + output_tokens + reasoning_tokens
            if total_tokens == 0:
                continue

            full_model_name = f"{current_provider}/{current_model}" if current_provider else current_model
            turn_index += 1
            turns.append(
                _build_turn(
                    turn_index=turn_index,
                    timestamp_ms=timestamp_ms,
                    model=current_model,
                    tokens_in=input_tokens,
                    tokens_cache=cache_read,
                    tokens_out=output_tokens,
                    tokens_reasoning=reasoning_tokens,
                    cost=_PRICING_DB.get_cost(full_model_name, input_tokens, output_tokens, cache_read, 0),
                )
            )

    if not turns:
        return None

    return {
        "tool": "codex",
        "session_id": session_id,
        "project": _project_from_repo_or_path(repo_url or None, cwd or None),
        "turns": turns,
    }


@lru_cache(maxsize=8)
def _load_codex_sessions(signature: tuple[tuple[str, int, int], ...]) -> Dict[str, Dict[str, Any]]:
    sessions: Dict[str, Dict[str, Any]] = {}
    for path_str, mtime_ns, size in signature:
        raw = _parse_codex_session_file(path_str, mtime_ns, size)
        if raw:
            sessions[str(raw["session_id"])] = raw
    return sessions


def _codex_sessions() -> Dict[str, Dict[str, Any]]:
    root = Path.home() / ".codex" / "sessions"
    return _load_codex_sessions(_iter_file_signatures(root))


@lru_cache(maxsize=512)
def _parse_claude_session_file(path_str: str, _mtime_ns: int, _size: int) -> Optional[Dict[str, Any]]:
    session_path = Path(path_str)
    if not session_path.exists():
        return None

    session_id = session_path.stem
    project = "unknown"
    turns = []
    seen_message_ids = set()
    turn_index = 0

    with session_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except Exception:
                continue

            session_id = str(obj.get("sessionId") or session_id)
            if project == "unknown" and obj.get("cwd"):
                project = _project_from_repo_or_path(None, str(obj.get("cwd")))

            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if message.get("role") != "assistant":
                continue

            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            if not usage:
                continue

            message_id = str(message.get("id") or obj.get("uuid") or "")
            if message_id and message_id in seen_message_ids:
                continue
            if message_id:
                seen_message_ids.add(message_id)

            timestamp_ms = _parse_iso_to_ms(obj.get("timestamp"))
            if timestamp_ms is None:
                continue

            model = str(message.get("model") or "unknown")
            fresh_input = int(usage.get("input_tokens", usage.get("input", 0)) or 0)
            cache_read = int(usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0)) or 0)
            cache_write = int(usage.get("cache_creation_input_tokens", usage.get("cache_write_tokens", 0)) or 0)
            input_tokens = fresh_input + cache_write
            output_tokens = int(usage.get("output_tokens", usage.get("output", 0)) or 0)
            total_tokens = input_tokens + cache_read + output_tokens
            if total_tokens == 0:
                continue

            turn_index += 1
            turns.append(
                _build_turn(
                    turn_index=turn_index,
                    timestamp_ms=timestamp_ms,
                    model=model,
                    tokens_in=input_tokens,
                    tokens_cache=cache_read,
                    tokens_out=output_tokens,
                    tokens_reasoning=0,
                    cost=_PRICING_DB.get_cost(model, input_tokens, output_tokens, cache_read, 0),
                )
            )

    if not turns:
        return None

    return {
        "tool": "claude",
        "session_id": session_id,
        "project": project,
        "turns": turns,
    }


@lru_cache(maxsize=8)
def _load_claude_sessions(signature: tuple[tuple[str, int, int], ...]) -> Dict[str, Dict[str, Any]]:
    sessions: Dict[str, Dict[str, Any]] = {}
    for path_str, mtime_ns, size in signature:
        raw = _parse_claude_session_file(path_str, mtime_ns, size)
        if raw:
            session_id = str(raw["session_id"])
            if session_id in sessions:
                sessions[session_id] = _merge_raw_session(sessions[session_id], raw)
            else:
                sessions[session_id] = raw
    return sessions


def _claude_sessions() -> Dict[str, Dict[str, Any]]:
    root = Path.home() / ".claude" / "projects"
    return _load_claude_sessions(_iter_file_signatures(root))


def _opencode_db_signature() -> tuple[str, int, int] | None:
    db_path = Path.home() / ".local/share/opencode/opencode.db"
    if not db_path.exists():
        return None
    return _file_signature(db_path)


@lru_cache(maxsize=8)
def _load_opencode_sessions(path_str: str, _mtime_ns: int, _size: int) -> Dict[str, Dict[str, Any]]:
    db_path = Path(path_str)
    if not db_path.exists():
        return {}

    sessions: Dict[str, Dict[str, Any]] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
              s.id,
              s.directory,
              COALESCE(p.worktree, ''),
              m.time_created,
              m.data
            FROM message m
            JOIN session s ON m.session_id = s.id
            LEFT JOIN project p ON s.project_id = p.id
            ORDER BY m.time_created ASC
            """
        )
        turn_index_by_session: Dict[str, int] = {}
        for session_id, directory, worktree, created_ms, data_json in cur.fetchall():
            try:
                data = json.loads(data_json)
            except Exception:
                continue

            if data.get("role") != "assistant":
                continue

            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                continue

            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            fresh_input = int(tokens.get("input", 0) or 0)
            cache_write = int(cache.get("write", 0) or 0)
            cache_read = int(cache.get("read", 0) or 0)
            output_tokens = int(tokens.get("output", 0) or 0)
            reasoning_tokens = int(tokens.get("reasoning", 0) or 0)
            input_tokens = fresh_input + cache_write
            total_tokens = input_tokens + cache_read + output_tokens + reasoning_tokens
            if total_tokens == 0:
                continue

            model = str(data.get("modelID") or "unknown")
            provider = str(data.get("providerID") or "")
            full_model_name = f"{provider}/{model}" if provider else model

            project_path = str(worktree or "")
            if not project_path or project_path == "/":
                project_path = str(directory or "")
            if not project_path or project_path == "/":
                path_info = data.get("path") if isinstance(data.get("path"), dict) else {}
                project_path = str(path_info.get("cwd") or path_info.get("root") or "")

            raw = sessions.setdefault(
                str(session_id),
                {
                    "tool": "opencode",
                    "session_id": str(session_id),
                    "project": _project_from_repo_or_path(None, project_path or None),
                    "turns": [],
                },
            )
            if raw.get("project") == "unknown":
                raw["project"] = _project_from_repo_or_path(None, project_path or None)

            turn_index = turn_index_by_session.get(str(session_id), 0) + 1
            turn_index_by_session[str(session_id)] = turn_index

            raw["turns"].append(
                _build_turn(
                    turn_index=turn_index,
                    timestamp_ms=int(created_ms or 0),
                    model=model,
                    tokens_in=input_tokens,
                    tokens_cache=cache_read,
                    tokens_out=output_tokens,
                    tokens_reasoning=reasoning_tokens,
                    cost=_PRICING_DB.get_cost(full_model_name, input_tokens, output_tokens, cache_read, 0),
                )
            )
    finally:
        conn.close()

    return sessions


def _opencode_sessions() -> Dict[str, Dict[str, Any]]:
    signature = _opencode_db_signature()
    if signature is None:
        return {}
    return _load_opencode_sessions(*signature)


def _raw_sessions_for_tool(tool: str) -> Dict[str, Dict[str, Any]]:
    key = str(tool or "").strip().lower()
    if key == "codex":
        return _codex_sessions()
    if key == "claude":
        return _claude_sessions()
    if key == "opencode":
        return _opencode_sessions()
    raise ValueError(f"Unsupported session tool: {tool}")


def get_sessions_data(tool: str, period: str, date_from: Optional[str] = None, date_to: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
    key = str(tool or "").strip().lower()
    if key not in SESSION_TOOLS:
        raise ValueError(f"Unsupported session tool: {tool}")

    # If specific dates are provided, use them instead of period
    if date_from and date_to:
        since_dt, until_dt = parse_date_range(date_from, date_to)
        since_ms = int(since_dt.timestamp() * 1000)
        until_ms = int(until_dt.timestamp() * 1000)
    else:
        since_ms, until_ms = _period_range(period)

    sessions = []
    for raw in _raw_sessions_for_tool(key).values():
        summary = _summarize_session(raw, since_ms=since_ms, until_ms=until_ms)
        if summary:
            sessions.append(summary)

    sessions.sort(key=lambda row: (row.get("last_seen_at") or "", row.get("tokens") or 0), reverse=True)
    latest_session = sessions[0] if sessions else None
    visible_sessions = sessions if limit is None else sessions[: max(0, int(limit))]

    return {
        "tool": key,
        "tool_label": TOOL_LABELS.get(key, key.title()),
        "period": period,
        "latest_session": latest_session,
        "sessions": visible_sessions,
        "summary": {
            "session_count": len(sessions),
            "tokens": sum(int(row.get("tokens", 0) or 0) for row in sessions),
            "cost": sum(float(row.get("cost", 0.0) or 0.0) for row in sessions),
        },
        "timestamp": datetime.now().isoformat(),
    }


def get_session_detail(tool: str, session_id: str) -> Dict[str, Any]:
    key = str(tool or "").strip().lower()
    if key not in SESSION_TOOLS:
        raise ValueError(f"Unsupported session tool: {tool}")

    raw = _raw_sessions_for_tool(key).get(str(session_id))
    if not raw:
        raise FileNotFoundError(f"{TOOL_LABELS.get(key, key.title())} session not found: {session_id}")

    session = _summarize_session(raw)
    if session is None:
        raise FileNotFoundError(f"{TOOL_LABELS.get(key, key.title())} session not found: {session_id}")

    return {
        "session": session,
        "turns": _public_turns(raw.get("turns", [])),
        "timestamp": datetime.now().isoformat(),
    }


def get_codex_sessions_data(period: str, limit: Optional[int] = None) -> Dict[str, Any]:
    return get_sessions_data("codex", period, limit=limit)


def get_codex_session_detail(session_id: str) -> Dict[str, Any]:
    return get_session_detail("codex", session_id)
