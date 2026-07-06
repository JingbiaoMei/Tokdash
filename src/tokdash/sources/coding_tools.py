"""Coding tools token usage parsers.

These parsers emit tokscale-compatible `entries[]` rows and are used by
`tokdash.compute` when running with the local parsers backend.
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import time as _time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Tuple


try:
    from .. import clientpaths
    from ..pricing import PricingDatabase
except ImportError:  # pragma: no cover
    # Allow running as a script by file path.
    import clientpaths
    from pricing import PricingDatabase


# ---------------------------------------------------------------------------
# File-signature caching – avoids repeated rglob / glob.glob + stat() calls
# when multiple API requests arrive within a short window.
# ---------------------------------------------------------------------------
_sig_cache: Dict[str, Tuple[float, tuple]] = {}
_SIG_TTL = float(os.environ.get("TOKDASH_SIG_TTL", "5.0"))  # seconds; 0 to disable
_OPENCODE_QUERY_CACHE_MAX = 32  # max date-range entries before eviction


@dataclass(frozen=True)
class SourceSyncCapability:
    """Persistent-DB sync behavior declared by each parser.

    mode:
      - file_replace: unchanged files stay indexed; changed files are reparsed.
      - source_replace: source-wide replacement is required for correctness.
      - source_native_db: do not copy into the Tokdash usage store; query source DB.
    """

    mode: str = "source_replace"
    append_jsonl: bool = False
    session_store: bool = False
    reason: str = ""


def _timed_sigs(cache_key: str, scan_fn) -> tuple:
    """Return file signatures from *scan_fn*, reusing a cached value within TTL."""
    now = _time.monotonic()
    cached = _sig_cache.get(cache_key)
    if cached and (now - cached[0]) < _SIG_TTL:
        return cached[1]
    result = scan_fn()
    _sig_cache[cache_key] = (now, result)
    return result


def _rglob_sigs(root: Path, pattern: str = "*.jsonl") -> tuple:
    """Build sorted (path, mtime_ns, size) signatures via Path.rglob."""
    if not root.exists():
        return ()
    items: List[Tuple[str, int, int]] = []
    for p in root.rglob(pattern):
        try:
            s = p.stat()
            items.append((str(p), s.st_mtime_ns, s.st_size))
        except (FileNotFoundError, OSError):
            continue
    return tuple(sorted(items))


def _glob_sigs(pattern: str) -> tuple:
    """Build sorted (path, mtime_ns, size) signatures via glob.glob."""
    items: List[Tuple[str, int, int]] = []
    for p_str in glob.glob(pattern):
        try:
            s = os.stat(p_str)
            items.append((p_str, int(s.st_mtime_ns), int(s.st_size)))
        except (FileNotFoundError, OSError):
            continue
    return tuple(sorted(items))


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False


def _mimo_imported_message_ids(conn: sqlite3.Connection) -> set[str]:
    imported: set[str] = set()
    for table in ("external_import", "claude_import"):
        if not _sqlite_table_exists(conn, table):
            continue
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT message_ids FROM {table} WHERE message_ids IS NOT NULL")
            rows = cur.fetchall()
        except sqlite3.Error:
            continue
        for (message_ids_json,) in rows:
            try:
                message_ids = json.loads(message_ids_json)
            except (TypeError, ValueError):
                continue
            if not isinstance(message_ids, list):
                continue
            imported.update(str(message_id) for message_id in message_ids if message_id is not None)
    return imported


def _pb_read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _pb_parse_message(buf: bytes) -> Dict[int, list[Any]]:
    """Parse a minimal protobuf wire message into field-number buckets."""
    pos = 0
    out: Dict[int, list[Any]] = {}
    while pos < len(buf):
        tag, pos = _pb_read_varint(buf, pos)
        field = tag >> 3
        wire_type = tag & 0x07
        if field <= 0:
            raise ValueError("invalid field number")
        if wire_type == 0:
            value, pos = _pb_read_varint(buf, pos)
        elif wire_type == 1:
            if pos + 8 > len(buf):
                raise ValueError("truncated fixed64")
            value = buf[pos : pos + 8]
            pos += 8
        elif wire_type == 2:
            size, pos = _pb_read_varint(buf, pos)
            if pos + size > len(buf):
                raise ValueError("truncated length-delimited field")
            value = buf[pos : pos + size]
            pos += size
        elif wire_type == 5:
            if pos + 4 > len(buf):
                raise ValueError("truncated fixed32")
            value = buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        out.setdefault(field, []).append(value)
    return out


def _pb_get_path(msg: Dict[int, list[Any]], path: tuple[int, ...]) -> Any:
    cur: Any = msg
    for index, field in enumerate(path):
        if not isinstance(cur, dict):
            return None
        values = cur.get(field)
        if not values:
            return None
        value = values[-1]
        if index == len(path) - 1:
            return value
        if not isinstance(value, bytes):
            return None
        cur = _pb_parse_message(value)
    return None


def _pb_text(value: Any) -> str:
    if not isinstance(value, bytes):
        return ""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return ""


class BaseParser(ABC):
    source_name: str
    sync_capability = SourceSyncCapability()

    # Shared across all instances:
    #   {source_name: ((file_sigs, pricing_sig), [entries])}
    # pricing_sig is included so cost values are recomputed when pricing_db.json changes.
    _entry_cache: ClassVar[Dict[str, Tuple[tuple, List[Dict[str, Any]]]]] = {}

    def __init__(self, pricing_db: PricingDatabase):
        self.pricing_db = pricing_db

    def _file_signatures(self) -> tuple:
        """Hashable snapshot of source files; override per parser."""
        return ()

    def _pricing_signature(self) -> tuple:
        """Signature of the EFFECTIVE pricing DB (packaged baseline + data-dir override).

        Must cover BOTH files: a dashboard pricing edit writes ONLY the override under
        ``TOKDASH_DATA_DIR`` and never touches the packaged baseline, so statting the
        baseline alone would never bust ``_entry_cache`` (nor the persistent usage store,
        which keys on this same signature) and edited rates would silently not apply.
        ``PricingDatabase.signature()`` stats both files and is itself OSError-safe.
        """
        try:
            return tuple(self.pricing_db.signature())
        except (OSError, AttributeError):
            return ()

    @abstractmethod
    def _parse_all(self) -> List[Dict[str, Any]]:
        """Parse all entries without date filtering."""
        raise NotImplementedError

    def collect(self, since_date: Optional[datetime] = None, until_date: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Cached collect: parse once per file-signature, filter by date in memory.

        File signatures (path, mtime_ns, size) detect when source files change
        on disk.  When signatures match the cache, we skip re-parsing entirely
        and just filter the cached entry list by date – turning a multi-second
        I/O-bound operation into a fast in-memory scan.

        The cache key also includes the pricing DB file signature so that
        cached cost values are recomputed when pricing_db.json is updated.

        The cache is a ClassVar shared across all parser instances so that
        separate ``CodingToolsUsageTracker`` objects (e.g. for current-period
        and previous-period in ``compute_usage_with_comparison``) reuse the
        same parsed data.
        """
        sig = (self._file_signatures(), self._pricing_signature())
        cached = self._entry_cache.get(self.source_name)
        if cached is not None and cached[0] == sig:
            all_entries = cached[1]
        else:
            all_entries = self._parse_all()
            self._entry_cache[self.source_name] = (sig, all_entries)

        if since_date is None and until_date is None:
            return list(all_entries)

        s = self._to_utc(since_date)
        u = self._to_utc(until_date)
        s_ms = int(s.timestamp() * 1000) if s else 0
        u_ms = int(u.timestamp() * 1000) if u else 9999999999999
        return [e for e in all_entries if s_ms <= (e.get("timestamp") or 0) < u_ms]

    @staticmethod
    def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @classmethod
    def _in_range(cls, ts: datetime, since_date: Optional[datetime], until_date: Optional[datetime]) -> bool:
        s = cls._to_utc(since_date)
        u = cls._to_utc(until_date)
        t = cls._to_utc(ts)
        if t is None:
            return False
        if s and t < s:
            return False
        if u and t >= u:
            return False
        return True

    @staticmethod
    def _i(v: Any) -> int:
        try:
            return int(v or 0)
        except Exception:
            return 0


