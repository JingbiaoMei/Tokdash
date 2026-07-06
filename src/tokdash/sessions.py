from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from . import clientpaths
from .compute import cache_hit_rate, period_to_days
from .dateutil import parse_date_range
from .pricing import PricingDatabase
from .usage_store import UsageEntryStore, parser_code_signature, persistent_usage_db_enabled


SESSION_TOOLS = ("codex", "claude", "opencode", "pi_agent", "mimo")
TOOL_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "opencode": "OpenCode",
    "pi_agent": "Pi",
    "mimo": "Mimo",
}

_PRICING_DB = PricingDatabase()
DISPLAY_NAME_MAX_CHARS = 96

# Signature of the pricing files the singleton was last loaded from. Sessions cost is computed
# via the long-lived _PRICING_DB singleton, whose in-memory rates are refreshed only by
# reload_pricing_db() (the dashboard PUT). If the data-dir override changes by ANY other path
# (a manual edit while serving, or a sibling/--workers process that handled the PUT), the
# read path must reload the singleton so costs match the cache key — _pricing_signature() does
# that when this drifts. Initialized to the signature loaded at import.
try:
    _pricing_last_loaded_sig: tuple = _PRICING_DB.signature()
except (OSError, AttributeError):
    _pricing_last_loaded_sig = ()


def reload_pricing_db() -> None:
    """Reload session pricing and clear parsed session caches."""
    global _pricing_last_loaded_sig
    _PRICING_DB.load()
    try:
        _pricing_last_loaded_sig = _PRICING_DB.signature()
    except (OSError, AttributeError):
        _pricing_last_loaded_sig = ()
    _parse_codex_session_file.cache_clear()
    _load_codex_sessions.cache_clear()
    _load_codex_title_map.cache_clear()
    _parse_claude_session_file.cache_clear()
    _load_claude_sessions.cache_clear()
    _load_opencode_sessions.cache_clear()
    _parse_pi_session_file.cache_clear()
    _load_pi_sessions.cache_clear()
    _load_mimo_sessions.cache_clear()


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _short_session_id(session_id: Any) -> str:
    raw = str(session_id or "").strip()
    return raw[:8] if raw else "unknown"


