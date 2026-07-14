from __future__ import annotations

import inspect
import json
import os
import sqlite3
import threading
import hashlib
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import clientpaths
from .codex_quota_windows import classify_codex_api_windows
from .filelock import process_lock


SCHEMA_VERSION = 5
SIGNATURE_VERSION = 2

# quota_history consumption: reset times within this many seconds are treated as the same
# physical window, absorbing the ±1s poll-to-poll jitter (and Codex start-of-window
# splinters) that would otherwise split one window into two epochs and double/under-count.
# Genuinely distinct windows are far larger than this (>= ~1h in real data), so they are
# never merged.
RESET_JITTER_SECONDS = 5
QUOTA_RECOVERY_EPSILON_PERCENT = 0.5

# quota_history consumption/points: at a window rollover, providers can return the OLD
# window's near-max used_percent stamped with the NEW window's resets_at for one ~15-minute
# sample, then revert. Two shapes, opposite effects: an interior lone peak (0 -> 100 -> 2)
# over-counts consumption by the spike; a leading carry-over (100 -> 2 as the first reading
# of a new epoch) under-counts by becoming the running-high baseline and masking the whole
# window. Both are physically impossible within one window (used_percent only rises until
# reset), so a same-epoch reversion of at least this many percentage points is dropped as a
# torn read before points/consumption are derived. See `_drop_torn_reads`.
QUOTA_TORN_READ_MIN_PERCENT = 40.0

_WRITE_LOCK = threading.RLock()
_SCHEMA_LOCK = threading.RLock()
_SCHEMA_READY: set[str] = set()
_CODEX_PERCENT_SCALE_REPAIR_META_KEY = "quota_codex_percent_scale_repair_v2"
_CODEX_PERCENT_SCALE_REPAIR_DONE = "done"


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _codex_window_used_percent_from_raw(bucket: str, raw_json: str | None) -> float | None:
    try:
        raw = json.loads(raw_json or "{}")
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    usage = _dict_or_none(raw.get("usage"))
    if usage is None:
        return None

    if bucket in {"5h", "7d"}:
        # Mirror _usage_rate_limits precedence: an explicit rate_limits.primary/secondary
        # wins; rate_limit windows only fill the gaps.
        rate_limits = _dict_or_none(usage.get("rate_limits"))
        if rate_limits is not None:
            windows = classify_codex_api_windows(
                _dict_or_none(rate_limits.get("primary")),
                _dict_or_none(rate_limits.get("secondary")),
            )
            window = windows.get(bucket)
            if window is not None:
                return _as_float(window.get("used_percent"))
        rate_limit = _dict_or_none(usage.get("rate_limit"))
        if rate_limit is not None:
            primary = _dict_or_none(rate_limit.get("primary_window"))
            secondary = _dict_or_none(rate_limit.get("secondary_window"))
            if primary is None and secondary is None:
                # Flat legacy shape: "rate_limit" IS the 5h window. A 7d row's value came
                # from additional_rate_limits, so it cannot be re-derived from here.
                return _as_float(rate_limit.get("used_percent")) if bucket == "5h" else None
            window = classify_codex_api_windows(primary, secondary).get(bucket)
            return _as_float(window.get("used_percent")) if window is not None else None
        return None

    item = _dict_or_none(raw.get("additional_rate_limit"))
    if item is None:
        return None
    feature = str(item.get("metered_feature") or "")
    if not feature:
        return None
    nested = _dict_or_none(item.get("rate_limit")) or item
    primary = _dict_or_none(nested.get("primary_window"))
    secondary = _dict_or_none(nested.get("secondary_window"))
    if bucket in {f"{feature}_5h", f"{feature}_7d"}:
        suffix = "7d" if bucket.endswith("_7d") else "5h"
        window = classify_codex_api_windows(primary, secondary).get(suffix)
        return _as_float(window.get("used_percent")) if window is not None else None
    if bucket == feature and primary is None and secondary is None:
        # Legacy single-window shape keeps the unsuffixed bucket id (see codex.py).
        return _as_float(nested.get("used_percent"))
    return None


def _repair_codex_api_percent_scale_rows(conn: sqlite3.Connection) -> int:
    """Rewrite codex_api rows whose used_percent was fraction-scaled by the old parser.

    A corrupted row is provable from its own raw payload: the API value sits in (0, 1]
    (percent scale, so at most 1% used) while the stored value equals value*100. The scan
    is incremental — the meta key holds the highest row id already checked, so rows a
    stale old-parser process writes after the initial sweep still get repaired on a later
    open. Once a scan over newly written rows finds nothing to repair (every writer is on
    the fixed parser), the key flips to "done" and the scan never runs again.
    """
    state_row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (_CODEX_PERCENT_SCALE_REPAIR_META_KEY,)
    ).fetchone()
    state = str(state_row["value"]) if state_row is not None else ""
    if state == _CODEX_PERCENT_SCALE_REPAIR_DONE:
        return 0
    try:
        watermark = int(state)
    except ValueError:
        watermark = 0
    rows = conn.execute(
        """
        SELECT id, bucket, used_percent, raw_json
        FROM quota_snapshots
        WHERE id > ?
          AND provider = 'codex'
          AND source = 'codex_api'
          AND status = 'ok'
          AND used_percent > 0
          AND raw_json IS NOT NULL
        """,
        (watermark,),
    ).fetchall()
    updates: list[tuple[float, int]] = []
    max_id = watermark
    for row in rows:
        max_id = max(max_id, int(row["id"]))
        stored = _as_float(row["used_percent"])
        if stored is None:
            continue
        raw_used = _codex_window_used_percent_from_raw(str(row["bucket"]), row["raw_json"])
        if raw_used is None or not (0.0 < raw_used <= 1.0):
            continue
        scaled = round(raw_used * 100.0, 4)
        if abs(stored - scaled) <= 0.0001 and abs(stored - raw_used) > 0.0001:
            updates.append((round(raw_used, 4), int(row["id"])))
    if updates:
        conn.executemany("UPDATE quota_snapshots SET used_percent = ? WHERE id = ?", updates)
    # An empty scan range proves nothing about the writer fleet, so only a non-empty
    # clean scan may declare the repair finished.
    next_state = _CODEX_PERCENT_SCALE_REPAIR_DONE if (rows and not updates) else str(max_id)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        (_CODEX_PERCENT_SCALE_REPAIR_META_KEY, next_state),
    )
    return len(updates)


def _quota_history_uses_adjacent_deltas(provider: str, bucket: str, resets_at: Any) -> bool:
    """Whether quota consumption should count adjacent positive deltas.

    Fixed-window limits are better handled by the running-high path below: a reset advances
    ``resets_at`` and starts a new baseline, while transient dips inside one reset epoch do
    not inflate usage. Codex's primary and suffixed feature weekly buckets are rolling
    7-day windows, so usage can age out while ``resets_at`` stays stable. Rows without a
    reset timestamp have the same ambiguity: a reset is visible only as a drop.

    Legacy Codex metered-feature buckets without a ``_7d`` suffix are not distinguishable
    here without parsing raw JSON for every history row; those keep fixed-window semantics
    unless ``resets_at`` is missing.
    """
    if resets_at is None:
        return True
    if provider == "codex" and (bucket == "7d" or bucket.endswith("_7d")):
        return True
    return False


