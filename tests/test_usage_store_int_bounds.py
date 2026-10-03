"""#144: one out-of-range integer must not fail a whole source's sync, and the
value it leaves behind must not take a read path down either.

SQLite binds Python integers as signed 64-bit values; anything outside that
range raises ``OverflowError``. Because a source's sync commits all or nothing,
a single token count above 2**63 - 1 used to leave that source unindexed for as
long as the offending row stayed in its logs -- with the API silently serving
live-reparsed data instead (see #144). Clamping the write fixes the sync and
leaves a row at the ceiling in the database, where ``SUM()`` raises
``OperationalError: integer overflow`` on every aggregate that groups it with
another row. The first half of these tests pins the storage-boundary clamp; the
second half pins the overflow-proof reads that go with it.
"""

from __future__ import annotations

import sqlite3

import pytest

from tokdash.compute import parse_entries_json
from tokdash.sources.openclaw import _openclaw_usage_from_store
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


def _sync_rows(store: UsageEntryStore, rows: dict[str, list[dict]], source: str = "codex") -> bool:
    return store.sync_files(
        source,
        tuple((name, index, 100) for index, name in enumerate(rows, start=1)),
        parser={"v": 1},
        parse_file_entries=lambda file_sig: rows[file_sig[0]],
    )


def _total_of(*stored: int) -> int:
    """What a read returns for a group holding these stored values.

    The token sums run through SQLite's ``total()``, which accumulates in
    floating point, so a group that reaches the int64 ceiling reads back as the
    nearest double rather than the exact integer. That is the price of never
    raising: the clamped value is a number no source ever reported, every caller
    coerces back to int, and a total beats an ``OperationalError``. Written out
    here so the rounding is a stated property of the test rather than something
    a future reader has to rediscover from a failure.
    """
    return int(sum(float(value) for value in stored))


def _stored_values(db_path) -> list[tuple[int, int]]:
    with sqlite3.connect(db_path) as conn:
        return [
            (row[0], row[1])
            for row in conn.execute("SELECT input, message_count FROM usage_entries ORDER BY id")
        ]


def test_a_clamped_row_beside_a_good_row_reads_back_through_aggregate_entries(tmp_path):
    # The clamp moved the failure from write to read: this group raised
    # "OperationalError: integer overflow" in SUM(input) while the sync itself
    # reported success, so every surface that aggregates the source failed and
    # compute fell back to a live reparse. Both rows have to come back.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_rows(
        store,
        {
            "bad.jsonl": [_entry(entry_id="bad", input=10**25)],
            "good.jsonl": [_entry(entry_id="good", input=7)],
        },
    ) is True

    aggregate = store.aggregate_entries(sources=["codex"])
    app = aggregate["apps"]["codex"]
    model = app["models"][0]
    expected_in = _total_of(_SQLITE_INT_MAX, 7)
    assert (app["tokens"], app["tokens_in"], app["tokens_out"], app["messages"]) == (
        expected_in + 10,
        expected_in,
        10,
        2,
    )
    assert (aggregate["total_tokens"], aggregate["total_messages"]) == (expected_in + 10, 2)
    assert model["tokens"] == expected_in + 10
    assert isinstance(model["tokens"], int)


