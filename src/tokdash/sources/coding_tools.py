"""Coding tools token usage parsers.

These parsers emit tokscale-compatible `entries[]` rows and are used by
`tokdash.compute` when running with the local parsers backend.
"""

import argparse
import glob
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time as _time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Dict, Iterator, List, Optional, Tuple


try:
    from .. import clientpaths
    from ..pricing import PricingDatabase
    from ..usage_store import (
        USAGE_ENTRY_FORMAT_VERSION,
        usage_billing_fixed,
        usage_billing_pricing,
        usage_entry_cost,
    )
    from . import dsh_log as dsh_log_module
    from .dsh_log import decode_dsh_session_file, dsh_entry_id, dsh_file_signatures, fold_dsh_usage_samples
except ImportError:  # pragma: no cover
    # Allow running as a script by file path.
    import clientpaths
    from pricing import PricingDatabase
    from usage_store import (
        USAGE_ENTRY_FORMAT_VERSION,
        usage_billing_fixed,
        usage_billing_pricing,
        usage_entry_cost,
    )
    import dsh_log as dsh_log_module
    from dsh_log import decode_dsh_session_file, dsh_entry_id, dsh_file_signatures, fold_dsh_usage_samples

logger = logging.getLogger(__name__)


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
    # True when one entry_key can legitimately occur in several files of the
    # same source (Copied stable keys: Codex rollout resumption, Cline fork).
    # The store syncs such sources so that the earliest timestamp owns the
    # key, and re-parses surviving files when the owner is removed or
    # rewritten. See UsageEntryStore.sync_files.
    cross_file_stable_keys: bool = False
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
    try:
        for p in root.rglob(pattern):
            try:
                s = p.stat()
                items.append((str(p), s.st_mtime_ns, s.st_size))
            except (FileNotFoundError, OSError):
                continue
    except OSError:
        # pathlib's recursive selector only swallows PermissionError; a different
        # OSError from a mid-walk is_dir()/scandir (e.g. a cloud-file placeholder on
        # Windows) would otherwise propagate and blank the whole source. Keep the
        # partial walk, matching the PermissionError behavior.
        pass
    return tuple(sorted(items))


def connect_sqlite_readonly(path: Path) -> sqlite3.Connection:
    """Open a live third-party database read-only, with a read-write fallback.

    A plain RW connect takes a write lock that on native Windows can block the
    owning client's own writes for the duration of our read (and would create the
    file if it were missing). Shared by the usage parsers in this module and the
    session loaders in sessions.py.
    """
    try:
        return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except Exception:
        return sqlite3.connect(str(path))


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


