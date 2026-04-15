"""Coding tools token usage parsers.

These parsers emit tokscale-compatible `entries[]` rows and are used by
`tokdash.compute` when running with the local parsers backend.
"""

import argparse
import glob
import json
import os
import sqlite3
import time as _time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Tuple


try:
    from ..pricing import PricingDatabase
except ImportError:  # pragma: no cover
    # Allow running as a script by file path.
    from pricing import PricingDatabase


# ---------------------------------------------------------------------------
# File-signature caching – avoids repeated rglob / glob.glob + stat() calls
# when multiple API requests arrive within a short window.
# ---------------------------------------------------------------------------
_sig_cache: Dict[str, Tuple[float, tuple]] = {}
_SIG_TTL = float(os.environ.get("TOKDASH_SIG_TTL", "5.0"))  # seconds; 0 to disable
_OPENCODE_QUERY_CACHE_MAX = 32  # max date-range entries before eviction


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


class BaseParser(ABC):
    source_name: str

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
        """Signature of pricing_db.json so cached costs are invalidated on update."""
        try:
            s = self.pricing_db.db_path.stat()
            return (s.st_mtime_ns, s.st_size)
        except (FileNotFoundError, OSError, AttributeError):
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

    # Per-query cache: {(s_ms, u_ms): [entries]}, invalidated when DB or pricing changes.
    # Bounded to _OPENCODE_QUERY_CACHE_MAX entries to prevent unbounded growth.
    _query_cache: ClassVar[Dict[tuple, List[Dict[str, Any]]]] = {}
    _query_cache_sig: ClassVar[tuple] = ()

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.messages_dir = Path.home() / ".local/share/opencode/storage/message"
        self.db_path = Path.home() / ".local/share/opencode/opencode.db"

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
        try:
            s = self.db_path.stat()
            return ((str(self.db_path), s.st_mtime_ns, s.st_size),)
        except (FileNotFoundError, OSError):
            return ()

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

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.sessions_dir = Path.home() / ".codex/sessions"

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

                for line in session_file.read_text(encoding="utf-8").splitlines():
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
                        }
                    )
            except Exception:
                continue

        return out


class ClaudeParser(BaseParser):
    source_name = "claude"

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.projects_dir = Path.home() / ".claude/projects"

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
        return _timed_sigs(f"claude:{self.projects_dir}", lambda: _rglob_sigs(self.projects_dir))

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        # Track seen message IDs to avoid duplicates
        # Claude Code writes the same API message multiple times (for different content chunks)
        seen_message_ids = set()

        for path_str, _, _ in self._file_signatures():
            session_file = Path(path_str)
            try:
                for line in session_file.read_text(encoding="utf-8").splitlines():
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                    if msg.get("role") != "assistant":
                        continue
                    usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                    if not usage:
                        continue

                    # Deduplicate by message.id (API message ID)
                    msg_id = msg.get("id")
                    if msg_id in seen_message_ids:
                        continue
                    if msg_id:
                        seen_message_ids.add(msg_id)

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

                    model = str(msg.get("model") or "unknown")
                    out.append(
                        {
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
                        }
                    )
            except Exception:
                continue

        return out


class GeminiCLIParser(BaseParser):
    """
    Parser for Gemini CLI session files.

    ========================================================================
    GEMINI CLI SESSION FILE SCHEMA (fixture-friendly notes)
    ========================================================================
    Location: ~/.gemini/tmp/<projectHash>/chats/session-*.json

    Top-level fields:
      - sessionId: UUID string
      - projectHash: SHA256-like hex string (per-project hash)
      - startTime: ISO 8601 timestamp (e.g., "2026-01-03T12:02:18.267Z")
      - lastUpdated: ISO 8601 timestamp
      - messages: array of message objects

    Message object schema (type="gemini" only has tokens):
      - id: UUID string (unique per message, use for dedup)
      - timestamp: ISO 8601 string
      - type: "user" | "gemini" | "info" | "error"
      - content: string (for user/gemini messages)
      - model: string (e.g., "gemini-3-flash-preview")
      - tokens: object (only present for type="gemini")
          - input: int (prompt tokens)
          - output: int (completion tokens)
          - cached: int (cache read tokens) -> maps to cacheRead
          - thoughts: int (reasoning tokens) -> maps to reasoning
          - tool: int (tool call tokens) -> currently ignored per spec
          - total: int (sum of above, for validation)

    Field mapping to normalized entry:
      source <- "gemini_cli"
      provider <- "google"
      input <- tokens.input
      output <- tokens.output
      cacheRead <- tokens.cached
      reasoning <- tokens.thoughts
      cacheWrite <- 0 (not exposed in current schema)
      timestamp <- ISO timestamp converted to epoch ms

    Dedup key: message.id (UUID, unique per response)

    Known schema versions: 2025-07 to present
    Last verified: 2026-02-15

    FUTURE DATA-SHAPE UPDATES:
    - If token field names change, add fallback aliases in _build_entry()
    - If new token types are added, map to existing fields or add new
    - If session file location changes, update glob pattern in collect()
    ========================================================================
    """

    source_name = "gemini_cli"

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.gemini_root = Path.home() / ".gemini"

    def _build_entry(self, model: str, tokens: Dict[str, Any], ts_ms: int) -> Dict[str, Any]:
        input_t = self._i(tokens.get("input"))
        output_t = self._i(tokens.get("output"))
        cache_r = self._i(tokens.get("cached"))
        cache_w = 0  # cache_write not present in Gemini CLI tokens
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
        pattern = str(self.gemini_root / "tmp" / "*" / "chats" / "session-*.json")
        return _timed_sigs(f"gemini:{self.gemini_root}", lambda: _glob_sigs(pattern))

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen_ids = set()
        for path_str, _, _ in self._file_signatures():
            try:
                with open(path_str, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            messages = data.get("messages")
            if not isinstance(messages, list):
                continue
            for msg in messages:
                try:
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
                    out.append(self._build_entry(model, tokens, ts_ms))
                except Exception:
                    continue
        return out


class AmpParser(BaseParser):
    source_name = "amp"

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.amp_root = Path.home() / ".amp"

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

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        kimi_share_dir = os.environ.get("KIMI_SHARE_DIR", "").strip()
        self.kimi_root = Path(kimi_share_dir).expanduser() if kimi_share_dir else (Path.home() / ".kimi")

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
            "amp": AmpParser(self.pricing_db),
            "kimi": KimiParser(self.pricing_db),
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
    parser.add_argument("--sources", type=str, default="opencode,codex,claude,gemini_cli,amp,kimi")
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