def test_contribution_days_reads_back_a_clamped_row_beside_a_good_row(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_rows(
        store,
        {
            "bad.jsonl": [_entry(entry_id="bad", input=10**25)],
            "good.jsonl": [_entry(entry_id="good", input=7)],
        },
    ) is True

    days = store.contribution_days(sources=["codex"])
    assert len(days) == 1
    expected_in = _total_of(_SQLITE_INT_MAX, 7)
    assert days[0]["totals"] == {"tokens": expected_in + 10, "cost": 0.0, "messages": 2}
    assert days[0]["tokenBreakdown"]["input"] == expected_in
    assert days[0]["tokenBreakdown"]["output"] == 10
    assert days[0]["sources"][0]["messages"] == 2


def test_insight_and_project_rows_read_back_a_clamped_row(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_rows(
        store,
        {
            # message_count is an INTEGER column of its own: SUM(message_count)
            # overflowed exactly like the token sums did.
            "bad.jsonl": [_entry(entry_id="bad", input=10**25, messageCount=10**25)],
            "good.jsonl": [_entry(entry_id="good", input=7)],
        },
    ) is True

    groups = store.insight_rows(sources=["codex"])
    assert len(groups) == 1
    assert groups[0]["tokens"] == _total_of(_SQLITE_INT_MAX + 5, 12)
    assert groups[0]["messages"] == _total_of(_SQLITE_INT_MAX, 1)
    assert groups[0]["entries"] == 2

    per_file = {row["file_path"]: row for row in store.project_usage_rows(sources=["codex"])}
    assert per_file["bad.jsonl"]["tokens"] == _total_of(_SQLITE_INT_MAX + 5)
    assert per_file["good.jsonl"]["tokens"] == 12


def test_openclaw_totals_read_back_a_clamped_row(tmp_path):
    # OpenClaw groups the same table into its own answer, so its query has to
    # carry the same guard; it now takes the SQL from the store that owns the
    # table instead of keeping a second copy of it.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_rows(
        store,
        {
            "bad.jsonl": [_entry(source="openclaw", model="m1", entry_id="bad", input=10**25)],
            "good.jsonl": [_entry(source="openclaw", model="m1", entry_id="good", input=7)],
        },
        source="openclaw",
    ) is True

    totals = _openclaw_usage_from_store(store, None, None)
    expected_in = _total_of(_SQLITE_INT_MAX, 7)
    assert totals["total_tokens"] == expected_in + 10
    assert totals["total_messages"] == 2
    assert totals["models"]["m1"]["tokens_in"] == expected_in
    assert totals["contributions"][0]["totals"]["tokens"] == expected_in + 10


def test_reads_survive_a_database_that_already_stores_the_ceiling(tmp_path):
    # A database written by a build that already clamped holds the ceiling
    # whatever goes through the write path now, and a restored backup can hold
    # anything. The reads are the only guard there, so they are exercised
    # against a row written straight into SQLite, without the parser or the
    # clamp in the way -- including the message_count sum, which is an INTEGER
    # column of its own and overflowed the same way.
    db_path = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(db_path)
    assert _sync_rows(store, {"good.jsonl": [_entry(entry_id="good", input=7)]}) is True
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO usage_entries (
                source, file_path, entry_key, model, provider, timestamp,
                input, output, cache_read, cache_write, reasoning, cost,
                message_count, raw_json, billing_json, cost_authoritative
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0.0, ?, '{}', '', 0)
            """,
            (
                "codex",
                "hand.jsonl",
                "hash:ceiling",
                "gpt-5.3",
                "",
                1_700_000_000_000,
                _SQLITE_INT_MAX,
                5,
                _SQLITE_INT_MAX,
            ),
        )
    assert _stored_values(db_path) == [(7, 1), (_SQLITE_INT_MAX, _SQLITE_INT_MAX)]

    aggregate = store.aggregate_entries(sources=["codex"])
    # tokens = input totals (7 + the ceiling) + output totals (5 + 5), the same
    # shape a group of ordinary rows takes.
    assert aggregate["total_tokens"] == _total_of(_SQLITE_INT_MAX, 7) + 10
    assert aggregate["total_messages"] >= _SQLITE_INT_MAX
    # contribution_days counts messages as rows, so it reports two here; the
    # message_count column is summed by the aggregate and the insights scan.
    assert store.contribution_days(sources=["codex"])[0]["totals"]["messages"] == 2
    assert store.insight_rows(sources=["codex"])[0]["messages"] >= _SQLITE_INT_MAX
    # The query OpenClaw now reads the table with, called the way it calls it.
    rows = store._read_priced(lambda conn: store.model_totals_rows(conn, sources=["codex"]))
    assert [int(row["input_sum"]) for row in rows] == [_total_of(_SQLITE_INT_MAX, 7)]
    assert int(rows[0]["message_count_sum"]) >= _SQLITE_INT_MAX
    per_file = {row["file_path"]: row for row in store.project_usage_rows(sources=["codex"])}
    assert per_file["good.jsonl"]["tokens"] == 12
    assert per_file["hand.jsonl"]["tokens"] == _total_of(_SQLITE_INT_MAX + 5)


def test_ordinary_totals_stay_exact_integers(tmp_path):
    # total() returns REAL, so this is the guard that the round-trip through a
    # float never leaks into a real number: token counts sit far below 2**53 and
    # must come back as the same ints the SQL had summed.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_rows(
        store,
        {
            "a.jsonl": [_entry(entry_id="a", input=100)],
            "b.jsonl": [_entry(entry_id="b", input=7)],
        },
    ) is True

    app = store.aggregate_entries(sources=["codex"])["apps"]["codex"]
    assert (app["tokens"], app["tokens_in"], app["tokens_out"], app["messages"]) == (117, 107, 10, 2)
    day = store.contribution_days(sources=["codex"])[0]
    assert day["totals"]["tokens"] == 117
    assert day["tokenBreakdown"]["input"] == 107
    group = store.insight_rows(sources=["codex"])[0]
    assert (group["tokens"], group["messages"], group["entries"]) == (117, 2, 2)
    assert all(
        isinstance(value, int)
        for value in (app["tokens"], app["tokens_in"], day["totals"]["tokens"], group["tokens"])
    )


def test_store_and_live_paths_agree_on_an_ordinary_entry(tmp_path):
    # aggregate_entries is the store's version of parse_entries_json. The
    # float-backed sums must not move the two apart for ordinary input.
    entry = _entry(entry_id="live", input=100, output=5, cacheRead=40, cacheWrite=2, reasoning=3)
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_rows(store, {"a.jsonl": [entry]}) is True

    live = parse_entries_json({"entries": [entry]})
    stored = store.aggregate_entries(sources=["codex"])
    assert stored["total_tokens"] == live["total_tokens"] == 150
    assert stored["total_messages"] == live["total_messages"] == 1
    assert stored["total_cost"] == live["total_cost"]
    stored_model = stored["all_models"][0]
    live_model = live["all_models"][0]
    # The store's model row carries one extra field (tokens_reasoning); the two
    # paths have to agree on everything both of them have.
    for key in ("tokens", "tokens_in", "tokens_out", "tokens_cache", "messages"):
        assert stored_model[key] == live_model[key], key
    assert stored_model["tokens_reasoning"] == entry["reasoning"]