def codex_token_event_key(session_id: Any, info: Any) -> Optional[str]:
    """Return a stable identity for a Codex token-count event.

    Codex can copy a session's history into a later rollout file when the user
    resumes a thread. Those copies keep the logical session id and the complete
    cumulative/per-call usage snapshots, but restamp every event with the resume
    time. Hashing the stable usage state lets both live parsing and the persistent
    store reject the copy without relying on timestamps, file paths, token sizes,
    or a particular resume/subagent source shape.

    Older/partial logs that omit ``total_token_usage`` deliberately return None.
    Falling back to the file/line identity may over-count a replay, but it cannot
    silently merge two genuine calls that merely used the same number of tokens.
    """
    sid = str(session_id or "").strip()
    if not sid or not isinstance(info, dict):
        return None
    total = info.get("total_token_usage")
    last = info.get("last_token_usage")
    if not isinstance(total, dict) or not total or not isinstance(last, dict) or not last:
        return None

    fields = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )

    def normalized(snapshot: dict[str, Any]) -> dict[str, int]:
        values: dict[str, int] = {}
        for field in fields:
            value = snapshot.get(field, 0)
            if isinstance(value, bool):
                raise ValueError("boolean token count")
            values[field] = int(value or 0)
        return values

    try:
        canonical = json.dumps(
            {
                "version": 1,
                "session_id": sid,
                # Codex 0.146 can add explicit zero-valued fields while replaying
                # snapshots written by an older CLI. Missing and zero are the same
                # usage state, so normalize the fields the parser actually counts.
                "total_token_usage": normalized(total),
                "last_token_usage": normalized(last),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return f"codex-token-v1:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


# Placeholder resolved for rows written before a file's first model signal (a
# fork's replayed parent prefix, or old formats without turn_context). Rows
# carry a ``_model_placeholder`` marker until then so backfill can tell "no
# signal yet" apart from an explicit user selection of this same model — the
# name is real, so the value alone must never be the sentinel.
CODEX_DEFAULT_MODEL = "gpt-5.3-codex"


def codex_replay_key_session_id(
    is_fork_file: bool,
    own_session_id: Any,
    current_session_id: Any,
    subagent_parent_id: Any,
    saw_turn_context: bool,
) -> Tuple[Any, bool]:
    """Return ``(key scope, replay_fallback)`` for a Codex token_count event.

    Fork files (``thread_spawn`` subagents, user ``/fork``, exec forks — any file
    whose first ``session_meta`` declares an ancestor) replay the parent thread's
    history before their own work. Keying the replay to the parent session
    collapses it against the parent file via the stable event key:

    - Codex 0.144 shape: the file replays the parent's ``session_meta``, so the
      current session id IS the parent's and the key is already parent-scoped.
    - Codex 0.146+ single-meta fork shape (``forked_from_id``): one session_meta
      carrying the child's own id; the replayed parent prefix precedes the
      child's first ``turn_context``.

    ``replay_fallback`` marks rows that may be source-gated, but only when they
    lack the cumulative state a stable key needs (old/partial logs); anything
    unrecognized degrades toward visible over-counting, not silent loss.
    Shared by both Codex parsers so Overview and Sessions cannot drift apart.
    See CODEX_USAGE_COUNTING.md.
    """
    if not is_fork_file or own_session_id is None or current_session_id is None:
        return current_session_id, False
    if subagent_parent_id is not None:
        if current_session_id == subagent_parent_id:
            return current_session_id, True
        if current_session_id == own_session_id and not saw_turn_context:
            return subagent_parent_id, False
    elif current_session_id != own_session_id:
        return current_session_id, True
    return current_session_id, False


def codex_fork_ancestry(payload: Any) -> Tuple[bool, Optional[str]]:
    """Return ``(is_thread_spawn, declared_parent_id)`` from a session_meta payload.

    Codex has declared fork ancestry three ways: ``source.subagent.thread_spawn
    .parent_thread_id`` (MultiAgent V2) and the top-level ``forked_from_id`` /
    ``parent_thread_id`` fields (user ``/fork``, exec forks, and single-meta
    spawn files). Any of them marks the file as a history-replaying fork;
    ``thread_spawn`` additionally distinguishes subagent sessions for activity
    classification. A self-reference is not ancestry. The bare ``session_id``
    top-level field is deliberately NOT accepted: it has never carried ancestry
    in any observed log (0/1009 files) and its generic name risks treating
    ordinary sessions as forks, which would rekey real rows into a shared scope.
    """
    p = payload if isinstance(payload, dict) else {}
    own_id = str(p.get("id") or "")
    source = p.get("source") if isinstance(p.get("source"), dict) else {}
    subagent = source.get("subagent") if isinstance(source.get("subagent"), dict) else None
    thread_spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    is_thread_spawn = isinstance(thread_spawn, dict)
    pid = None
    if is_thread_spawn:
        pid = (thread_spawn or {}).get("parent_thread_id")
    if not pid:
        for field in ("forked_from_id", "parent_thread_id"):
            candidate = p.get(field)
            if candidate and str(candidate) != own_id:
                pid = candidate
                break
    return is_thread_spawn, (str(pid) if pid else None)


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

    # Explicit semantic version of what this parser STORES in the persistent
    # usage cache. Required for every file_replace / source_replace parser;
    # None for source_native_db parsers, which are queried live and never
    # copied into usage_entries (see test_usage_parser_registry).
    #
    # Bump it whenever this parser's stored output changes: extraction, dedup
    # or entry keys, timestamps, token buckets, or the billing inputs it
    # records. Do NOT bump it for a refactor that leaves the rows identical.
    #
    # It is deliberately a hand-written integer rather than a hash of the
    # module: every parser in this file shares one source file, so a module
    # hash made an unrelated parser's edit invalidate all of them.
    persistent_parser_version: ClassVar[Optional[int]] = None

    # Shared across all instances:
    #   {source_name: ((file_sigs, pricing_sig, runtime_sig), [entries])}
    # pricing_sig is included so cost values are recomputed when pricing_db.json changes.
    # runtime_sig covers validated environment overrides that change output.
    _entry_cache: ClassVar[Dict[str, Tuple[tuple, List[Dict[str, Any]]]]] = {}

    def __init__(self, pricing_db: PricingDatabase):
        self.pricing_db = pricing_db

    def _file_signatures(self) -> tuple:
        """Hashable snapshot of source files; override per parser."""
        return ()

    def _pricing_signature(self) -> tuple:
        """Runtime signature of the effective pricing DB.

        Must cover BOTH files: a dashboard pricing edit writes ONLY the override under
        ``TOKDASH_DATA_DIR`` and never touches the packaged baseline, so statting the
        baseline alone would never bust ``_entry_cache`` and edited rates would silently
        not apply.
        ``PricingDatabase.signature()`` stats both files and is itself OSError-safe.
        """
        try:
            return tuple(self.pricing_db.signature())
        except (OSError, AttributeError):
            return ()

    def runtime_config_signature(self) -> Optional[Dict[str, Any]]:
        """Validated environment overrides that affect parse output.

        The default is None: parsers whose output depends only on the
        source files and the pricing DB keep byte-identical cache
        signatures on every path that embeds this value.
        """
        return None

    def persistent_parser_signature(self) -> Dict[str, Any]:
        """Identity of this parser for the persistent usage cache.

        Explicit and content-free: it names the class, its declared version and
        the shared usage-entry format. Package version, install path and file
        mtimes are absent by construction, so a reinstall or an edit to an
        unrelated parser cannot invalidate this source's cached rows.
        """
        cls = type(self)
        version = cls.persistent_parser_version
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError(
                f"{cls.__module__}.{cls.__name__} is persistently stored but declares "
                f"no valid persistent_parser_version"
            )
        return {
            "object": f"{cls.__module__}.{cls.__name__}",
            "version": version,
            "entry_format": USAGE_ENTRY_FORMAT_VERSION,
        }

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
        cached cost values are recomputed when pricing_db.json is updated,
        and the runtime configuration signature so that a validated
        environment override triggers a re-parse without file changes.

        The cache is a ClassVar shared across all parser instances so that
        separate ``CodingToolsUsageTracker`` objects (e.g. for current-period
        and previous-period in ``compute_usage_with_comparison``) reuse the
        same parsed data.
        """
        sig = (
            self._file_signatures(),
            self._pricing_signature(),
            self.runtime_config_signature(),
        )
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
    # Queried live from the source DB; never copied into usage_entries, so
    # there is nothing stored for a version to identify.
    persistent_parser_version = None

    # Per-query cache: {(s_ms, u_ms): [entries]}, invalidated when DB or pricing changes.
    # Bounded to _OPENCODE_QUERY_CACHE_MAX entries to prevent unbounded growth.
    _query_cache: ClassVar[Dict[tuple, List[Dict[str, Any]]]] = {}
    _query_cache_sig: ClassVar[tuple] = ()

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
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

    def _db_paths(self) -> List[Path]:
        """Live source databases for this tool (one for OpenCode; Kilo
        overrides to cover its channel DBs)."""
        if self.db_path.exists():
            return [self.db_path]
        return []

    def _query_db(self, db_path: Path, s_ms: int, u_ms: int) -> List[Dict[str, Any]]:
        try:
            conn = connect_sqlite_readonly(db_path)
        except Exception:
            return []
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT data, time_created FROM message WHERE time_created >= ? AND time_created < ? ORDER BY time_created",
                (s_ms, u_ms),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        out: List[Dict[str, Any]] = []
        for data_json, ts_ms in rows:
            try:
                data = json.loads(data_json)
                tokens = data.get("tokens")
                if not isinstance(tokens, dict):
                    continue
                out.append(
                    self._build_entry(
                        str(data.get("modelID") or "unknown"),
                        str(data.get("providerID") or ""),
                        tokens,
                        self._i(ts_ms),
                    )
                )
            except Exception:
                continue
        return out

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
        sig = (
            self._file_signatures(),
            self._pricing_signature(),
            self.runtime_config_signature(),
        )
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

        # IMPORTANT: Only use the SQLite DB to avoid double-counting!
        # File storage (~/.local/share/opencode/storage/message) contains the SAME messages as the DB.
        # Using both sources would result in 100% duplication.
        # See: patchFixSetup/09-fixes/OpenCode_Double_Counting_Fix.md

        for db_path in self._db_paths():
            out.extend(self._query_db(db_path, s_ms, u_ms))

        # Evict all entries when cache exceeds bound to prevent unbounded growth.
        if len(type(self)._query_cache) >= _OPENCODE_QUERY_CACHE_MAX:
            type(self)._query_cache.clear()
        type(self)._query_cache[cache_key] = out
        return list(out)


class KiloCodeParser(OpenCodeParser):
    """
    Parser for Kilo Code token usage.

    =======================================================================
    KILO CODE — OPENCODE CODEBASE, app = "kilo"
    =======================================================================
    The CLI and the current VS Code extension are built on the OpenCode
    codebase. Its SQLite ``message`` table is field-for-field the shape
    OpenCodeParser already queries (data.modelID / data.providerID /
    data.tokens), so this subclass only redirects the DB paths:
    clientpaths.kilo_db_paths() — kilo.db, dev-channel kilo-<channel>.db,
    and the pre-rename opencode*.db fallback (taken only while no
    kilo-named file exists, so a migrated install is never read twice).
    Usage is cache-exclusive (input + cache.read = full prompt), same as
    OpenCode; recorded cost is ignored and the pricing DB decides.
    =======================================================================
    """

    source_name = "kilocode"
    sync_capability = SourceSyncCapability(
        mode="source_native_db",
        session_store=False,
        reason="Kilo already stores messages in a SQLite DB and supports SQL date windows.",
    )

    # K1: redeclare both query-cache ClassVars. A subclass otherwise inherits
    # the base dict object while assigning its own _query_cache_sig, so with
    # both tools installed OpenCode would silently serve Kilo's rows (and
    # vice versa) for the same date window.
    _query_cache: ClassVar[Dict[tuple, List[Dict[str, Any]]]] = {}
    _query_cache_sig: ClassVar[tuple] = ()

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.db_paths = clientpaths.kilo_db_paths()
        # db_path points at the canonical file so code paths that consult it
        # see a plausible value; _db_paths()/_file_signatures() use db_paths.
        self.db_path = self.db_paths[0] if self.db_paths else clientpaths.kilo_data_dir() / "kilo.db"

    def _file_signatures(self) -> tuple:
        out: list[tuple[str, int, int]] = []
        for db_path in self.db_paths:
            if not db_path.exists():
                continue
            for candidate in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
                try:
                    s = candidate.stat()
                    out.append((str(candidate), s.st_mtime_ns, s.st_size))
                except (FileNotFoundError, OSError):
                    continue
        return tuple(out)

    def _db_paths(self) -> List[Path]:
        return [db_path for db_path in self.db_paths if db_path.exists()]


def _split_cline_cache_inclusive_input(
    raw_input: int, raw_cache_read: int, raw_cache_write: int
) -> Tuple[int, int, int]:
    """Disjoint (input, cacheRead, cacheWrite) from Cline's normalized row.

    inputTokens already includes the cache portions; clamp each part so a
    malformed row (cache share larger than the prompt) cannot go negative.
    """
    cache_r = min(max(raw_cache_read, 0), raw_input)
    cache_w = min(max(raw_cache_write, 0), raw_input - cache_r)
    return raw_input - cache_r - cache_w, cache_r, cache_w


def cline_message_file_signatures(data_dir: Path) -> tuple:
    """(path, mtime_ns, size) of every sessions/*/*.messages.json file.

    Shared by ClineParser._file_signatures and the Sessions-tab loader so
    the two can never see different file sets.
    """
    if not data_dir.is_dir():
        return ()
    sigs: List[Tuple[str, int, int]] = []
    for path in data_dir.glob("sessions/*/*.messages.json"):
        if not path.is_file():
            continue
        try:
            s = path.stat()
            sigs.append((str(path), s.st_mtime_ns, s.st_size))
        except OSError:
            continue
    return tuple(sorted(sigs))


def parse_cline_message_file(
    path_str: str, unavailable: Optional[type[Exception]] = None
) -> List[Dict[str, Any]]:
    """Assistant model-call rows from one .messages.json file.

    Row: {entry_id, ts, model, provider, input, output, cacheRead,
    cacheWrite}. No cost: priced on read by whichever consumer (Overview
    or the Sessions harness) holds the pricing DB. Badly shaped files
    yield [] (the caller decides whether to log it); a transient open
    failure (lock/AV/indexer) also yields [] unless ``unavailable`` gives
    an exception class, in which case it is raised so the caller can
    retry the file instead of memoizing an empty parse.
    """
    try:
        with open(path_str, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except OSError as exc:
        if unavailable is not None:
            raise unavailable(path_str) from exc
        return []
    except ValueError:
        return []
    messages = doc.get("messages") if isinstance(doc, dict) else None
    if not isinstance(messages, list):
        return []

    out: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        metrics = msg.get("metrics")
        if not isinstance(metrics, dict):
            continue
        msg_id = msg.get("id")
        if msg_id:
            msg_id = str(msg_id)
        ts = BaseParser._i(msg.get("ts"))
        if ts <= 0:
            continue

        model_info = msg.get("modelInfo") if isinstance(msg.get("modelInfo"), dict) else {}
        model = str(model_info.get("id") or "unknown")
        provider = str(model_info.get("provider") or "")

        input_t, cache_r, cache_w = _split_cline_cache_inclusive_input(
            BaseParser._i(metrics.get("inputTokens")),
            BaseParser._i(metrics.get("cacheReadTokens")),
            BaseParser._i(metrics.get("cacheWriteTokens")),
        )
        output_t = BaseParser._i(metrics.get("outputTokens"))
        if input_t == 0 and output_t == 0 and cache_r == 0 and cache_w == 0:
            continue

        # Pricing DB only; metrics.cost is deliberately ignored (C6).
        out.append({
            "entry_id": f"cline:{msg_id}" if msg_id else "",
            "ts": ts,
            "model": model,
            "provider": provider,
            "input": input_t,
            "output": output_t,
            "cacheRead": cache_r,
            "cacheWrite": cache_w,
        })
    return out


class ClineParser(BaseParser):
    """
    Parser for Cline token usage (CLI; the VS Code extension shares the store).

    =======================================================================
    CLINE — PER-SESSION MESSAGE FILES ARE THE SOURCE OF TRUTH
    =======================================================================
    Data dir: clientpaths.cline_data_dir()
    ($CLINE_DATA_DIR > $CLINE_DIR/data > ~/.cline/data).

    Token-bearing store: ``sessions/<sessionId>/<sessionId>.messages.json``
    and subagent siblings ``sessions/<sessionId>/agent_<agentId>.messages.json``.
    Top level: version, updated_at, agent, sessionId, origin, messages[].
    Assistant messages carry a stable id (msg_...), epoch-ms ts,
    metrics {inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens}
    and modelInfo {id, provider}.

    Why files, not the sessions table: with enableSpawn the parent row's
    usage is the sum of the PARENT's own model calls only; subagent rows
    carry no usage at all, and subagent tokens exist only in their
    agent_*.messages.json. A sessions-db parser undercounts every
    subagent run; the message files are complete and non-duplicating
    (each model call is exactly one assistant message in exactly one file).
    sessions.db is metadata/cross-check only in Phase 1 — a session row
    with usage but no message file is a deleted session, not a gap to fill.

    Cache-inclusive input (C1): Cline normalizes every provider so
    inputTokens is the FULL prompt including cache-read/write portions
    ("Do not add cache fields back on top" — their usage service). The
    parser splits it into disjoint buckets before emitting, so compute's
    total = input + cacheW + output + cacheR + reasoning stays correct.

    Dedup key (C7): source-global "cline:<message id>", NOT
    session-scoped. Resume rewrites the same file in place keeping ids;
    fork copies the parent's messages (ids and metrics intact) into a NEW
    session file. A session-scoped key would count every replayed/forked
    call twice; the global key counts each real model call once. Because
    ids are copied across files, cline needs the store's
    cross-file-stable-key sync (SourceSyncCapability.cross_file_stable_keys)
    for correct ownership when a copy-bearing file is deleted.

    Cost (C6): pricing DB only; metrics.cost is ignored, same as
    OpenCode/Kilo. Self-hosted ids absent from the DB cost 0.00.
    =======================================================================
    """

    source_name = "cline"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        cross_file_stable_keys=True,
        reason=(
            "Each .messages.json is a whole-file JSON document rewritten in place; "
            "forked session files carry stable message-id copies, so ownership must "
            "follow the earliest occurrence across files."
        ),
    )
    # 1: assistant messages with dict metrics, cache-inclusive inputTokens
    #    split into disjoint buckets, source-global "cline:<message id>" key,
    #    pricing-DB cost only.
    persistent_parser_version = 1

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.data_dir = clientpaths.cline_data_dir()

    def _file_signatures(self) -> tuple:
        return _timed_sigs(
            f"{self.source_name}:{self.data_dir}",
            lambda: cline_message_file_signatures(self.data_dir),
        )

    @staticmethod
    def _split_cache_inclusive_input(
        raw_input: int, raw_cache_read: int, raw_cache_write: int
    ) -> Tuple[int, int, int]:
        return _split_cline_cache_inclusive_input(
            raw_input, raw_cache_read, raw_cache_write
        )

    def _parse_message_file(self, path_str: str) -> List[Dict[str, Any]]:
        # The shared helper yields unpriced rows; the parser adds the
        # pricing-DB cost (metrics.cost ignored, C6) and billing record.
        out: List[Dict[str, Any]] = []
        for row in parse_cline_message_file(path_str):
            cost = self.pricing_db.get_cost(
                row["model"], row["input"], row["output"],
                row["cacheRead"], row["cacheWrite"],
            )
            billing = usage_billing_pricing(
                [row["model"]],
                input_tokens=row["input"],
                output_tokens=row["output"],
                cache_read=row["cacheRead"],
                cache_write=row["cacheWrite"],
            )
            out.append({
                "source": self.source_name,
                "model": row["model"],
                "provider": row["provider"],
                "input": row["input"],
                "output": row["output"],
                "cacheRead": row["cacheRead"],
                "cacheWrite": row["cacheWrite"],
                "reasoning": 0,
                "cost": cost,
                "timestamp": row["ts"],
                "entry_id": row["entry_id"],
                "_billing": billing,
            })
        return out

    def _parse_all(self) -> List[Dict[str, Any]]:
        # The store's stable-key upsert keeps the earliest-timestamped
        # occurrence of a copied id; do the same here so live and
        # persistent totals agree even when a fork restamps a copy (C7).
        by_id: Dict[str, Dict[str, Any]] = {}
        anonymous: List[Dict[str, Any]] = []
        for path_str, _, _ in self._file_signatures():
            for entry in self._parse_message_file(path_str):
                entry_id = entry["entry_id"]
                if not entry_id:
                    anonymous.append(entry)
                    continue
                prev = by_id.get(entry_id)
                if prev is None or entry["timestamp"] < prev["timestamp"]:
                    by_id[entry_id] = entry
        return list(by_id.values()) + anonymous


class CodexParser(BaseParser):
    source_name = "codex"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        session_store=True,
        cross_file_stable_keys=True,
        reason=(
            "Codex JSONL files are reparsed independently; stable usage-state keys "
            "deduplicate resumed-history copies across files."
        ),
    )
    # 1: token_count deltas keyed on the stable usage-state event id, fresh
    #    input split out of Codex's cache-inclusive input_tokens, placeholder
    #    models resolved to the file's own first model signal.
    persistent_parser_version = 1

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.sessions_dir = clientpaths.codex_sessions_dir()
        self.archived_sessions_dir = clientpaths.codex_archived_sessions_dir()
        self.replay_events_skipped = 0

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
        # Both roots: archived rollouts keep their content, so stable event keys
        # collapse any overlap with sessions/ instead of double-counting.
        def _scan() -> tuple:
            sigs = list(_rglob_sigs(self.sessions_dir)) + list(_rglob_sigs(self.archived_sessions_dir))
            sigs.sort()
            return tuple(sigs)

        return _timed_sigs(f"codex:{self.sessions_dir}:{self.archived_sessions_dir}", _scan)

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        self.replay_events_skipped = 0
        event_index_by_key: dict[str, int] = {}

        for path_str, _, _ in self._file_signatures():
            session_file = Path(path_str)
            try:
                model: Optional[str] = None
                provider = "openai"
                own_session_id = None
                current_session_id = None
                subagent_parent_id = None
                is_fork_file = False
                saw_turn_context = False
                first_model_seen: Optional[str] = None
                first_provider_seen: Optional[str] = None
                # Indices this file inserted OR replaced. A duplicate with an
                # earlier timestamp can replace an entry owned by a previously
                # parsed file (index < len(out) at file start), so a slice from
                # the file's first append would miss it — its placeholder marker
                # must still be resolved by THIS file's model signal.
                file_entry_indices: set[int] = set()

                for line_no, line in enumerate(session_file.read_text(encoding="utf-8").splitlines(), start=1):
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue

                    p = msg.get("payload") or {}
                    if msg.get("type") == "turn_context":
                        saw_turn_context = True
                        if p.get("model"):
                            model = str(p.get("model"))
                            provider = self._infer_provider(model, provider)
                            if first_model_seen is None:
                                first_model_seen = model
                                first_provider_seen = provider
                    elif (
                        not saw_turn_context
                        and msg.get("type") == "event_msg"
                        and p.get("type") == "thread_settings_applied"
                    ):
                        # Newer Codex applies the thread settings before the first
                        # turn_context; an authoritative early model source. Once a
                        # turn_context exists it owns per-turn attribution.
                        settings = p.get("thread_settings") if isinstance(p.get("thread_settings"), dict) else {}
                        if settings.get("model"):
                            model = str(settings.get("model"))
                            provider = self._infer_provider(model, provider)
                            if settings.get("model_provider_id"):
                                provider = str(settings.get("model_provider_id"))
                            if first_model_seen is None:
                                first_model_seen = model
                                first_provider_seen = provider
                    elif msg.get("type") == "session_meta":
                        sid = p.get("id")
                        if sid:
                            sid = str(sid)
                            if own_session_id is None:
                                own_session_id = sid
                                # First session_meta identifies the file. A fork file
                                # (thread_spawn subagent, user /fork, exec fork)
                                # replays ancestor history; capture the declared parent
                                # from any ancestry field so we skip only those replays
                                # (never the fork's own or a stray id).
                                _is_thread_spawn, subagent_parent_id = codex_fork_ancestry(p)
                                is_fork_file = _is_thread_spawn or subagent_parent_id is not None
                            current_session_id = sid
                        if p.get("model_provider"):
                            provider = str(p.get("model_provider"))
                        if not saw_turn_context:
                            # Issue #23 reported the selected model nested under
                            # base_instructions.provenance. Unconfirmed: no real
                            # log through Codex 0.147.0 carries that key, but the
                            # lookup is harmless and keeps the reporter's case
                            # covered if a future build adds it.
                            base = p.get("base_instructions") if isinstance(p.get("base_instructions"), dict) else {}
                            provenance = base.get("provenance") if isinstance(base.get("provenance"), dict) else {}
                            if provenance.get("model"):
                                model = str(provenance.get("model"))
                                provider = self._infer_provider(model, provider)
                                if first_model_seen is None:
                                    first_model_seen = model
                                    first_provider_seen = provider

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

                    key_session_id, replay_fallback = codex_replay_key_session_id(
                        is_fork_file,
                        own_session_id,
                        current_session_id,
                        subagent_parent_id,
                        saw_turn_context,
                    )

                    event_key = codex_token_event_key(key_session_id, info)
                    if replay_fallback and event_key is None:
                        self.replay_events_skipped += 1
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

                    entry_model = model or CODEX_DEFAULT_MODEL
                    entry = {
                        "source": self.source_name,
                        "model": entry_model,
                        "provider": provider,
                        "input": input_t,
                        "output": output_t,
                        "cacheRead": cache_read,
                        "cacheWrite": 0,
                        "reasoning": reasoning,
                        "cost": self.pricing_db.get_cost(entry_model, input_t, output_t, cache_read, 0),
                        "timestamp": int(ts.timestamp() * 1000),
                        "entry_id": event_key or f"{session_file}:{line_no}",
                        # Codex logs no cache writes, so the billed cache-write
                        # bucket is 0 rather than the entry's cacheWrite field.
                        "_billing": usage_billing_pricing(
                            [entry_model],
                            input_tokens=input_t,
                            output_tokens=output_t,
                            cache_read=cache_read,
                        ),
                    }
                    if model is None:
                        entry["_model_placeholder"] = True
                    if event_key and event_key in event_index_by_key:
                        self.replay_events_skipped += 1
                        existing_index = event_index_by_key[event_key]
                        if entry["timestamp"] < out[existing_index]["timestamp"]:
                            out[existing_index] = entry
                            # The replacement may sit before this file's first
                            # append; the file now owns that slot's marker too.
                            file_entry_indices.add(existing_index)
                        continue
                    if event_key:
                        event_index_by_key[event_key] = len(out)
                    file_entry_indices.add(len(out))
                    out.append(entry)

                if first_model_seen:
                    # Rows written before the file's first model signal (a fork's
                    # replayed parent prefix that outlives an unindexed parent, or
                    # old formats) would otherwise bill under the placeholder
                    # default. The file's own first model is the closest truthful
                    # attribution. Only placeholder rows move: an explicit
                    # selection of CODEX_DEFAULT_MODEL is real data, not a signal
                    # gap, and must survive even when the model changes mid-file.
                    for index in file_entry_indices:
                        entry = out[index]
                        if not entry.pop("_model_placeholder", False):
                            continue
                        entry["model"] = first_model_seen
                        entry["provider"] = first_provider_seen or entry["provider"]
                        entry["cost"] = self.pricing_db.get_cost(
                            first_model_seen, entry["input"], entry["output"], entry["cacheRead"], 0
                        )
                        entry["_billing"] = usage_billing_pricing(
                            [first_model_seen],
                            input_tokens=entry["input"],
                            output_tokens=entry["output"],
                            cache_read=entry["cacheRead"],
                        )
                else:
                    # No model signal anywhere in the file: label the rows
                    # explicitly unknown (issue #23) instead of billing them
                    # under a default model that never ran. Unknown models carry
                    # token counts but price to $0.
                    for index in file_entry_indices:
                        entry = out[index]
                        if entry.pop("_model_placeholder", False):
                            entry["model"] = "unknown"
                            entry["cost"] = 0.0
                            # No model ran here as far as the log knows, so
                            # there is no pricing key to reprice against. An
                            # empty candidate list keeps the row at zero under
                            # any future pricing file, exactly like a reparse.
                            entry["_billing"] = usage_billing_pricing(
                                [],
                                input_tokens=entry["input"],
                                output_tokens=entry["output"],
                                cache_read=entry["cacheRead"],
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
    # 1: assistant usage rows keyed on message id, streaming snapshots
    #    collapsed to the latest, cache writes billed at the input rate.
    persistent_parser_version = 1

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
                        "_billing": usage_billing_pricing(
                            [model],
                            input_tokens=input_t,
                            output_tokens=output_t,
                            cache_read=cache_r,
                            cache_write=cache_w,
                        ),
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
    # 1: gemini rows keyed on message id, cached prompt tokens subtracted out
    #    of the inclusive input count, thoughts kept as reasoning.
    persistent_parser_version = 1

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
        # cache-hit rate. See docs/development/CHANGELOG.md.
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
            # The billed key is the raw model, which is what get_cost is called
            # with above; the displayed model falls back to "unknown".
            "_billing": usage_billing_pricing(
                [str(model or "")],
                input_tokens=input_t,
                output_tokens=output_t,
                cache_read=cache_r,
                cache_write=cache_w,
            ),
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
    # 1: gen_metadata protobuf rows keyed on (db stem, idx), visible output
    #    preferred over total-minus-reasoning.
    persistent_parser_version = 1

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
            "_billing": usage_billing_pricing(
                [model],
                input_tokens=input_t,
                output_tokens=output_t,
                cache_read=cache_r,
                cache_write=cache_w,
            ),
        }

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for path_str, _, _ in self._file_signatures():
            db_path = Path(path_str)
            rows = None
            # The helper already opens RO with an RW fallback, so the plain connect
            # is only the last resort: sqlite3 opens lazily, so an RO open that needs
            # recovery (the client crashed mid-write, leaving a WAL) fails on the
            # first query — and recovery requires a writable connection.
            for opener in (connect_sqlite_readonly, lambda p: sqlite3.connect(str(p))):
                try:
                    conn = opener(db_path)
                except Exception:
                    continue
                try:
                    rows = conn.execute("SELECT idx, data FROM gen_metadata ORDER BY idx").fetchall()
                    break
                except Exception:
                    pass
                finally:
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
    # 1: placeholder — emits nothing. Bump when it starts emitting rows.
    persistent_parser_version = 1

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.amp_root = clientpaths.amp_root()

    def _parse_all(self) -> List[Dict[str, Any]]:
        # TODO(coding_tools): Amp parser placeholder.
        # Keep fail-soft until we have schema + fixtures.
        return []


class KimiParser(BaseParser):
    """
    Parser for Kimi CLI / Kimi Code session files.

    =======================================================================
    SCHEMA A — legacy Kimi CLI (<0.26)
    =======================================================================
    Location: ~/.kimi/sessions/<userId>/<sessionId>/wire.jsonl
    Root override: $KIMI_SHARE_DIR

    Token usage is captured in "StatusUpdate" messages:

      {"timestamp": <float seconds>,
       "message": {"type": "StatusUpdate",
                   "payload": {"token_usage": {"input_other": int,
                                               "output": int,
                                               "input_cache_read": int,
                                               "input_cache_creation": int},
                               "message_id": str}}}

    Dedup key: message.payload.message_id. The resolved model is not exposed,
    so a default billing model is inferred by time window.

    =======================================================================
    SCHEMA B — Kimi Code (>=0.26)
    =======================================================================
    Location: ~/.kimi-code/sessions/<workspace>/<sessionId>/agents/<agent>/wire.jsonl
    Root override: $KIMI_CODE_HOME (KIMI_SHARE_DIR was removed in 0.26)

    Token usage is captured in top-level "usage.record" rows. Each row is an
    incremental per-step delta (one row per LLM request, not per user turn):

      {"type": "usage.record", "model": "kimi-code/k3",
       "usage": {"inputOther": int, "output": int,
                 "inputCacheRead": int, "inputCacheCreation": int},
       "usageScope": "turn", "time": <int milliseconds>}

    usageScope is attribution metadata only — the CLI derives it as
    `source?.type === "turn" ? "turn" : "session"` and its own aggregator sums
    every usage.record row without inspecting the scope. Rows emitted from
    non-turn paths (e.g. compaction calls) carry "session" and MUST be counted
    too, so no scope filter is applied here. There is no message id, so the
    dedup key is a SHA-1 of (file path, time, model, usage): including the
    path keeps identical rows from distinct sessions/agents countable while
    still collapsing duplicated lines within one file.

    Field mapping to normalized entry (both schemas):
      source <- "kimi"
      provider <- "moonshotai" (Kimi is from Moonshot AI)
      input / output / cacheRead / cacheWrite <- usage fields
      reasoning <- 0 (not exposed separately)
      timestamp <- epoch milliseconds

    Known schema versions: 2025-03 to present (legacy), 0.26+ (Kimi Code)
    =======================================================================
    """

    source_name = "kimi"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        append_jsonl=True,
        reason=(
            "Kimi wire JSONL usage rows are append-safe: legacy rows carry stable message IDs, "
            "Kimi Code usage.record rows dedup on a content hash."
        ),
    )
    # 1: legacy StatusUpdate rows keyed on message id and Kimi Code
    #    usage.record rows keyed on a (path, content) hash, wire model names
    #    mapped through _WIRE_MODEL_MAP.
    persistent_parser_version = 1

    # Wire model names emitted by Kimi Code usage.record rows, mapped to
    # canonical TokDash pricing keys. The display names in the CLI's own
    # config (~/.kimi-code/config.toml) identify kimi-for-coding* as
    # "K2.7 Coding"; "k3" is the current flagship with its own pricing key.
    _WIRE_MODEL_MAP: ClassVar[Dict[str, str]] = {
        "kimi-for-coding": "kimi-k2.7-code",
        "kimi-for-coding-highspeed": "kimi-k2.7-code",
        "k3": "kimi-k3",
        # Same rate as kimi-k3 today, but mapped explicitly so it does not depend
        # on the pricing DB continuing to resolve the bare wire name.
        "k3-256k": "kimi-k3-256k",
    }

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        # Legacy root, kept for backward compatibility (tests, external refs).
        self.kimi_root = clientpaths.kimi_root()
        # All roots to scan: Kimi Code (>=0.26) first, then the legacy root.
        self.kimi_roots = clientpaths.kimi_roots()

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
            "_billing": usage_billing_pricing(
                [model or "kimi-k2.5"],
                input_tokens=input_other,
                output_tokens=output_t,
                cache_read=cache_read,
                cache_write=cache_write,
            ),
        }

    @staticmethod
    def _model_for_wire_name(wire_name: Any) -> str:
        """Map a Kimi Code wire model (e.g. ``kimi-code/k3``) to a TokDash model key.

        ``kimi-for-coding(-highspeed)`` is the managed subscription alias whose
        backend is K2.7 Coding, so it maps to the ``kimi-k2.7-code`` pricing
        key; ``k3`` maps to the canonical ``kimi-k3`` key (the pricing DB also
        carries a namespaced ``kimi-code/k3`` alias). Unknown models keep their
        bare id; ids missing from the pricing DB cost 0 until the DB learns
        them, but token counts stay correct.
        """
        name = str(wire_name or "").strip().lower().split("/")[-1]
        if not name:
            return "kimi-k2.5"
        return KimiParser._WIRE_MODEL_MAP.get(name, name)

    def _file_signatures(self) -> tuple:
        def _scan() -> tuple:
            sigs: List[Tuple[str, int, int]] = []
            for root in self.kimi_roots:
                sessions_dir = root / "sessions"
                # Legacy layout: sessions/<userId>/<sessionId>/wire.jsonl
                sigs.extend(_glob_sigs(str(sessions_dir / "*" / "*" / "wire.jsonl")))
                # Kimi Code >=0.26: sessions/<ws>/<sessionId>/agents/<agent>/wire.jsonl
                sigs.extend(_glob_sigs(str(sessions_dir / "*" / "*" / "agents" / "*" / "wire.jsonl")))
            return tuple(sorted(set(sigs)))

        cache_key = "kimi:" + "|".join(str(r) for r in self.kimi_roots)
        return _timed_sigs(cache_key, _scan)

    def _entry_from_status_update(self, record: Dict[str, Any], seen_message_ids: set) -> Optional[Dict[str, Any]]:
        """Parse a legacy-schema row (StatusUpdate message); None if not applicable."""
        msg = record.get("message", {})
        if not isinstance(msg, dict) or msg.get("type") != "StatusUpdate":
            return None

        payload = msg.get("payload", {})
        token_usage = payload.get("token_usage")
        if not isinstance(token_usage, dict):
            return None

        # Deduplicate by message_id
        message_id = payload.get("message_id", "")
        if not message_id or message_id in seen_message_ids:
            return None
        seen_message_ids.add(message_id)

        # Parse timestamp (float seconds)
        ts_raw = record.get("timestamp")
        if not ts_raw:
            return None
        try:
            ts = datetime.fromtimestamp(float(ts_raw), timezone.utc)
        except (ValueError, TypeError, OSError):
            return None

        model = self._default_model_for_timestamp(ts)
        ts_ms = int(ts.timestamp() * 1000)
        return self._build_entry(model, token_usage, ts_ms, message_id)

    def _entry_from_usage_record(self, record: Dict[str, Any], path_str: str, seen_message_ids: set) -> Optional[Dict[str, Any]]:
        """Parse a Kimi Code >=0.26 usage.record row; None if not applicable."""
        if record.get("type") != "usage.record":
            return None

        # No usageScope filter: every row is an incremental delta. The CLI
        # derives scope as `source?.type === "turn" ? "turn" : "session"` and
        # its own aggregator sums all rows regardless of scope, so "session"
        # rows (e.g. compaction calls) are real usage that must be counted.

        usage = record.get("usage")
        if not isinstance(usage, dict):
            return None

        # `time` is already epoch milliseconds.
        ts_ms = self._i(record.get("time"))
        if ts_ms <= 0:
            return None

        model = self._model_for_wire_name(record.get("model"))

        # No message id in this schema; dedup on (path, content) instead. The
        # path keeps identical rows from distinct sessions/agents countable,
        # while duplicated lines within one file still collapse.
        dedup_key = hashlib.sha1(
            json.dumps(
                [
                    path_str,
                    ts_ms,
                    model,
                    usage.get("inputOther"),
                    usage.get("output"),
                    usage.get("inputCacheRead"),
                    usage.get("inputCacheCreation"),
                ],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if dedup_key in seen_message_ids:
            return None
        seen_message_ids.add(dedup_key)

        token_usage = {
            "input_other": usage.get("inputOther"),
            "output": usage.get("output"),
            "input_cache_read": usage.get("inputCacheRead"),
            "input_cache_creation": usage.get("inputCacheCreation"),
        }
        return self._build_entry(model, token_usage, ts_ms, dedup_key)

    def _parse_all(self) -> List[Dict[str, Any]]:
        """Collect token usage from Kimi CLI / Kimi Code session files."""
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
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        entry = self._entry_from_usage_record(record, path_str, seen_message_ids)
                        if entry is None:
                            entry = self._entry_from_status_update(record, seen_message_ids)
                        if entry is not None:
                            out.append(entry)

            except Exception:
                continue

        return out


class GrokParser(BaseParser):
    """Parse Grok Build's global usage log for per-inference token counts.

    Grok Build appends one JSON object per line to ``$GROK_HOME/logs/unified.jsonl``.
    ``shell.turn.inference_done`` rows carry the real prompt / cached / completion /
    reasoning split — everything needed to price a turn accurately — but no model id, so the
    active model is tracked per CLI process (``pid``) from the model-change events the CLI
    also logs. This mirrors the Grok CLI's own (and openusage's) billing accounting; the
    older cumulative ``updates.jsonl`` reader could only lump every token into ``input`` and
    mark it estimated.
    """

    source_name = "grok"
    sync_capability = SourceSyncCapability(
        mode="source_replace",
        reason=(
            "Grok usage is a single append-only unified.jsonl with no stable per-row id, so a "
            "change reparses the whole file."
        ),
    )
    # 1: inference_done rows attributed to the model announced for their pid,
    #    reasoning folded into output, cached prompt tokens split out of input.
    persistent_parser_version = 1

    _UNKNOWN_MODEL = "grok-unknown"
    _MODEL_EVENTS = frozenset(
        {
            "model changed",
            "model catalog: notifying clients",
            "backend_search: model switch",
            "subagent model resolved",
        }
    )

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.log_path = clientpaths.grok_home() / "logs" / "unified.jsonl"

    def _file_signatures(self) -> tuple:
        def _scan() -> tuple:
            try:
                st = self.log_path.stat()
            except OSError:
                return ()
            return ((str(self.log_path), st.st_mtime_ns, st.st_size),)

        return _timed_sigs(f"grok:{self.log_path}", _scan)

    @staticmethod
    def _int(value: Any) -> int:
        if value is None or isinstance(value, bool):
            return 0
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _timestamp_ms(raw: Any) -> Optional[int]:
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            return int(parsed.timestamp() * 1000)
        except (OSError, OverflowError):
            return None

    @staticmethod
    def _model_change(msg: str, ctx: Dict[str, Any]) -> Optional[str]:
        # The active model is announced through several event shapes, all keyed by pid.
        if msg == "model changed":
            raw = ctx.get("model")
        elif msg == "model catalog: notifying clients":
            raw = ctx.get("current_model_id")
        elif msg == "backend_search: model switch":
            raw = ctx.get("model") or ctx.get("current_model_id") or ctx.get("model_id")
        elif msg == "subagent model resolved":
            raw = ctx.get("model_id") or ctx.get("model")
        else:
            return None
        model = str(raw or "").strip()
        return model or None

    def _entry(
        self, pid: Any, loop_index: int, model: str, timestamp: int, input_tokens: int, cache_read: int, output: int
    ) -> Dict[str, Any]:
        entry_id = f"grok:{pid}:{timestamp}:{loop_index}"
        resolved = model or self._UNKNOWN_MODEL
        return {
            "source": self.source_name,
            "model": resolved,
            "provider": "xai",
            "input": input_tokens,
            "output": output,
            "cacheRead": cache_read,
            "cacheWrite": 0,
            "reasoning": 0,
            # Price at ingest like every other parser; cache_read is billable input
            # at the Grok cached-input rate (no separate cache_write dimension).
            # The store also has a defensive fallback for historical zero-cost rows.
            "cost": self.pricing_db.get_cost(resolved, input_tokens, output, cache_read, 0),
            "timestamp": timestamp,
            "message_id": entry_id,
            "entry_id": entry_id,
            "estimated": False,
            # Reasoning is already folded into `output`; Grok has no cache-write
            # dimension, so the billed cache-write bucket stays 0.
            "_billing": usage_billing_pricing(
                [resolved],
                input_tokens=input_tokens,
                output_tokens=output,
                cache_read=cache_read,
            ),
        }

    def _parse_all(self) -> List[Dict[str, Any]]:
        if not self.log_path.is_file():
            return []
        # The row rules live in iter_grok_usage_rows, shared with the sessions
        # harness: both sides price one survivor set, so they cannot drift.
        out: List[Dict[str, Any]] = []
        for row in iter_grok_usage_rows(self.log_path):
            out.append(
                self._entry(
                    row["pid"],
                    row["loop_index"],
                    row["model"],
                    row["timestamp_ms"],
                    row["input_tokens"],
                    row["cache_read"],
                    row["output"],
                )
            )
        return out


def iter_grok_usage_rows(log_path: Path) -> Iterator[Dict[str, Any]]:
    """The Grok unified.jsonl survivor set, shared by GrokParser (Overview)
    and the sessions harness so the two cannot drift on row rules: the string
    pre-filter, the JSON skip, pid-keyed model attribution, the min/max token
    derivation, the all-zero drop, unattributable-pid exclusion, and the
    global entry_id first-wins dedupe.

    Yields one dict per surviving row: sid, pid, loop_index, model,
    timestamp_ms, input_tokens, cache_read, output, entry_id.
    """
    model_by_pid: Dict[Any, str] = {}
    seen: set = set()
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                # Cheap pre-filter before JSON parsing: only model events and token rows matter.
                if "inference_done" not in line and "model" not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                msg = str(row.get("msg") or "")
                ctx = row.get("ctx") if isinstance(row.get("ctx"), dict) else {}
                pid = row.get("pid")

                if msg in GrokParser._MODEL_EVENTS:
                    model = GrokParser._model_change(msg, ctx)
                    if model and pid is not None:
                        model_by_pid[pid] = model
                    continue

                if msg != "shell.turn.inference_done":
                    continue
                timestamp = GrokParser._timestamp_ms(row.get("ts"))
                if timestamp is None:
                    continue
                prompt = GrokParser._int(ctx.get("prompt_tokens"))
                cache_read = min(GrokParser._int(ctx.get("cached_prompt_tokens")), prompt)
                input_tokens = max(0, prompt - cache_read)
                # reasoning is billed as output; fold it in (pricing has no reasoning rate).
                output = GrokParser._int(ctx.get("completion_tokens")) + GrokParser._int(ctx.get("reasoning_tokens"))
                if input_tokens + cache_read + output <= 0:
                    continue
                # Token rows carry no model id — attribute via the row's process. A row we
                # can't attribute can't be priced, so exclude it rather than bucket it under
                # an unpriceable unknown model.
                model = model_by_pid.get(pid)
                if not model:
                    continue
                loop_index = GrokParser._int(ctx.get("loop_index"))
                entry_id = f"grok:{pid}:{timestamp}:{loop_index}"
                if entry_id in seen:
                    continue
                seen.add(entry_id)
                yield {
                    "sid": row.get("sid"),
                    "pid": pid,
                    "loop_index": loop_index,
                    "model": model,
                    "timestamp_ms": timestamp,
                    "input_tokens": input_tokens,
                    "cache_read": cache_read,
                    "output": output,
                    "entry_id": entry_id,
                }
    except (OSError, UnicodeError):
        # Deliberate divergence from the legacy whole-file discard: the log is
        # append-only, so a mid-read error must not erase the valid prefix.
        # The entry cache is keyed on the file signature, so the next change
        # to the file re-parses it from scratch.
        return


class PiAgentParser(BaseParser):
    """
    Parser for pi-agent session files.

    =======================================================================
    PI-AGENT SESSION FILE SCHEMA
    =======================================================================
    Location: ~/.pi/agent/sessions/<encoded-cwd>/<isoTime>_<sessionUUID>.jsonl
    Override: see clientpaths.pi_agent_search_dirs
              (PI_CODING_AGENT_SESSION_DIR / PI_CODING_AGENT_DIR / legacy PI_AGENT_DIR).

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
    # 1: assistant rows keyed on (session id, row id), totalTokens fallback
    #    attributed to output, a positive recorded cost.total kept as fixed.
    persistent_parser_version = 1

    # Cost policy hook: when True, a positive usage.cost.total is kept as a
    # fixed (never-repriced) cost; when False, the row is priced from the
    # pricing DB. The hook must toggle the whole cost branch — value AND
    # billing provenance together — so a pricing-DB row is never flagged
    # cost-authoritative.
    use_recorded_cost = True

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

        cache_key = f"{self.source_name}:{','.join(str(d) for d in self.search_dirs)}"
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

                        # Track model changes. pi's modelId is always a bare id
                        # and is used verbatim; omp writes model instead, and
                        # that value may be provider-qualified
                        # ("provider/model") — split only that form so the
                        # pricing lookup sees the bare model id (O3).
                        if msg_type == "model_change":
                            model_id = obj.get("modelId")
                            raw_model = obj.get("model")
                            if isinstance(raw_model, str) and raw_model and "/" in raw_model and not model_id:
                                prefix, _, suffix = raw_model.partition("/")
                                cur_provider = obj.get("provider") or prefix or cur_provider
                                cur_model = suffix or cur_model
                            else:
                                cur_provider = obj.get("provider") or cur_provider
                                if isinstance(model_id, str) and model_id:
                                    cur_model = model_id
                                elif isinstance(raw_model, str) and raw_model:
                                    cur_model = raw_model
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

                        # Cost: prefer usage.cost.total when present and > 0,
                        # unless the source prices from the pricing DB itself
                        # (use_recorded_cost = False).
                        cost_obj = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
                        cost_total = float(cost_obj.get("total") or 0.0)
                        if self.use_recorded_cost and cost_total > 0:
                            cost = cost_total
                            # Pi's own number. A pricing edit must never move it.
                            billing = usage_billing_fixed(cost_total)
                        else:
                            cost = self.pricing_db.get_cost(model, input_t, output_t, cache_r, cache_w)
                            billing = usage_billing_pricing(
                                [model],
                                input_tokens=input_t,
                                output_tokens=output_t,
                                cache_read=cache_r,
                                cache_write=cache_w,
                            )

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
                            "entry_id": f"{self.source_name}:{entry_id}" if entry_id else "",
                            "_billing": billing,
                        })
            except Exception:
                continue

        return out


class OmpParser(PiAgentParser):
    """
    Parser for omp (oh-my-pi) session files.

    omp is a Rust+TS port of pi-mono; its session JSONL is field-compatible
    with pi's (see the PiAgentParser docstring), so this subclass inherits
    _parse_all unchanged and only redirects the search dirs:
    clientpaths.omp_agent_search_dirs() (~/.omp/agent/sessions, the XDG
    migration path, named profiles, PI_CONFIG_DIR).

    O2: entry ids and the signature cache key are keyed on self.source_name
    in the base, so omp rows are "omp:…" and never collide with pi_agent's
    cache entries.

    O6: omp bills self-hosted models from its bundled catalog, which would
    price the same endpoint at a different rate than every other source on
    the dashboard. use_recorded_cost = False prices from the pricing DB
    instead; self-hosted ids absent from it cost 0.00.

    O5: the two sources share ONE _parse_all implementation. A future edit
    to PiAgentParser._parse_all changes the stored rows of BOTH pi_agent
    and omp and needs persistent_parser_version bumps for BOTH.
    """

    source_name = "omp"
    use_recorded_cost = False

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.search_dirs = clientpaths.omp_agent_search_dirs()


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
    # 1: OTel chat spans priced from their own token attributes, events.jsonl
    #    output-only rows emitted when no OTel row already covers them.
    persistent_parser_version = 1

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
                # Billed under the raw attribute value, which is what get_cost
                # sees; the displayed model falls back to "unknown".
                "_billing": usage_billing_pricing(
                    [str(model or "")],
                    input_tokens=tokens["input"],
                    output_tokens=tokens["output"],
                    cache_read=tokens["cacheRead"],
                    cache_write=tokens["cacheWrite"],
                ),
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
                            "_billing": usage_billing_pricing([model], output_tokens=output_t),
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
    # 1: session-level rows keyed on the Hermes row id, actual/estimated cost
    #    kept as fixed, otherwise priced provider-qualified then bare.
    persistent_parser_version = 1

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
                conn = connect_sqlite_readonly(db_path)
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
                            # Hermes' own subscription-aware number; never repriced.
                            billing = usage_billing_fixed(actual_cost_f)
                        elif estimated_cost_f > 0:
                            cost = estimated_cost_f
                            billing = usage_billing_fixed(estimated_cost_f)
                        else:
                            # Try provider/model first, then bare model
                            provider_model = f"{provider}/{model}" if provider else str(model or "")
                            cost = self.pricing_db.get_cost(provider_model, input_t, output_t, cache_r, cache_w)
                            if cost == 0.0 and provider:
                                cost = self.pricing_db.get_cost(str(model or ""), input_t, output_t, cache_r, cache_w)
                            # Same ordered candidates, so a later provider-specific
                            # rate still shadows the bare key on a reprice.
                            billing = usage_billing_pricing(
                                [provider_model] + ([str(model or "")] if provider else []),
                                input_tokens=input_t,
                                output_tokens=output_t,
                                cache_read=cache_r,
                                cache_write=cache_w,
                            )

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
                            "_billing": billing,
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
    # Queried live from the source DB; nothing is stored persistently.
    persistent_parser_version = None

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
        sig = (
            self._file_signatures(),
            self._pricing_signature(),
            self.runtime_config_signature(),
        )
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
                conn = connect_sqlite_readonly(self.db_path)
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


# Snapshot copy attempts within one collect: if ZCode appends to the
# WAL or checkpoints between the two sequential copies, the db/-wal pair
# may span two generations; that attempt is dropped and re-copied.
_ZCODE_SNAPSHOT_MAX_ATTEMPTS = 3


class ZCodeSnapshotError(RuntimeError):
    """A transient failure opening a ZCode snapshot (copy or connect).

    There is no fallback open in another mode: the read fails and the
    caller retries later (failed reads are never cached).
    """


class _ZCodeSnapshot:
    """A temp-dir copy of a ZCode DB, opened for read-only reporting.

    ``conn`` is a live connection into the copy. On context exit,
    closing and cleanup always run. A close error sets ``close_failed``
    and never escapes: the data was already read from the copy, and the
    caller uses the flag to decide whether to cache the result (shared
    close-failure contract: the result is returned, never cached).
    """

    def __init__(self, conn: sqlite3.Connection, tmpdir: Path) -> None:
        self.conn = conn
        self.tmpdir = tmpdir
        self.close_failed = False


def zcode_snapshot_signatures(db_path: Path) -> tuple:
    # (path, mtime_ns, size) of exactly the files the snapshot copies:
    # the main DB and, while present, the -wal. The live -shm is
    # excluded on purpose: it is not copied (SQLite rebuilds the WAL
    # index inside the temp dir), and reader traffic in a live -shm
    # must not force coherence retries.
    out = []
    for p in (db_path, Path(str(db_path) + "-wal")):
        try:
            st = p.stat()
            out.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(p), None, None))
    return tuple(out)


def _zcode_open_snapshot(db_path: Path) -> Optional[Tuple[sqlite3.Connection, Path]]:
    # Open a private temp-dir copy, never the source file. A ?mode=ro
    # open of a WAL database is still not side-effect free: when the
    # -shm file is missing, SQLite creates it in the source directory
    # (it is the WAL coordination file, and the reader needs it whether
    # it wants it or not). The reader must never write sidecar state
    # into the source tree, so db + -wal are copied (the live rows sit
    # in the WAL) and the copy is opened normally. The -shm is NOT
    # copied: it is live coordination state, and SQLite rebuilds the
    # WAL index from the copied -wal inside the temp dir. The caller
    # deletes the whole dir afterwards.
    #
    # Coherence: the copies are sequential while ZCode may append to
    # the WAL or checkpoint between them. The db/-wal signatures are
    # taken before and after copying, and any copy failure is
    # re-checked against the pre-copy signatures: a difference means
    # a generation change landed mid-copy (a checkpoint deleting the
    # -wal between the exists() check and its copy is the normal
    # one) and the attempt is retried, bounded by
    # _ZCODE_SNAPSHOT_MAX_ATTEMPTS. A failure with unchanged
    # signatures is terminal for this read; failed reads are not
    # cached, so the next read retries.
    #
    # Returns (conn, tmpdir); None on a copy/open error or when every
    # attempt raced a source change - no fallback open in another mode.
    for _attempt in range(_ZCODE_SNAPSHOT_MAX_ATTEMPTS):
        before = zcode_snapshot_signatures(db_path)
        tmpdir: Optional[Path] = None
        try:
            tmpdir = Path(tempfile.mkdtemp(prefix="tokdash-zcode."))
            shutil.copy2(db_path, tmpdir / db_path.name)
            wal = Path(str(db_path) + "-wal")
            if wal.exists():
                shutil.copy2(wal, tmpdir / (db_path.name + "-wal"))
            if zcode_snapshot_signatures(db_path) != before:
                raise OSError("source changed mid-copy")
            conn = sqlite3.connect(str(tmpdir / db_path.name))
        except (OSError, sqlite3.Error):
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)
            # A failure accompanied by a signature change is a
            # generation change that landed mid-copy - retry it.
            if (
                zcode_snapshot_signatures(db_path) != before
                and _attempt + 1 < _ZCODE_SNAPSHOT_MAX_ATTEMPTS
            ):
                continue
            return None
        return conn, tmpdir
    return None


@contextmanager
def zcode_snapshot(db_path: Path) -> Iterator[_ZCodeSnapshot]:
    """A coherent, side-effect-free read view of a WAL-mode ZCode DB.

    Wraps _zcode_open_snapshot with the exit lifecycle: close the
    connection (a close error marks the snapshot close_failed and is
    logged by the caller, never raised here) and remove the temp dir,
    always. Raises ZCodeSnapshotError when no snapshot could be opened.
    """
    opened = _zcode_open_snapshot(db_path)
    if opened is None:
        raise ZCodeSnapshotError(f"could not open a coherent snapshot of {db_path}")
    conn, tmpdir = opened
    snap = _ZCodeSnapshot(conn, tmpdir)
    try:
        yield snap
    finally:
        try:
            conn.close()
        except Exception:
            snap.close_failed = True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class ZCodeParser(BaseParser):
    """
    Parser for ZCode (Z.ai's GLM coding app) token usage.

    =======================================================================
    ZCODE SQLITE DATABASE SCHEMA
    =======================================================================
    Location: $ZCODE_HOME/cli/db/db.sqlite (default ~/.zcode/cli/db/db.sqlite,
    %USERPROFILE%\\.zcode\\cli\\db\\db.sqlite on Windows). WAL mode, so
    live rows accumulate in the -wal file between checkpoints.

    Table: model_usage — one row per model request. Retries are distinct
    rows sharing a logical_request_id with different attempt_index values.
      - id TEXT PRIMARY KEY
      - session_id TEXT
      - model_id TEXT                (e.g. GLM-5-Turbo — the pricing key)
      - provider_id TEXT             (e.g. builtin:zai-start-plan — label only)
      - status TEXT                  (running/completed/error/cancelled)
      - started_at INTEGER           (epoch ms)
      - input_tokens INTEGER          TOTAL prompt tokens, inclusive of cache
      - output_tokens INTEGER         already includes reasoning_tokens
      - reasoning_tokens INTEGER
      - cache_read_input_tokens INTEGER    subset of input_tokens
      - cache_creation_input_tokens INTEGER

    Token accounting (see docs/development/technical-notes/ZCODE_SUPPORT_DESIGN.md):
      - input_tokens is inclusive of the cached slice, so the entry bills
        max(0, input - cache_read) as fresh input and cache_read separately.
      - output_tokens already includes reasoning, so the entry carries a
        disjoint output/reasoning split for the displayed total, but cost is
        computed from the FULL output_tokens: get_cost never sees the entry's
        reasoning bucket, and z.ai bills reasoning at the output rate. A row
        that reports reasoning above output is anomalous (the subset
        assumption is broken); for that row both are billed and displayed as
        disjoint, so the displayed total and the billed tokens stay equal.
    =======================================================================
    """

    source_name = "zcode"
    sync_capability = SourceSyncCapability(
        mode="source_native_db",
        session_store=False,
        reason="ZCode is a WAL-mode SQLite DB and supports SQL date windows.",
    )
    # Queried live from a coherent snapshot; nothing is stored persistently.
    persistent_parser_version = None

    _query_cache: ClassVar[Dict[tuple, List[Dict[str, Any]]]] = {}
    _query_cache_sig: ClassVar[tuple] = ()
    # Guards the pair above: the signature check/clear at the top of
    # collect() and the store-time recheck must run under the same lock,
    # or a query in flight under an older signature can store its stale
    # result under a signature a concurrent collect has already advanced.
    _cache_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.db_path = clientpaths.zcode_db_path()

    def _file_signatures(self) -> tuple:
        # Main file plus -wal and -shm: ZCode's live rows sit in the WAL
        # between checkpoints, so a signature on the main file alone goes
        # stale while the app is running and cached results silently lag.
        if not self.db_path.exists():
            return ()
        out: list[tuple[str, int, int]] = []
        for candidate in (
            self.db_path,
            Path(str(self.db_path) + "-wal"),
            Path(str(self.db_path) + "-shm"),
        ):
            try:
                s = candidate.stat()
                out.append((str(candidate), s.st_mtime_ns, s.st_size))
            except (FileNotFoundError, OSError):
                continue
        return tuple(out)

    def _snapshot_signatures(self) -> tuple:
        return zcode_snapshot_signatures(self.db_path)

    def _open_snapshot(self) -> Optional[Tuple[sqlite3.Connection, Path]]:
        # Direct seam to the shared open helper (collect() goes through
        # the zcode_snapshot context manager, which owns the close and
        # cleanup on top of the same helper), so the coherence rule
        # (signatures before/after the copy, bounded retry, no -shm
        # copy, temp-dir cleanup) has exactly one implementation.
        return _zcode_open_snapshot(self.db_path)

    def _build_entry(self, row: sqlite3.Row) -> Optional[Dict[str, Any]]:
        model = str(row["model_id"] or "").strip()
        if not model:
            return None

        input_total = self._i(row["input_tokens"])
        cache_r = self._i(row["cache_read_input_tokens"])
        cache_w = self._i(row["cache_creation_input_tokens"])
        output_total = self._i(row["output_tokens"])
        reasoning = self._i(row["reasoning_tokens"])

        # Token-presence guard: a cancelled request that already burned
        # tokens still bills.
        if input_total == 0 and output_total == 0 and cache_r == 0 and cache_w == 0 and reasoning == 0:
            return None

        # Fresh input only: ZCode's input_tokens is inclusive of the cached
        # slice, and get_cost bills the buckets additively.
        input_t = max(0, input_total - cache_r)
        # z.ai bills reasoning at the output rate, so cost normally uses the
        # FULL output_total (reasoning is a subset of it), while the entry's
        # split output keeps compute.py's additive displayed total correct.
        # If a row reports more reasoning than output, the subset assumption
        # is broken for that row: treat the two as disjoint for BOTH display
        # and billing, so the displayed total and the billed tokens agree.
        if reasoning > output_total:
            billed_output = output_total + reasoning
            display_output = output_total
        else:
            billed_output = output_total
            display_output = output_total - reasoning
        cost = self.pricing_db.get_cost(model, input_t, billed_output, cache_r, cache_w)

        return {
            "source": self.source_name,
            "model": model,
            "provider": str(row["provider_id"] or ""),
            "input": input_t,
            "output": display_output,
            "cacheRead": cache_r,
            "cacheWrite": cache_w,
            "reasoning": reasoning,
            "cost": cost,
            "timestamp": int(row["started_at"]),
            "entry_id": f"zcode:{row['id']}",
        }

    def _parse_all(self) -> List[Dict[str, Any]]:
        return []

    def collect(self, since_date: Optional[datetime] = None, until_date: Optional[datetime] = None) -> List[Dict[str, Any]]:
        sig = (
            self._file_signatures(),
            self._pricing_signature(),
            self.runtime_config_signature(),
        )
        s_ms = int(self._to_utc(since_date).timestamp() * 1000) if since_date else 0
        u_ms = int(self._to_utc(until_date).timestamp() * 1000) if until_date else 9999999999999
        cache_key = (s_ms, u_ms)
        # Signature validation and the cache lookup are one critical
        # section: a concurrent collector must not be able to clear and
        # repopulate the cache between them, in which case this request
        # would consume entries parsed or priced under a different
        # signature than the one it just validated.
        with type(self)._cache_lock:
            if sig != type(self)._query_cache_sig:
                type(self)._query_cache.clear()
                type(self)._query_cache_sig = sig
            cached = type(self)._query_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        out: List[Dict[str, Any]] = []
        read_ok = False
        snap = None
        if self.db_path.exists():
            try:
                with zcode_snapshot(self.db_path) as snap:
                    conn = snap.conn
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    # Probe sqlite_master inline instead of via
                    # _sqlite_table_exists (which swallows sqlite3.Error):
                    # an absent table is a legitimate empty success, but a
                    # probe error must surface as a failed read rather than
                    # be mistaken for an absent table and cached as such.
                    cur.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='model_usage'"
                    )
                    if cur.fetchone() is not None:
                        cur.execute(
                            """
                            SELECT id, session_id, model_id, provider_id, started_at,
                                   input_tokens, output_tokens, reasoning_tokens,
                                   cache_read_input_tokens, cache_creation_input_tokens
                            FROM model_usage
                            WHERE started_at >= ? AND started_at < ?
                            ORDER BY started_at
                            """,
                            (s_ms, u_ms),
                        )
                        for row in cur.fetchall():
                            try:
                                entry = self._build_entry(row)
                            except Exception:
                                continue
                            if entry is not None:
                                out.append(entry)
                    read_ok = True
            except Exception:
                # Transient read failure (snapshot copy, connect, probe, or
                # query): a restored permission or cleared SQLite error may
                # not change the file signatures, so the empty result must
                # NOT be cached - the next collect retries.
                read_ok = False
            if read_ok and snap.close_failed:
                # The data was read and is returned, but a snapshot that
                # could not be closed counts as a failed (uncached) read.
                read_ok = False

        if read_ok:
            with type(self)._cache_lock:
                # Recheck under the lock: if a concurrent collect advanced
                # the signature while we were reading, this result belongs
                # to the old signature and must not be stored under the new
                # one (it is still returned for this request).
                if sig == type(self)._query_cache_sig:
                    if len(type(self)._query_cache) >= _OPENCODE_QUERY_CACHE_MAX:
                        type(self)._query_cache.clear()
                    type(self)._query_cache[cache_key] = out
        return list(out)


class QoderIdeParser(BaseParser):
    """Parser for Qoder IDE (GUI) token usage from the local cache DB.

    =======================================================================
    QODER IDE SQLITE DATABASE SCHEMA
    =======================================================================
    Location: one deterministic pick, see clientpaths.qoder_ide_db_path():
      - Windows: %APPDATA%\\Qoder(CN)\\SharedClientCache\\cache\\db\\local.db
      - macOS:   ~/Library/Application Support/Qoder(CN)/SharedClientCache/cache/db/local.db
      - WSL:     the Windows install under /mnt/c
      - QODER_IDE_DATA_DIR overrides the app user-data root.
    Read from a temp-dir snapshot (a WAL-mode open of the live DB would
    create -shm sidecar state in the source tree), exactly like ZCode.

    Table: chat_message -- one row per message (user/assistant/tool). The
    international build fills token_info ({"prompt_tokens",
    "completion_tokens", "cached_tokens", "max_input_tokens"}) and
    model_info ({"model_key": "auto" | "<pinned model>"}); the CN build
    leaves model_info empty -- every call runs the opaque "auto" router.

    Token accounting:
      - prompt_tokens INCLUDES the cached slice, so the entry bills
        prompt - cached as fresh input and the cached slice separately
        as cacheRead.
      - Every role with parseable token_info counts: rows are 1:1 with
        model calls (verified against the ACP context_usage log), so
        user/tool rows are real usage, not noise.
    =======================================================================
    """

    source_name = "qoder"
    sync_capability = SourceSyncCapability(
        mode="source_native_db",
        session_store=False,
        reason="Qoder IDE is a SQLite DB read from a temp-dir snapshot.",
    )
    # Queried live from a coherent snapshot; nothing is stored persistently.
    persistent_parser_version = None

    _query_cache: ClassVar[Dict[tuple, List[Dict[str, Any]]]] = {}
    _query_cache_sig: ClassVar[tuple] = ()
    # Same guard as ZCodeParser: the signature check/clear and the store-time
    # recheck must run under one lock, or a read in flight under an older
    # signature can be stored under a newer one.
    _cache_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.db_path = clientpaths.qoder_ide_db_path()

    def _file_signatures(self) -> tuple:
        if self.db_path is None or not self.db_path.exists():
            return ()
        # Main file plus -wal (a missing WAL is a stable state); the live
        # -shm is excluded, same rule as ZCode.
        return zcode_snapshot_signatures(self.db_path)

    def _build_entry(self, row: sqlite3.Row) -> Optional[Dict[str, Any]]:
        try:
            info = json.loads(row["token_info"])
        except (TypeError, ValueError):
            return None
        if not isinstance(info, dict):
            return None
        prompt = max(0, self._i(info.get("prompt_tokens")))
        # The cached slice is INSIDE prompt_tokens; clamp a torn row that
        # reports more cache than prompt instead of going negative.
        cached = min(self._i(info.get("cached_tokens")), prompt)
        output = self._i(info.get("completion_tokens"))
        if prompt == 0 and output == 0 and cached == 0:
            return None
        model = "auto"
        try:
            model_info = json.loads(row["model_info"])
            model = str(model_info.get("model_key") or "") or "auto"
        except (TypeError, ValueError):
            pass
        input_t = prompt - cached
        return {
            "source": self.source_name,
            "model": model,
            "input": input_t,
            "output": output,
            "cacheRead": cached,
            "cacheWrite": 0,
            "cost": self.pricing_db.get_cost(model, input_t, output, cached, 0),
            "timestamp": int(row["gmt_create"] or 0),
            "entry_id": f"qoder:{row['id']}",
        }

    def _parse_all(self) -> Tuple[List[Dict[str, Any]], bool]:
        """All entries plus a read_ok flag (a failed read is not cacheable)."""
        if self.db_path is None or not self.db_path.exists():
            return [], True
        out: List[Dict[str, Any]] = []
        read_ok = False
        snap = None
        try:
            with zcode_snapshot(self.db_path) as snap:
                conn = snap.conn
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                # Probe sqlite_master inline (not via _sqlite_table_exists,
                # which swallows transient errors): an absent table is a
                # legitimate empty success, a probe error is a failed read.
                cur.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='chat_message'"
                )
                if cur.fetchone() is not None:
                    cur.execute(
                        """
                        SELECT id, session_id, role, model_info, token_info, gmt_create
                        FROM chat_message
                        WHERE length(token_info) > 2
                        ORDER BY gmt_create
                        """
                    )
                    for row in cur.fetchall():
                        try:
                            entry = self._build_entry(row)
                        except Exception:
                            continue
                        if entry is not None:
                            out.append(entry)
                read_ok = True
            if snap.close_failed:
                # Data was read and is returned, but a snapshot that could
                # not be closed is a failed (uncached) read.
                read_ok = False
        except Exception:
            read_ok = False
        return out, read_ok

    def collect(self, since_date: Optional[datetime] = None, until_date: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Live snapshot read with in-memory date filtering.

        Like BaseParser.collect() (the date window is applied in memory, not
        in SQL), with the native-DB rule that a failed read is returned
        empty but NOT cached, so the next collect retries.
        """
        sig = (
            self._file_signatures(),
            self._pricing_signature(),
            self.runtime_config_signature(),
        )
        with type(self)._cache_lock:
            if sig != type(self)._query_cache_sig:
                type(self)._query_cache.clear()
                type(self)._query_cache_sig = sig
            all_entries = type(self)._query_cache.get(sig)
        if all_entries is None:
            all_entries, read_ok = self._parse_all()
            if read_ok:
                with type(self)._cache_lock:
                    if sig == type(self)._query_cache_sig:
                        type(self)._query_cache[sig] = all_entries
        s_ms = int(self._to_utc(since_date).timestamp() * 1000) if since_date else 0
        u_ms = int(self._to_utc(until_date).timestamp() * 1000) if until_date else 9999999999999
        return [e for e in all_entries if s_ms <= (e.get("timestamp") or 0) < u_ms]


def _qoder_cli_iso_ms(value: Any) -> int:
    """ISO-8601 timestamp (Z or explicit offset) to epoch ms; 0 on failure."""
    if not isinstance(value, str) or not value.strip():
        return 0
    text = value.strip()
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).astimezone(timezone.utc).timestamp() * 1000)
    except ValueError:
        return 0


class QoderCliParser(BaseParser):
    """Parser for Qoder CLI usage: transcript credits + segment tokens.

    =======================================================================
    QODER CLI STORAGE LAYOUT
    =======================================================================
    Roots: clientpaths.qoder_cli_roots() -- the union of QODER_CLI_HOME
    (Tokdash-only, comma-separated), QODER_CONFIG_DIR (Qoder's real
    single-root override) and the default homes ~/.qoder (international)
    and ~/.qoder-cn (CN).

    Per root:
      projects/<project-id>/<session-id>.jsonl
          Transcript. COST SOURCE: the assistant row's message.usage
          carries credits (the exact amount Qoder billed), request_id,
          context_usage_ratio, and -- depending on build -- token fields.
          The international build (v1.1.28, verified) zero-fills the
          token fields and fills credits + ratio.
      logs/sessions/<project-id>/<session-id>/segments/*.jsonl
          Segment event log. TOKEN SOURCE: model.response.completed
          lines carry request_id (top-level; the CN emitter also writes
          it into data) and the token buckets in data.*.

    Merge: one entry per request_id across both passes and ALL roots.
    A transcript + segment of one request_id is a MERGE (cost from the
    transcript, tokens from the segment when non-zero); the same type in
    two roots is a true duplicate (first root in scan order wins). The
    merge needs cross-file visibility, so the source syncs as a whole
    (source_replace), never file by file.

    Token accounting:
      - input_tokens EXCLUDES the cache buckets (Anthropic style), so
        the buckets stay separate for the additive aggregator.
      - context_usage_ratio is prompt_tokens / window and recovered as
        input = int(round(ratio * window)), exact on every captured
        request. Windows are model-dependent and only auto @ 180000 is
        evidenced (both captured machines), so recovery defaults to
        auto only; a pinned model recovers only under an explicit
        QODER_CLI_CONTEXT_WINDOW override, which then applies to every
        model. Recovery also requires both cache buckets to be 0: the
        ratio is the TOTAL prompt (cache included), and assigning it to
        input beside non-zero cache buckets would double-count them.
      - A record with nothing attributable (all token fields 0 and no
        usable ratio) is skipped even when credits > 0: the aggregator
        drops zero-token rows before reading their cost, so it could
        never be displayed (documented under-count edge).
    =======================================================================
    """

    source_name = "qoder_cli"
    sync_capability = SourceSyncCapability(
        mode="source_replace",
        append_jsonl=False,
        session_store=False,
        reason=(
            "the cost (transcript) and tokens (segment) of one request live in "
            "different files, so the per-request_id merge needs cross-file "
            "visibility"
        ),
    )
    # 1: request_id-keyed transcript/segment merge with billing-provenance
    #    entries (fixed for credit rows, pricing for token rows).
    persistent_parser_version = 1

    # The only evidenced context window; model-dependent, so it applies to
    # auto only unless QODER_CLI_CONTEXT_WINDOW is set explicitly.
    _AUTO_CONTEXT_WINDOW = 180_000
    # Documented default for an unset/invalid QODER_USD_PER_CREDIT. An
    # estimate (not a Qoder-published rate), so credit-derived costs stay
    # labeled estimates in user-facing docs.
    _DEFAULT_USD_PER_CREDIT = 0.01

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.roots = clientpaths.qoder_cli_roots()

    # --- runtime configuration ---------------------------------------------

    def _runtime_config(self) -> Tuple[Optional[float], Optional[int]]:
        """Validated overrides: (usd_per_credit or None, window or None).

        Unparseable, non-finite, zero or negative values are rejected and
        treated as unset (the documented default policy applies) instead of
        blanking the source or letting NaN/negatives into the output.
        """
        rate: Optional[float] = None
        raw = os.environ.get("QODER_USD_PER_CREDIT", "").strip()
        if raw:
            try:
                value = float(raw)
            except ValueError:
                value = float("nan")
            if not math.isfinite(value) or value <= 0:
                logger.warning(
                    "tokdash qoder_cli: invalid QODER_USD_PER_CREDIT %r; "
                    "using the $0.01/credit estimate",
                    raw,
                )
            else:
                rate = value
        window: Optional[int] = None
        raw = os.environ.get("QODER_CLI_CONTEXT_WINDOW", "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError:
                value = None
            if value is None or value <= 0:
                logger.warning(
                    "tokdash qoder_cli: invalid QODER_CLI_CONTEXT_WINDOW %r; "
                    "window stays unset (auto-only ratio recovery)",
                    raw,
                )
            else:
                window = value
        return rate, window

    def runtime_config_signature(self) -> Optional[Dict[str, Any]]:
        """The validated overrides themselves, for the cache identities.

        Storing the override -- not the effective value -- matters for the
        window: unset (auto-only recovery) and an explicit 180000 (applies
        to every model) behave differently and must sign differently, while
        an invalid value behaves and signs like unset.
        """
        rate, window = self._runtime_config()
        return {"usd_per_credit": rate, "context_window": window}

    # --- discovery -----------------------------------------------------------

    def _discovered_files(self) -> List[Path]:
        out: List[Path] = []
        seen = set()
        for root in self.roots:
            # Transcripts: top level of projects/<project-id>/ only (the
            # <session-id>.jsonl files). The transcript/ subdir (GUI
            # session copies, usage-less) and other files are excluded by
            # the hex pattern plus the is_file check.
            candidates = (
                root.glob("projects/*/[0-9a-f-]*.jsonl"),
                root.glob("logs/sessions/*/*/segments/*.jsonl"),
            )
            for pattern in candidates:
                for f in sorted(pattern):
                    if f.is_file() and f not in seen:
                        seen.add(f)
                        out.append(f)
        return out

    def _file_signatures(self) -> tuple:
        out = []
        for f in self._discovered_files():
            s = f.stat()
            out.append((str(f), s.st_mtime_ns, s.st_size))
        return tuple(out)

    # --- passes ---------------------------------------------------------------

    def _window_for(self, model: str, override: Optional[int]) -> Optional[int]:
        if override is not None:
            return override
        if model == "auto":
            return self._AUTO_CONTEXT_WINDOW
        return None

    @staticmethod
    def _is_number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def _transcript_candidate(self, d: Dict[str, Any], window: Optional[int]) -> Optional[Tuple[str, Dict[str, Any]]]:
        msg = d.get("message")
        if not isinstance(msg, dict):
            return None
        u = msg.get("usage")
        if not isinstance(u, dict):
            return None
        rid = u.get("request_id") or d.get("uuid")
        if not rid:
            return None
        model = str(msg.get("model") or "") or "auto"
        # Presence, not truthiness: a present credits: 0 (especially with
        # billable: false) is a FREE request, not a missing value.
        has_credits = u.get("credits") is not None
        credits = float(u["credits"]) if has_credits else 0.0
        in_t = self._i(u.get("input_tokens"))
        out_t = self._i(u.get("output_tokens"))
        cache_r = self._i(u.get("cache_read_input_tokens"))
        cache_w = self._i(u.get("cache_creation_input_tokens"))
        ratio = u.get("context_usage_ratio")
        ratio_usable = (
            self._is_number(ratio)
            and self._window_for(model, window) is not None
        )
        if in_t == 0 and cache_r == 0 and cache_w == 0 and ratio_usable:
            in_t = max(0, int(round(float(ratio) * self._window_for(model, window))))
        # Skip records where nothing is attributable (see class docstring).
        if in_t == 0 and out_t == 0 and cache_r == 0 and cache_w == 0 and not ratio_usable:
            return None
        return str(rid), {
            "has_credits": has_credits,
            "credits": credits,
            "input": in_t,
            "output": out_t,
            "cacheRead": cache_r,
            "cacheWrite": cache_w,
            "model": model,
            "ts": _qoder_cli_iso_ms(d.get("timestamp")),
        }

    def _segment_candidate(self, d: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
        if d.get("type") != "model.response.completed":
            return None
        data = d.get("data")
        if not isinstance(data, dict):
            return None
        # Top-level is the international reality; the data-level read is a
        # compatibility fallback for the CN emitter shape.
        rid = d.get("request_id") or data.get("request_id")
        if not rid:
            return None
        in_t = self._i(data.get("input_tokens"))
        out_t = self._i(data.get("output_tokens"))
        cache_r = self._i(data.get("cache_read_input_tokens"))
        cache_w = self._i(data.get("cache_creation_input_tokens"))
        # A fully-zero event (the current international behavior)
        # contributes nothing on its own.
        if in_t == 0 and out_t == 0 and cache_r == 0 and cache_w == 0:
            return None
        return str(rid), {
            "input": in_t,
            "output": out_t,
            "cacheRead": cache_r,
            "cacheWrite": cache_w,
            "model": str(data.get("model") or "") or "auto",
            "ts": _qoder_cli_iso_ms(d.get("ts")),
        }

    # --- merge ------------------------------------------------------------------

    def _merged_entry(
        self,
        rid: str,
        tcand: Optional[Dict[str, Any]],
        scand: Optional[Dict[str, Any]],
        rate: float,
    ) -> Optional[Dict[str, Any]]:
        base = tcand if tcand is not None else scand
        model = base["model"]
        ts = base["ts"]
        if scand is not None:
            # The segment is the finer-grained token truth.
            in_t, out_t = scand["input"], scand["output"]
            cr, cw = scand["cacheRead"], scand["cacheWrite"]
        else:
            in_t, out_t = tcand["input"], tcand["output"]
            cr, cw = tcand["cacheRead"], tcand["cacheWrite"]
        if in_t == 0 and out_t == 0 and cr == 0 and cw == 0:
            return None
        if tcand is not None and tcand["has_credits"]:
            # Provider-reported cost: credits are exact, the credit->USD
            # rate is an estimate, and the result is never repriced.
            billing = usage_billing_fixed(tcand["credits"] * rate)
            cost_authoritative = True
        else:
            billing = usage_billing_pricing(
                [model],
                input_tokens=in_t,
                output_tokens=out_t,
                cache_read=cr,
                cache_write=cw,
            )
            cost_authoritative = False
        return {
            "source": self.source_name,
            "model": model,
            "input": in_t,
            "output": out_t,
            "cacheRead": cr,
            "cacheWrite": cw,
            "cost": usage_entry_cost(billing, self.pricing_db),
            "_billing": billing,
            "costAuthoritative": cost_authoritative,
            "entry_id": f"qoder-cli:{rid}",
            "timestamp": ts,
        }

    def _parse_all(self) -> List[Dict[str, Any]]:
        rate, window = self._runtime_config()
        # The documented default applies when the override is unset or
        # invalid; the runtime signature still distinguishes unset from an
        # explicit value, so a later fix re-parses either way.
        effective_rate = self._DEFAULT_USD_PER_CREDIT if rate is None else rate
        # One global candidate map per type across ALL roots: first root in
        # scan order wins between candidates of the same type (true
        # duplicate), while a transcript in one root and a segment in
        # another are complementary and merge.
        transcript_cands: Dict[str, Dict[str, Any]] = {}
        segment_cands: Dict[str, Dict[str, Any]] = {}
        for path in self._discovered_files():
            # Whole-source correctness: an open/read failure on ANY
            # discovered file aborts the whole parse. sync_source computes
            # every row before it deletes the stored corpus, so raising
            # preserves the prior corpus. (Kimi's per-file
            # catch-and-continue is only safe under file_replace.)
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
            is_segment = "segments" in path.parts
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue  # malformed individual line: skip
                if not isinstance(d, dict):
                    continue
                cand = (
                    self._segment_candidate(d)
                    if is_segment
                    else self._transcript_candidate(d, window)
                )
                if cand is None:
                    continue
                target = segment_cands if is_segment else transcript_cands
                rid, data = cand
                if rid not in target:
                    target[rid] = data
        entries: List[Dict[str, Any]] = []
        for rid, tcand in transcript_cands.items():
            scand = segment_cands.pop(rid, None)
            entry = self._merged_entry(rid, tcand, scand, effective_rate)
            if entry is not None:
                entries.append(entry)
        for rid, scand in segment_cands.items():
            entry = self._merged_entry(rid, None, scand, effective_rate)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda e: e["timestamp"])
        return entries



class DSHParser(BaseParser):
    """Parser for DeepSeek Harness (dsh) session logs.

    Location: ``$DSH_HOME/sessions/<project-key>/<session-id>/session.jsonl.zstd``
    (or an uncompressed ``session.jsonl``), defaulting to ``~/.dsh``.

    Each file is an append-only logical JSONL event log whose first row is the
    session header; provider usage arrives as an early ``assistant/chunk``
    usage sample and/or the finalized ``assistant/message`` usage, folded
    replace-not-add per ``(turn, step)``. Framing, fork boundaries and the
    fold itself live in ``sources/dsh_log.py``, shared with the session parser.
    """

    source_name = "dsh"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        append_jsonl=False,
        session_store=True,
        reason=(
            "DSH append batches are concatenated zstd frames, and a final usage message replaces an "
            "earlier same-step chunk; changed files are reparsed whole."
        ),
    )
    # 1: folded (turn, step) usage samples keyed on dsh_entry_id. The shared
    #    decoder's own versions ride along in persistent_parser_signature().
    persistent_parser_version = 1

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.sessions_dir = clientpaths.dsh_sessions_dir()

    def persistent_parser_signature(self) -> Dict[str, Any]:
        """DSH also depends on the shared log decoder, so its versions ride along.

        Framing/fold semantics live in ``sources/dsh_log.py``, not here: bumping
        either decoder version changes what this parser stores and must
        invalidate DSH — and only DSH, which is why it is folded into this
        source's identity rather than into a shared token.
        """
        signature = super().persistent_parser_signature()
        signature["decoder"] = {
            "version": dsh_log_module.DSH_DECODER_VERSION,
            "accounting": dsh_log_module.DSH_ACCOUNTING_VERSION,
        }
        return signature

    def _file_signatures(self) -> tuple:
        return _timed_sigs(
            f"dsh:{self.sessions_dir}",
            lambda: dsh_file_signatures(self.sessions_dir),
        )

    def _parse_all(self) -> List[Dict[str, Any]]:
        # Keyed on the stable entry id so duplicate physical files for one
        # session id (both suffixes present, or one id under two project keys)
        # never bill twice. Discovery is sorted, so the later file's sample
        # wins deterministically; the emitted order below stays timestamp-sorted.
        by_entry_id: Dict[str, Dict[str, Any]] = {}
        for path_str, _, _ in self._file_signatures():
            try:
                decoded = decode_dsh_session_file(Path(path_str))
                if decoded.skip_reason is not None or decoded.header is None:
                    continue
                session_id = str(decoded.header.get("id") or Path(path_str).parent.name)
                for sample in fold_dsh_usage_samples(decoded.header, decoded.events):
                    model = sample["model"]
                    input_t = sample["input"]
                    output_t = sample["output"]
                    cache_r = sample["cache_read"]
                    cache_w = sample["cache_write"]
                    entry = {
                        "source": self.source_name,
                        "model": model,
                        "provider": sample["provider"],
                        "input": input_t,
                        "output": output_t,
                        "cacheRead": cache_r,
                        "cacheWrite": cache_w,
                        "reasoning": 0,
                        "cost": self.pricing_db.get_cost(model, input_t, output_t, cache_r, cache_w),
                        "timestamp": int(sample["timestamp_ms"]),
                        "entry_id": dsh_entry_id(session_id, sample["turn"], sample["step"]),
                        "_billing": usage_billing_pricing(
                            [model],
                            input_tokens=input_t,
                            output_tokens=output_t,
                            cache_read=cache_r,
                            cache_write=cache_w,
                        ),
                    }
                    by_entry_id[entry["entry_id"]] = entry
            except Exception:
                # One malformed file never blanks the whole DSH source.
                continue
        out = sorted(by_entry_id.values(), key=lambda entry: int(entry.get("timestamp", 0) or 0))
        return out


class ReasonixParser(BaseParser):
    """Parser for Reasonix daily token usage logs.

    Location: ``$REASONIX_HOME/stats/YYYY-MM-DD.jsonl`` (``REASONIX_HOME``
    defaults to ``~/.reasonix``), one JSON object per provider request appended
    during the day. Reasonix talks to any provider configured in its
    ``config.toml``, so ``model`` is ``<provider-label>/<model-id>`` with the
    provider half a user-chosen config name, not a vendor.

    Observed row::

        {"ts":"2026-08-15T12:24:35.556495944+01:00","model":"minimax-cn/MiniMax-M3",
         "prompt":8247,"completion":56,"cache_hit":128,"cache_miss":8119,"total":8303}

    ``prompt`` counts cached and uncached input together (``prompt ==
    cache_hit + cache_miss``), while Tokdash's buckets are disjoint: ``input``
    is uncached input and ``cacheRead`` is cached input. The mapping therefore
    splits ``prompt`` rather than copying it, or every aggregate would bill the
    cached slice twice — once at the input rate and once at the cache rate::

        input      <- cache_miss, else prompt - cache_hit
        cacheRead  <- cache_hit (absent means zero)
        cacheWrite <- 0     # the stats log has no cache-write bucket
        output     <- completion
        reasoning  <- 0

    ``total``, ``requests`` and Reasonix's own ``cost_*`` / ``display_*`` fields
    are ignored: the first two are redundant and the rest describe Reasonix's
    pricing status, while Tokdash prices from its own database.
    """

    source_name = "reasonix"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        append_jsonl=False,
        session_store=True,
        reason=(
            "Reasonix stats logs are append-only daily JSONL files; entry ids are content-keyed, "
            "so a changed day file is reparsed whole without rebilling its earlier rows."
        ),
    )
    # 1: daily stats rows keyed on a content digest plus an occurrence
    #    counter, prompt split into disjoint input / cacheRead halves.
    persistent_parser_version = 1

    # Reasonix writes up to 9 fractional-second digits. datetime.fromisoformat
    # accepts only 3 or 6 before Python 3.11, and Tokdash supports 3.10, where
    # every row would otherwise be dropped as unparseable.
    _ISO_SUBSECOND_OVERFLOW = re.compile(r"(\.\d{6})\d+")

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.stats_dir = clientpaths.reasonix_stats_dir()

    def _file_signatures(self) -> tuple:
        def scan() -> tuple:
            if not self.stats_dir.exists():
                return ()
            sigs: List[Tuple[str, int, int]] = []
            for p in self.stats_dir.glob("*.jsonl"):
                try:
                    s = p.stat()
                    sigs.append((str(p), s.st_mtime_ns, s.st_size))
                except (FileNotFoundError, OSError):
                    continue
            return tuple(sorted(sigs))

        return _timed_sigs(f"reasonix:{self.stats_dir}", scan)

    @staticmethod
    def _split_model(raw_model: str) -> Tuple[str, str]:
        raw = (raw_model or "").strip()
        if "/" in raw:
            parts = raw.split("/", 1)
            return parts[0].strip(), parts[1].strip()
        return "", raw

    @staticmethod
    def _token_int(value: Any) -> Optional[int]:
        """An explicit non-negative token count, or None when invalid.

        Mirrors dsh_log._to_int: a missing field is the caller's business, an
        unusable one rejects its row rather than raising into the per-file
        handler and silently discarding the rest of the day.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        try:
            result = int(value)
        except (TypeError, ValueError):
            return None
        return result if result >= 0 else None

    @classmethod
    def _token_field(cls, entry: Dict[str, Any], key: str) -> Optional[int]:
        """A row's token count: 0 when absent, None when present but unusable.

        Absent is not malformed. Reasonix omits ``cache_hit`` when nothing was
        cached, so a missing field is a real zero; a field that is present but
        cannot be read as a count means the row cannot be trusted and is
        dropped. Coercing with ``or 0`` would erase that distinction and let
        ``false``, ``""``, ``[]`` and ``{}`` through as zeroes.
        """
        if key not in entry:
            return 0
        raw = entry[key]
        if raw is None:  # explicit JSON null reads the same as absent
            return 0
        return cls._token_int(raw)

    @classmethod
    def _timestamp_ms(cls, raw: Any) -> Optional[int]:
        if not isinstance(raw, str) or not raw.strip():
            return None
        text = cls._ISO_SUBSECOND_OVERFLOW.sub(r"\1", raw.strip().replace("Z", "+00:00"))
        try:
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            return int(parsed.timestamp() * 1000)
        except (OSError, OverflowError):
            return None

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        # Entry ids are keyed on row content, never on the file path or line
        # number: a moved REASONIX_HOME or a rewritten day file would otherwise
        # re-ingest the whole history as new rows. Two byte-identical requests
        # in one day are indistinguishable by content, so an occurrence counter
        # keeps them apart; unlike a line number it is stable under appends.
        seen_digests: Dict[str, int] = {}
        for path_str, _, _ in self._file_signatures():
            try:
                with open(path_str, "r", encoding="utf-8") as f:
                    lines = list(f)
            except Exception:
                # One unreadable day file never blanks the whole source.
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue

                ts_str = entry.get("ts")
                ts_ms = self._timestamp_ms(ts_str)
                if ts_ms is None:
                    continue

                raw_model = str(entry.get("model") or "unknown")
                provider, model = self._split_model(raw_model)
                if not model:
                    model = "unknown"

                prompt = self._token_field(entry, "prompt")
                completion = self._token_field(entry, "completion")
                # cache_hit is optional; absent means nothing was cached.
                cache_hit = self._token_field(entry, "cache_hit")
                if prompt is None or completion is None or cache_hit is None:
                    continue
                # cache_miss is optional too, but its absence has to stay
                # distinguishable from a zero so the split below knows whether
                # to trust it or fall back to the subtraction.
                if entry.get("cache_miss") is None:
                    cache_miss = None
                else:
                    cache_miss = self._token_int(entry["cache_miss"])
                    if cache_miss is None:
                        continue

                # Split prompt into its disjoint halves. cache_miss is the
                # source's own uncached count and wins; the subtraction is the
                # fallback for rows that omit it. A row whose cache_hit exceeds
                # prompt is self-contradictory, so the floor keeps input sane.
                uncached = cache_miss if cache_miss is not None else prompt - cache_hit
                uncached = max(0, uncached)
                if uncached == 0 and completion == 0 and cache_hit == 0:
                    continue

                digest = hashlib.sha256(
                    f"{ts_str}|{raw_model}|{prompt}|{completion}|{cache_hit}|{uncached}".encode("utf-8")
                ).hexdigest()
                occurrence = seen_digests.get(digest, 0)
                seen_digests[digest] = occurrence + 1
                entry_id = f"reasonix:{digest}" if not occurrence else f"reasonix:{digest}:{occurrence}"

                out.append({
                    "source": self.source_name,
                    "model": model,
                    "provider": provider,
                    "input": uncached,
                    "output": completion,
                    "cacheRead": cache_hit,
                    "cacheWrite": 0,
                    "reasoning": 0,
                    "cost": self.pricing_db.get_cost(
                        model,
                        uncached,
                        completion,
                        cache_read=cache_hit,
                        cache_write=0,
                    ),
                    "timestamp": ts_ms,
                    "entry_id": entry_id,
                    "_billing": usage_billing_pricing(
                        [model],
                        input_tokens=uncached,
                        output_tokens=completion,
                        cache_read=cache_hit,
                    ),
                })
        out.sort(key=lambda item: int(item.get("timestamp", 0) or 0))
        return out


class WorkBuddyParser(BaseParser):
    """Parse WorkBuddy (Tencent AI assistant) desktop transcripts.

    WorkBuddy appends one JSON row per event to
    ``~/.workbuddy-ai/projects/<cwd-slug>/<sessionId>.jsonl`` (same layout on
    Windows, macOS, and Linux; ``$WORKBUDDY_DATA_DIR`` points the reader at
    other stores, e.g. a Windows dir from WSL). Assistant message rows carry
    the per-call usage in ``providerData``:

      {"id": <chat.completion id>, "timestamp": <epoch ms>,
       "type": "message", "role": "assistant",
       "providerData": {"messageId": <id>, "model": <id or router alias>,
                        "rawUsage": {OpenAI-style snake-case usage + credit},
                        "usage": {camel-case mirror + details arrays}}}

    ``rawUsage`` is the primary adapter; ``usage`` is used only when
    ``rawUsage`` is absent. ``message.usage`` is deliberately never read: it
    mixes conventions (``input_tokens`` includes the cached slice while
    ``cache_read_input_tokens`` repeats it).

    Normalized entry mapping (spec: docs/local/20260821_workbuddy_support/
    FINDINGS.md, phase 1):
      model      <- providerData.model VERBATIM (router aliases such as
                    ``default-model`` are absent from the pricing DB and
                    cost 0.00; PricingDatabase normalizes for lookup)
      input      <- prompt minus cache (``prompt_cache_miss_tokens`` when
                    present; the cache slice is inside prompt_tokens)
      output     <- completion minus reasoning (reasoning is a subset of
                    completion, OpenAI convention)
      cacheRead  <- WorkBuddy UsageUtils read precedence, clamped to prompt
      cacheWrite <- 0: the vendor's write chain is extracted but unbilled
                    until it is established whether prompt_tokens /
                    prompt_cache_miss_tokens include the write slice
      cost       <- get_cost(model, fresh, FULL completion, cached, 0)
      workbuddy_credit <- rawUsage.credit, per-turn credit, metadata only

    Rows are parsed fail-soft: a bad line skips itself, never the file.
    """

    source_name = "workbuddy"
    sync_capability = SourceSyncCapability(
        mode="file_replace",
        append_jsonl=True,
        reason=(
            "WorkBuddy transcripts are append-only JSONL and every usage row "
            "carries a stable providerData.messageId, so entry_id is stable per call."
        ),
    )
    # 1: assistant message rows keyed by the provider call id; cache-inclusive
    #    prompt split into fresh input / cacheRead, reasoning split out for
    #    display while billing uses the full completion, cache writes held at 0
    #    until the vendor's write-slice semantics are verified.
    persistent_parser_version = 1

    def __init__(self, pricing_db: PricingDatabase):
        super().__init__(pricing_db)
        self.roots = clientpaths.workbuddy_roots()

    @staticmethod
    def _f(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _first_positive(cls, *values: Any) -> int:
        """First value in the chain that is a positive integer, else 0."""
        for value in values:
            n = cls._i(value)
            if n > 0:
                return n
        return 0

    def _usage_from_provider_data(self, pd: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize providerData.rawUsage (primary) or providerData.usage (fallback).

        Returns None when neither carries usable (non-zero) usage.
        """
        ru = pd.get("rawUsage")
        if isinstance(ru, dict) and ru:
            prompt = max(0, self._i(ru.get("prompt_tokens")))
            details = ru.get("prompt_tokens_details")
            # Read chain: WorkBuddy UsageUtils precedence, exactly.
            cached = self._first_positive(
                ru.get("cache_read_input_tokens"),
                ru.get("cacheReadInputTokens"),
                details.get("cached_tokens") if isinstance(details, dict) else None,
                ru.get("prompt_cache_hit_tokens"),
            )
            # Write chain: extracted in WorkBuddy order, but deliberately
            # unbilled (see _build_entry).
            write = self._first_positive(
                ru.get("cache_creation_input_tokens"),
                ru.get("cacheCreationInputTokens"),
                ru.get("prompt_cache_write_tokens"),
            )
            missed = max(0, self._i(ru.get("prompt_cache_miss_tokens")))
            completion = max(0, self._i(ru.get("completion_tokens")))
            comp_details = ru.get("completion_tokens_details")
            reasoning = self._i(
                comp_details.get("reasoning_tokens") if isinstance(comp_details, dict) else None
            ) or self._i(ru.get("completion_thinking_tokens"))
            credit = self._f(ru.get("credit"))
        else:
            usage = pd.get("usage")
            if not isinstance(usage, dict) or not usage:
                return None
            # Fallback schema: camel-case, cache/reasoning in details arrays,
            # no cache-write column.
            prompt = max(0, self._i(usage.get("inputTokens")))
            cached = 0
            for detail in usage.get("inputTokensDetails") or []:
                if isinstance(detail, dict):
                    cached += max(0, self._i(detail.get("cached_tokens")))
            write = 0
            missed = 0
            completion = max(0, self._i(usage.get("outputTokens")))
            reasoning = 0
            for detail in usage.get("outputTokensDetails") or []:
                if isinstance(detail, dict):
                    reasoning += max(0, self._i(detail.get("reasoning_tokens")))
            credit = self._f(usage.get("credit"))

        # Subset counters can never exceed their parent, no matter what the
        # provider stamped.
        cached = min(max(0, cached), prompt)
        reasoning = min(max(0, reasoning), completion)

        if prompt == 0 and completion == 0:
            return None
        model = str(pd.get("model") or pd.get("requestModelId") or "workbuddy-auto")
        return {
            "model": model,
            "prompt": prompt,
            "cached": cached,
            "write": write,
            "missed": missed,
            "completion": completion,
            "reasoning": reasoning,
            "credit": credit,
        }

    def _build_entry(self, usage: Dict[str, Any], ts_ms: int, call_id: str) -> Dict[str, Any]:
        prompt = usage["prompt"]
        cached = usage["cached"]
        completion = usage["completion"]
        reasoning = usage["reasoning"]
        # The cache slice is inside prompt_tokens; prefer the explicit miss
        # count when present, and never go negative.
        fresh = max(0, usage["missed"] or (prompt - cached))
        # Reasoning is a subset of completion and compute.py adds it on top of
        # output, so it must be split out of output here.
        output = max(0, completion - reasoning)
        # usage["write"] is extracted but deliberately unbilled: it is not
        # yet established whether prompt_tokens / prompt_cache_miss_tokens
        # include the write slice (every captured row has write = 0), and
        # emitting it could double-count it. Open item: FINDINGS.md phase 1.
        return {
            "source": self.source_name,
            "model": usage["model"],
            "provider": "",
            "input": fresh,
            "output": output,
            "cacheRead": cached,
            "cacheWrite": 0,
            "reasoning": reasoning,
            "cost": self.pricing_db.get_cost(usage["model"], fresh, completion, cached, 0),
            "_billing": usage_billing_pricing(
                [usage["model"]],
                input_tokens=fresh,
                output_tokens=completion,
                cache_read=cached,
                cache_write=0,
            ),
            "workbuddy_credit": usage["credit"],
            "entry_id": f"workbuddy:{call_id}",
            "message_id": call_id,
            "timestamp": int(ts_ms),
        }

    def _entry_from_row(self, record: Dict[str, Any], seen_call_ids: set) -> Optional[Dict[str, Any]]:
        if record.get("type") != "message" or record.get("role") != "assistant":
            return None
        pd = record.get("providerData")
        if not isinstance(pd, dict):
            return None
        usage = self._usage_from_provider_data(pd)
        if usage is None:
            return None
        call_id = str(pd.get("messageId") or record.get("id") or "").strip()
        ts_ms = self._i(record.get("timestamp"))
        if not call_id or ts_ms <= 0:
            return None
        if call_id in seen_call_ids:
            return None
        seen_call_ids.add(call_id)
        return self._build_entry(usage, ts_ms, call_id)

    def _file_signatures(self) -> tuple:
        def _scan() -> tuple:
            sigs: List[Tuple[str, int, int]] = []
            for root in self.roots:
                sigs.extend(_glob_sigs(str(root / "projects" / "*" / "*.jsonl")))
            return tuple(sorted(set(sigs)))

        cache_key = "workbuddy:" + "|".join(str(r) for r in self.roots)
        return _timed_sigs(cache_key, _scan)

    def _parse_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen_call_ids: set[str] = set()
        for path_str, _, _ in self._file_signatures():
            try:
                handle = open(path_str, "r", encoding="utf-8")
            except OSError:
                continue
            with handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        entry = (
                            self._entry_from_row(record, seen_call_ids)
                            if isinstance(record, dict)
                            else None
                        )
                    except Exception:
                        logger.warning("workbuddy: skipping malformed row in %s", path_str)
                        entry = None
                    if entry is not None:
                        out.append(entry)
        out.sort(key=lambda item: int(item.get("timestamp", 0) or 0))
        return out


