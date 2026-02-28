"""Coding tools token usage parsers.

These parsers emit tokscale-compatible `entries[]` rows and are used by
`tokdash.compute` when running with the local parsers backend.
"""

import argparse
import glob
import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


try:
    from ..pricing import PricingDatabase
except ImportError:  # pragma: no cover
    # Allow running as a script by file path.
    from pricing import PricingDatabase


class BaseParser(ABC):
    source_name: str

    def __init__(self, pricing_db: PricingDatabase):
        self.pricing_db = pricing_db

    @abstractmethod
    def collect(self, since_date: Optional[datetime], until_date: Optional[datetime]) -> List[Dict[str, Any]]:
        raise NotImplementedError

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

    def collect(self, since_date: Optional[datetime], until_date: Optional[datetime]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        # IMPORTANT: Only use SQLite DB to avoid double-counting!
        # File storage (~/.local/share/opencode/storage/message) contains the SAME messages as the DB.
        # Using both sources would result in 100% duplication.
        # See: patchFixSetup/09-fixes/OpenCode_Double_Counting_Fix.md

        if self.db_path.exists():
            s_ms = int(self._to_utc(since_date).timestamp() * 1000) if since_date else 0
            u_ms = int(self._to_utc(until_date).timestamp() * 1000) if until_date else 9999999999999
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

        return out


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

    def collect(self, since_date: Optional[datetime], until_date: Optional[datetime]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.sessions_dir.exists():
            return out

        for session_file in self.sessions_dir.rglob("*.jsonl"):
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
                    
                    # Only include entries within the date range
                    if not self._in_range(ts, since_date, until_date):
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

    def collect(self, since_date: Optional[datetime], until_date: Optional[datetime]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.projects_dir.exists():
            return out

        # Track seen message IDs to avoid duplicates
        # Claude Code writes the same API message multiple times (for different content chunks)
        seen_message_ids = set()

        for session_file in self.projects_dir.rglob("*.jsonl"):
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
                    if not self._in_range(ts, since_date, until_date):
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

    def collect(self, since_date: Optional[datetime], until_date: Optional[datetime]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen_ids = set()
        # primary location: ~/.gemini/tmp/*/chats/session-*.json
        pattern = self.gemini_root / "tmp" / "*" / "chats" / "session-*.json"
        for file_path in glob.glob(str(pattern)):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
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
                    if not self._in_range(ts, since_date, until_date):
                        continue
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

    def collect(self, since_date: Optional[datetime], until_date: Optional[datetime]) -> List[Dict[str, Any]]:
        # TODO(coding_tools): Amp parser placeholder.
        # Keep fail-soft until we have schema + fixtures.
        return []


class CodingToolsUsageTracker:
    """Registry-driven tracker for coding clients."""

    # From `tokscale --help`: OpenCode, Claude Code, Codex, Gemini, Amp.
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
    parser.add_argument("--sources", type=str, default="opencode,codex,claude,gemini_cli,amp")
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
