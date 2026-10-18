"""Performance benchmark for Tokdash Goose/Roo Code parsing.

Measures cold loads, warm unchanged requests, and single changed task
for both the current branch (with performance fixes) and main.

Usage:
    python benchmarks/perf_goose_roo.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tokdash.sources.coding_tools import GooseParser, RooCodeParser, _sig_cache, _SIG_TTL
from tokdash.pricing import PricingDatabase


def _create_goose_db(db_path: Path, num_sessions: int = 50, rows_per_session: int = 20) -> None:
    """Create a synthetic Goose sessions.db matching the real v1.51.0 schema."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    # Schema copied from the captured Goose v1.51.0 database (see test_goose_parser.py)
    cur.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            user_set_name BOOLEAN DEFAULT FALSE,
            session_type TEXT NOT NULL DEFAULT 'user',
            working_dir TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            extension_data TEXT DEFAULT '{}',
            total_tokens INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            accumulated_total_tokens INTEGER,
            accumulated_input_tokens INTEGER,
            accumulated_output_tokens INTEGER,
            accumulated_cache_read_tokens INTEGER,
            accumulated_cache_write_tokens INTEGER,
            accumulated_cost REAL,
            schedule_id TEXT,
            recipe_json TEXT,
            user_recipe_values_json TEXT,
            provider_name TEXT,
            model_config_json TEXT,
            goose_mode TEXT NOT NULL DEFAULT 'auto',
            archived_at TIMESTAMP,
            project_id TEXT,
            parent_session_id TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE usage_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            created_timestamp INTEGER NOT NULL,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            cost REAL,
            cost_source TEXT,
            is_compaction INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT,
            tokens INTEGER,
            elapsedMs INTEGER,
            model TEXT,
            created_timestamp INTEGER
        )
    """)

    base_time = 1700000000
    for i in range(num_sessions):
        sess_id = f"sess-{i:04d}"
        model = ["gpt-4o", "claude-3-5-sonnet", "gpt-4o-mini"][i % 3]
        cur.execute(
            "INSERT INTO sessions (id, name, working_dir, total_tokens, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, accumulated_total_tokens, accumulated_input_tokens, accumulated_output_tokens, accumulated_cache_read_tokens, accumulated_cache_write_tokens, accumulated_cost, model_config_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sess_id, f"Session {i}", f"/tmp/project{i}",
             1700 * rows_per_session, 1000 * rows_per_session, 500 * rows_per_session,
             200 * rows_per_session, 100 * rows_per_session,
             1700 * rows_per_session, 1000 * rows_per_session, 500 * rows_per_session,
             200 * rows_per_session, 100 * rows_per_session, 0.1,
             json.dumps({"model": model})),
        )
        for j in range(rows_per_session):
            cur.execute(
                "INSERT INTO usage_ledger (session_id, created_timestamp, model, input_tokens, output_tokens, total_tokens, cache_read_tokens, cache_write_tokens, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sess_id, base_time + i * 100 + j, model,
                 1000, 500, 1700, 200, 100, 0.002),
            )
        for j in range(rows_per_session):
            cur.execute(
                "INSERT INTO messages (session_id, role, content, tokens, elapsedMs, model, created_timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sess_id, "user" if j % 2 == 0 else "assistant", f"Message content {j}", 50, 100, model, base_time + i * 100 + j),
            )
    conn.commit()
    conn.close()


def _create_roo_tasks(root: Path, num_tasks: int = 100, files_per_task: int = 5) -> None:
    """Create synthetic Roo Code task files matching the real 3.54.0 format."""
    tasks_dir = root / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    base_time = 1789932971243  # epoch ms, the unit Roo stamps
    for i in range(num_tasks):
        task_dir = tasks_dir / f"task-{i:04d}"
        task_dir.mkdir(exist_ok=True)

        # ui_messages.json — list of say:"api_req_started" messages
        ui_messages = []
        for j in range(files_per_task):
            ts = base_time + i * 1000 + j * 10
            model = ["gpt-4o", "claude-3-5-sonnet"][j % 2]
            ui_messages.append({
                "type": "say",
                "say": "api_req_started",
                "ts": ts,
                "text": json.dumps({
                    "apiProtocol": "openai",
                    "tokensIn": 1000,
                    "tokensOut": 500,
                    "cacheWrites": 100,
                    "cacheReads": 200,
                    "cost": 0.002,
                }),
            })
            # Also add a user message carrying the model header
            ui_messages.append({
                "role": "user",
                "ts": ts - 5,
                "content": [
                    {"type": "text", "text": f"Do thing {j}"},
                    {"type": "text", "text": f"<model>{model}</model>"},
                ],
            })
        (task_dir / "ui_messages.json").write_text(json.dumps(ui_messages), encoding="utf-8")

        # api_conversation_history.json — model information
        api_history = []
        for j in range(files_per_task):
            api_history.append({
                "model": ["gpt-4o", "claude-3-5-sonnet"][j % 2],
                "tokensIn": 1000,
                "tokensOut": 500,
                "cacheRead": 200,
                "cacheWrite": 100,
            })
        (task_dir / "api_conversation_history.json").write_text(json.dumps(api_history), encoding="utf-8")


def benchmark_cold_load(goose_db: Path, roo_root: Path) -> dict:
    """Measure cold load performance (first parse after cache clear)."""
    _sig_cache.clear()
    GooseParser._entry_cache.clear()
    RooCodeParser._entry_cache.clear()

    # Set up environment so parsers find the synthetic data
    os.environ["GOOSE_PATH_ROOT"] = str(goose_db.parent.parent.parent)
    os.environ["TOKDASH_ROO_STORAGE_DIR"] = str(roo_root)

    tracemalloc.start()
    start = time.perf_counter()

    goose_parser = GooseParser(PricingDatabase())
    goose_entries = goose_parser.collect(None, None)

    roo_parser = RooCodeParser(PricingDatabase())
    roo_entries = roo_parser.collect(None, None)

    elapsed = time.perf_counter() - start
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "goose_entries": len(goose_entries),
        "roo_entries": len(roo_entries),
        "total_entries": len(goose_entries) + len(roo_entries),
        "elapsed_seconds": round(elapsed, 4),
        "peak_memory_mb": round(peak / 1024 / 1024, 2),
    }


def benchmark_warm_request(goose_db: Path, roo_root: Path) -> dict:
    """Measure warm request performance (second parse with cached signatures)."""
    os.environ["GOOSE_PATH_ROOT"] = str(goose_db.parent.parent.parent)
    os.environ["TOKDASH_ROO_STORAGE_DIR"] = str(roo_root)

    # First call to populate cache
    goose_parser = GooseParser(PricingDatabase())
    goose_parser.collect(None, None)
    roo_parser = RooCodeParser(PricingDatabase())
    roo_parser.collect(None, None)

    # Second call should use cached signatures
    tracemalloc.start()
    start = time.perf_counter()

    goose_parser2 = GooseParser(PricingDatabase())
    goose_entries = goose_parser2.collect(None, None)

    roo_parser2 = RooCodeParser(PricingDatabase())
    roo_entries = roo_parser2.collect(None, None)

    elapsed = time.perf_counter() - start
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "goose_entries": len(goose_entries),
        "roo_entries": len(roo_entries),
        "total_entries": len(goose_entries) + len(roo_entries),
        "elapsed_seconds": round(elapsed, 4),
        "peak_memory_mb": round(peak / 1024 / 1024, 2),
    }


def benchmark_single_change(goose_db: Path, roo_root: Path) -> dict:
    """Measure performance when a single task changes."""
    os.environ["GOOSE_PATH_ROOT"] = str(goose_db.parent.parent.parent)
    os.environ["TOKDASH_ROO_STORAGE_DIR"] = str(roo_root)

    # Populate cache
    goose_parser = GooseParser(PricingDatabase())
    goose_parser.collect(None, None)
    roo_parser = RooCodeParser(PricingDatabase())
    roo_parser.collect(None, None)

    # Change one Roo task
    task_dir = roo_root / "tasks" / "task-0000"
    api_file = task_dir / "api_conversation_history.json"
    data = json.loads(api_file.read_text())
    data[0]["model"] = "gpt-4o-mini"
    api_file.write_text(json.dumps(data))

    # Measure re-parse
    tracemalloc.start()
    start = time.perf_counter()

    goose_parser2 = GooseParser(PricingDatabase())
    goose_parser2.collect(None, None)

    roo_parser2 = RooCodeParser(PricingDatabase())
    roo_entries = roo_parser2.collect(None, None)

    elapsed = time.perf_counter() - start
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "roo_entries": len(roo_entries),
        "elapsed_seconds": round(elapsed, 4),
        "peak_memory_mb": round(peak / 1024 / 1024, 2),
    }


def main():
    print("=" * 70)
    print("Tokdash Goose/Roo Code Performance Benchmark")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create synthetic data
        # Goose parser expects: {GOOSE_PATH_ROOT}/data/sessions/sessions.db
        goose_db = tmpdir / "data" / "sessions" / "sessions.db"
        goose_db.parent.mkdir(parents=True)
        _create_goose_db(goose_db, num_sessions=50, rows_per_session=20)

        roo_root = tmpdir / "roo"
        _create_roo_tasks(roo_root, num_tasks=100, files_per_task=5)

        print(f"\nDataset: 50 Goose sessions (20 rows each), 100 Roo tasks (5 files each)")
        print(f"TTL: {_SIG_TTL}s")

        # Cold load
        print("\n--- Cold Load ---")
        cold = benchmark_cold_load(goose_db, roo_root)
        print(f"  Goose entries: {cold['goose_entries']}")
        print(f"  Roo entries: {cold['roo_entries']}")
        print(f"  Total entries: {cold['total_entries']}")
        print(f"  Elapsed: {cold['elapsed_seconds']}s")
        print(f"  Peak memory: {cold['peak_memory_mb']} MB")

        # Warm request
        print("\n--- Warm Request (cached signatures) ---")
        warm = benchmark_warm_request(goose_db, roo_root)
        print(f"  Goose entries: {warm['goose_entries']}")
        print(f"  Roo entries: {warm['roo_entries']}")
        print(f"  Total entries: {warm['total_entries']}")
        print(f"  Elapsed: {warm['elapsed_seconds']}s")
        print(f"  Peak memory: {warm['peak_memory_mb']} MB")

        # Single change
        print("\n--- Single Changed Task ---")
        change = benchmark_single_change(goose_db, roo_root)
        print(f"  Roo entries: {change['roo_entries']}")
        print(f"  Elapsed: {change['elapsed_seconds']}s")
        print(f"  Peak memory: {change['peak_memory_mb']} MB")

        # Summary
        print("\n" + "=" * 70)
        print("Summary")
        print("=" * 70)
        print(f"  Cold load: {cold['elapsed_seconds']}s ({cold['total_entries']} entries)")
        print(f"  Warm request: {warm['elapsed_seconds']}s ({warm['total_entries']} entries)")
        print(f"  Single change: {change['elapsed_seconds']}s ({change['roo_entries']} entries)")
        print(f"  Speedup (cold/warm): {cold['elapsed_seconds'] / warm['elapsed_seconds']:.1f}x")

        # Output JSON for comparison
        result = {
            "cold": cold,
            "warm": warm,
            "change": change,
        }
        print(f"\nJSON output:\n{json.dumps(result, indent=2)}")


if __name__ == "__main__":
    main()