def search_dir_claim_key(directory: Any) -> str:
    """Canonical ownership key for a tree-scanned directory.

    Shared by CodingToolsUsageTracker._claim_search_dirs (Overview) and the
    sessions loaders that must agree with it on dir ownership (e.g. omp drops
    the dirs pi_agent claims), so the two implementations cannot drift.
    """
    key = str(Path(str(directory)).expanduser().resolve())
    return key.lower() if os.name == "nt" else key


class CodingToolsUsageTracker:
    """Registry-driven tracker for coding clients."""

    # From `tokscale --help`: OpenCode, Claude Code, Codex, Gemini, Amp, Kimi.
    # TODO: Amp parser is currently a placeholder until we have stable local fixtures
    # with explicit token fields.

    def __init__(self):
        self.entries: List[Dict[str, Any]] = []
        self.source_errors: List[Dict[str, str]] = []
        self.pricing_db = PricingDatabase()
        self.parsers = {
            "opencode": OpenCodeParser(self.pricing_db),
            "kilocode": KiloCodeParser(self.pricing_db),
            "cline": ClineParser(self.pricing_db),
            "codex": CodexParser(self.pricing_db),
            "claude": ClaudeParser(self.pricing_db),
            "gemini_cli": GeminiCLIParser(self.pricing_db),
            "antigravity_cli": AntigravityCLIParser(self.pricing_db),
            "amp": AmpParser(self.pricing_db),
            "kimi": KimiParser(self.pricing_db),
            "grok": GrokParser(self.pricing_db),
            "pi_agent": PiAgentParser(self.pricing_db),
            "omp": OmpParser(self.pricing_db),
            "copilot_cli": CopilotCLIParser(self.pricing_db),
            "hermes": HermesParser(self.pricing_db),
            "mimo": MimoParser(self.pricing_db),
            "zcode": ZCodeParser(self.pricing_db),
            "dsh": DSHParser(self.pricing_db),
            "reasonix": ReasonixParser(self.pricing_db),
            "workbuddy": WorkBuddyParser(self.pricing_db),
            "qoder": QoderIdeParser(self.pricing_db),
            "qoder_cli": QoderCliParser(self.pricing_db),
        }
        # Two parsers must never scan the same directory: the usage store
        # dedups on (source, entry_key) and never across sources, so an
        # overlap (e.g. PI_CODING_AGENT_DIR pointed at an omp tree) would
        # count every token twice.
        self._dir_conflicts = self._claim_search_dirs()

    def _claim_search_dirs(self) -> List[Dict[str, str]]:
        """Assign each tree-scanned directory to exactly one parser.

        Registration order decides the owner; a later source that claims a
        dir already claimed drops it from its own search list. The returned
        conflict notes are re-emitted by collect() — a note appended here in
        __init__ would be wiped by collect's source_errors reset before any
        consumer saw it.
        """
        claimed: Dict[str, str] = {}
        conflicts: List[Dict[str, str]] = []
        for name, parser in self.parsers.items():
            dirs = getattr(parser, "search_dirs", None)
            if not dirs:
                continue
            kept = []
            for d in dirs:
                key = search_dir_claim_key(d)
                owner = claimed.get(key)
                if owner is not None and owner != name:
                    conflicts.append({
                        "source": name,
                        "error": (
                            f"search dir {d} is also claimed by {owner}; {name} dropped "
                            f"it to avoid counting its tokens twice"
                        ),
                    })
                    continue
                claimed.setdefault(key, name)
                kept.append(d)
            if len(kept) != len(dirs):
                parser.search_dirs = kept
        return conflicts

    def collect(self, since_date: Optional[datetime] = None, until_date: Optional[datetime] = None, sources: Optional[List[str]] = None):
        self.entries = []
        self.source_errors = []
        selected = sources or list(self.parsers.keys())
        # Re-emit the dir-ownership notes computed in __init__ (see
        # _claim_search_dirs); the reset above would otherwise wipe them.
        # Only notes for the requested sources — a caller collecting
        # ["claude"] should not be told about omp's dropped dir.
        self.source_errors.extend(
            c for c in getattr(self, "_dir_conflicts", ()) if c["source"] in selected
        )
        for name in selected:
            parser = self.parsers.get(name)
            if parser:
                try:
                    self.entries.extend(parser.collect(since_date, until_date))
                except Exception as exc:
                    # One broken source (locked dir, unreadable file, bad path)
                    # must not blank the whole usage view — skip it, keep the rest,
                    # and record it so consumers can show "unavailable" instead of 0.
                    logger.warning("tokdash usage source %s failed; skipped", name, exc_info=True)
                    self.source_errors.append({"source": name, "error": str(exc)})

    def to_json(self) -> Dict[str, Any]:
        return {"entries": self.entries, "total": len(self.entries), "source_errors": self.source_errors}


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
    parser.add_argument(
        "--sources",
        type=str,
        default=None,
        help="Comma-separated source names (default: all registered sources)",
    )
    args = parser.parse_args()

    since_date, until_date = _date_range(args)
    sources = [s.strip() for s in (args.sources or "").split(",") if s.strip()]

    tracker = CodingToolsUsageTracker()
    tracker.collect(since_date, until_date, sources or None)

    if args.json:
        print(json.dumps(tracker.to_json(), indent=2))
    else:
        print(f"Total entries: {len(tracker.entries)}")


if __name__ == "__main__":
    main()
