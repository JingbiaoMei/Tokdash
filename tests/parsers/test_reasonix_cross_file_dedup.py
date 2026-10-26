"""Issue #149: a copied Reasonix day file bills once in both database modes.

Reasonix appends each provider request to exactly one day file, under
``stats/.append.lock``, stamped with nanoseconds, so the same row in two scanned
``*.jsonl`` files arrived there by copying the file -- a sync client's conflict
copy or a hand-made duplicate that kept the ``.jsonl`` suffix. Content-keyed entry
ids already collapsed that copy in the store, but the live (DB-off) path numbered
occurrences across files, so it billed the copy twice; and the store let whichever
file committed last own the collapsed row, so deleting that copy discarded usage
the original file still recorded.

Both surfaces now resolve a copy to one row owned by the smallest path, which is
the earliest-(timestamp, file_path) rule UsageEntryStore.sync_files applies for
cross_file_stable_keys sources. Every copy of a key carries the same timestamp
because the timestamp is inside the digest, so the path tie-break decides.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tokdash import clientpaths, compute
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import (
    BaseParser,
    CodingToolsUsageTracker,
    ReasonixParser,
    _sig_cache,
)


@pytest.fixture(autouse=True)
def _isolated_reasonix(monkeypatch, tmp_path):
    monkeypatch.setenv("REASONIX_HOME", str(tmp_path / "reasonix-home"))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    yield tmp_path
    _sig_cache.clear()
    BaseParser._entry_cache.clear()


def _row(index: int, prompt: int) -> dict:
    """One stats row, distinguished by a nanosecond timestamp and its token counts."""
    return {
        "ts": f"2026-09-15T12:00:0{index}.00000000{index}+01:00",
        "model": "minimax-cn/MiniMax-M3",
        "prompt": prompt,
        "completion": 5,
        "cache_hit": 0,
        "cache_miss": prompt,
    }


R1, R2, R3 = _row(1, 100), _row(2, 200), _row(3, 300)


def _stats_dir() -> Path:
    return clientpaths.reasonix_stats_dir()


def _write(name: str, rows: list[dict]) -> Path:
    """Write one day file on its own, so a scenario never rewrites a file it leaves alone."""
    path = _stats_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    return path


def _remove(name: str) -> None:
    (_stats_dir() / name).unlink()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()


def _live() -> list[dict]:
    return ReasonixParser(PricingDatabase()).collect(None, None)


def _stored() -> list[dict]:
    tracker = CodingToolsUsageTracker()
    tracker.parsers = {"reasonix": ReasonixParser(tracker.pricing_db)}
    store, _sources = compute._sync_usage_store(tracker)
    return store.query_entries(sources=["reasonix"])


def _owners() -> dict[str, int]:
    """Stored rows per owning file, straight from the usage DB."""
    with sqlite3.connect(clientpaths.usage_db_path()) as conn:
        rows = conn.execute(
            "SELECT file_path, entry_key FROM usage_entries WHERE source = 'reasonix'"
        ).fetchall()
    owners: dict[str, int] = {}
    for path, _key in rows:
        name = Path(str(path)).name
        owners[name] = owners.get(name, 0) + 1
    return owners


def _assert_both_views(count: int, input_total: int) -> None:
    live, stored = _live(), _stored()
    assert len(live) == count, f"live view counted {len(live)} rows"
    assert len(stored) == count, f"stored view counted {len(stored)} rows"
    assert sum(e["input"] for e in live) == input_total
    assert sum(int(r["input"]) for r in stored) == input_total
    # Key equality is the point: the two views must resolve a corpus to the same
    # rows, not merely to matching totals.
    assert sorted(e["entry_id"] for e in live) == sorted(r["entry_key"] for r in stored)


def test_copied_day_file_bills_once_in_both_views():
    """The reporter's trigger: a sync client or a hand-made copy of a whole day file."""
    _write("2026-09-15.jsonl", [R1, R2])
    _assert_both_views(2, 300)

    _write("2026-09-15 (1).jsonl", [R1, R2])
    _assert_both_views(2, 300)