def _quota_adjacent_consumed_delta(prev: float, pct: float, prior_high: float | None) -> float:
    """Positive adjacent delta, with transient recovery to a prior high suppressed.

    Rolling/unknown-reset windows need adjacent deltas so real usage after an age-out/drop
    still counts. The hard ambiguous case is a low outlier that simply recovers to the
    previous high. Treat recovery to within a small band around that prior high as noise;
    if it rises clearly above the old high, count only the excess above the old high.
    """
    if pct <= prev:
        return 0.0
    delta = pct - prev
    if prior_high is not None and prev < prior_high and pct >= prior_high - QUOTA_RECOVERY_EPSILON_PERCENT:
        delta = max(0.0, pct - prior_high)
    return 0.0 if delta <= QUOTA_RECOVERY_EPSILON_PERCENT else delta


def _drop_torn_reads(
    ordered: list[tuple[int, float, Any]], reset_epoch: dict[Any, Any]
) -> list[tuple[int, float, Any]]:
    """Drop reset-boundary torn-read samples from a per-series ordered reading list.

    Display/derivation-only: this filters the list used to build `points` and
    `consumption`; it never touches raw DB rows. A reading is dropped when it is
    >= QUOTA_TORN_READ_MIN_PERCENT above the *immediate next* reading in the same epoch,
    and either it is that epoch's first reading (leading carry-over) or it is also
    >= QUOTA_TORN_READ_MIN_PERCENT above the *immediate previous* reading in the same epoch
    (interior lone peak). Neighbours in a different epoch (or missing) count as absent, so a
    reversion that can't be confirmed within the same window is never dropped.
    """

    def epoch(resets_at: Any) -> Any:
        return reset_epoch.get(resets_at, resets_at)

    keep = [True] * len(ordered)
    for i, (_ts, pct, rst) in enumerate(ordered):
        e = epoch(rst)
        nxt = (
            ordered[i + 1][1]
            if i + 1 < len(ordered) and epoch(ordered[i + 1][2]) == e
            else None
        )
        if nxt is None or pct - nxt < QUOTA_TORN_READ_MIN_PERCENT:
            continue
        prv = (
            ordered[i - 1][1]
            if i - 1 >= 0 and epoch(ordered[i - 1][2]) == e
            else None
        )
        if prv is None or pct - prv >= QUOTA_TORN_READ_MIN_PERCENT:  # leading OR interior peak
            keep[i] = False
    return [row for k, row in zip(keep, ordered) if k]


