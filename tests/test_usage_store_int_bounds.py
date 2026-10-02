"""#144: one out-of-range integer must not fail a whole source's sync.

SQLite binds Python integers as signed 64-bit values; anything outside that
range raises ``OverflowError``. Because a source's sync commits all or nothing,
a single token count above 2**63 - 1 used to leave that source unindexed for as
long as the offending row stayed in its logs -- with the API silently serving
live-reparsed data instead (see #144). These pin the storage-boundary clamp.
"""

from __future__ import annotations

import sqlite3

import pytest

from tokdash.usage_store import UsageEntryStore

_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1
_FILES = (("a.jsonl", 1, 100),)


def _entry(**overrides: object) -> dict:
    entry = {
        "source": "codex",
        "model": "gpt-5.3",
        "provider": "",
        "timestamp": 1_700_000_000_000,
        "input": 100,
        "output": 5,
        "cacheRead": 0,
        "cacheWrite": 0,
        "reasoning": 0,
        "cost": 0.0,
        "messageCount": 1,
        "entry_id": "e1",
    }
    entry.update(overrides)
    return entry


def _sync(store: UsageEntryStore, entry: dict) -> bool:
    return store.sync_files(
        "codex",
        _FILES,
        parser={"v": 1},
        parse_file_entries=lambda file_sig: [entry],
    )


@pytest.mark.parametrize(
    "field",
    ["input", "output", "cacheRead", "cacheWrite", "reasoning", "messageCount"],
)
def test_huge_ints_are_clamped_and_the_sync_succeeds(tmp_path, field):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync(store, _entry(**{field: 10**25})) is True
    rows = store.query_entries(sources=["codex"])
    assert [row[field] for row in rows] == [_SQLITE_INT_MAX]


def test_hugely_negative_ints_are_clamped(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync(store, _entry(input=-(10**25))) is True
    rows = store.query_entries(sources=["codex"])
    assert [row["input"] for row in rows] == [_SQLITE_INT_MIN]


def test_one_out_of_range_row_does_not_cost_the_source_its_other_rows(tmp_path):
    # The shape #144 reported: the transaction is all or nothing, so before the
    # clamp one bad row left every other file in the source unindexed too.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    per_file = {
        "bad.jsonl": [_entry(entry_id="bad", input=10**25)],
        "good.jsonl": [_entry(entry_id="good", input=7)],
    }
    ok = store.sync_files(
        "codex",
        tuple((name, index, 100) for index, name in enumerate(per_file, start=1)),
        parser={"v": 1},
        parse_file_entries=lambda file_sig: per_file[file_sig[0]],
    )
    assert ok is True
    rows = store.query_entries(sources=["codex"])
    assert sorted(row["entry_id"] for row in rows) == ["bad", "good"]
    assert {row["entry_id"]: row["input"] for row in rows} == {
        "bad": _SQLITE_INT_MAX,
        "good": 7,
    }


def test_huge_timestamp_is_clamped_and_the_row_is_kept(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync(store, _entry(entry_id="e2", timestamp=10**25)) is True
    rows = store.query_entries(sources=["codex"])
    assert [row["timestamp"] for row in rows] == [_SQLITE_INT_MAX]


def _sync_session(store: UsageEntryStore, turns: list[dict]) -> bool:
    session = {"tool": "codex", "session_id": "s1", "turns": turns}
    return store.sync_session_files(
        "codex",
        (("s1.jsonl", 1, 100),),
        parser={"v": 1},
        parse_file_session=lambda _file_sig: session,
    )


def _session_bounds(db_path) -> list[tuple[int, int]]:
    with sqlite3.connect(db_path) as conn:
        return [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT started_at_ms, last_seen_at_ms FROM session_records WHERE tool = 'codex'"
            )
        ]


def test_huge_session_turn_timestamp_is_clamped_and_the_sync_succeeds(tmp_path):
    # session_records.started_at_ms / last_seen_at_ms are INTEGER columns fed by
    # the turn timestamps, so the same out-of-range value failed this sync too.
    db_path = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(db_path)
    assert _sync_session(store, [{"turn_index": 1, "timestamp_ms": 10**25, "tokens": 1}]) is True
    assert _session_bounds(db_path) == [(_SQLITE_INT_MAX, _SQLITE_INT_MAX)]


def test_non_finite_session_turn_timestamp_is_skipped_not_fatal(tmp_path):
    # A JSON number too large to be finite (1e400) parses to inf, and int(inf)
    # raises OverflowError -- which the (TypeError, ValueError) guard here did
    # not catch, so it escaped and failed the whole session sync.
    db_path = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(db_path)
    assert _sync_session(
        store,
        [
            {"turn_index": 1, "timestamp_ms": float("inf"), "tokens": 1},
            {"turn_index": 2, "timestamp_ms": 1_700_000_000_000, "tokens": 2},
        ],
    ) is True
    assert _session_bounds(db_path) == [(1_700_000_000_000, 1_700_000_000_000)]