def test_conflict_copy_sharing_one_row_bills_the_shared_row_once():
    _write("2026-09-15.jsonl", [R1, R2])
    _write("2026-09-15-copy.jsonl", [R2, R3])
    _assert_both_views(3, 600)


def test_same_row_in_two_files_collapses_in_both_views():
    """The accepted collapse: byte-identical rows in two files cannot be told from a copy."""
    _write("2026-09-15.jsonl", [R1])
    _write("2026-09-15-copy.jsonl", [R1])
    _assert_both_views(1, 100)


def test_copied_file_multiplicity_is_the_count_the_largest_holder_records():
    """One file holds a duplicate pair, its copy holds it once: two requests, not three."""
    _write("a-2026-09-15.jsonl", [R1])
    _write("b-2026-09-15-copy.jsonl", [R1, R1])
    _assert_both_views(2, 200)


def test_identical_rows_in_one_file_stay_two_requests_in_both_views():
    """Inside one append-only day file the writer did emit both rows; the counter keeps them."""
    _write("2026-09-15.jsonl", [R1, R1])
    _assert_both_views(2, 200)


def test_genuine_split_across_day_files_counts_every_row():
    """Splitting a day's rows across files changes no total: the copies that collapse are copies."""
    _write("2026-09-15.jsonl", [R1, R2, R3])
    whole_day = sum(e["input"] for e in _live())
    assert whole_day == 600

    _remove("2026-09-15.jsonl")
    _write("2026-09-15-morning.jsonl", [R1, R2])
    _write("2026-09-15-evening.jsonl", [R3])
    _assert_both_views(3, 600)


def test_copy_suffix_outside_the_scanned_glob_is_not_read():
    """``.bak`` copies never enter ``stats/*.jsonl``, so they cannot affect totals either way."""
    _write("2026-09-15.jsonl", [R1, R2])
    (_stats_dir() / "2026-09-15.jsonl.bak").write_text(
        "".join(json.dumps(row) + "\n" for row in [R1, R2]), encoding="utf-8"
    )
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    _assert_both_views(2, 300)


def test_deduped_rows_belong_to_the_file_that_sorts_first():
    """Ownership is the smallest path, so the rows outlive whichever copy the user removes."""
    _write("a-2026-09-15.jsonl", [R1, R2])
    _write("b-2026-09-15-copy.jsonl", [R1, R2])
    _stored()
    assert _owners() == {"a-2026-09-15.jsonl": 2}


def test_deleting_the_copy_keeps_the_rows_the_original_still_records(monkeypatch):
    """Without cross-file stable keys the last file written owned the rows and took them on deletion."""
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "0")
    _write("a-2026-09-15.jsonl", [R1, R2])
    _write("b-2026-09-15-copy.jsonl", [R1, R2])
    _stored()

    _remove("b-2026-09-15-copy.jsonl")
    _assert_both_views(2, 300)


def test_deleting_the_original_leaves_the_copy_to_carry_the_rows(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "0")
    _write("a-2026-09-15.jsonl", [R1, R2])
    _write("b-2026-09-15-copy.jsonl", [R1, R2])
    _stored()

    _remove("a-2026-09-15.jsonl")
    _assert_both_views(2, 300)
    assert _owners() == {"b-2026-09-15-copy.jsonl": 2}


def test_rewriting_the_copy_without_the_shared_row_keeps_the_originals_rows():
    """Durable mode keeps no reparse of the untouched original, so ownership alone saves the rows."""
    _write("a-2026-09-15.jsonl", [R1])
    _write("b-2026-09-15-copy.jsonl", [R1])
    _stored()

    _write("b-2026-09-15-copy.jsonl", [R2])
    _assert_both_views(2, 300)


def test_appending_to_the_only_day_file_still_bills_every_row():
    """The ordinary path: an append-only day file grows and nothing collapses."""
    _write("2026-09-15.jsonl", [R1])
    _assert_both_views(1, 100)
    _write("2026-09-15.jsonl", [R1, R2])
    _assert_both_views(2, 300)