def persistent_usage_db_enabled() -> bool:
    value = os.environ.get("TOKDASH_USAGE_DB", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def usage_db_path() -> Path:
    """Delegates to :func:`tokdash.clientpaths.usage_db_path` (Tier 0 seams refactor)."""
    return clientpaths.usage_db_path()


@contextmanager
def usage_db_process_lock(db_path: Optional[Path] = None):
    """Serialize DB writes/resyncs across Tokdash processes when supported.

    Thin wrapper delegating to :func:`tokdash.filelock.process_lock` (Tier 0 seams
    refactor) so this module's lock contract — and the additional process-local
    ``_WRITE_LOCK`` serialization below — stays exactly as it was for callers.
    """
    path = db_path or usage_db_path()
    with _WRITE_LOCK:
        with process_lock(Path(str(path) + ".lock")):
            yield


def durable_usage_db_enabled() -> bool:
    value = os.environ.get("TOKDASH_USAGE_DB_DURABLE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def parser_code_signature(obj: Any) -> dict[str, Any]:
    """Return a cheap signature for the parser implementation module.

    The persistent store is a parse cache, not a source of truth. Including the
    parser module file in the signature invalidates cached rows after package
    upgrades or local parser edits, even when the source logs did not change.
    """
    try:
        obj = getattr(obj, "__wrapped__", obj)
        if inspect.isclass(obj):
            label = f"{obj.__module__}.{obj.__name__}"
            path = inspect.getsourcefile(obj)
        elif inspect.isfunction(obj):
            label = f"{obj.__module__}.{obj.__name__}"
            path = inspect.getsourcefile(obj)
        else:
            cls = obj.__class__
            label = f"{cls.__module__}.{cls.__name__}"
            path = inspect.getsourcefile(cls)
        if not path:
            return {"object": label}
        stat = Path(path).stat()
        return {
            "object": label,
            "path": str(Path(path).resolve()),
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
        }
    except Exception:
        return {"object": obj.__class__.__name__}


def build_source_signature(*, files: Any, pricing: Any = None, parser: Any = None, extra: Any = None) -> str:
    return stable_json(
        {
            "signature_version": SIGNATURE_VERSION,
            "files": files,
            "pricing": pricing,
            "parser": parser,
            "extra": extra,
        }
    )


def _timestamp_ms(value: Any) -> int:
    try:
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.astimezone(timezone.utc).timestamp() * 1000)
        return int(value or 0)
    except Exception:
        return 0


def _int_field(entry: dict[str, Any], key: str) -> int:
    try:
        return int(entry.get(key, 0) or 0)
    except Exception:
        return 0


def _float_field(entry: dict[str, Any], key: str) -> float:
    try:
        return float(entry.get(key, 0.0) or 0.0)
    except Exception:
        return 0.0


def _entry_for_storage(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    source = str(entry.get("source") or "unknown")
    model = str(entry.get("model") or "unknown")
    provider = str(entry.get("provider") or "")
    timestamp = _timestamp_ms(entry.get("timestamp"))
    if timestamp <= 0:
        return None

    raw = dict(entry)
    raw["source"] = source
    raw["model"] = model
    raw["provider"] = provider
    raw["input"] = _int_field(raw, "input")
    raw["output"] = _int_field(raw, "output")
    raw["cacheRead"] = _int_field(raw, "cacheRead")
    raw["cacheWrite"] = _int_field(raw, "cacheWrite")
    raw["reasoning"] = _int_field(raw, "reasoning")
    raw["cost"] = _float_field(raw, "cost")
    raw["timestamp"] = timestamp
    raw["messageCount"] = _int_field(raw, "messageCount") or 1
    raw["entry_key"] = _entry_key(raw)
    return raw


def _entry_key(entry: dict[str, Any]) -> str:
    explicit = entry.get("entry_id") or entry.get("message_id") or entry.get("id")
    if explicit:
        return str(explicit)
    basis = {
        "source": entry.get("source"),
        "model": entry.get("model"),
        "provider": entry.get("provider"),
        "timestamp": entry.get("timestamp"),
        "input": entry.get("input"),
        "output": entry.get("output"),
        "cacheRead": entry.get("cacheRead"),
        "cacheWrite": entry.get("cacheWrite"),
        "reasoning": entry.get("reasoning"),
        "cost": round(float(entry.get("cost", 0.0) or 0.0), 10),
    }
    digest = hashlib.sha1(stable_json(basis).encode("utf-8")).hexdigest()
    return f"hash:{digest}"


def _normalize_file_signatures(file_signatures: Iterable[Any]) -> tuple[tuple[str, int, int], ...]:
    out: list[tuple[str, int, int]] = []
    for item in file_signatures:
        try:
            path, mtime_ns, size = item[:3]
            out.append((str(path), int(mtime_ns), int(size)))
        except Exception:
            continue
    return tuple(sorted(out))


def _session_record_list(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    out: list[dict[str, Any]] = []
    try:
        for item in raw:
            if isinstance(item, dict):
                out.append(item)
    except TypeError:
        return []
    return out


class UsageEntryStore:
    """SQLite-backed persistent cache for normalized token usage rows."""

    def __init__(self, db_path: Optional[Path] = None):
        self.path = db_path or usage_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self, *, ensure_schema: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.row_factory = sqlite3.Row
        if ensure_schema:
            self._ensure_schema_once(conn)
        return conn

    def _ensure_schema_once(self, conn: sqlite3.Connection) -> None:
        key = str(self.path.resolve())
        if key in _SCHEMA_READY:
            return
        with _SCHEMA_LOCK:
            if key in _SCHEMA_READY:
                return
            self._ensure_schema(conn)
            _SCHEMA_READY.add(key)

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_state (
                source TEXT PRIMARY KEY,
                signature TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                entry_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS file_state (
                source TEXT NOT NULL,
                path TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                safe_offset INTEGER NOT NULL DEFAULT 0,
                missing INTEGER NOT NULL DEFAULT 0,
                signature TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                entry_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (source, path)
            );
            CREATE TABLE IF NOT EXISTS usage_entries (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                file_path TEXT NOT NULL DEFAULT '',
                entry_key TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                timestamp INTEGER NOT NULL,
                input INTEGER NOT NULL DEFAULT 0,
                output INTEGER NOT NULL DEFAULT 0,
                cache_read INTEGER NOT NULL DEFAULT 0,
                cache_write INTEGER NOT NULL DEFAULT 0,
                reasoning INTEGER NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 1,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_records (
                tool TEXT NOT NULL,
                session_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                safe_offset INTEGER NOT NULL DEFAULT 0,
                missing INTEGER NOT NULL DEFAULT 0,
                signature TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (tool, file_path, session_id)
            );
            CREATE TABLE IF NOT EXISTS quota_snapshots (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                account TEXT NOT NULL DEFAULT 'default',
                bucket TEXT NOT NULL,
                bucket_label TEXT,
                used_percent REAL,
                resets_at INTEGER,
                plan TEXT,
                captured_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ok',
                raw_json TEXT,
                UNIQUE(provider, account, bucket, source, captured_at)
            );
            CREATE TABLE IF NOT EXISTS quota_file_state (
                source TEXT NOT NULL,
                path TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                safe_offset INTEGER NOT NULL DEFAULT 0,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (source, path)
            );
            CREATE INDEX IF NOT EXISTS idx_usage_entries_source_time
                ON usage_entries(source, timestamp);
            CREATE INDEX IF NOT EXISTS idx_usage_entries_source_file
                ON usage_entries(source, file_path);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_entries_source_key
                ON usage_entries(source, entry_key)
                WHERE entry_key != '';
            CREATE INDEX IF NOT EXISTS idx_usage_entries_time
                ON usage_entries(timestamp);
            CREATE INDEX IF NOT EXISTS idx_usage_entries_group
                ON usage_entries(source, provider, model, timestamp);
            CREATE INDEX IF NOT EXISTS idx_session_records_tool_session
                ON session_records(tool, session_id);
            CREATE INDEX IF NOT EXISTS idx_quota_snap_lookup
                ON quota_snapshots(provider, bucket, captured_at);
            """
        )
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(usage_entries)").fetchall()}
        if "file_path" not in columns:
            conn.execute("ALTER TABLE usage_entries ADD COLUMN file_path TEXT NOT NULL DEFAULT ''")
        if "entry_key" not in columns:
            conn.execute("ALTER TABLE usage_entries ADD COLUMN entry_key TEXT NOT NULL DEFAULT ''")
        file_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(file_state)").fetchall()}
        if "safe_offset" not in file_columns:
            conn.execute("ALTER TABLE file_state ADD COLUMN safe_offset INTEGER NOT NULL DEFAULT 0")
        if "missing" not in file_columns:
            conn.execute("ALTER TABLE file_state ADD COLUMN missing INTEGER NOT NULL DEFAULT 0")
        session_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(session_records)").fetchall()}
        if "safe_offset" not in session_columns:
            conn.execute("ALTER TABLE session_records ADD COLUMN safe_offset INTEGER NOT NULL DEFAULT 0")
        if "missing" not in session_columns:
            conn.execute("ALTER TABLE session_records ADD COLUMN missing INTEGER NOT NULL DEFAULT 0")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        current = int(row["value"]) if row else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"unsupported usage DB schema {current}; expected <= {SCHEMA_VERSION}")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        _repair_codex_api_percent_scale_rows(conn)
        conn.commit()

    def source_signature(self, source: str) -> Optional[str]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT signature FROM source_state WHERE source = ?",
                (source,),
            ).fetchone()
            return str(row["signature"]) if row else None

    def sync_source(
        self,
        source: str,
        signature: str,
        parse_entries: Callable[[], Iterable[dict[str, Any]]],
    ) -> bool:
        """Sync one source if its signature changed.

        Returns True when rows were replaced, False when the stored source was
        already current. Parser exceptions are intentionally allowed to bubble
        so callers can fail open to the live parser path.
        """
        if self.source_signature(source) == signature:
            return False

        rows = [_entry_for_storage(e) for e in parse_entries()]
        entries = [e for e in rows if e is not None]

        with usage_db_process_lock(self.path):
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT signature FROM source_state WHERE source = ?",
                    (source,),
                ).fetchone()
                if row and row["signature"] == signature:
                    return False
                if durable_usage_db_enabled() and not entries:
                    existing_count = int(
                        conn.execute(
                            "SELECT COUNT(*) AS n FROM usage_entries WHERE source = ?",
                            (source,),
                        ).fetchone()["n"]
                    )
                    if existing_count > 0:
                        return False

                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM usage_entries WHERE source = ?", (source,))
                conn.execute("DELETE FROM file_state WHERE source = ?", (source,))
                conn.executemany(
                    """
                    INSERT INTO usage_entries (
                        source, file_path, entry_key, model, provider, timestamp,
                        input, output, cache_read, cache_write, reasoning,
                        cost, message_count, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            e["source"],
                            "",
                            e["entry_key"],
                            e["model"],
                            e["provider"],
                            e["timestamp"],
                            e["input"],
                            e["output"],
                            e["cacheRead"],
                            e["cacheWrite"],
                            e["reasoning"],
                            e["cost"],
                            e["messageCount"],
                            stable_json(e),
                        )
                        for e in entries
                    ],
                )
                conn.execute(
                    """
                    INSERT INTO source_state(source, signature, updated_at_ms, entry_count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source) DO UPDATE SET
                        signature = excluded.signature,
                        updated_at_ms = excluded.updated_at_ms,
                        entry_count = excluded.entry_count
                    """,
                    (
                        source,
                        signature,
                        int(datetime.now(timezone.utc).timestamp() * 1000),
                        len(entries),
                    ),
                )
                conn.commit()
                return True

    def sync_files(
        self,
        source: str,
        file_signatures: Iterable[Any],
        *,
        pricing: Any = None,
        parser: Any = None,
        parse_file_entries: Callable[[tuple[str, int, int]], Iterable[dict[str, Any]]],
        parse_file_tail_entries: Optional[
            Callable[[tuple[str, int, int], int], tuple[Iterable[dict[str, Any]], int]]
        ] = None,
        durable: Optional[bool] = None,
    ) -> bool:
        """Sync a file-backed source by replacing only changed files.

        This is the middle tier between agentview-style append ingestion and the
        old whole-source replacement. It keeps correctness simple: a changed file
        is fully reparsed and its previous rows are deleted by (source, path),
        while unchanged files remain indexed and queryable.
        """
        files = _normalize_file_signatures(file_signatures)
        file_sig_by_path = {
            path: build_source_signature(
                files=[(path, mtime_ns, size)],
                pricing=pricing,
                parser=parser,
                extra={"mode": "file"},
            )
            for path, mtime_ns, size in files
        }
        source_signature = build_source_signature(
            files=files,
            pricing=pricing,
            parser=parser,
            extra={"mode": "files"},
        )

        keep_missing = durable_usage_db_enabled() if durable is None else durable

        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT path, mtime_ns, size, safe_offset, missing, signature FROM file_state WHERE source = ?",
                (source,),
            ).fetchall()
            stored = {
                str(row["path"]): {
                    "mtime_ns": int(row["mtime_ns"] or 0),
                    "size": int(row["size"] or 0),
                    "safe_offset": int(row["safe_offset"] or row["size"] or 0),
                    "missing": int(row["missing"] or 0),
                    "signature": str(row["signature"]),
                }
                for row in rows
            }

        current_paths = {path for path, _, _ in files}
        removed_paths = sorted(
            path
            for path, state in stored.items()
            if path not in current_paths and (not keep_missing or not int(state.get("missing") or 0))
        )
        changed_files = [
            file_sig
            for file_sig in files
            if stored.get(file_sig[0], {}).get("signature") != file_sig_by_path[file_sig[0]]
            or int(stored.get(file_sig[0], {}).get("missing") or 0)
        ]

        if not removed_paths and not changed_files:
            return False

        parsed: list[tuple[tuple[str, int, int], list[dict[str, Any]], int, bool]] = []
        for file_sig in changed_files:
            path, mtime_ns, size = file_sig
            state = stored.get(path)
            appended = False
            safe_offset = int(size)
            if parse_file_tail_entries is not None and state and not state.get("missing"):
                old_size = int(state.get("size") or state.get("safe_offset") or 0)
                old_sig = build_source_signature(
                    files=[(path, int(state.get("mtime_ns") or 0), old_size)],
                    pricing=pricing,
                    parser=parser,
                    extra={"mode": "file"},
                )
                if size > old_size and old_sig == state.get("signature"):
                    try:
                        tail_entries, safe_offset = parse_file_tail_entries(file_sig, old_size)
                        rows = [_entry_for_storage(e) for e in tail_entries]
                        parsed.append((file_sig, [e for e in rows if e is not None], int(safe_offset), True))
                        appended = True
                    except Exception:
                        appended = False
            if not appended:
                rows = [_entry_for_storage(e) for e in parse_file_entries(file_sig)]
                parsed.append((file_sig, [e for e in rows if e is not None], int(size), False))

        with usage_db_process_lock(self.path):
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                for path in removed_paths:
                    if keep_missing:
                        conn.execute(
                            "UPDATE file_state SET missing = 1, updated_at_ms = ? WHERE source = ? AND path = ?",
                            (int(datetime.now(timezone.utc).timestamp() * 1000), source, path),
                        )
                    else:
                        conn.execute(
                            "DELETE FROM usage_entries WHERE source = ? AND file_path = ?",
                            (source, path),
                        )
                        conn.execute(
                            "DELETE FROM file_state WHERE source = ? AND path = ?",
                            (source, path),
                        )

                total_changed_entries = 0
                for (path, mtime_ns, size), entries, safe_offset, appended in parsed:
                    total_changed_entries += len(entries)
                    if not appended:
                        conn.execute(
                            "DELETE FROM usage_entries WHERE source = ? AND file_path = ?",
                            (source, path),
                        )
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO usage_entries (
                            source, file_path, entry_key, model, provider, timestamp,
                            input, output, cache_read, cache_write, reasoning,
                            cost, message_count, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                e["source"],
                                path,
                                e["entry_key"],
                                e["model"],
                                e["provider"],
                                e["timestamp"],
                                e["input"],
                                e["output"],
                                e["cacheRead"],
                                e["cacheWrite"],
                                e["reasoning"],
                                e["cost"],
                                e["messageCount"],
                                stable_json(e),
                            )
                            for e in entries
                        ],
                    )
                    conn.execute(
                        """
                        INSERT INTO file_state(
                            source, path, mtime_ns, size, safe_offset, missing,
                            signature, updated_at_ms, entry_count
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source, path) DO UPDATE SET
                            mtime_ns = excluded.mtime_ns,
                            size = excluded.size,
                            safe_offset = excluded.safe_offset,
                            missing = excluded.missing,
                            signature = excluded.signature,
                            updated_at_ms = excluded.updated_at_ms,
                            entry_count = excluded.entry_count
                        """,
                        (
                            source,
                            path,
                            mtime_ns,
                            safe_offset,
                            safe_offset,
                            0,
                            build_source_signature(
                                files=[(path, mtime_ns, safe_offset)],
                                pricing=pricing,
                                parser=parser,
                                extra={"mode": "file"},
                            ),
                            int(datetime.now(timezone.utc).timestamp() * 1000),
                            len(entries),
                        ),
                    )

                conn.execute(
                    """
                    UPDATE file_state
                    SET entry_count = (
                        SELECT COUNT(*)
                        FROM usage_entries
                        WHERE usage_entries.source = file_state.source
                          AND usage_entries.file_path = file_state.path
                    )
                    WHERE source = ?
                    """,
                    (source,),
                )
                total_entries_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM usage_entries WHERE source = ?",
                    (source,),
                ).fetchone()
                total_entries = int(total_entries_row["n"] if total_entries_row else total_changed_entries)
                conn.execute(
                    """
                    INSERT INTO source_state(source, signature, updated_at_ms, entry_count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source) DO UPDATE SET
                        signature = excluded.signature,
                        updated_at_ms = excluded.updated_at_ms,
                        entry_count = excluded.entry_count
                    """,
                    (
                        source,
                        source_signature,
                        int(datetime.now(timezone.utc).timestamp() * 1000),
                        total_entries,
                    ),
                )
                conn.commit()
                return True

    def query_entries(
        self,
        *,
        sources: Optional[Iterable[str]] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []

        source_list = [s for s in (sources or []) if s]
        if source_list:
            placeholders = ",".join("?" for _ in source_list)
            where.append(f"source IN ({placeholders})")
            args.extend(source_list)

        if since is not None:
            where.append("timestamp >= ?")
            args.append(_timestamp_ms(since))
        if until is not None:
            where.append("timestamp < ?")
            args.append(_timestamp_ms(until))

        query = "SELECT raw_json FROM usage_entries"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY timestamp ASC, id ASC"

        with closing(self._connect()) as conn:
            rows = conn.execute(query, args).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                obj = json.loads(row["raw_json"])
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def aggregate_entries(
        self,
        *,
        sources: Optional[Iterable[str]] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Return parse_entries_json-compatible aggregates using SQL grouping."""
        where, args = self._where(sources=sources, since=since, until=until)
        query = """
            SELECT
                source,
                model,
                provider,
                SUM(input) AS input_sum,
                SUM(output) AS output_sum,
                SUM(cache_read) AS cache_read_sum,
                SUM(cache_write) AS cache_write_sum,
                SUM(reasoning) AS reasoning_sum,
                SUM(cost) AS cost_sum,
                SUM(message_count) AS message_count_sum
            FROM usage_entries
        """
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " GROUP BY source, provider, model"

        with closing(self._connect()) as conn:
            rows = conn.execute(query, args).fetchall()

        apps: dict[str, Any] = {}
        all_models: list[dict[str, Any]] = []
        total_cost = 0.0
        total_tokens = 0
        total_messages = 0
        total_in = 0
        total_cache = 0

        for row in rows:
            source = str(row["source"] or "unknown")
            model = str(row["model"] or "unknown")
            provider = str(row["provider"] or "")
            full_model_name = f"{provider}/{model}" if provider else model
            input_raw = int(row["input_sum"] or 0)
            output = int(row["output_sum"] or 0)
            cache_read = int(row["cache_read_sum"] or 0)
            cache_write = int(row["cache_write_sum"] or 0)
            reasoning = int(row["reasoning_sum"] or 0)
            cost = float(row["cost_sum"] or 0.0)
            messages = int(row["message_count_sum"] or 0)

            tokens_in = input_raw + cache_write
            tokens_cache = cache_read
            tokens = tokens_in + output + tokens_cache + reasoning
            if tokens == 0:
                continue

            app_ref = apps.setdefault(
                source,
                {
                    "tokens": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "tokens_cache": 0,
                    "cost": 0.0,
                    "messages": 0,
                    "models": [],
                },
            )
            model_ref = {
                "name": full_model_name,
                "tokens": tokens,
                "tokens_in": tokens_in,
                "tokens_out": output,
                "tokens_cache": tokens_cache,
                "cost": cost,
                "messages": messages,
                "cache_hit_rate": _cache_hit_rate(tokens_in, tokens_cache),
            }
            app_ref["tokens"] += tokens
            app_ref["tokens_in"] += tokens_in
            app_ref["tokens_out"] += output
            app_ref["tokens_cache"] += tokens_cache
            app_ref["cost"] += cost
            app_ref["messages"] += messages
            app_ref["models"].append(model_ref)

            all_models.append({"source": source, **model_ref})
            total_cost += cost
            total_tokens += tokens
            total_messages += messages
            total_in += tokens_in
            total_cache += tokens_cache

        for app_ref in apps.values():
            app_ref["models"].sort(key=lambda x: x["cost"], reverse=True)
            app_ref["cache_hit_rate"] = _cache_hit_rate(app_ref["tokens_in"], app_ref["tokens_cache"])
        all_models.sort(key=lambda x: x["cost"], reverse=True)

        return {
            "total_cost": total_cost,
            "total_tokens": total_tokens,
            "total_messages": total_messages,
            "cache_hit_rate": _cache_hit_rate(total_in, total_cache),
            "apps": apps,
            "all_models": all_models,
        }

    def contribution_days(
        self,
        *,
        sources: Optional[Iterable[str]] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Return Stats-tab contribution rows using SQL date/model grouping."""
        where, args = self._where(sources=sources, since=since, until=until)
        query = """
            SELECT
                date(timestamp / 1000, 'unixepoch', 'localtime') AS day,
                source,
                model,
                provider,
                SUM(input) AS input_sum,
                SUM(output) AS output_sum,
                SUM(cache_read) AS cache_read_sum,
                SUM(cache_write) AS cache_write_sum,
                SUM(reasoning) AS reasoning_sum,
                SUM(cost) AS cost_sum,
                COUNT(*) AS row_count
            FROM usage_entries
        """
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " GROUP BY day, source, provider, model ORDER BY day ASC"

        with closing(self._connect()) as conn:
            rows = conn.execute(query, args).fetchall()

        by_date: dict[str, dict[str, Any]] = {}
        for row in rows:
            date = str(row["day"] or "")
            if not date:
                continue
            input_raw = int(row["input_sum"] or 0)
            cache_write = int(row["cache_write_sum"] or 0)
            input_tokens = input_raw + cache_write
            output = int(row["output_sum"] or 0)
            cache_read = int(row["cache_read_sum"] or 0)
            reasoning = int(row["reasoning_sum"] or 0)
            cost = float(row["cost_sum"] or 0.0)
            messages = int(row["row_count"] or 0)
            tokens = input_tokens + output + cache_read + reasoning

            day = by_date.setdefault(
                date,
                {
                    "date": date,
                    "totals": {"tokens": 0, "cost": 0.0, "messages": 0},
                    "intensity": 0,
                    "tokenBreakdown": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0},
                    "sources": [],
                },
            )
            day["totals"]["tokens"] += tokens
            day["totals"]["cost"] += cost
            day["totals"]["messages"] += messages
            tb = day["tokenBreakdown"]
            tb["input"] += input_tokens
            tb["output"] += output
            tb["cacheRead"] += cache_read
            tb["cacheWrite"] += 0
            tb["reasoning"] += reasoning
            day["sources"].append(
                {
                    "source": str(row["source"] or "unknown"),
                    "modelId": str(row["model"] or "unknown"),
                    "providerId": str(row["provider"] or "") or "unknown",
                    "tokens": {
                        "input": input_tokens,
                        "output": output,
                        "cacheRead": cache_read,
                        "cacheWrite": 0,
                        "reasoning": reasoning,
                    },
                    "cost": cost,
                    "messages": messages,
                }
            )

        return [by_date[k] for k in sorted(by_date)]

    def sync_session_files(
        self,
        tool: str,
        file_signatures: Iterable[Any],
        *,
        parser: Any = None,
        parse_file_session: Callable[[tuple[str, int, int]], Any],
        durable: Optional[bool] = None,
    ) -> bool:
        files = _normalize_file_signatures(file_signatures)
        keep_missing = durable_usage_db_enabled() if durable is None else durable
        sig_by_path = {
            path: build_source_signature(
                files=[(path, mtime_ns, size)],
                parser=parser,
                extra={"mode": "session-file"},
            )
            for path, mtime_ns, size in files
        }

        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT file_path, signature, missing FROM session_records WHERE tool = ?",
                (tool,),
            ).fetchall()
            stored = {
                str(row["file_path"]): {
                    "signature": str(row["signature"]),
                    "missing": int(row["missing"] or 0),
                }
                for row in rows
            }

        current_paths = {path for path, _, _ in files}
        removed_paths = sorted(
            path
            for path, state in stored.items()
            if path not in current_paths and (not keep_missing or not int(state.get("missing") or 0))
        )
        changed_files = [
            file_sig
            for file_sig in files
            if stored.get(file_sig[0], {}).get("signature") != sig_by_path[file_sig[0]]
            or int(stored.get(file_sig[0], {}).get("missing") or 0)
        ]

        if not removed_paths and not changed_files:
            return False

        parsed: list[tuple[tuple[str, int, int], list[dict[str, Any]]]] = []
        for file_sig in changed_files:
            parsed.append((file_sig, _session_record_list(parse_file_session(file_sig))))

        with usage_db_process_lock(self.path):
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                for path in removed_paths:
                    if keep_missing:
                        conn.execute(
                            "UPDATE session_records SET missing = 1, updated_at_ms = ? WHERE tool = ? AND file_path = ?",
                            (now_ms, tool, path),
                        )
                    else:
                        conn.execute(
                            "DELETE FROM session_records WHERE tool = ? AND file_path = ?",
                            (tool, path),
                        )

                for (path, mtime_ns, size), records in parsed:
                    conn.execute(
                        "DELETE FROM session_records WHERE tool = ? AND file_path = ?",
                        (tool, path),
                    )
                    for raw in records:
                        session_id = str(raw.get("session_id") or Path(path).stem)
                        conn.execute(
                            """
                            INSERT INTO session_records(
                                tool, session_id, file_path, mtime_ns, size, safe_offset,
                                missing, signature, updated_at_ms, raw_json
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                tool,
                                session_id,
                                path,
                                mtime_ns,
                                size,
                                size,
                                0,
                                sig_by_path[path],
                                now_ms,
                                stable_json(raw),
                            ),
                        )
                conn.commit()
                return True

    def query_session_records(self, tool: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT raw_json
                FROM session_records
                WHERE tool = ?
                ORDER BY file_path ASC, session_id ASC
                """,
                (tool,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                obj = json.loads(row["raw_json"])
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def quota_meta_get(self, key: str) -> Optional[str]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def quota_meta_set(self, key: str, value: str) -> None:
        with usage_db_process_lock(self.path), closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (str(key), str(value)),
            )
            conn.commit()

    def quota_file_watermarks(self, source: str) -> dict[str, dict[str, int]]:
        """Return ``{path: {mtime_ns, size, safe_offset}}`` for a quota session source."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT path, mtime_ns, size, safe_offset FROM quota_file_state WHERE source = ?",
                (source,),
            ).fetchall()
        return {
            str(row["path"]): {
                "mtime_ns": int(row["mtime_ns"] or 0),
                "size": int(row["size"] or 0),
                "safe_offset": int(row["safe_offset"] or 0),
            }
            for row in rows
        }

    _QUOTA_SNAPSHOT_INSERT_SQL = """
        INSERT OR IGNORE INTO quota_snapshots(
            provider, account, bucket, bucket_label, used_percent,
            resets_at, plan, captured_at, source, status, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    _QUOTA_WATERMARK_UPSERT_SQL = """
        INSERT INTO quota_file_state(source, path, mtime_ns, size, safe_offset, updated_at_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, path) DO UPDATE SET
            mtime_ns = excluded.mtime_ns,
            size = excluded.size,
            safe_offset = excluded.safe_offset,
            updated_at_ms = excluded.updated_at_ms
    """

    @staticmethod
    def _quota_snapshot_rows(snapshots: Iterable[Any]) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = []
        for snapshot in snapshots:
            raw = snapshot.as_dict() if hasattr(snapshot, "as_dict") else dict(snapshot)
            rows.append(
                (
                    str(raw.get("provider") or ""),
                    str(raw.get("account") or "default"),
                    str(raw.get("bucket") or ""),
                    raw.get("bucket_label"),
                    raw.get("used_percent"),
                    raw.get("resets_at"),
                    raw.get("plan"),
                    int(raw.get("captured_at") or 0),
                    str(raw.get("source") or ""),
                    str(raw.get("status") or "ok"),
                    stable_json(raw.get("raw") or {}),
                )
            )
        return rows

    def commit_quota_session_batch(
        self,
        snapshots: Iterable[Any],
        source: str,
        updates: Iterable[tuple[str, int, int, int]],
        *,
        backfill_meta_key: Optional[str] = None,
    ) -> int:
        """Insert session snapshots and advance their file watermarks in ONE transaction.

        Watermarks — and the one-time backfill-done flag — must never outrun the snapshot
        rows they cover: if they were committed first and the insert then failed (crash,
        disk full), the skipped bytes would never be re-read and the snapshots lost
        forever (worst case: the whole backfill marked done with nothing stored).
        Committing everything together means a failure rolls the batch back and the next
        cycle simply re-reads the same bytes. Returns the number of rows inserted.
        """
        rows = self._quota_snapshot_rows(snapshots)
        watermark_rows = [
            (source, str(path), int(mtime_ns), int(size), int(safe_offset))
            for path, mtime_ns, size, safe_offset in updates
        ]
        if not rows and not watermark_rows and backfill_meta_key is None:
            return 0
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        with usage_db_process_lock(self.path), closing(self._connect()) as conn:
            before = int(conn.execute("SELECT COUNT(*) AS n FROM quota_snapshots").fetchone()["n"] or 0)
            conn.execute("BEGIN IMMEDIATE")
            if rows:
                conn.executemany(self._QUOTA_SNAPSHOT_INSERT_SQL, rows)
            if watermark_rows:
                conn.executemany(
                    self._QUOTA_WATERMARK_UPSERT_SQL,
                    [(s, p, m, sz, off, now_ms) for (s, p, m, sz, off) in watermark_rows],
                )
            if backfill_meta_key is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES(?, '1')",
                    (str(backfill_meta_key),),
                )
            self._prune_quota_snapshots(conn)
            conn.commit()
            after = int(conn.execute("SELECT COUNT(*) AS n FROM quota_snapshots").fetchone()["n"] or 0)
            return max(0, after - before)

    def insert_quota_snapshots(self, snapshots: Iterable[Any]) -> int:
        rows = self._quota_snapshot_rows(snapshots)
        if not rows:
            return 0

        with usage_db_process_lock(self.path), closing(self._connect()) as conn:
            before = int(conn.execute("SELECT COUNT(*) AS n FROM quota_snapshots").fetchone()["n"] or 0)
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(self._QUOTA_SNAPSHOT_INSERT_SQL, rows)
            self._prune_quota_snapshots(conn)
            conn.commit()
            after = int(conn.execute("SELECT COUNT(*) AS n FROM quota_snapshots").fetchone()["n"] or 0)
            return max(0, after - before)

    def _prune_quota_snapshots(self, conn: sqlite3.Connection) -> None:
        # Retention is OFF by default (snapshots are small and the history charts are the
        # feature); a positive TOKDASH_QUOTA_RETENTION_DAYS opts in to pruning.
        try:
            days = int(os.environ.get("TOKDASH_QUOTA_RETENTION_DAYS", "0") or 0)
        except ValueError:
            days = 0
        if days <= 0:
            return
        cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
        conn.execute("DELETE FROM quota_snapshots WHERE captured_at < ?", (cutoff,))

    def latest_quota_snapshots(self) -> list[dict[str, Any]]:
        query = """
            SELECT q.*
            FROM quota_snapshots q
            JOIN (
                SELECT provider, account, bucket, MAX(captured_at) AS captured_at
                FROM quota_snapshots
                GROUP BY provider, account, bucket
            ) latest
              ON q.provider = latest.provider
             AND q.account = latest.account
             AND q.bucket = latest.bucket
             AND q.captured_at = latest.captured_at
            ORDER BY q.provider, q.account, q.bucket
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(query).fetchall()
        return [self._quota_row_to_dict(row) for row in rows]

    def query_quota_snapshots(
        self,
        *,
        providers: Optional[Iterable[str]] = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        provider_list = [p for p in (providers or []) if p]
        if provider_list:
            placeholders = ",".join("?" for _ in provider_list)
            where.append(f"provider IN ({placeholders})")
            args.extend(provider_list)
        if start is not None:
            where.append("captured_at >= ?")
            args.append(int(start))
        if end is not None:
            where.append("captured_at <= ?")
            args.append(int(end))
        query = "SELECT * FROM quota_snapshots"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY provider, account, bucket, captured_at ASC, id ASC"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, args).fetchall()
        return [self._quota_row_to_dict(row) for row in rows]

    def quota_history(
        self,
        *,
        providers: Optional[Iterable[str]] = None,
        granularity: str = "hour",
        start: Optional[int] = None,
        end: Optional[int] = None,
        max_points: int | None = 300,
        network_only_providers: Optional[Iterable[str]] = None,
    ) -> dict[str, Any]:
        if granularity not in {"hour", "day"}:
            raise ValueError("granularity must be 'hour' or 'day'")
        if max_points is not None and max_points <= 0:
            raise ValueError("max_points must be a positive integer")
        period = 3600 if granularity == "hour" else 86400
        # network_only_providers: providers whose caller has opted into live API polling, so
        # the API is the sole oracle for their series — stale/cached session-source rows
        # (currently only `codex_session`; see the module docstring on `_drop_torn_reads` for
        # the reset-boundary issue those rows can compound) are excluded from the series
        # entirely rather than merged in. Providers not in this set fall back to whatever
        # sources they have (session rows included) and are marked `estimated` below.
        network_only = {p for p in (network_only_providers or []) if p}
        where = ["used_percent IS NOT NULL", "bucket NOT IN ('api', 'reset_credits')"]
        args: list[Any] = []
        provider_list = [p for p in (providers or []) if p]
        if provider_list:
            where.append(f"provider IN ({','.join('?' for _ in provider_list)})")
            args.extend(provider_list)
        if "codex" in network_only:
            where.append("NOT (provider = 'codex' AND source = 'codex_session')")
        if start is not None:
            where.append("captured_at >= ?")
            args.append(int(start))
        if end is not None:
            where.append("captured_at <= ?")
            args.append(int(end))
        # Account is intentionally absent from ORDER BY: codex session rows (account
        # "default") and network rows (real account id) describe the SAME window and must
        # merge into one time-ordered series per (provider, bucket). On a timestamp
        # collision the later insert (higher id) wins, mirroring `_freshest_usage_rows`.
        # The single linear pass over sorted rows is what keeps this route fast on
        # 100k-row tables — no per-row dicts, no raw_json parsing.
        query = (
            "SELECT provider, bucket, bucket_label, account, used_percent, resets_at, captured_at"
            " FROM quota_snapshots WHERE " + " AND ".join(where)
            + " ORDER BY provider, bucket, captured_at ASC, id ASC"
        )

        series: list[dict[str, Any]] = []

        def _flush(key: tuple[str, str] | None, ordered: list[tuple[int, float, Any]], label: Any, account: Any) -> None:
            if key is None or not ordered:
                return
            # Consumption per period = how much the window FILLED. Fixed reset windows use a
            # running high per window and count only increases above that window's own high:
            #   * two windows with different reset times that get merged into one bucket
            #     (e.g. two Codex accounts' 7-day windows, days apart) no longer read as
            #     reset+refill on every switch between them — each is measured against its
            #     own high;
            #   * a genuine window rollover is a NEW window that starts a fresh baseline, so
            #     the drop is never counted as usage;
            #   * transient dips (a stray low reading that immediately recovers) never inflate
            #     the total, because a recovery to a value already seen is not a new high.
            #
            # A window is identified by its reset time, but resets_at jitters ±1s poll-to-poll
            # (providers round the wall clock differently each poll — e.g. Claude reports the
            # same 5h window as 13:39:59 then 13:40:00), and Codex adds a few start-of-window
            # splinters. Keying on the *exact* value would split one physical window into two
            # epochs and count the same climb in both (measured: Claude 5h/weekly inflated ~2x).
            # So reset times within RESET_JITTER_SECONDS of each other are chained into one
            # window identity. This never merges genuinely distinct windows: the closest ones
            # in real data are ~1h apart, and the interleaved two-account windows are days apart.
            #
            # Some buckets need adjacent-delta semantics instead; see
            # `_quota_history_uses_adjacent_deltas` for the exact invariant and known limits.
            resets_sorted = sorted({r for _, _, r in ordered if r is not None})
            reset_epoch: dict[Any, Any] = {}
            anchor: Any = None
            for i, value in enumerate(resets_sorted):
                if i == 0 or value - resets_sorted[i - 1] > RESET_JITTER_SECONDS:
                    anchor = value
                reset_epoch[value] = anchor

            # Drop reset-boundary torn reads before deriving EITHER points or consumption from
            # this series, so the chart line and the consumption bars agree. Raw DB rows are
            # untouched — this only filters the in-memory `ordered` list used below.
            ordered = _drop_torn_reads(ordered, reset_epoch)
            points = [{"captured_at": ts, "used_percent": pct} for ts, pct, _ in ordered]

            consumption: dict[int, float] = {}
            epoch_high: dict[Any, float] = {}
            epoch_prev: dict[Any, float] = {}
            for ts, pct, resets in ordered:
                epoch = reset_epoch.get(resets, resets)  # None-reset rows form one epoch (None)
                if _quota_history_uses_adjacent_deltas(key[0], key[1], resets):
                    prev = epoch_prev.get(epoch)
                    high = epoch_high.get(epoch)
                    epoch_prev[epoch] = pct
                    epoch_high[epoch] = pct if high is None else max(high, pct)
                    if prev is None:
                        continue
                    delta = _quota_adjacent_consumed_delta(prev, pct, high)
                    if delta:
                        period_start = ts - (ts % period)
                        consumption[period_start] = round(consumption.get(period_start, 0.0) + delta, 4)
                    continue
                prev = epoch_high.get(epoch)
                if prev is None:
                    epoch_high[epoch] = pct  # first sighting of this window = baseline
                    continue
                if pct > prev:
                    period_start = ts - (ts % period)
                    consumption[period_start] = round(consumption.get(period_start, 0.0) + (pct - prev), 4)
                    epoch_high[epoch] = pct
            consumption_points = [
                {"period_start": k, "consumed_percent": v} for k, v in sorted(consumption.items())
            ]
            # A series is `estimated` when it is NOT covered by API-only mode and can include
            # stale session-source rows. Only codex has a session source today; codex is
            # estimated exactly when the caller has not opted it into network_only_providers
            # (i.e. codex_api polling is off). Claude/Antigravity are always API-only.
            estimated = key[0] == "codex" and "codex" not in network_only
            series.append(
                {
                    "provider": key[0],
                    "account": account,
                    "bucket": key[1],
                    "bucket_label": label or key[1],
                    "points": _downsample_series_points(points, max_points),
                    "consumption": _downsample_series_points(consumption_points, max_points),
                    "estimated": estimated,
                }
            )

        with closing(self._connect()) as conn:
            current_key: tuple[str, str] | None = None
            ordered: list[tuple[int, float, Any]] = []
            label: Any = None
            account: Any = None
            for row in conn.execute(query, args):
                key = (str(row["provider"]), str(row["bucket"]))
                if key != current_key:
                    _flush(current_key, ordered, label, account)
                    current_key, ordered = key, []
                ts = int(row["captured_at"])
                pct = float(row["used_percent"])
                resets = row["resets_at"]
                if ordered and ordered[-1][0] == ts:
                    ordered[-1] = (ts, pct, resets)
                else:
                    ordered.append((ts, pct, resets))
                label = row["bucket_label"]
                account = str(row["account"])
            _flush(current_key, ordered, label, account)
        return {
            "granularity": granularity,
            "series": series,
            "any_estimated": any(s["estimated"] for s in series),
        }

    @staticmethod
    def _quota_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        raw_json = row["raw_json"] or "{}"
        try:
            raw = json.loads(raw_json)
        except Exception:
            raw = {}
        keys = row.keys()
        return {
            "id": int(row["id"]) if "id" in keys and row["id"] is not None else None,
            "provider": str(row["provider"]),
            "account": str(row["account"]),
            "bucket": str(row["bucket"]),
            "bucket_label": row["bucket_label"],
            "used_percent": None if row["used_percent"] is None else float(row["used_percent"]),
            "resets_at": None if row["resets_at"] is None else int(row["resets_at"]),
            "plan": row["plan"],
            "captured_at": int(row["captured_at"]),
            "source": str(row["source"]),
            "status": str(row["status"]),
            "raw": raw,
        }

    def status(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            meta = {str(row["key"]): str(row["value"]) for row in conn.execute("SELECT key, value FROM meta")}
            sources = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT source, entry_count, updated_at_ms
                    FROM source_state
                    ORDER BY source
                    """
                ).fetchall()
            ]
            file_rows = conn.execute(
                """
                SELECT
                    source,
                    COUNT(*) AS files,
                    SUM(CASE WHEN missing != 0 THEN 1 ELSE 0 END) AS missing_files,
                    SUM(entry_count) AS entries
                FROM file_state
                GROUP BY source
                ORDER BY source
                """
            ).fetchall()
            session_rows = conn.execute(
                """
                SELECT
                    tool,
                    COUNT(*) AS records,
                    COUNT(DISTINCT session_id) AS sessions,
                    SUM(CASE WHEN missing != 0 THEN 1 ELSE 0 END) AS missing_records
                FROM session_records
                GROUP BY tool
                ORDER BY tool
                """
            ).fetchall()
            total_entries = conn.execute("SELECT COUNT(*) AS n FROM usage_entries").fetchone()["n"]
            quota_snapshots = conn.execute("SELECT COUNT(*) AS n FROM quota_snapshots").fetchone()["n"]
        return {
            "path": str(self.path),
            "meta": meta,
            "usage_entries": int(total_entries or 0),
            "quota_snapshots": int(quota_snapshots or 0),
            "sources": sources,
            "files": [dict(row) for row in file_rows],
            "sessions": [dict(row) for row in session_rows],
            "durable": durable_usage_db_enabled(),
        }

    def checkpoint(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def repair(self, *, apply: bool = True) -> dict[str, Any]:
        """Check DB health and repair derived counters when safe.

        This does not try to repair physical SQLite corruption. If SQLite's
        integrity check fails, callers should run a full resync.
        """
        actions: list[str] = []
        with closing(self._connect()) as conn:
            quick_rows = conn.execute("PRAGMA quick_check").fetchall()
            quick_check = [str(row[0]) for row in quick_rows] or ["ok"]
            integrity_ok = quick_check == ["ok"]
            total_entries = conn.execute("SELECT COUNT(*) AS n FROM usage_entries").fetchone()["n"]
            total_sessions = conn.execute("SELECT COUNT(*) AS n FROM session_records").fetchone()["n"]

        if integrity_ok and apply:
            with usage_db_process_lock(self.path), closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE file_state
                    SET entry_count = (
                        SELECT COUNT(*)
                        FROM usage_entries
                        WHERE usage_entries.source = file_state.source
                          AND usage_entries.file_path = file_state.path
                    )
                    """
                )
                actions.append("recomputed file_state.entry_count")
                conn.execute(
                    """
                    UPDATE source_state
                    SET entry_count = (
                        SELECT COUNT(*)
                        FROM usage_entries
                        WHERE usage_entries.source = source_state.source
                    )
                    """
                )
                actions.append("recomputed source_state.entry_count")
                conn.execute("COMMIT")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                actions.append("checkpointed WAL")
                total_entries = conn.execute("SELECT COUNT(*) AS n FROM usage_entries").fetchone()["n"]
                total_sessions = conn.execute("SELECT COUNT(*) AS n FROM session_records").fetchone()["n"]
        elif integrity_ok:
            actions.append("dry-run: counters and WAL checkpoint not changed")

        return {
            "ok": bool(integrity_ok),
            "path": str(self.path),
            "quick_check": quick_check,
            "usage_entries": int(total_entries or 0),
            "session_records": int(total_sessions or 0),
            "actions": actions,
            "recommendation": "run `tokdash db resync`" if not integrity_ok else "",
        }

    def _where(
        self,
        *,
        sources: Optional[Iterable[str]] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        args: list[Any] = []

        source_list = [s for s in (sources or []) if s]
        if source_list:
            placeholders = ",".join("?" for _ in source_list)
            where.append(f"source IN ({placeholders})")
            args.extend(source_list)

        if since is not None:
            where.append("timestamp >= ?")
            args.append(_timestamp_ms(since))
        if until is not None:
            where.append("timestamp < ?")
            args.append(_timestamp_ms(until))
        return where, args


def _cache_hit_rate(tokens_in: Any, tokens_cache: Any) -> Optional[float]:
    num = int(tokens_cache or 0)
    den = int(tokens_in or 0) + num
    if den <= 0:
        return None
    return round(num / den, 4)


def _downsample_series_points(items: list[dict[str, Any]], max_points: int | None) -> list[dict[str, Any]]:
    """Evenly-spaced downsample; always keeps the most recent (last) item."""
    n = len(items)
    if not max_points or max_points <= 0 or n <= max_points:
        return items
    step = n / max_points
    indices = sorted({min(n - 1, int(i * step)) for i in range(max_points)})
    if indices[-1] != n - 1:
        indices[-1] = n - 1
    return [items[i] for i in indices]