def _clean_display_name(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("text", "content", "value", "name", "title"):
            cleaned = _clean_display_name(value.get(key))
            if cleaned:
                return cleaned
        return ""
    if isinstance(value, list):
        parts = [_clean_display_name(item) for item in value]
        text = " ".join(part for part in parts if part)
    else:
        text = str(value)
    text = " ".join(text.split())
    if not text:
        return ""
    return text[: DISPLAY_NAME_MAX_CHARS - 1].rstrip() + "…" if len(text) > DISPLAY_NAME_MAX_CHARS else text


def _fallback_display_name(session_id: Any, project: Any = "") -> str:
    project_name = _clean_display_name(project)
    if project_name and project_name != "unknown":
        return project_name
    return _short_session_id(session_id)


def _is_codex_guardian_session(meta_payload: Dict[str, Any]) -> bool:
    source = meta_payload.get("source")
    if not isinstance(source, dict):
        return False
    subagent = source.get("subagent")
    return isinstance(subagent, dict) and subagent.get("other") == "guardian"


def _include_codex_review_sessions(include_review_sessions: Optional[bool]) -> bool:
    if include_review_sessions is not None:
        return bool(include_review_sessions)
    return _truthy_env("TOKDASH_INCLUDE_CODEX_GUARDIAN")


def _message_text_preview(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return _clean_display_name(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
                text = _clean_display_name(item.get("text") or item.get("content"))
                if text:
                    parts.append(text)
            else:
                text = _clean_display_name(item)
                if text:
                    parts.append(text)
        return _clean_display_name(parts)
    return _clean_display_name(content)


def _period_to_days(period: str) -> int:
    """Delegate to the canonical mapping in ``compute`` so that ``/api/sessions``
    and ``/api/usage`` agree on what named periods mean. Previously this had its
    own copy that mapped ``year``/``all``/unknown to 1 (today), so e.g.
    ``/api/sessions?period=all`` silently behaved like today while
    ``/api/usage?period=all`` spanned all-time."""
    return period_to_days(period)


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


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


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
        "cache_hit_rate": cache_hit_rate(tokens_in, tokens_cache),
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
        "display_name": raw.get("display_name")
        or _fallback_display_name(raw.get("session_id", "unknown"), raw.get("project", "unknown")),
        "project": raw.get("project", "unknown"),
        "is_review_session": bool(raw.get("is_review_session", False)),
        "model": top_model,
        "token_events": len(turns),
        "tokens_in": tokens_in,
        "tokens_cache": tokens_cache,
        "tokens_out": tokens_out,
        "tokens_reasoning": tokens_reasoning,
        "tokens": total_tokens,
        # cache_ratio = cacheRead / ALL tokens (incl. output) — a cache SHARE, kept for
        # back-compat. cache_hit_rate is the faithful prompt hit rate: cacheRead over
        # prompt input only (tokens_in already folds cacheWrite into billable input).
        "cache_ratio": (tokens_cache / total_tokens) if total_tokens > 0 else 0.0,
        "cache_hit_rate": cache_hit_rate(tokens_in, tokens_cache),
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
        "display_name": existing.get("display_name") or new.get("display_name") or "",
        "is_review_session": bool(existing.get("is_review_session") or new.get("is_review_session")),
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
    if not merged["display_name"]:
        merged["display_name"] = _fallback_display_name(merged["session_id"], merged["project"])
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


def _pricing_signature() -> tuple:
    # Cover baseline AND the data-dir override so session caches bust when either changes
    # (a dashboard pricing edit writes only the override). Also reload the singleton's
    # in-memory rates here when the signature drifts, so a cache MISS re-parses with the
    # CURRENT pricing even when the change didn't come through reload_pricing_db() (manual
    # edit / sibling process) — otherwise the new cache entry would be filled with stale costs.
    global _pricing_last_loaded_sig
    try:
        sig = _PRICING_DB.signature()
    except (OSError, AttributeError):
        return ()
    if sig != _pricing_last_loaded_sig:
        try:
            _PRICING_DB.load()
            _pricing_last_loaded_sig = sig
        except Exception:
            pass
    return sig


def _codex_state_db_path() -> Path:
    return clientpaths.codex_state_db_path()


def _codex_state_signature() -> tuple:
    db_path = _codex_state_db_path()
    parts: list[tuple[str, int, int]] = []
    for path in (db_path, Path(str(db_path) + "-wal")):
        try:
            stat = path.stat()
        except (FileNotFoundError, OSError):
            continue
        parts.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(parts)


@lru_cache(maxsize=8)
def _load_codex_title_map(_state_sig: tuple = ()) -> Dict[str, str]:
    db_path = _codex_state_db_path()
    if not db_path.exists():
        return {}
    titles: Dict[str, str] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.05)
    except sqlite3.Error:
        return {}
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 50")
        cols = _sqlite_columns(conn, "threads")
        if "id" not in cols:
            return {}
        preferred = [name for name in ("title", "preview", "first_user_message") if name in cols]
        if not preferred:
            return {}
        select_cols = ", ".join(["id", *preferred])
        where_clause = " OR ".join(f"COALESCE({name}, '') <> ''" for name in preferred)
        for row in conn.execute(f"SELECT {select_cols} FROM threads WHERE {where_clause}"):
            session_id = str(row[0] or "")
            if not session_id:
                continue
            for value in row[1:]:
                title = _clean_display_name(value)
                if title:
                    titles[session_id] = title
                    break
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return titles


def _apply_codex_title_map(sessions: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    titles = _load_codex_title_map(_codex_state_signature())
    copied = {session_id: dict(session) for session_id, session in sessions.items()}
    for session_id, session in sessions.items():
        title = titles.get(str(session_id))
        if title:
            copied[session_id]["display_name"] = title
    return copied


@lru_cache(maxsize=512)
def _parse_codex_session_file(path_str: str, _mtime_ns: int, _size: int, _pricing_sig: tuple = ()) -> Optional[Dict[str, Any]]:
    session_path = Path(path_str)
    if not session_path.exists():
        return None

    session_id = session_path.stem
    current_model = "gpt-5.3-codex"
    current_provider = "openai"
    cwd = ""
    repo_url = ""
    thread_name = ""
    is_review_session = False
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
                is_review_session = is_review_session or _is_codex_guardian_session(payload)
                if payload.get("model_provider"):
                    current_provider = str(payload.get("model_provider"))
                continue

            if obj_type == "turn_context":
                current_model = str(payload.get("model") or current_model)
                cwd = str(payload.get("cwd") or cwd)
                continue

            payload_type = payload.get("type")
            if payload_type == "thread_name_updated":
                thread_name = _clean_display_name(payload.get("thread_name")) or thread_name
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

    project = _project_from_repo_or_path(repo_url or None, cwd or None)
    return {
        "tool": "codex",
        "session_id": session_id,
        "display_name": thread_name or _fallback_display_name(session_id, project),
        "project": project,
        "is_review_session": is_review_session,
        "turns": turns,
    }


@lru_cache(maxsize=8)
def _load_codex_sessions(signature: tuple[tuple[str, int, int], ...], pricing_sig: tuple = ()) -> Dict[str, Dict[str, Any]]:
    sessions: Dict[str, Dict[str, Any]] = {}
    for path_str, mtime_ns, size in signature:
        raw = _parse_codex_session_file(path_str, mtime_ns, size, pricing_sig)
        if raw:
            sessions[str(raw["session_id"])] = raw
    return sessions


def _codex_sessions() -> Dict[str, Dict[str, Any]]:
    root = clientpaths.codex_sessions_dir()
    return _apply_codex_title_map(_load_codex_sessions(_iter_file_signatures(root), _pricing_signature()))


@lru_cache(maxsize=512)
def _parse_claude_session_file(path_str: str, _mtime_ns: int, _size: int, _pricing_sig: tuple = ()) -> Optional[Dict[str, Any]]:
    session_path = Path(path_str)
    if not session_path.exists():
        return None

    session_id = session_path.stem
    project = "unknown"
    custom_title = ""
    ai_title = ""
    agent_name = ""
    turns = []
    seen_message_ids = set()
    snapshot_turns_by_message_id: Dict[str, Dict[str, Any]] = {}

    with session_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except Exception:
                continue

            obj_type = obj.get("type")
            session_id = str(obj.get("sessionId") or session_id)
            if project == "unknown" and obj.get("cwd"):
                project = _project_from_repo_or_path(None, str(obj.get("cwd")))

            if obj_type == "custom-title":
                custom_title = _clean_display_name(obj.get("customTitle")) or custom_title
                continue
            if obj_type == "ai-title":
                ai_title = _clean_display_name(obj.get("aiTitle")) or ai_title
                continue
            if obj_type == "agent-name":
                agent_name = _clean_display_name(obj.get("agentName")) or agent_name
                continue

            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            role = message.get("role")
            is_top_level_assistant = role is None and obj_type == "assistant"
            if role != "assistant" and not is_top_level_assistant:
                continue

            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            if not usage:
                continue

            message_id = str(message.get("id") or obj.get("uuid") or "")
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

            # Legacy role-bearing logs repeat the same message id; skip the
            # duplicates before pricing the turn.
            if message_id and not is_top_level_assistant and message_id in seen_message_ids:
                continue

            turn = _build_turn(
                turn_index=0,
                timestamp_ms=timestamp_ms,
                model=model,
                tokens_in=input_tokens,
                tokens_cache=cache_read,
                tokens_out=output_tokens,
                tokens_reasoning=0,
                cost=_PRICING_DB.get_cost(model, input_tokens, output_tokens, cache_read, 0),
            )
            if not message_id:
                turns.append(turn)
                continue

            if is_top_level_assistant:
                # Newer Claude Code builds log assistant turns as role-less
                # streaming snapshots sharing one id; keep the latest snapshot.
                existing = snapshot_turns_by_message_id.get(message_id)
                if existing is None or timestamp_ms >= int(existing.get("timestamp_ms", 0) or 0):
                    snapshot_turns_by_message_id[message_id] = turn
                continue

            # First non-zero occurrence of this legacy id.
            seen_message_ids.add(message_id)
            turns.append(turn)

    turns.extend(
        turn
        for message_id, turn in snapshot_turns_by_message_id.items()
        if message_id not in seen_message_ids
    )
    turns.sort(key=lambda item: int(item.get("timestamp_ms", 0) or 0))
    for turn_index, turn in enumerate(turns, start=1):
        turn["turn_index"] = turn_index

    if not turns:
        return None

    return {
        "tool": "claude",
        "session_id": session_id,
        "display_name": custom_title or ai_title or agent_name or _fallback_display_name(session_id, project),
        "project": project,
        "turns": turns,
    }


@lru_cache(maxsize=8)
def _load_claude_sessions(signature: tuple[tuple[str, int, int], ...], pricing_sig: tuple = ()) -> Dict[str, Dict[str, Any]]:
    sessions: Dict[str, Dict[str, Any]] = {}
    for path_str, mtime_ns, size in signature:
        raw = _parse_claude_session_file(path_str, mtime_ns, size, pricing_sig)
        if raw:
            session_id = str(raw["session_id"])
            if session_id in sessions:
                sessions[session_id] = _merge_raw_session(sessions[session_id], raw)
            else:
                sessions[session_id] = raw
    return sessions


def _claude_sessions() -> Dict[str, Dict[str, Any]]:
    all_sigs: list[tuple[str, int, int]] = []
    for projects_dir in clientpaths.claude_project_dirs():
        all_sigs.extend(_iter_file_signatures(projects_dir))
    all_sigs.sort(key=lambda item: item[0])
    return _load_claude_sessions(tuple(all_sigs), _pricing_signature())


def _opencode_db_signature() -> tuple[tuple[str, int, int], ...]:
    db_path = clientpaths.opencode_db_path()
    if not db_path.exists():
        return ()
    signatures: list[tuple[str, int, int]] = []
    for candidate in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        try:
            signatures.append(_file_signature(candidate))
        except FileNotFoundError:
            continue
    return tuple(signatures)


@lru_cache(maxsize=8)
def _load_opencode_sessions(
    signature: tuple[tuple[str, int, int], ...],
    _pricing_sig: tuple = (),
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    if not signature:
        return {}
    db_path = Path(signature[0][0])
    if not db_path.exists():
        return {}

    try:
        return _load_opencode_sessions_scalar(db_path, since_ms=since_ms, until_ms=until_ms)
    except sqlite3.Error:
        return _load_opencode_sessions_raw_json(db_path, since_ms=since_ms, until_ms=until_ms)


def _opencode_window_clause(since_ms: Optional[int], until_ms: Optional[int]) -> tuple[str, list[int]]:
    where: list[str] = []
    args: list[int] = []
    if since_ms is not None:
        where.append("m.time_created >= ?")
        args.append(int(since_ms))
    if until_ms is not None:
        where.append("m.time_created < ?")
        args.append(int(until_ms))
    return (" WHERE " + " AND ".join(where)) if where else "", args


def _opencode_project_path(directory: Any, worktree: Any, cwd: Any = "", root: Any = "") -> str:
    project_path = str(worktree or "")
    if not project_path or project_path == "/":
        project_path = str(directory or "")
    if not project_path or project_path == "/":
        project_path = str(cwd or root or "")
    return project_path


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        return {str(row[1]) for row in cur.fetchall()}
    except sqlite3.Error:
        return set()


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False


def _mimo_import_exclusion_clause(conn: sqlite3.Connection) -> str:
    clauses: list[str] = []
    for table in ("external_import", "claude_import"):
        if not _sqlite_table_exists(conn, table):
            continue
        clauses.append(
            f"""
            m.id NOT IN (
                SELECT value
                FROM {table}, json_each({table}.message_ids)
                WHERE {table}.message_ids IS NOT NULL
            )
            """
        )
    return " AND ".join(clauses)


def _append_opencode_turn(
    sessions: Dict[str, Dict[str, Any]],
    turn_index_by_session: Dict[str, int],
    *,
    tool: str = "opencode",
    session_id: Any,
    directory: Any,
    worktree: Any,
    created_ms: Any,
    model: Any,
    provider: Any,
    fresh_input: Any,
    cache_write: Any,
    cache_read: Any,
    output_tokens: Any,
    reasoning_tokens: Any,
    cwd: Any = "",
    root: Any = "",
    title: Any = "",
    slug: Any = "",
    recorded_cost: Any = None,
) -> None:
    fresh_input = int(fresh_input or 0)
    cache_write = int(cache_write or 0)
    cache_read = int(cache_read or 0)
    output_tokens = int(output_tokens or 0)
    reasoning_tokens = int(reasoning_tokens or 0)
    input_tokens = fresh_input + cache_write
    total_tokens = input_tokens + cache_read + output_tokens + reasoning_tokens
    if total_tokens == 0:
        return

    model = str(model or "unknown")
    provider = str(provider or "")
    full_model_name = f"{provider}/{model}" if provider else model
    try:
        data_cost = float(recorded_cost or 0.0)
    except (TypeError, ValueError):
        data_cost = 0.0
    cost = data_cost if data_cost > 0 else _PRICING_DB.get_cost(full_model_name, input_tokens, output_tokens, cache_read, 0)
    project_path = _opencode_project_path(directory, worktree, cwd, root)
    sid = str(session_id)
    project = _project_from_repo_or_path(None, project_path or None)
    display_name = _clean_display_name(title) or _clean_display_name(slug) or _fallback_display_name(sid, project)

    raw = sessions.setdefault(
        sid,
        {
            "tool": tool,
            "session_id": sid,
            "display_name": display_name,
            "project": project,
            "turns": [],
        },
    )
    if raw.get("project") == "unknown":
        raw["project"] = _project_from_repo_or_path(None, project_path or None)
    if not raw.get("display_name"):
        raw["display_name"] = display_name

    turn_index = turn_index_by_session.get(sid, 0) + 1
    turn_index_by_session[sid] = turn_index
    raw["turns"].append(
        _build_turn(
            turn_index=turn_index,
            timestamp_ms=int(created_ms or 0),
            model=model,
            tokens_in=input_tokens,
            tokens_cache=cache_read,
            tokens_out=output_tokens,
            tokens_reasoning=reasoning_tokens,
            cost=cost,
        )
    )


def _load_opencode_sessions_scalar(
    db_path: Path,
    *,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    window_clause, args = _opencode_window_clause(since_ms, until_ms)
    role_clause = "json_valid(m.data) AND json_extract(m.data, '$.role') = 'assistant'"
    if window_clause:
        where_clause = f"{window_clause} AND {role_clause}"
    else:
        where_clause = f" WHERE {role_clause}"

    sessions: Dict[str, Dict[str, Any]] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        session_cols = _sqlite_columns(conn, "session")
        title_expr = "s.title" if "title" in session_cols else "''"
        slug_expr = "s.slug" if "slug" in session_cols else "''"
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
              s.id,
              s.directory,
              {title_expr},
              {slug_expr},
              COALESCE(p.worktree, ''),
              m.time_created,
              json_extract(m.data, '$.tokens.input'),
              json_extract(m.data, '$.tokens.cache.write'),
              json_extract(m.data, '$.tokens.cache.read'),
              json_extract(m.data, '$.tokens.output'),
              json_extract(m.data, '$.tokens.reasoning'),
              json_extract(m.data, '$.modelID'),
              json_extract(m.data, '$.providerID'),
              json_extract(m.data, '$.path.cwd'),
              json_extract(m.data, '$.path.root')
            FROM message m
            JOIN session s ON m.session_id = s.id
            LEFT JOIN project p ON s.project_id = p.id
            {where_clause}
            ORDER BY m.time_created ASC
            """,
            args,
        )
        turn_index_by_session: Dict[str, int] = {}
        for (
            session_id,
            directory,
            title,
            slug,
            worktree,
            created_ms,
            fresh_input,
            cache_write,
            cache_read,
            output_tokens,
            reasoning_tokens,
            model,
            provider,
            cwd,
            root,
        ) in cur.fetchall():
            _append_opencode_turn(
                sessions,
                turn_index_by_session,
                session_id=session_id,
                directory=directory,
                worktree=worktree,
                created_ms=created_ms,
                model=model,
                provider=provider,
                fresh_input=fresh_input,
                cache_write=cache_write,
                cache_read=cache_read,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                cwd=cwd,
                root=root,
                title=title,
                slug=slug,
            )
    finally:
        conn.close()

    return sessions


def _load_opencode_sessions_raw_json(
    db_path: Path,
    *,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    window_clause, args = _opencode_window_clause(since_ms, until_ms)

    sessions: Dict[str, Dict[str, Any]] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        session_cols = _sqlite_columns(conn, "session")
        title_expr = "s.title" if "title" in session_cols else "''"
        slug_expr = "s.slug" if "slug" in session_cols else "''"
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
              s.id,
              s.directory,
              {title_expr},
              {slug_expr},
              COALESCE(p.worktree, ''),
              m.time_created,
              m.data
            FROM message m
            JOIN session s ON m.session_id = s.id
            LEFT JOIN project p ON s.project_id = p.id
            {window_clause}
            ORDER BY m.time_created ASC
            """,
            args,
        )
        turn_index_by_session: Dict[str, int] = {}
        for session_id, directory, title, slug, worktree, created_ms, data_json in cur.fetchall():
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
            path_info = data.get("path") if isinstance(data.get("path"), dict) else {}
            _append_opencode_turn(
                sessions,
                turn_index_by_session,
                session_id=session_id,
                directory=directory,
                worktree=worktree,
                created_ms=created_ms,
                model=data.get("modelID"),
                provider=data.get("providerID"),
                fresh_input=tokens.get("input", 0),
                cache_write=cache.get("write", 0),
                cache_read=cache.get("read", 0),
                output_tokens=tokens.get("output", 0),
                reasoning_tokens=tokens.get("reasoning", 0),
                cwd=path_info.get("cwd"),
                root=path_info.get("root"),
                title=title,
                slug=slug,
            )
    finally:
        conn.close()

    return sessions


def _opencode_sessions(since_ms: Optional[int] = None, until_ms: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    signature = _opencode_db_signature()
    if not signature:
        return {}
    return _load_opencode_sessions(signature, _pricing_signature(), since_ms, until_ms)


def _pi_session_roots() -> list[Path]:
    return clientpaths.pi_agent_search_dirs()


def _pi_session_signatures() -> tuple[tuple[str, int, int], ...]:
    signatures: list[tuple[str, int, int]] = []
    for root in _pi_session_roots():
        if root.is_file() and root.suffix == ".jsonl":
            try:
                signatures.append(_file_signature(root))
            except FileNotFoundError:
                continue
        else:
            signatures.extend(_iter_file_signatures(root))
    signatures.sort(key=lambda item: item[0])
    return tuple(signatures)


def _pi_session_id_from_path(path: Path) -> str:
    stem = path.stem
    if "_" in stem:
        tail = stem.rsplit("_", 1)[-1]
        if tail:
            return tail
    return stem


@lru_cache(maxsize=512)
def _parse_pi_session_file(path_str: str, _mtime_ns: int, _size: int, _pricing_sig: tuple = ()) -> Optional[Dict[str, Any]]:
    session_path = Path(path_str)
    if not session_path.exists():
        return None

    session_id = _pi_session_id_from_path(session_path)
    cwd = ""
    session_name = ""
    first_user_preview = ""
    current_model = ""
    current_provider = ""
    turns: list[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    turn_index = 0

    with session_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            obj_type = obj.get("type")
            if obj_type == "session":
                session_id = str(obj.get("id") or session_id)
                cwd = str(obj.get("cwd") or cwd)
                continue
            if obj_type == "session_info":
                session_name = _clean_display_name(obj.get("name")) or session_name
                cwd = str(obj.get("cwd") or cwd)
                continue
            if obj_type == "model_change":
                current_provider = str(obj.get("provider") or current_provider)
                current_model = str(obj.get("modelId") or current_model)
                continue
            if obj_type != "message":
                continue

            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if message.get("role") == "user" and not first_user_preview:
                first_user_preview = _message_text_preview(message)
                continue
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            if not usage:
                continue

            entry_id = str(obj.get("id") or "")
            if entry_id and entry_id in seen_ids:
                continue
            if entry_id:
                seen_ids.add(entry_id)

            timestamp_ms = _parse_iso_to_ms(obj.get("timestamp"))
            if timestamp_ms is None:
                continue

            model = str(message.get("model") or current_model or "unknown")
            provider = str(message.get("provider") or current_provider or "")
            fresh_input = _to_int(usage.get("input"))
            output_tokens = _to_int(usage.get("output"))
            cache_read = _to_int(usage.get("cacheRead"))
            cache_write = _to_int(usage.get("cacheWrite"))
            total_tokens = _to_int(usage.get("totalTokens"))
            if fresh_input == 0 and output_tokens == 0 and cache_read == 0 and cache_write == 0 and total_tokens > 0:
                output_tokens = total_tokens
            if fresh_input == 0 and output_tokens == 0 and cache_read == 0 and cache_write == 0:
                continue

            cost_obj = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
            try:
                cost_total = float(cost_obj.get("total") or 0.0)
            except Exception:
                cost_total = 0.0
            full_model_name = f"{provider}/{model}" if provider else model
            cost = (
                cost_total
                if cost_total > 0
                else _PRICING_DB.get_cost(full_model_name, fresh_input, output_tokens, cache_read, cache_write)
            )
            turn_index += 1
            turns.append(
                _build_turn(
                    turn_index=turn_index,
                    timestamp_ms=timestamp_ms,
                    model=model,
                    tokens_in=fresh_input + cache_write,
                    tokens_cache=cache_read,
                    tokens_out=output_tokens,
                    tokens_reasoning=0,
                    cost=cost,
                )
            )

    if not turns:
        return None

    project = _project_from_repo_or_path(None, cwd or None)
    return {
        "tool": "pi_agent",
        "session_id": session_id,
        "display_name": session_name or first_user_preview or _fallback_display_name(session_id, project),
        "project": project,
        "turns": turns,
    }


@lru_cache(maxsize=8)
def _load_pi_sessions(signature: tuple[tuple[str, int, int], ...], pricing_sig: tuple = ()) -> Dict[str, Dict[str, Any]]:
    sessions: Dict[str, Dict[str, Any]] = {}
    for path_str, mtime_ns, size in signature:
        raw = _parse_pi_session_file(path_str, mtime_ns, size, pricing_sig)
        if not raw:
            continue
        session_id = str(raw["session_id"])
        if session_id in sessions:
            sessions[session_id] = _merge_raw_session(sessions[session_id], raw)
        else:
            sessions[session_id] = raw
    return sessions


def _pi_sessions() -> Dict[str, Dict[str, Any]]:
    return _load_pi_sessions(_pi_session_signatures(), _pricing_signature())


def _mimo_db_signature() -> tuple[tuple[str, int, int], ...]:
    db_path = clientpaths.mimocode_db_path()
    if not db_path.exists():
        return ()
    signatures: list[tuple[str, int, int]] = []
    for candidate in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        try:
            signatures.append(_file_signature(candidate))
        except FileNotFoundError:
            continue
    return tuple(signatures)


@lru_cache(maxsize=8)
def _load_mimo_sessions(
    signature: tuple[tuple[str, int, int], ...],
    _pricing_sig: tuple = (),
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    if not signature:
        return {}
    db_path = Path(signature[0][0])
    if not db_path.exists():
        return {}

    try:
        return _load_mimo_sessions_scalar(db_path, since_ms=since_ms, until_ms=until_ms)
    except sqlite3.Error:
        return _load_mimo_sessions_raw_json(db_path, since_ms=since_ms, until_ms=until_ms)


def _load_mimo_sessions_scalar(
    db_path: Path,
    *,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    window_clause, args = _opencode_window_clause(since_ms, until_ms)

    sessions: Dict[str, Dict[str, Any]] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        role_clause = "json_valid(m.data) AND json_extract(m.data, '$.role') = 'assistant'"
        import_clause = _mimo_import_exclusion_clause(conn)
        if window_clause:
            where_clause = f"{window_clause} AND {role_clause}"
        else:
            where_clause = f" WHERE {role_clause}"
        if import_clause:
            where_clause = f"{where_clause} AND {import_clause}"

        session_cols = _sqlite_columns(conn, "session")
        title_expr = "s.title" if "title" in session_cols else "''"
        slug_expr = "s.slug" if "slug" in session_cols else "''"
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
              s.id,
              s.directory,
              {title_expr},
              {slug_expr},
              COALESCE(p.worktree, ''),
              m.time_created,
              json_extract(m.data, '$.tokens.input'),
              json_extract(m.data, '$.tokens.cache.write'),
              json_extract(m.data, '$.tokens.cache.read'),
              json_extract(m.data, '$.tokens.output'),
              json_extract(m.data, '$.tokens.reasoning'),
              json_extract(m.data, '$.modelID'),
              json_extract(m.data, '$.providerID'),
              json_extract(m.data, '$.path.cwd'),
              json_extract(m.data, '$.path.root'),
              json_extract(m.data, '$.cost')
            FROM message m
            JOIN session s ON m.session_id = s.id
            LEFT JOIN project p ON s.project_id = p.id
            {where_clause}
            ORDER BY m.time_created ASC
            """,
            args,
        )
        turn_index_by_session: Dict[str, int] = {}
        for (
            session_id,
            directory,
            title,
            slug,
            worktree,
            created_ms,
            fresh_input,
            cache_write,
            cache_read,
            output_tokens,
            reasoning_tokens,
            model,
            provider,
            cwd,
            root,
            recorded_cost,
        ) in cur.fetchall():
            _append_opencode_turn(
                sessions,
                turn_index_by_session,
                tool="mimo",
                session_id=session_id,
                directory=directory,
                worktree=worktree,
                created_ms=created_ms,
                model=model,
                provider=provider,
                fresh_input=fresh_input,
                cache_write=cache_write,
                cache_read=cache_read,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                cwd=cwd,
                root=root,
                title=title,
                slug=slug,
                recorded_cost=recorded_cost,
            )
    finally:
        conn.close()

    return sessions


def _load_mimo_sessions_raw_json(
    db_path: Path,
    *,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    window_clause, args = _opencode_window_clause(since_ms, until_ms)

    sessions: Dict[str, Dict[str, Any]] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        import_clause = _mimo_import_exclusion_clause(conn)
        if window_clause and import_clause:
            where_clause = f"{window_clause} AND {import_clause}"
        elif import_clause:
            where_clause = f" WHERE {import_clause}"
        else:
            where_clause = window_clause

        session_cols = _sqlite_columns(conn, "session")
        title_expr = "s.title" if "title" in session_cols else "''"
        slug_expr = "s.slug" if "slug" in session_cols else "''"
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
              s.id,
              s.directory,
              {title_expr},
              {slug_expr},
              COALESCE(p.worktree, ''),
              m.time_created,
              m.data
            FROM message m
            JOIN session s ON m.session_id = s.id
            LEFT JOIN project p ON s.project_id = p.id
            {where_clause}
            ORDER BY m.time_created ASC
            """,
            args,
        )
        turn_index_by_session: Dict[str, int] = {}
        for session_id, directory, title, slug, worktree, created_ms, data_json in cur.fetchall():
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
            path_info = data.get("path") if isinstance(data.get("path"), dict) else {}
            _append_opencode_turn(
                sessions,
                turn_index_by_session,
                tool="mimo",
                session_id=session_id,
                directory=directory,
                worktree=worktree,
                created_ms=created_ms,
                model=data.get("modelID"),
                provider=data.get("providerID"),
                fresh_input=tokens.get("input", 0),
                cache_write=cache.get("write", 0),
                cache_read=cache.get("read", 0),
                output_tokens=tokens.get("output", 0),
                reasoning_tokens=tokens.get("reasoning", 0),
                cwd=path_info.get("cwd"),
                root=path_info.get("root"),
                title=title,
                slug=slug,
                recorded_cost=data.get("cost"),
            )
    finally:
        conn.close()

    return sessions


def _mimo_sessions(since_ms: Optional[int] = None, until_ms: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    signature = _mimo_db_signature()
    if not signature:
        return {}
    return _load_mimo_sessions(signature, _pricing_signature(), since_ms, until_ms)


def _raw_sessions_for_tool(
    tool: str,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    key = str(tool or "").strip().lower()
    if key in {"codex", "claude"} and persistent_usage_db_enabled():
        try:
            return _stored_sessions_for_tool(key)
        except Exception:
            pass
    if key == "codex":
        return _codex_sessions()
    if key == "claude":
        return _claude_sessions()
    if key == "opencode":
        return _opencode_sessions(since_ms=since_ms, until_ms=until_ms)
    if key == "pi_agent":
        return _pi_sessions()
    if key == "mimo":
        return _mimo_sessions(since_ms=since_ms, until_ms=until_ms)
    raise ValueError(f"Unsupported session tool: {tool}")


def _turn_identity_key(turn: Dict[str, Any]) -> tuple[int, str, int, int, int, int, float]:
    return (
        int(turn.get("timestamp_ms", 0) or 0),
        str(turn.get("model") or "unknown"),
        int(turn.get("tokens_in", 0) or 0),
        int(turn.get("tokens_cache", 0) or 0),
        int(turn.get("tokens_out", 0) or 0),
        int(turn.get("tokens_reasoning", 0) or 0),
        round(float(turn.get("cost", 0.0) or 0.0), 8),
    )


def _session_records_to_raw_sessions(tool: str, records: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    sessions: Dict[str, Dict[str, Any]] = {}
    seen_turns: dict[str, set[tuple[int, str, int, int, int, int, float]]] = {}
    for raw in records:
        session_id = str(raw.get("session_id") or "")
        if not session_id:
            continue
        if tool == "codex":
            # Match the live Codex loader: sorted files with the same session_id
            # are not merged; the later record wins.
            sessions[session_id] = raw
            continue

        session = sessions.get(session_id)
        if session is None:
            session = dict(raw)
            session["tool"] = session.get("tool") or tool
            session["session_id"] = session_id
            session["project"] = session.get("project") or "unknown"
            session["turns"] = []
            sessions[session_id] = session
            seen_turns[session_id] = set()
        elif session.get("project") == "unknown" and raw.get("project"):
            session["project"] = raw.get("project")

        seen = seen_turns[session_id]
        for turn in raw.get("turns", []):
            key = _turn_identity_key(turn)
            if key in seen:
                continue
            seen.add(key)
            session["turns"].append(dict(turn))

    for session in sessions.values():
        session.setdefault("display_name", _fallback_display_name(session.get("session_id"), session.get("project")))
        session["is_review_session"] = bool(session.get("is_review_session", False))
        if tool != "codex":
            session["turns"].sort(key=lambda item: (int(item.get("timestamp_ms", 0) or 0), int(item.get("turn_index", 0) or 0)))
            for turn_index, turn in enumerate(session["turns"], start=1):
                turn["turn_index"] = turn_index
    return sessions


def _stored_sessions_for_tool(tool: str) -> Dict[str, Dict[str, Any]]:
    store = UsageEntryStore()
    if tool == "codex":
        root = clientpaths.codex_sessions_dir()
        signatures = _iter_file_signatures(root)
        parser_sig = {"parser": parser_code_signature(_parse_codex_session_file), "pricing": _pricing_signature()}
        pricing_sig = _pricing_signature()
        store.sync_session_files(
            "codex",
            signatures,
            parser=parser_sig,
            parse_file_session=lambda file_sig: _parse_codex_session_file(*file_sig, pricing_sig),
        )
    elif tool == "claude":
        all_sigs: list[tuple[str, int, int]] = []
        for projects_dir in clientpaths.claude_project_dirs():
            all_sigs.extend(_iter_file_signatures(projects_dir))
        all_sigs.sort(key=lambda item: item[0])
        parser_sig = {"parser": parser_code_signature(_parse_claude_session_file), "pricing": _pricing_signature()}
        pricing_sig = _pricing_signature()
        store.sync_session_files(
            "claude",
            tuple(all_sigs),
            parser=parser_sig,
            parse_file_session=lambda file_sig: _parse_claude_session_file(*file_sig, pricing_sig),
        )
    else:
        raise ValueError(f"Unsupported stored session tool: {tool}")

    sessions = _session_records_to_raw_sessions(tool, store.query_session_records(tool))
    if tool == "codex":
        return _apply_codex_title_map(sessions)
    return sessions


def get_sessions_data(
    tool: str,
    period: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: Optional[int] = None,
    include_review_sessions: Optional[bool] = None,
) -> Dict[str, Any]:
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
    window_since_ms = since_ms if key in {"opencode", "mimo"} else None
    window_until_ms = until_ms if key in {"opencode", "mimo"} else None
    include_codex_review = _include_codex_review_sessions(include_review_sessions)
    for raw in _raw_sessions_for_tool(key, since_ms=window_since_ms, until_ms=window_until_ms).values():
        if key == "codex" and raw.get("is_review_session") and not include_codex_review:
            continue
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
        # Echo the effective review-session default (param, else TOKDASH_INCLUDE_CODEX_GUARDIAN)
        # so the dashboard toggle can reflect the server default before the user opts in.
        "include_review_sessions": include_codex_review,
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


def get_codex_sessions_data(
    period: str,
    limit: Optional[int] = None,
    include_review_sessions: Optional[bool] = None,
) -> Dict[str, Any]:
    return get_sessions_data("codex", period, limit=limit, include_review_sessions=include_review_sessions)


def get_codex_session_detail(session_id: str) -> Dict[str, Any]:
    return get_session_detail("codex", session_id)