class OpenCodeParser(BaseParser):
    source_name = "opencode"
    sync_capability = SourceSyncCapability(
        mode="source_native_db",
        session_store=False,
        reason="OpenCode already stores messages in a large SQLite DB and supports SQL date windows.",
    )

    # Per-query cache: {(s_ms, u_ms): [entries]}, invalidated when DB or pricing changes.
    # Bounded to _OPENCODE_QUERY_CACHE_MAX entries to prevent unbounded growth.
    _query_cache: ClassVar[Dict[tuple, List[Dict[str, Any]]]] = {}
    _query_cache_sig: ClassVar[tuple] = ()

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.messages_dir = clientpaths.opencode_messages_dir()
        self.db_path = clientpaths.opencode_db_path()

    def _build_entry(self, model: str, provider: str, tokens: Dict[str, Any], ts_ms: int) -> Dict[str, Any]:
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        input_t = self._i(tokens.get("input"))
        output_t = self._i(tokens.get("output"))
        cache_r = self._i(cache.get("read"))
        cache_w = self._i(cache.get("write"))
        reasoning = self._i(tokens.get("reasoning"))
        return {
            "source": self.source_name,
            "model": model or "unknown",
            "provider": provider or "",
            "input": input_t,
            "output": output_t,
            "cacheRead": cache_r,
            "cacheWrite": cache_w,
            "reasoning": reasoning,
            "cost": self.pricing_db.get_cost(model, input_t, output_t, cache_r, cache_w),
            "timestamp": int(ts_ms),
        }

    def _file_signatures(self) -> tuple:
        if not self.db_path.exists():
            return ()
        out: list[tuple[str, int, int]] = []
        for candidate in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            try:
                s = candidate.stat()
                out.append((str(candidate), s.st_mtime_ns, s.st_size))
            except (FileNotFoundError, OSError):
                continue
        return tuple(out)

    def _parse_all(self) -> List[Dict[str, Any]]:
        return []  # collect() is overridden; this satisfies the ABC contract

    def collect(self, since_date: Optional[datetime] = None, until_date: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Override: use SQL date filtering with per-query caching.

        The OpenCode DB can be very large (700MB+), so we keep SQL-level
        date filtering instead of loading everything into memory.  Results
        are cached per (db_signature, pricing_signature, date_range) and
        invalidated when the DB file or pricing DB changes on disk.
        The cache is bounded to ``_OPENCODE_QUERY_CACHE_MAX`` entries.
        """
        sig = (self._file_signatures(), self._pricing_signature())
        # Invalidate all cached queries when the DB or pricing file changes.
        if sig != type(self)._query_cache_sig:
            type(self)._query_cache.clear()
            type(self)._query_cache_sig = sig

        s_ms = int(self._to_utc(since_date).timestamp() * 1000) if since_date else 0
        u_ms = int(self._to_utc(until_date).timestamp() * 1000) if until_date else 9999999999999
        cache_key = (s_ms, u_ms)

        cached = type(self)._query_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        out: List[Dict[str, Any]] = []

        # IMPORTANT: Only use SQLite DB to avoid double-counting!
        # File storage (~/.local/share/opencode/storage/message) contains the SAME messages as the DB.
        # Using both sources would result in 100% duplication.
        # See: patchFixSetup/09-fixes/OpenCode_Double_Counting_Fix.md

        if self.db_path.exists():
            try:
                conn = sqlite3.connect(str(self.db_path))
                cur = conn.cursor()
                cur.execute("SELECT data, time_created FROM message WHERE time_created >= ? AND time_created < ? ORDER BY time_created", (s_ms, u_ms))
                rows = cur.fetchall()
                conn.close()
                for data_json, ts_ms in rows:
                    try:
                        data = json.loads(data_json)
                        tokens = data.get("tokens")
                        if not isinstance(tokens, dict):
                            continue
                        out.append(self._build_entry(str(data.get("modelID") or "unknown"), str(data.get("providerID") or ""), tokens, self._i(ts_ms)))
                    except Exception:
                        continue
            except Exception:
                pass

        # Evict all entries when cache exceeds bound to prevent unbounded growth.
        if len(type(self)._query_cache) >= _OPENCODE_QUERY_CACHE_MAX:
            type(self)._query_cache.clear()
        type(self)._query_cache[cache_key] = out
        return list(out)


class CodexParser(BaseParser):
    source_name = "codex"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        session_store=True,
        reason="Codex JSONL session files can be indexed independently; tail append needs stronger line-offset IDs first.",
    )

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.sessions_dir = clientpaths.codex_sessions_dir()

    @staticmethod
    def _infer_provider(model: str, fallback: str = "openai") -> str:
        m = (model or "").lower()
        if m.startswith("claude"):
            return "anthropic"
        if "gemini" in m:
            return "google"
        if m.startswith("gpt") or "codex" in m:
            return "openai"
        return fallback

    def _file_signatures(self) -> tuple:
        return _timed_sigs(f"codex:{self.sessions_dir}", lambda: _rglob_sigs(self.sessions_dir))

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        for path_str, _, _ in self._file_signatures():
            session_file = Path(path_str)
            try:
                model = "gpt-5.3-codex"
                provider = "openai"

                for line_no, line in enumerate(session_file.read_text(encoding="utf-8").splitlines(), start=1):
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue

                    p = msg.get("payload") or {}
                    if msg.get("type") == "turn_context" and p.get("model"):
                        model = str(p.get("model"))
                        provider = self._infer_provider(model, provider)
                    elif msg.get("type") == "session_meta" and p.get("model_provider"):
                        provider = str(p.get("model_provider"))

                    if msg.get("type") != "event_msg" or p.get("type") != "token_count":
                        continue

                    ts_raw = msg.get("timestamp")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
                    except Exception:
                        continue

                    info = p.get("info") if isinstance(p.get("info"), dict) else {}

                    # Use last_token_usage (per-turn delta) instead of total_token_usage (cumulative)
                    usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
                    if not usage:
                        continue

                    # In Codex: input_tokens INCLUDES cached tokens
                    # So fresh_input = input_tokens - cached_input_tokens
                    total_input = self._i(usage.get("input_tokens"))
                    cache_read = self._i(usage.get("cached_input_tokens"))
                    input_t = total_input - cache_read  # Fresh input only
                    output_t = self._i(usage.get("output_tokens"))
                    reasoning = self._i(usage.get("reasoning_output_tokens"))

                    if input_t == 0 and output_t == 0 and cache_read == 0 and reasoning == 0:
                        continue

                    out.append(
                        {
                            "source": self.source_name,
                            "model": model,
                            "provider": provider,
                            "input": input_t,
                            "output": output_t,
                            "cacheRead": cache_read,
                            "cacheWrite": 0,
                            "reasoning": reasoning,
                            "cost": self.pricing_db.get_cost(model, input_t, output_t, cache_read, 0),
                            "timestamp": int(ts.timestamp() * 1000),
                            "entry_id": f"{session_file}:{line_no}",
                        }
                    )
            except Exception:
                continue

        return out


class ClaudeParser(BaseParser):
    source_name = "claude"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        session_store=True,
        reason="Claude streaming snapshots require full-file dedup context; tail append is unsafe.",
    )

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.projects_dirs = clientpaths.claude_project_dirs()

    @staticmethod
    def _infer_provider(model: str) -> str:
        m = (model or "").lower()
        if m.startswith("claude"):
            return "anthropic"
        if "gemini" in m:
            return "google"
        if m.startswith("gpt") or "codex" in m:
            return "openai"
        return ""

    def _file_signatures(self) -> tuple:
        all_sigs = []
        for projects_dir in self.projects_dirs:
            all_sigs.extend(
                _timed_sigs(
                    f"claude:{projects_dir}",
                    lambda d=projects_dir: _rglob_sigs(d),
                )
            )
        return tuple(sorted(all_sigs))

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen_message_ids = set()
        snapshot_entries_by_message_id: Dict[str, Dict[str, Any]] = {}

        for path_str, _, _ in self._file_signatures():
            session_file = Path(path_str)
            try:
                for line in session_file.read_text(encoding="utf-8").splitlines():
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                    role = msg.get("role")
                    is_top_level_assistant = role is None and obj.get("type") == "assistant"
                    if role != "assistant" and not is_top_level_assistant:
                        continue
                    usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                    if not usage:
                        continue

                    ts_raw = obj.get("timestamp")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
                    except Exception:
                        continue

                    input_t = self._i(usage.get("input_tokens", usage.get("input")))
                    output_t = self._i(usage.get("output_tokens", usage.get("output")))
                    cache_r = self._i(usage.get("cache_read_input_tokens", usage.get("cache_read_tokens")))
                    cache_w = self._i(usage.get("cache_creation_input_tokens", usage.get("cache_write_tokens")))
                    if input_t + output_t + cache_r + cache_w == 0:
                        continue

                    msg_id = str(msg.get("id") or obj.get("uuid") or "")
                    # Legacy role-bearing logs write the same message id many
                    # times; skip the duplicates before building/pricing the entry.
                    if msg_id and not is_top_level_assistant and msg_id in seen_message_ids:
                        continue

                    model = str(msg.get("model") or "unknown")
                    entry = {
                        "source": self.source_name,
                        "model": model,
                        "provider": self._infer_provider(model),
                        "input": input_t,
                        "output": output_t,
                        "cacheRead": cache_r,
                        "cacheWrite": cache_w,
                        "reasoning": 0,
                        "cost": self.pricing_db.get_cost(model, input_t, output_t, cache_r, cache_w),
                        "timestamp": int(ts.timestamp() * 1000),
                        "entry_id": f"claude:{msg_id}" if msg_id else "",
                    }
                    if not msg_id:
                        out.append(entry)
                        continue

                    if is_top_level_assistant:
                        # Newer Claude Code builds (so far seen via OpenAI-compatible
                        # endpoints) log assistant turns as role-less streaming
                        # snapshots sharing one id; keep the latest, which carries
                        # the most complete usage.
                        existing = snapshot_entries_by_message_id.get(msg_id)
                        if existing is None or entry["timestamp"] >= existing["timestamp"]:
                            snapshot_entries_by_message_id[msg_id] = entry
                        continue

                    # First non-zero occurrence of this legacy id.
                    seen_message_ids.add(msg_id)
                    out.append(entry)
            except Exception:
                continue

        out.extend(
            entry
            for msg_id, entry in snapshot_entries_by_message_id.items()
            if msg_id not in seen_message_ids
        )
        out.sort(key=lambda entry: int(entry.get("timestamp", 0) or 0))
        return out


class GeminiCLIParser(BaseParser):
    """
    Parser for Gemini CLI session files.

    ========================================================================
    GEMINI CLI SESSION FILE SCHEMA (fixture-friendly notes)
    ========================================================================
    Location: ~/.gemini/tmp/<projectHash>/chats/session-*.json or session-*.jsonl

    Top-level fields:
      - sessionId: UUID string
      - projectHash: SHA256-like hex string (per-project hash)
      - startTime: ISO 8601 timestamp (e.g., "2026-01-03T12:02:18.267Z")
      - lastUpdated: ISO 8601 timestamp
      - messages: array of message objects in JSON files; one message object per
        line in JSONL files

    Message object schema (type="gemini" only has tokens):
      - id: UUID string (unique per message, use for dedup)
      - timestamp: ISO 8601 string
      - type: "user" | "gemini" | "info" | "error"
      - content: string (for user/gemini messages)
      - model: string (e.g., "gemini-3-flash-preview")
      - tokens: object (only present for type="gemini")
          - input: int (TOTAL prompt tokens, INCLUSIVE of cached; like the Gemini
                   API's promptTokenCount, this already contains tokens.cached)
          - output: int (completion tokens)
          - cached: int (cache read tokens, a subset of input) -> maps to cacheRead
          - thoughts: int (reasoning tokens) -> maps to reasoning
          - tool: int (tool call tokens) -> currently ignored per spec
          - total: int (== input + output + thoughts + tool; cached is already
                   inside input, so it is NOT added again here — used for validation)

    Field mapping to normalized entry:
      source <- "gemini_cli"
      provider <- "google"
      input <- tokens.input - tokens.cached   (fresh/uncached prompt only; tokens.input
               is cache-inclusive, so subtract to avoid double-counting cached tokens in
               totals/cost — matches the Codex/Copilot parsers; see _build_entry)
      output <- tokens.output
      cacheRead <- tokens.cached
      reasoning <- tokens.thoughts
      cacheWrite <- 0 (not exposed in current schema)
      timestamp <- ISO timestamp converted to epoch ms

    Dedup key: message.id (UUID, unique per response)

    Known schema versions: 2025-07 to present
    Last verified: 2026-05-29 (confirmed tokens.input is cache-inclusive across real sessions)

    FUTURE DATA-SHAPE UPDATES:
    - If token field names change, add fallback aliases in _build_entry()
    - If new token types are added, map to existing fields or add new
    - If session file location changes, update glob pattern in _file_signatures()
    ========================================================================
    """

    source_name = "gemini_cli"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        append_jsonl=True,
        reason="Gemini JSONL rows have stable message IDs; JSON array files still fall back to file replacement.",
    )

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.gemini_root = clientpaths.gemini_root()

    def _build_entry(self, model: str, tokens: Dict[str, Any], ts_ms: int) -> Dict[str, Any]:
        raw_input = self._i(tokens.get("input"))
        output_t = self._i(tokens.get("output"))
        cache_r = self._i(tokens.get("cached"))
        cache_w = 0  # cache_write not present in Gemini CLI tokens
        # Gemini CLI reports tokens.input INCLUSIVE of the cached prompt tokens
        # (a session's `total` = input + output + thoughts confirms cached ⊆ input),
        # so subtract to recover the fresh/uncached portion — matching the Codex and
        # Copilot parsers. Without this, cached tokens are double-counted (once in
        # input, once as cacheRead), inflating Gemini totals, cost, and depressing the
        # cache-hit rate. See docs/CHANGELOG.md.
        input_t = max(0, raw_input - cache_r)
        reasoning = self._i(tokens.get("thoughts"))
        provider = "google"
        return {
            "source": self.source_name,
            "model": model or "unknown",
            "provider": provider,
            "input": input_t,
            "output": output_t,
            "cacheRead": cache_r,
            "cacheWrite": cache_w,
            "reasoning": reasoning,
            "cost": self.pricing_db.get_cost(model, input_t, output_t, cache_r, cache_w),
            "timestamp": int(ts_ms),
        }

    def _file_signatures(self) -> tuple:
        def scan() -> tuple:
            json_pattern = clientpaths.gemini_chats_json_glob(self.gemini_root)
            jsonl_pattern = clientpaths.gemini_chats_jsonl_glob(self.gemini_root)
            return tuple(sorted(_glob_sigs(json_pattern) + _glob_sigs(jsonl_pattern)))

        return _timed_sigs(f"gemini:{self.gemini_root}", scan)

    @staticmethod
    def _iter_messages(path_str: str) -> List[Dict[str, Any]]:
        path = Path(path_str)
        if path.suffix == ".jsonl":
            messages: List[Dict[str, Any]] = []
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        messages.append(obj)
            return messages

        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        messages = data.get("messages") if isinstance(data, dict) else None
        return messages if isinstance(messages, list) else []

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen_ids = set()
        for path_str, _, _ in self._file_signatures():
            try:
                messages = self._iter_messages(path_str)
            except Exception:
                continue
            for msg in messages:
                try:
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") != "gemini":
                        continue
                    tokens = msg.get("tokens")
                    if not isinstance(tokens, dict):
                        continue
                    msg_id = msg.get("id")
                    if msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                    ts_str = msg.get("timestamp")
                    if not ts_str:
                        continue
                    # Convert ISO timestamp with Z to datetime
                    ts_str = ts_str.replace("Z", "+00:00")
                    ts = datetime.fromisoformat(ts_str).astimezone(timezone.utc)
                    model = msg.get("model") or "unknown"
                    ts_ms = int(ts.timestamp() * 1000)
                    entry = self._build_entry(model, tokens, ts_ms)
                    entry["entry_id"] = f"gemini_cli:{msg_id}"
                    out.append(entry)
                except Exception:
                    continue
        return out


class AntigravityCLIParser(BaseParser):
    """
    Parser for Antigravity CLI (agy) generation metadata SQLite DBs.

    ========================================================================
    ANTIGRAVITY CLI GEN_METADATA SCHEMA (fixture-friendly notes)
    ========================================================================
    Location: ~/.gemini/antigravity-cli/conversations/<conversation_uuid>.db
    Table: gen_metadata(idx INTEGER, data BLOB, size)

    Each row is one LLM generation. The data BLOB is protobuf wire format. This
    parser intentionally uses a small stdlib-only wire walker instead of adding
    a protobuf runtime dependency.

    Outer-message paths:
      - 1.19: model id string, e.g. "gemini-3-flash-a" or
        "claude-opus-4-6-thinking"
      - 1.21: display name string, currently ignored
      - 1.9.4.1 / 1.9.4.2: completion timestamp seconds / nanos
      - 1.4: ModelUsageStats sub-message

    ModelUsageStats fields at path 1.4:
      - field 1: model enum, ignored
      - field 2: input_tokens -> input (fresh/uncached; use directly)
      - field 3: output_tokens, total output including thinking
      - field 4: cache_write_tokens -> cacheWrite
      - field 5: cache_read_tokens -> cacheRead
      - field 6: api_provider enum, ignored
      - field 9: thinking_output_tokens -> reasoning (additive in Tokdash totals)
      - field 10: response_output_tokens -> output (visible output)

    Field mapping to normalized entry:
      source <- "antigravity_cli"
      provider <- "anthropic" for model ids beginning with "claude", else
                  "google"
      input <- field 1.4.2, no cache subtraction
      output <- field 1.4.10, falling back to field 1.4.3 - field 1.4.9 when
                field 10 is absent. Field 1.4.3 includes thinking tokens, and
                Tokdash totals add reasoning separately, so mapping field 3
                directly would double-count Gemini thinking tokens.
      cacheRead <- field 1.4.5
      cacheWrite <- field 1.4.4
      reasoning <- field 1.4.9
      timestamp <- (1.9.4.1 * 1000) + (1.9.4.2 // 1_000_000)

    Dedup key: entry_id = "antigravity_cli:<db_stem>:<idx>"

    Known schema version: agy build verified 2026-07-02. The token mapping is
    descriptor-pinned in docs/local/20260702_antigravity_usage/
    antigravity_gen_metadata_schema.md. Legacy .pb files in the conversations
    directory are intentionally skipped; only *.db is parsed.

    WAL note: Antigravity DBs run in WAL mode. _file_signatures() folds -wal
    and -shm metadata into each .db signature while preserving the .db path so
    file_replace sync keys stay stable.
    ========================================================================
    """

    source_name = "antigravity_cli"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        reason="Each conversation is an independent SQLite DB; changed DBs are reparsed whole.",
    )

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.conversations_dir = clientpaths.antigravity_conversations_dir()

    def _file_signatures(self) -> tuple:
        def scan() -> tuple:
            sigs: List[Tuple[str, int, int]] = []
            for db_path_str in glob.glob(clientpaths.antigravity_conversations_glob()):
                db_path = Path(db_path_str)
                try:
                    db_stat = db_path.stat()
                except (FileNotFoundError, OSError):
                    continue

                max_mtime = int(db_stat.st_mtime_ns)
                total_size = int(db_stat.st_size)
                wal_path = Path(str(db_path) + "-wal")
                shm_path = Path(str(db_path) + "-shm")
                for sidecar in (wal_path, shm_path):
                    try:
                        sidecar_stat = sidecar.stat()
                    except (FileNotFoundError, OSError):
                        continue
                    max_mtime = max(max_mtime, int(sidecar_stat.st_mtime_ns))
                    if sidecar == wal_path:
                        total_size += int(sidecar_stat.st_size)
                sigs.append((str(db_path), max_mtime, total_size))
            return tuple(sorted(sigs))

        return _timed_sigs(f"antigravity_cli:{self.conversations_dir}", scan)

    @staticmethod
    def _connect_readonly(path: Path) -> sqlite3.Connection:
        try:
            return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        except Exception:
            return sqlite3.connect(str(path))

    @classmethod
    def _decode_row(cls, data: bytes) -> Optional[Dict[str, Any]]:
        outer = _pb_parse_message(bytes(data))
        usage_blob = _pb_get_path(outer, (1, 4))
        if not isinstance(usage_blob, bytes):
            return None
        usage = _pb_parse_message(usage_blob)

        sec = cls._i(_pb_get_path(outer, (1, 9, 4, 1)))
        nanos = cls._i(_pb_get_path(outer, (1, 9, 4, 2)))
        input_t = cls._i((usage.get(2) or [0])[-1])
        output_total = cls._i((usage.get(3) or [0])[-1])
        cache_w = cls._i((usage.get(4) or [0])[-1])
        cache_r = cls._i((usage.get(5) or [0])[-1])
        reasoning = cls._i((usage.get(9) or [0])[-1])
        output_visible = usage.get(10)
        output_t = cls._i(output_visible[-1]) if output_visible else max(0, output_total - reasoning)

        return {
            "model": _pb_text(_pb_get_path(outer, (1, 19))) or "unknown",
            "input": input_t,
            "output": output_t,
            "cacheRead": cache_r,
            "cacheWrite": cache_w,
            "reasoning": reasoning,
            "timestamp": int(sec * 1000 + nanos // 1_000_000),
        }

    def _build_entry(self, idx: int, db_stem: str, decoded: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        model = str(decoded.get("model") or "unknown")
        input_t = self._i(decoded.get("input"))
        output_t = self._i(decoded.get("output"))
        cache_r = self._i(decoded.get("cacheRead"))
        cache_w = self._i(decoded.get("cacheWrite"))
        reasoning = self._i(decoded.get("reasoning"))
        if input_t == 0 and output_t == 0 and cache_r == 0:
            return None
        provider = "anthropic" if model.lower().startswith("claude") else "google"
        return {
            "source": self.source_name,
            "model": model,
            "provider": provider,
            "input": input_t,
            "output": output_t,
            "cacheRead": cache_r,
            "cacheWrite": cache_w,
            "reasoning": reasoning,
            "cost": self.pricing_db.get_cost(model, input_t, output_t, cache_r, cache_w),
            "timestamp": self._i(decoded.get("timestamp")),
            "entry_id": f"antigravity_cli:{db_stem}:{idx}",
        }

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for path_str, _, _ in self._file_signatures():
            db_path = Path(path_str)
            rows = None
            for use_readonly in (True, False):
                try:
                    conn = self._connect_readonly(db_path) if use_readonly else sqlite3.connect(str(db_path))
                except Exception:
                    if use_readonly:
                        continue
                    break
                try:
                    rows = conn.execute("SELECT idx, data FROM gen_metadata ORDER BY idx").fetchall()
                    break
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    if use_readonly:
                        continue
                    break
                finally:
                    if rows is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            if rows is None:
                continue

            for idx, data in rows:
                try:
                    decoded = self._decode_row(data)
                    if decoded is None:
                        continue
                    entry = self._build_entry(self._i(idx), db_path.stem, decoded)
                    if entry is not None:
                        out.append(entry)
                except Exception:
                    continue
        return out


class AmpParser(BaseParser):
    source_name = "amp"
    sync_capability = SourceSyncCapability(
        mode="source_replace",
        reason="Parser placeholder returns no rows until a stable local schema is available.",
    )

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.amp_root = clientpaths.amp_root()

    def _parse_all(self) -> List[Dict[str, Any]]:
        # TODO(coding_tools): Amp parser placeholder.
        # Keep fail-soft until we have schema + fixtures.
        return []


class KimiParser(BaseParser):
    """
    Parser for Kimi CLI session files.

    =======================================================================
    KIMI CLI SESSION FILE SCHEMA
    =======================================================================
    Location: ~/.kimi/sessions/<userId>/<sessionId>/wire.jsonl

    The wire.jsonl file contains JSON lines with different message types.
    Token usage is captured in "StatusUpdate" messages.

    Relevant fields:
      - timestamp: Unix timestamp (float, seconds since epoch)
      - message.type: "StatusUpdate"
      - message.payload.token_usage: object with token counts
          - input_other: int (fresh input tokens)
          - output: int (output/completion tokens)
          - input_cache_read: int (cache read tokens)
          - input_cache_creation: int (cache write tokens)
      - message.payload.message_id: str (unique message ID for dedup)

    Field mapping to normalized entry:
      source <- "kimi"
      provider <- "moonshotai" (Kimi is from Moonshot AI)
      input <- token_usage.input_other
      output <- token_usage.output
      cacheRead <- token_usage.input_cache_read
      cacheWrite <- token_usage.input_cache_creation
      reasoning <- 0 (not exposed separately in Kimi CLI)
      timestamp <- timestamp * 1000 (convert to milliseconds)

    Dedup key: message.payload.message_id

    Known schema versions: 2025-03 to present
    =======================================================================
    """

    source_name = "kimi"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        append_jsonl=True,
        reason="Kimi wire JSONL rows expose stable message IDs and are append-safe for token usage rows.",
    )

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.kimi_root = clientpaths.kimi_root()

    @staticmethod
    def _default_model_for_timestamp(ts: datetime) -> str:
        # Kimi's local session files do not currently expose the resolved model for each
        # StatusUpdate event, so we infer a default billing model by time window.
        #
        # Current assumption: "kimi-for-coding" maps to kimi-k2.5 for the period we
        # support today. When Kimi changes the default backend model, update this
        # function to use a timestamp split, e.g. entries before <cutover timestamp>
        # -> "kimi-k2.5", entries on/after that instant -> "kimi-k3.0".
        return "kimi-k2.5"

    def _build_entry(self, model: str, token_usage: Dict[str, Any], ts_ms: int, message_id: str) -> Dict[str, Any]:
        """Build a normalized entry from Kimi token usage."""
        input_other = self._i(token_usage.get("input_other"))
        output_t = self._i(token_usage.get("output"))
        cache_read = self._i(token_usage.get("input_cache_read"))
        cache_write = self._i(token_usage.get("input_cache_creation"))

        return {
            "source": self.source_name,
            "model": model or "kimi-k2.5",  # Default to kimi-k2.5 if unknown
            "provider": "moonshotai",
            "input": input_other,
            "output": output_t,
            "cacheRead": cache_read,
            "cacheWrite": cache_write,
            "reasoning": 0,  # Kimi doesn't expose reasoning separately
            "cost": self.pricing_db.get_cost(model or "kimi-k2.5", input_other, output_t, cache_read, cache_write),
            "timestamp": int(ts_ms),
            "message_id": message_id,  # For deduplication
            "entry_id": f"kimi:{message_id}",
        }

    def _file_signatures(self) -> tuple:
        sessions_dir = self.kimi_root / "sessions"
        pattern = str(sessions_dir / "*" / "*" / "wire.jsonl")
        return _timed_sigs(f"kimi:{self.kimi_root}", lambda: _glob_sigs(pattern))

    def _parse_all(self) -> List[Dict[str, Any]]:
        """Collect token usage from Kimi CLI session files."""
        out: List[Dict[str, Any]] = []
        seen_message_ids: set[str] = set()

        for path_str, _, _ in self._file_signatures():
            try:
                with open(path_str, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # Only process StatusUpdate messages with token_usage
                        msg = entry.get("message", {})
                        if msg.get("type") != "StatusUpdate":
                            continue

                        payload = msg.get("payload", {})
                        token_usage = payload.get("token_usage")
                        if not isinstance(token_usage, dict):
                            continue

                        # Deduplicate by message_id
                        message_id = payload.get("message_id", "")
                        if not message_id:
                            continue
                        if message_id in seen_message_ids:
                            continue
                        seen_message_ids.add(message_id)

                        # Parse timestamp
                        ts_raw = entry.get("timestamp")
                        if not ts_raw:
                            continue
                        try:
                            ts = datetime.fromtimestamp(float(ts_raw), timezone.utc)
                        except (ValueError, TypeError):
                            continue

                        model = self._default_model_for_timestamp(ts)

                        ts_ms = int(ts.timestamp() * 1000)
                        out.append(self._build_entry(model, token_usage, ts_ms, message_id))

            except Exception:
                continue

        return out


class PiAgentParser(BaseParser):
    """
    Parser for pi-agent session files.

    =======================================================================
    PI-AGENT SESSION FILE SCHEMA
    =======================================================================
    Location: ~/.pi/agent/sessions/<encoded-cwd>/<isoTime>_<sessionUUID>.jsonl
    Override: PI_AGENT_DIR env var — comma-separated list of root dirs.

    Each JSONL file contains one JSON object per line:
      - type="session"        — first line; ignored for token counting.
      - type="thinking_level_change" — ignored.
      - type="model_change"   — tracks current provider + modelId.
      - type="message"        — assistant messages with usage.

    Token-bearing rows: type="message" with message.role="assistant" and
    message.usage present. The outer "id" field (8-char hex) is the dedup key.

    Field mapping:
      source      <- "pi_agent"
      model       <- message.model (preferred) or last-seen model_change.modelId
      provider    <- message.provider or last-seen model_change.provider
      input       <- usage.input
      output      <- usage.output
      cacheRead   <- usage.cacheRead
      cacheWrite  <- usage.cacheWrite
      reasoning   <- 0 (not exposed)
      cost        <- usage.cost.total when present & > 0, else pricing DB
      timestamp   <- outer timestamp (ISO-8601 with Z) → epoch ms

    Dedup key: outer "id" (8-char hex).
    Totals fallback: if all breakdown tokens are zero but totalTokens > 0,
    attribute everything to output (matches ccusage apply_total_token_fallback).
    =======================================================================
    """

    source_name = "pi_agent"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        reason="Pi Agent JSONL rows have stable top-level IDs but are kept on full-file replacement until tail semantics are proven.",
    )

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.search_dirs = clientpaths.pi_agent_search_dirs()
        self.use_rglob = True

    @staticmethod
    def _infer_provider(model: str, fallback: str = "") -> str:
        m = (model or "").lower()
        if m.startswith("claude"):
            return "anthropic"
        if "gemini" in m:
            return "google"
        if m.startswith("gpt") or "codex" in m:
            return "openai"
        if "minimax" in m or m.startswith("m2.") or m.startswith("m1."):
            return "minimax"
        return fallback

    def _file_signatures(self) -> tuple:
        def scan() -> tuple:
            sigs: List[Tuple[str, int, int]] = []
            if self.use_rglob:
                for d in self.search_dirs:
                    for p_str, mt, sz in _rglob_sigs(d, "*.jsonl"):
                        sigs.append((p_str, mt, sz))
            else:
                for d in self.search_dirs:
                    pattern = str(d / "*" / "*.jsonl")
                    for p_str, mt, sz in _glob_sigs(pattern):
                        sigs.append((p_str, mt, sz))
            return tuple(sorted(sigs))

        cache_key = f"pi_agent:{','.join(str(d) for d in self.search_dirs)}"
        return _timed_sigs(cache_key, scan)

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        # Dedup by (session id, message id). Scoping on session id removes genuine
        # duplicates of a message (e.g. the same row re-logged across files on resume)
        # while avoiding dropping rows when Pi's 8-char hex message ids collide across
        # different sessions at scale — which would diverge from the session view.
        seen_ids: set = set()

        for path_str, _, _ in self._file_signatures():
            try:
                cur_model = ""
                cur_provider = ""
                cur_session_id = ""
                with open(path_str, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        msg_type = obj.get("type")

                        # Track the current session so dedup is scoped per session.
                        if msg_type == "session":
                            cur_session_id = str(obj.get("id") or cur_session_id)
                            continue

                        # Track model changes
                        if msg_type == "model_change":
                            cur_provider = obj.get("provider") or cur_provider
                            cur_model = obj.get("modelId") or cur_model
                            continue

                        if msg_type != "message":
                            continue

                        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                        if msg.get("role") != "assistant":
                            continue
                        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                        if not usage:
                            continue

                        # Dedup by (session id, outer id)
                        entry_id = obj.get("id")
                        if entry_id:
                            dedup_key = (cur_session_id, entry_id)
                            if dedup_key in seen_ids:
                                continue
                            seen_ids.add(dedup_key)

                        # Parse timestamp
                        ts_raw = obj.get("timestamp")
                        if not ts_raw:
                            continue
                        try:
                            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
                        except Exception:
                            continue

                        model = str(msg.get("model") or cur_model or "unknown")
                        provider = str(msg.get("provider") or cur_provider or self._infer_provider(model))

                        input_t = self._i(usage.get("input"))
                        output_t = self._i(usage.get("output"))
                        cache_r = self._i(usage.get("cacheRead"))
                        cache_w = self._i(usage.get("cacheWrite"))
                        total_t = self._i(usage.get("totalTokens"))

                        # Totals fallback: if all breakdowns are zero but totalTokens > 0,
                        # attribute everything to output (ccusage apply_total_token_fallback).
                        if input_t == 0 and output_t == 0 and cache_r == 0 and cache_w == 0 and total_t > 0:
                            output_t = total_t

                        # Skip truly empty rows
                        if input_t == 0 and output_t == 0 and cache_r == 0 and cache_w == 0:
                            continue

                        # Cost: prefer usage.cost.total when present and > 0
                        cost_obj = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
                        cost_total = float(cost_obj.get("total") or 0.0)
                        if cost_total > 0:
                            cost = cost_total
                        else:
                            cost = self.pricing_db.get_cost(model, input_t, output_t, cache_r, cache_w)

                        out.append({
                            "source": self.source_name,
                            "model": model,
                            "provider": provider,
                            "input": input_t,
                            "output": output_t,
                            "cacheRead": cache_r,
                            "cacheWrite": cache_w,
                            "reasoning": 0,
                            "cost": cost,
                            "timestamp": int(ts.timestamp() * 1000),
                            "entry_id": f"pi_agent:{entry_id}" if entry_id else "",
                        })
            except Exception:
                continue

        return out


class CopilotCLIParser(BaseParser):
    """
    Parser for GitHub Copilot CLI token usage.

    =======================================================================
    GITHUB COPILOT CLI — TWO DATA SOURCES
    =======================================================================

    SOURCE A (preferred): OTel JSONL exporter
    Location: ~/.copilot/otel/*.jsonl
              AND the file at COPILOT_OTEL_FILE_EXPORTER_PATH (single file).
    Note: OTel is opt-in; files may not exist.  Fall through silently.

    Four candidate record types (priority high → low):
      1. ChatSpan           — span with gen_ai.operation.name="chat" or name starts with "chat "
      2. InferenceLog       — non-span with event.name="gen_ai.client.inference.operation.details"
      3. AgentTurnLog       — non-span with event.name="copilot_chat.agent.turn"
      4. AgentSummarySpan   — span with gen_ai.operation.name="invoke_agent"

    Dedup: OTel-seen traceIds / response_ids prevent double-counting when
    multiple candidate types cover the same inference call.

    SOURCE B (fallback): events.jsonl
    Location: ~/.copilot/session-state/*/events.jsonl
    Contains type="assistant.message" records with outputTokens only.
    Events whose requestId/messageId appear in the OTel set are suppressed
    to avoid double-counting.  When in doubt, prefer suppression over
    double-counting inclusion.
    =======================================================================
    """

    source_name = "copilot_cli"
    sync_capability = SourceSyncCapability(
        mode="source_replace",
        reason="OTel rows can suppress fallback events across files, so cross-file precedence must be preserved.",
    )

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.otel_dir = clientpaths.copilot_otel_dir()
        self.events_glob = clientpaths.copilot_events_glob()

    @staticmethod
    def _infer_provider(model: str) -> str:
        m = (model or "").lower()
        if m.startswith("claude"):
            return "anthropic"
        if m.startswith("gemini"):
            return "google"
        if m.startswith("gpt") or re.match(r"^o\d", m) or "chatgpt" in m:
            return "openai"
        return "copilot"

    def _file_signatures(self) -> tuple:
        def scan() -> tuple:
            sigs = list(_rglob_sigs(self.otel_dir, "*.jsonl"))
            otel_env = clientpaths.copilot_otel_exporter_path()
            if otel_env:
                try:
                    s = os.stat(otel_env)
                    sigs.append((otel_env, int(s.st_mtime_ns), int(s.st_size)))
                except (FileNotFoundError, OSError):
                    pass
            sigs.extend(_glob_sigs(self.events_glob))
            return tuple(sorted(sigs))

        return _timed_sigs(f"copilot_cli:{self.otel_dir}", scan)

    @staticmethod
    def _is_span(record: Dict[str, Any]) -> bool:
        if record.get("type") == "span":
            return True
        span_fields = {"spanId", "traceId", "startTime", "endTime", "duration", "kind"}
        return bool(record.get("name")) and bool(span_fields & set(record.keys()))

    @staticmethod
    def _attrs(record: Dict[str, Any]) -> Dict[str, Any]:
        a = record.get("attributes")
        return a if isinstance(a, dict) else {}

    @staticmethod
    def _first_nonzero(*values) -> int:
        for v in values:
            iv = int(v or 0)
            if iv:
                return iv
        return 0

    @staticmethod
    def _parse_otel_timestamp(record: Dict[str, Any], file_mtime: float) -> int:
        """Parse OTel timestamp into epoch ms. Falls back to file mtime."""
        # Try 2-element array [seconds, nanos] forms
        for key in ("endTime", "startTime", "hrTime", "_hrTime"):
            v = record.get(key)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                try:
                    return int(int(v[0]) * 1000 + int(v[1]) // 1_000_000)
                except Exception:
                    pass

        # Scalar forms: auto-scale based on magnitude.
        # Thresholds mirror ccusage's copilot::timestamp_from_scalar:
        #   >= 1e17 → nanoseconds   (current epoch ns ≈ 1.78e18)
        #   >= 1e14 → microseconds  (current epoch μs ≈ 1.78e15)
        #   >= 1e11 → milliseconds  (current epoch ms ≈ 1.78e12)
        #   else    → seconds       (current epoch  s ≈ 1.78e9)
        # The previous thresholds (>1e15, >1e12) misclassified real
        # millisecond values like 1748000010500 (~1.748e12) as μs,
        # divided them by 1000, and landed them in 1970.
        for key in ("time", "timestamp", "observedTimestamp"):
            v = record.get(key)
            if v is None:
                continue
            try:
                fv = float(v)
                if fv >= 1e17:           # nanoseconds → ms
                    return int(fv // 1_000_000)
                elif fv >= 1e14:         # microseconds → ms
                    return int(fv // 1000)
                elif fv >= 1e11:         # milliseconds (use as-is)
                    return int(fv)
                elif fv > 0:             # seconds → ms
                    return int(fv * 1000)
            except Exception:
                pass

        # timeUnixNano
        v = record.get("timeUnixNano")
        if v is not None:
            try:
                return int(int(v) // 1_000_000)
            except Exception:
                pass

        return int(file_mtime * 1000)

    def _parse_otel_tokens(self, attrs: Dict[str, Any]) -> Dict[str, int]:
        """Extract token counts from OTel span/log attributes."""
        raw_input = self._i(attrs.get("gen_ai.usage.input_tokens"))
        cache_r = self._i(attrs.get("gen_ai.usage.cache_read.input_tokens"))
        cache_w = self._first_nonzero(
            attrs.get("gen_ai.usage.cache_write.input_tokens"),
            attrs.get("gen_ai.usage.cache_creation.input_tokens"),
        )
        reasoning = self._first_nonzero(
            attrs.get("gen_ai.usage.reasoning.output_tokens"),
            attrs.get("gen_ai.usage.reasoning_tokens"),
        )
        output_t = self._i(attrs.get("gen_ai.usage.output_tokens"))
        # NB: gen_ai.usage.input_tokens INCLUDES cache_read; subtract to get fresh input.
        input_t = max(0, raw_input - cache_r)

        total_t = self._first_nonzero(
            attrs.get("gen_ai.usage.total_tokens"),
            attrs.get("gen_ai.usage.total.token_count"),
        )

        # Totals fallback when parts are missing
        if input_t == 0 and output_t == 0 and cache_r == 0 and cache_w == 0 and total_t > 0:
            output_t = total_t

        return {
            "input": input_t,
            "output": output_t,
            "cacheRead": cache_r,
            "cacheWrite": cache_w,
            "reasoning": reasoning,
        }

    @staticmethod
    def _get_session_id(attrs: Dict[str, Any], record: Dict[str, Any]) -> str:
        """Extract session ID using priority order from attributes."""
        for key in (
            "gen_ai.conversation.id",
            "copilot_chat.session_id",
            "copilot_chat.chat_session_id",
            "session.id",
            "github.copilot.interaction_id",
            "gen_ai.response.id",
        ):
            v = attrs.get(key)
            if v:
                return str(v)
        trace_id = record.get("traceId")
        if trace_id:
            return str(trace_id)
        return "unknown-session"

    @staticmethod
    def _get_model(attrs: Dict[str, Any]) -> str:
        m = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model")
        return str(m) if m else ""

    def _parse_otel_files(self, otel_paths: List[str]) -> List[Dict[str, Any]]:
        """Parse all OTel JSONL files and return deduplicated entries."""
        # Collect records into four candidate buckets
        chat_spans: List[Dict[str, Any]] = []
        inference_logs: List[Dict[str, Any]] = []
        agent_turn_logs: List[Dict[str, Any]] = []
        agent_summary_spans: List[Dict[str, Any]] = []

        for path_str in otel_paths:
            try:
                file_mtime = os.stat(path_str).st_mtime
            except OSError:
                file_mtime = 0.0
            try:
                with open(path_str, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(rec, dict):
                            continue

                        rec["_file_mtime"] = file_mtime
                        attrs = self._attrs(rec)
                        is_span = self._is_span(rec)
                        op_name = attrs.get("gen_ai.operation.name", "")
                        rec_name = str(rec.get("name") or "")
                        event_name = attrs.get("event.name", "")
                        body = str(rec.get("body") or "")

                        if is_span and (op_name == "chat" or rec_name.startswith("chat ")):
                            chat_spans.append(rec)
                        elif not is_span and (
                            event_name == "gen_ai.client.inference.operation.details"
                            or body.startswith("GenAI inference:")
                        ):
                            inference_logs.append(rec)
                        elif not is_span and (
                            event_name == "copilot_chat.agent.turn"
                            or body.startswith("copilot_chat.agent.turn")
                        ):
                            agent_turn_logs.append(rec)
                        elif is_span and (op_name == "invoke_agent" or rec_name.startswith("invoke_agent ")):
                            agent_summary_spans.append(rec)
            except Exception:
                continue

        out: List[Dict[str, Any]] = []
        seen_trace_ids: set = set()
        seen_response_ids: set = set()
        seen_dedup_keys: set = set()  # for cross-source dedup

        def _extract_ids(rec: Dict[str, Any]):
            attrs = self._attrs(rec)
            trace_id = rec.get("traceId") or ""
            resp_id = attrs.get("gen_ai.response.id") or ""
            return str(trace_id), str(resp_id)

        def _emit(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            attrs = self._attrs(rec)
            tokens = self._parse_otel_tokens(attrs)
            if all(v == 0 for v in tokens.values()):
                return None
            model = self._get_model(attrs)
            if not model:
                # Try to resolve model from attrs keys
                for k, v in attrs.items():
                    if "model" in k and v:
                        model = str(v)
                        break
            file_mtime = rec.pop("_file_mtime", 0.0)
            ts_ms = self._parse_otel_timestamp(rec, file_mtime)
            provider = self._infer_provider(model)
            cost = self.pricing_db.get_cost(model, tokens["input"], tokens["output"], tokens["cacheRead"], tokens["cacheWrite"])
            return {
                "source": self.source_name,
                "model": model or "unknown",
                "provider": provider,
                "input": tokens["input"],
                "output": tokens["output"],
                "cacheRead": tokens["cacheRead"],
                "cacheWrite": tokens["cacheWrite"],
                "reasoning": tokens["reasoning"],
                "cost": cost,
                "timestamp": ts_ms,
                "entry_id": str(attrs.get("gen_ai.response.id") or rec.get("traceId") or ""),
            }

        # ChatSpan: always emit
        for rec in chat_spans:
            entry = _emit(rec)
            if entry:
                out.append(entry)
                trace_id, resp_id = _extract_ids(rec)
                if trace_id:
                    seen_trace_ids.add(trace_id)
                if resp_id:
                    seen_response_ids.add(resp_id)

        # InferenceLog: emit only if not already seen
        for rec in inference_logs:
            trace_id, resp_id = _extract_ids(rec)
            if (trace_id and trace_id in seen_trace_ids) or (resp_id and resp_id in seen_response_ids):
                continue
            entry = _emit(rec)
            if entry:
                out.append(entry)
                if trace_id:
                    seen_trace_ids.add(trace_id)
                if resp_id:
                    seen_response_ids.add(resp_id)

        # AgentTurnLog: emit only if not already seen
        for rec in agent_turn_logs:
            trace_id, resp_id = _extract_ids(rec)
            if (trace_id and trace_id in seen_trace_ids) or (resp_id and resp_id in seen_response_ids):
                continue
            entry = _emit(rec)
            if entry:
                out.append(entry)
                if trace_id:
                    seen_trace_ids.add(trace_id)
                if resp_id:
                    seen_response_ids.add(resp_id)

        # AgentSummarySpan: emit only if not already seen
        for rec in agent_summary_spans:
            trace_id, resp_id = _extract_ids(rec)
            if (trace_id and trace_id in seen_trace_ids) or (resp_id and resp_id in seen_response_ids):
                continue
            entry = _emit(rec)
            if entry:
                out.append(entry)
                if trace_id:
                    seen_trace_ids.add(trace_id)
                if resp_id:
                    seen_response_ids.add(resp_id)

        # Record all OTel response IDs for cross-source dedup with events.jsonl
        for rec in chat_spans + inference_logs + agent_turn_logs + agent_summary_spans:
            _, resp_id = _extract_ids(rec)
            if resp_id:
                seen_dedup_keys.add(resp_id)

        # Attach seen_dedup_keys as an attribute for use by the caller
        # We encode this into the return list via a sentinel; simpler: return alongside.
        # Actually we'll store it on self for use in _parse_all.
        self._otel_seen_keys = seen_dedup_keys  # type: ignore[attr-defined]
        return out

    def _parse_all(self) -> List[Dict[str, Any]]:
        # Collect OTel paths
        otel_paths: List[str] = []
        for path_str, _, _ in _rglob_sigs(self.otel_dir, "*.jsonl"):
            otel_paths.append(path_str)
        otel_env = clientpaths.copilot_otel_exporter_path()
        if otel_env and otel_env not in otel_paths:
            if os.path.isfile(otel_env):
                otel_paths.append(otel_env)

        self._otel_seen_keys: set = set()  # type: ignore[attr-defined]
        out: List[Dict[str, Any]] = []

        if otel_paths:
            out.extend(self._parse_otel_files(otel_paths))

        # SOURCE B: events.jsonl fallback (output-tokens only).
        # OTel entries take precedence: suppress any events.jsonl entry whose
        # requestId or messageId was already seen in the OTel pass.
        # When in doubt, prefer suppression to avoid double-counting.
        otel_seen = getattr(self, "_otel_seen_keys", set())
        seen_event_ids: set = set()

        for path_str, _, _ in _glob_sigs(self.events_glob):
            try:
                file_mtime = os.stat(path_str).st_mtime
            except OSError:
                file_mtime = 0.0
            try:
                with open(path_str, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(obj, dict):
                            continue
                        if obj.get("type") != "assistant.message":
                            continue

                        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
                        msg_id = data.get("messageId") or ""
                        request_id = data.get("requestId") or ""

                        # Suppress if already covered by OTel data
                        if msg_id in otel_seen or request_id in otel_seen:
                            continue
                        dedup_key = msg_id or request_id
                        if dedup_key and dedup_key in seen_event_ids:
                            continue
                        if dedup_key:
                            seen_event_ids.add(dedup_key)

                        output_t = self._i(data.get("outputTokens"))
                        if output_t == 0:
                            continue

                        model = str(data.get("model") or "unknown")

                        ts_raw = obj.get("timestamp")
                        if ts_raw:
                            try:
                                ts_ms = int(
                                    datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                                    .astimezone(timezone.utc)
                                    .timestamp() * 1000
                                )
                            except Exception:
                                ts_ms = int(file_mtime * 1000)
                        else:
                            ts_ms = int(file_mtime * 1000)

                        out.append({
                            "source": self.source_name,
                            "model": model,
                            "provider": self._infer_provider(model),
                            "input": 0,
                            "output": output_t,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                            "reasoning": 0,
                            "cost": self.pricing_db.get_cost(model, 0, output_t, 0, 0),
                            "timestamp": ts_ms,
                            "entry_id": f"copilot_event:{dedup_key}" if dedup_key else "",
                        })
            except Exception:
                continue

        return out


class HermesParser(BaseParser):
    """
    Parser for Hermes agent session database.

    =======================================================================
    HERMES SESSION DATABASE SCHEMA
    =======================================================================
    Location: ~/.hermes/state.db (SQLite)
    Override: HERMES_HOME env var — comma-separated list of dirs.
              Each dir contributes its state.db if present.

    Query: SELECT id, model, billing_provider, started_at,
                  message_count, input_tokens, output_tokens,
                  cache_read_tokens, cache_write_tokens,
                  reasoning_tokens, estimated_cost_usd, actual_cost_usd
           FROM sessions
           WHERE model IS NOT NULL AND TRIM(model) != ''

    One entry per session row.  started_at is a Python float Unix timestamp
    in seconds; multiply by 1000 for epoch-ms (or treat as-is if > 1e12).

    Cost precedence:
      1. actual_cost_usd if positive
      2. estimated_cost_usd if positive
      3. pricing DB lookup via billing_provider/model, then bare model
    NOTE: a recorded zero (e.g. ChatGPT Plus subscription) is treated as
    "no cost recorded" and falls through to pricing-DB calc — it does NOT
    short-circuit.

    Dedup: by "id" across multiple state.db files.

    Skip rows where all tokens are 0 AND no recorded cost (positive).
    =======================================================================
    """

    source_name = "hermes"
    sync_capability = SourceSyncCapability(
        mode="source_replace",
        reason="Hermes is DB-backed; current safe cache unit is the whole source until DB-native incremental sync is added.",
    )

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.search_dirs = clientpaths.hermes_search_dirs()

    @staticmethod
    def _infer_provider(model: str) -> str:
        m = (model or "").lower()
        if m.startswith("claude"):
            return "anthropic"
        if "gemini" in m:
            return "google"
        if m.startswith("gpt") or re.match(r"^o\d", m) or "chatgpt" in m:
            return "openai"
        if "minimax" in m or m.startswith("m2.") or m.startswith("m1."):
            return "minimax"
        if "kimi" in m or "moonshot" in m:
            return "moonshotai"
        return ""

    def _db_paths(self) -> List[Path]:
        paths = []
        for d in self.search_dirs:
            p = d / "state.db"
            if p.exists():
                paths.append(p)
        return paths

    def _file_signatures(self) -> tuple:
        def scan() -> tuple:
            sigs: List[Tuple[str, int, int]] = []
            for p in self._db_paths():
                try:
                    s = p.stat()
                    sigs.append((str(p), s.st_mtime_ns, s.st_size))
                except (FileNotFoundError, OSError):
                    pass
            return tuple(sorted(sigs))

        cache_key = f"hermes:{','.join(str(d) for d in self.search_dirs)}"
        return _timed_sigs(cache_key, scan)

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen_ids: set = set()

        for db_path in self._db_paths():
            try:
                conn = sqlite3.connect(str(db_path))
                cur = conn.cursor()
                try:
                    cur.execute(
                        """
                        SELECT id, model, billing_provider, started_at,
                               message_count, input_tokens, output_tokens,
                               cache_read_tokens, cache_write_tokens,
                               reasoning_tokens, estimated_cost_usd, actual_cost_usd
                        FROM sessions
                        WHERE model IS NOT NULL AND TRIM(model) != ''
                        """
                    )
                    rows = cur.fetchall()
                except Exception:
                    conn.close()
                    continue
                conn.close()

                for row in rows:
                    try:
                        (
                            row_id, model, billing_provider, started_at,
                            message_count, input_t, output_t,
                            cache_r, cache_w, reasoning,
                            estimated_cost, actual_cost,
                        ) = row

                        # Dedup across multiple state.db files
                        if row_id in seen_ids:
                            continue
                        seen_ids.add(row_id)

                        input_t = self._i(input_t)
                        output_t = self._i(output_t)
                        cache_r = self._i(cache_r)
                        cache_w = self._i(cache_w)
                        reasoning = self._i(reasoning)

                        actual_cost_f = float(actual_cost or 0.0)
                        estimated_cost_f = float(estimated_cost or 0.0)

                        # Skip rows with no tokens AND no recorded cost
                        has_tokens = (input_t + output_t + cache_r + cache_w + reasoning) > 0
                        has_cost = actual_cost_f > 0 or estimated_cost_f > 0
                        if not has_tokens and not has_cost:
                            continue

                        # Timestamp: started_at is seconds; if > 1e12 already in ms.
                        try:
                            sa = float(started_at or 0.0)
                        except (ValueError, TypeError):
                            sa = 0.0
                        ts_ms = int(sa * 1000) if sa < 1e12 else int(sa)

                        # Cost precedence: actual > estimated > pricing DB.
                        # A recorded zero is NOT treated as a real zero — fall through.
                        provider = str(billing_provider or "").strip() or self._infer_provider(str(model or ""))
                        if actual_cost_f > 0:
                            cost = actual_cost_f
                        elif estimated_cost_f > 0:
                            cost = estimated_cost_f
                        else:
                            # Try provider/model first, then bare model
                            provider_model = f"{provider}/{model}" if provider else str(model or "")
                            cost = self.pricing_db.get_cost(provider_model, input_t, output_t, cache_r, cache_w)
                            if cost == 0.0 and provider:
                                cost = self.pricing_db.get_cost(str(model or ""), input_t, output_t, cache_r, cache_w)

                        out.append({
                            "source": self.source_name,
                            "model": str(model or "unknown"),
                            "provider": provider,
                            "input": input_t,
                            "output": output_t,
                            "cacheRead": cache_r,
                            "cacheWrite": cache_w,
                            "reasoning": reasoning,
                            "cost": cost,
                            "timestamp": ts_ms,
                            # Hermes rows are session-level aggregates: one
                            # entry represents N messages. Propagate the count
                            # so compute.py credits sessions correctly instead
                            # of treating each row as a single message.
                            "messageCount": int(self._i(message_count)),
                            "entry_id": f"hermes:{row_id}",
                        })
                    except Exception:
                        continue
            except Exception:
                continue

        return out


class MimoParser(BaseParser):
    """
    Parser for Mimocode / Mimo token usage.

    =======================================================================
    MIMO SQLite DATABASE SCHEMA
    =======================================================================
    Location: ~/.local/share/mimocode/mimocode.db

    Table: message
      - id TEXT
      - session_id TEXT
      - time_created INTEGER  (epoch ms)
      - time_updated INTEGER  (epoch ms)
      - data TEXT             (JSON blob)

    The data JSON for assistant messages contains:
      - role: "assistant"
      - cost: float (direct cost when available)
      - tokens:
          - input: int
          - output: int
          - reasoning: int
          - cache:
              - read: int
              - write: int
      - modelID: str
      - providerID: str
      - time.created: int (epoch ms)
      - time.completed: int (epoch ms)

    Field mapping to normalized entry:
      source    <- "mimo"
      model     <- data.modelID
      provider  <- data.providerID
      input     <- data.tokens.input
      output    <- data.tokens.output
      cacheRead <- data.tokens.cache.read
      cacheWrite<- data.tokens.cache.write
      reasoning <- data.tokens.reasoning
      cost      <- data.cost when > 0, else pricing DB lookup
      timestamp <- time_created (column, epoch ms)

    Dedup: message.id (text primary key).
    =======================================================================
    """

    source_name = "mimo"
    sync_capability = SourceSyncCapability(
        mode="source_native_db",
        session_store=False,
        reason="Mimo is an OpenCode-shaped SQLite DB and supports SQL date windows.",
    )

    _query_cache: ClassVar[Dict[tuple, List[Dict[str, Any]]]] = {}
    _query_cache_sig: ClassVar[tuple] = ()

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.db_path = clientpaths.mimocode_db_path()

    def _build_entry(self, data: Dict[str, Any], ts_ms: int) -> Dict[str, Any]:
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        input_t = self._i(tokens.get("input"))
        output_t = self._i(tokens.get("output"))
        cache_r = self._i(cache.get("read"))
        cache_w = self._i(cache.get("write"))
        reasoning = self._i(tokens.get("reasoning"))
        model = str(data.get("modelID") or "unknown")
        provider = str(data.get("providerID") or "")

        # Prefer direct cost from the data when available.
        try:
            data_cost = float(data.get("cost") or 0.0)
        except (TypeError, ValueError):
            data_cost = 0.0
        if data_cost > 0:
            cost = data_cost
        else:
            cost = self.pricing_db.get_cost(model, input_t, output_t, cache_r, cache_w)

        return {
            "source": self.source_name,
            "model": model,
            "provider": provider,
            "input": input_t,
            "output": output_t,
            "cacheRead": cache_r,
            "cacheWrite": cache_w,
            "reasoning": reasoning,
            "cost": cost,
            "timestamp": int(ts_ms),
        }

    def _file_signatures(self) -> tuple:
        if not self.db_path.exists():
            return ()
        out: list[tuple[str, int, int]] = []
        for candidate in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            try:
                s = candidate.stat()
                out.append((str(candidate), s.st_mtime_ns, s.st_size))
            except (FileNotFoundError, OSError):
                continue
        return tuple(out)

    def _parse_all(self) -> List[Dict[str, Any]]:
        return []

    def collect(self, since_date: Optional[datetime] = None, until_date: Optional[datetime] = None) -> List[Dict[str, Any]]:
        sig = (self._file_signatures(), self._pricing_signature())
        if sig != type(self)._query_cache_sig:
            type(self)._query_cache.clear()
            type(self)._query_cache_sig = sig

        s_ms = int(self._to_utc(since_date).timestamp() * 1000) if since_date else 0
        u_ms = int(self._to_utc(until_date).timestamp() * 1000) if until_date else 9999999999999
        cache_key = (s_ms, u_ms)

        cached = type(self)._query_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        out: List[Dict[str, Any]] = []
        if self.db_path.exists():
            try:
                conn = sqlite3.connect(str(self.db_path))
                try:
                    cur = conn.cursor()
                    imported_ids = _mimo_imported_message_ids(conn)
                    cur.execute(
                        """
                        SELECT id, data, time_created
                        FROM message
                        WHERE time_created >= ? AND time_created < ?
                        ORDER BY time_created
                        """,
                        (s_ms, u_ms),
                    )
                    rows = cur.fetchall()
                finally:
                    conn.close()
                for msg_id, data_json, ts_ms in rows:
                    try:
                        if str(msg_id) in imported_ids:
                            continue
                        data = json.loads(data_json)
                        if data.get("role") != "assistant":
                            continue
                        tokens = data.get("tokens")
                        if not isinstance(tokens, dict):
                            continue
                        entry = self._build_entry(data, self._i(ts_ms))
                        entry["entry_id"] = f"mimo:{msg_id}"
                        out.append(entry)
                    except Exception:
                        continue
            except Exception:
                pass

        if len(type(self)._query_cache) >= _OPENCODE_QUERY_CACHE_MAX:
            type(self)._query_cache.clear()
        type(self)._query_cache[cache_key] = out
        return list(out)


class CodingToolsUsageTracker:
    """Registry-driven tracker for coding clients."""

    # From `tokscale --help`: OpenCode, Claude Code, Codex, Gemini, Amp, Kimi.
    # TODO: Amp parser is currently a placeholder until we have stable local fixtures
    # with explicit token fields.

    def __init__(self):
        self.entries: List[Dict[str, Any]] = []
        self.pricing_db = PricingDatabase()
        self.parsers = {
            "opencode": OpenCodeParser(self.pricing_db),
            "codex": CodexParser(self.pricing_db),
            "claude": ClaudeParser(self.pricing_db),
            "gemini_cli": GeminiCLIParser(self.pricing_db),
            "antigravity_cli": AntigravityCLIParser(self.pricing_db),
            "amp": AmpParser(self.pricing_db),
            "kimi": KimiParser(self.pricing_db),
            "pi_agent": PiAgentParser(self.pricing_db),
            "copilot_cli": CopilotCLIParser(self.pricing_db),
            "hermes": HermesParser(self.pricing_db),
            "mimo": MimoParser(self.pricing_db),
        }

    def collect(self, since_date: Optional[datetime] = None, until_date: Optional[datetime] = None, sources: Optional[List[str]] = None):
        self.entries = []
        selected = sources or list(self.parsers.keys())
        for name in selected:
            parser = self.parsers.get(name)
            if parser:
                self.entries.extend(parser.collect(since_date, until_date))

    def to_json(self) -> Dict[str, Any]:
        return {"entries": self.entries, "total": len(self.entries)}


def _date_range(args: argparse.Namespace) -> Tuple[Optional[datetime], Optional[datetime]]:
    if args.today:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    until = (datetime.strptime(args.until, "%Y-%m-%d") + timedelta(days=1)) if args.until else None
    return since, until


def main():
    parser = argparse.ArgumentParser(description="Coding tools token usage tracker")
    parser.add_argument("--today", action="store_true")
    parser.add_argument("--since", type=str)
    parser.add_argument("--until", type=str)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--sources", type=str, default="opencode,codex,claude,gemini_cli,antigravity_cli,amp,kimi,pi_agent,copilot_cli,hermes,mimo")
    args = parser.parse_args()

    since_date, until_date = _date_range(args)
    sources = [s.strip() for s in (args.sources or "").split(",") if s.strip()]

    tracker = CodingToolsUsageTracker()
    tracker.collect(since_date, until_date, sources)

    if args.json:
        print(json.dumps(tracker.to_json(), indent=2))
    else:
        print(f"Total entries: {len(tracker.entries)}")


if __name__ == "__main__":
    main()
