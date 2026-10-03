"""#144: one out-of-range integer must not fail a whole source's sync, and it
must not change what the app reports either.

SQLite binds Python integers as signed 64-bit values; anything outside that
range raises ``OverflowError``. Because a source's sync commits all or nothing,
a single token count above 2**63 - 1 used to leave that source unindexed for as
long as the offending row stayed in its logs -- with the API silently serving
live-reparsed data instead (see #144).

What the store does with such a value is one decision, and these tests pin it:

* a count it cannot hold is stored as 0 -- the reading the parsers already give
  junk, applied in the parsers too (``_i``) so the live parse and the stored row
  agree. It is not clamped to 2**63 - 1: no source reported that number, one
  ``kimi-k2`` row priced at it bills $5,534,023,222,112.87 against the shipped
  pricing, and a stored ceiling makes ``SUM()`` raise
  ``OperationalError: integer overflow`` in every aggregate the row joins --
  which is the failure the clamp used to move from write to read. The sums are
  ``total()`` for exactly that reason.
* a timestamp it cannot hold drops the row, exactly as ``timestamp <= 0``
  already did. Clamping kept a row no date query can place (the aggregate
  counted it, the contribution grid could not) and pushed a session's
  ``last_seen_at_ms`` to the ceiling, so the session showed under Today forever.
* a session turn it cannot hold is skipped, as ``inf`` already was.

The write path can no longer produce a row at the ceiling, so two tests here
write one straight into SQLite and drive the reads with it: a database written by
a build that clamped still holds one, and so does a restored backup, and there
the ``total()`` sums are the only guard there is.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tokdash.compute import parse_entries_json
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import CodexParser
from tokdash.sources.openclaw import _openclaw_usage_from_store
from tokdash.usage_store import UsageEntryStore

_SQLITE_INT_MAX = 2**63 - 1
_FILES = (("a.jsonl", 1, 100),)
# 2023-11-14T22:13:20Z: a fixed instant, so a test never depends on today's date.
_TS = 1_700_000_000_000
_TS_TEXT = "2023-11-14T22:13:20Z"
_MODEL = "tokdash-int-bounds"


def _entry(**overrides: object) -> dict:
    entry = {
        "source": "codex",
        "model": "gpt-5.3",
        "provider": "",
        "timestamp": _TS,
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


def _sync_rows(store: UsageEntryStore, rows: dict[str, list[dict]], source: str = "codex") -> bool:
    return store.sync_files(
        source,
        tuple((name, index, 100) for index, name in enumerate(rows, start=1)),
        parser={"v": 1},
        parse_file_entries=lambda file_sig: rows[file_sig[0]],
    )


def _stored_values(db_path, columns: str) -> list[tuple[int, ...]]:
    with sqlite3.connect(db_path) as conn:
        return [
            tuple(row) for row in conn.execute(f"SELECT {columns} FROM usage_entries ORDER BY id")
        ]


# --- the write boundary ---------------------------------------------------


@pytest.mark.parametrize(
    "field", ["input", "output", "cacheRead", "cacheWrite", "reasoning", "messageCount"]
)
def test_an_out_of_range_count_is_stored_as_zero(tmp_path, field):
    # messageCount lands on 1, not 0: _entry_for_storage already reads a zero
    # message count as "this row is one message", and the row is still one.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync(store, _entry(**{field: 10**25})) is True
    rows = store.query_entries(sources=["codex"])
    assert [row[field] for row in rows] == [1 if field == "messageCount" else 0]


def test_a_count_past_either_end_of_the_range_reads_as_zero(tmp_path):
    # Both ends: a negative token count is no more countable than one too large,
    # and the live parsers read both the same way.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync(store, _entry(entry_id="high", input=10**25)) is True
    assert _sync_rows(store, {"b.jsonl": [_entry(entry_id="low", input=-(10**25))]}) is True
    assert {
        row["entry_id"]: row["input"] for row in store.query_entries(sources=["codex"])
    } == {"high": 0, "low": 0}


def test_junk_counts_still_read_as_zero(tmp_path):
    # The same reading junk already had: a count the parser could not read is 0,
    # not an error and not a bound.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync(store, _entry(input="not-a-number", output=float("inf"))) is True
    row = store.query_entries(sources=["codex"])[0]
    assert (row["input"], row["output"]) == (0, 0)


def test_an_out_of_range_timestamp_drops_the_row(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_rows(
        store,
        {
            "bad.jsonl": [_entry(entry_id="bad", timestamp=10**25)],
            "good.jsonl": [_entry(entry_id="good")],
        },
    ) is True
    assert [row["entry_id"] for row in store.query_entries(sources=["codex"])] == ["good"]


@pytest.mark.parametrize("timestamp", [0, -1, 10**25, -(10**25)])
def test_every_unusable_timestamp_drops_the_row_the_same_way(tmp_path, timestamp):
    # One rule for the whole unusable set: no date, no row. The row an unusable
    # timestamp used to leave behind was counted by aggregate_entries and had
    # no day for contribution_days to put it on.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync(store, _entry(timestamp=timestamp)) is True
    assert store.query_entries(sources=["codex"]) == []
    assert store.contribution_days(sources=["codex"]) == []


def test_one_out_of_range_row_does_not_cost_the_source_its_other_rows(tmp_path):
    # The shape #144 reported: the transaction is all or nothing, so before the
    # fix one bad row left every other file in the source unindexed too.
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
    assert {row["entry_id"]: row["input"] for row in rows} == {"bad": 0, "good": 7}


# --- session bounds -------------------------------------------------------


def _sync_session(store: UsageEntryStore, turns: list[dict]) -> bool:
    session = {"tool": "codex", "session_id": "s1", "turns": turns}
    return store.sync_session_files(
        "codex",
        (("s1.jsonl", 1, 100),),
        parser={"v": 1},
        parse_file_session=lambda _file_sig: session,
    )


def _session_bounds(db_path) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT started_at_ms, last_seen_at_ms FROM session_records WHERE tool = 'codex'"
            )
        ]


def test_an_out_of_range_turn_timestamp_is_skipped_not_clamped(tmp_path):
    # session_records.started_at_ms / last_seen_at_ms are INTEGER columns fed by
    # the turn timestamps, so the same out-of-range value failed this sync too.
    db_path = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(db_path)
    assert _sync_session(
        store,
        [
            {"turn_index": 1, "timestamp_ms": 10**25, "tokens": 1},
            {"turn_index": 2, "timestamp_ms": _TS, "tokens": 2},
        ],
    ) is True
    assert _session_bounds(db_path) == [(_TS, _TS)]


def test_a_session_whose_turns_are_all_unusable_lands_in_no_window(tmp_path):
    db_path = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(db_path)
    assert _sync_session(
        store, [{"turn_index": 1, "timestamp_ms": 10**25, "tokens": 1}]
    ) is True
    assert _session_bounds(db_path) == [(None, None)]
    # NULL bounds compare NULL in SQL, so the session is in no window at all --
    # the same reading a session with no timestamped turns already had.
    assert store.query_session_records("codex", since_ms=0, until_ms=2**45) == []


def test_a_session_with_one_bad_turn_lands_only_in_the_windows_of_its_good_turns(tmp_path):
    # The window read the Sessions panel uses: last_seen_at_ms >= since AND
    # started_at_ms < until. A clamped last_seen_at_ms clears every since from
    # 2023 to the end of time, so the session showed under Today forever.
    db_path = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(db_path)
    assert _sync_session(
        store,
        [
            {"turn_index": 1, "timestamp_ms": _TS, "tokens": 1},
            {"turn_index": 2, "timestamp_ms": 10**25, "tokens": 2},
        ],
    ) is True
    in_2023 = store.query_session_records(
        "codex", since_ms=_TS - 1000, until_ms=_TS + 1000
    )
    assert [record["session_id"] for record in in_2023] == ["s1"]
    # A window a thousand years out: only a bound that was clamped to the
    # ceiling can reach it, which is what put the session under Today forever.
    far_future = 2**45
    assert store.query_session_records(
        "codex", since_ms=far_future, until_ms=far_future + 1000
    ) == []


def test_a_non_finite_turn_timestamp_is_skipped_not_fatal(tmp_path):
    # A JSON number too large to be finite (1e400) parses to inf, and int(inf)
    # raises OverflowError -- which the (TypeError, ValueError) guard here did
    # not catch, so it escaped and failed the whole session sync.
    db_path = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(db_path)
    assert _sync_session(
        store,
        [
            {"turn_index": 1, "timestamp_ms": float("inf"), "tokens": 1},
            {"turn_index": 2, "timestamp_ms": _TS, "tokens": 2},
        ],
    ) is True
    assert _session_bounds(db_path) == [(_TS, _TS)]


# --- the read boundary ----------------------------------------------------


def _total_of(*stored: int) -> int:
    """What a read returns for a group holding these stored values.

    The token sums run through SQLite's ``total()``, which accumulates in
    floating point, so a group that reaches the int64 ceiling reads back as the
    nearest double rather than the exact integer. That is the price of never
    raising: the ceiling is a number no source ever reported, every caller
    coerces back to int, and a total beats an ``OperationalError``. Written out
    here so the rounding is a stated property of the test rather than something
    a future reader has to rediscover from a failure.
    """
    return int(sum(float(value) for value in stored))


def _write_ceiling_row(db_path) -> None:
    """A row holding the int64 ceiling, written straight into SQLite.

    The write path cannot produce one any more -- that is the point of the
    boundary -- but a database written by a build that clamped still holds the
    ceiling, and so does a restored backup. The sums are the only guard there,
    so they are exercised against a row that never went through the parser or
    the boundary. Every token column sits at the ceiling at once, which also
    covers the per-row addition inside the insight and project queries.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO usage_entries (
                source, file_path, entry_key, model, provider, timestamp,
                input, output, cache_read, cache_write, reasoning, cost,
                message_count, raw_json, billing_json, cost_authoritative
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, '{}', '', 0)
            """,
            (
                "codex",
                "hand.jsonl",
                "hash:ceiling",
                "gpt-5.3",
                "",
                _TS,
                _SQLITE_INT_MAX,
                _SQLITE_INT_MAX,
                _SQLITE_INT_MAX,
                _SQLITE_INT_MAX,
                _SQLITE_INT_MAX,
                _SQLITE_INT_MAX,
            ),
        )


def test_reads_survive_a_database_that_already_stores_the_ceiling(tmp_path):
    # Before total(), this group raised "OperationalError: integer overflow" in
    # SUM(input) while the sync itself reported success, so every surface that
    # aggregates the source failed, compute fell back to a live reparse, and
    # /api/insights -- which has no fallback -- returned 500.
    db_path = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(db_path)
    assert _sync_rows(store, {"good.jsonl": [_entry(entry_id="good", input=7)]}) is True
    _write_ceiling_row(db_path)

    # input, output, cache_read, cache_write and message_count are INTEGER
    # columns of their own; insight_rows and the OpenClaw totals sum them.
    assert _stored_values(db_path, "input, output, cache_read, message_count") == [
        (7, 5, 0, 1),
        (_SQLITE_INT_MAX,) * 4,
    ]

    aggregate = store.aggregate_entries(sources=["codex"])
    # The hand-written row contributes the ceiling on all five token columns;
    # the good row contributes 7 in and 5 out.
    assert aggregate["total_tokens"] == _total_of(*([_SQLITE_INT_MAX] * 5), 7, 5)
    assert aggregate["total_messages"] >= _SQLITE_INT_MAX
    # contribution_days counts messages as rows, so it reports two here; the
    # message_count column is summed by the aggregate and the insights scan.
    assert store.contribution_days(sources=["codex"])[0]["totals"]["messages"] == 2
    assert store.insight_rows(sources=["codex"])[0]["messages"] >= _SQLITE_INT_MAX
    # The query OpenClaw reads the table with, called the way it calls it.
    rows = store._read_priced(lambda conn: store.model_totals_rows(conn, sources=["codex"]))
    assert [int(row["input_sum"]) for row in rows] == [_total_of(_SQLITE_INT_MAX, 7)]
    assert int(rows[0]["message_count_sum"]) >= _SQLITE_INT_MAX
    per_file = {row["file_path"]: row for row in store.project_usage_rows(sources=["codex"])}
    assert per_file["good.jsonl"]["tokens"] == 12
    assert per_file["hand.jsonl"]["tokens"] == _total_of(*([_SQLITE_INT_MAX] * 5))


def test_reads_survive_a_row_that_already_holds_an_unplaceable_timestamp(tmp_path):
    # The other half of what a build that clamped could have written: a
    # timestamp at the ceiling has no date, because
    # date(9223372036854775807 / 1000, 'unixepoch') is NULL. The reads must not
    # raise on it, and the windowed ones must not claim a day it cannot name.
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
            ) VALUES ('codex', 'hand.jsonl', 'hash:dated', 'gpt-5.3', '', ?, 7, 0, 0, 0, 0, 0.0, 1, '{}', '', 0)
            """,
            (_SQLITE_INT_MAX,),
        )
    assert store.aggregate_entries(sources=["codex"])["total_tokens"] == 19
    # The insight scan groups by day and hour and drops a group it cannot date,
    # and the contribution grid has no day to place the row on either. Half a row
    # is why the write path refuses to store one of these at all.
    assert [group["entries"] for group in store.insight_rows(sources=["codex"])] == [1]
    days = store.contribution_days(sources=["codex"])
    assert [day["totals"] for day in days] == [{"tokens": 12, "cost": 0.0, "messages": 1}]


def test_aggregate_entries_reads_a_row_the_store_read_zero(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_rows(
        store,
        {
            "bad.jsonl": [_entry(entry_id="bad", input=10**25)],
            "good.jsonl": [_entry(entry_id="good", input=7)],
        },
    ) is True
    app = store.aggregate_entries(sources=["codex"])["apps"]["codex"]
    assert (app["tokens"], app["tokens_in"], app["tokens_out"], app["messages"]) == (17, 7, 10, 2)


def test_contribution_days_counts_a_row_the_store_read_zero(tmp_path):
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
    assert days[0]["totals"] == {"tokens": 17, "cost": 0.0, "messages": 2}
    assert days[0]["tokenBreakdown"]["input"] == 7


def test_insight_rows_counts_a_row_the_store_read_zero(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_rows(
        store,
        {
            "bad.jsonl": [_entry(entry_id="bad", input=10**25, messageCount=10**25)],
            "good.jsonl": [_entry(entry_id="good", input=7)],
        },
    ) is True
    groups = store.insight_rows(sources=["codex"])
    assert len(groups) == 1
    assert (groups[0]["tokens"], groups[0]["messages"], groups[0]["entries"]) == (17, 2, 2)


def test_openclaw_totals_read_a_row_the_store_read_zero(tmp_path):
    # OpenClaw groups the same table into its own answer, so its query has to
    # carry the same guard; it takes the SQL from the store that owns the table
    # instead of keeping a second copy of it.
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
    assert totals["total_tokens"] == 17
    assert totals["total_messages"] == 2
    assert totals["models"]["m1"]["tokens_in"] == 7


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


# --- live parse vs stored row --------------------------------------------


def _write_codex_log(home: Path, input_tokens: int) -> None:
    rows = [
        {"type": "session_meta", "payload": {"id": "s1", "cwd": "/w", "timestamp": _TS_TEXT}},
        {"type": "turn_context", "payload": {"model": _MODEL}},
        {
            "type": "event_msg",
            "timestamp": _TS_TEXT,
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": input_tokens, "output_tokens": 100}},
                "id": "s1-0",
            },
        },
    ]
    path = home / ".codex" / "sessions" / "2023" / "11" / "14" / "s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_a_parser_reads_an_out_of_range_count_as_zero(tmp_path, monkeypatch):
    # The live path has no store to fall back on, so this is the reading the
    # dashboard shows with the DB off. It has to be the same one the store
    # keeps, or the two views of one session disagree.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_codex_log(tmp_path, input_tokens=10**25)
    entries = CodexParser(PricingDatabase())._parse_all()
    assert [(entry["input"], entry["output"]) for entry in entries] == [(0, 100)]


def test_an_out_of_range_count_reads_the_same_live_and_stored(tmp_path, monkeypatch):
    # parse_entries_json is the live fold over a parser's output;
    # aggregate_entries is the store's version of the same numbers. One log, one
    # count too large for SQLite: a live parse that still said 10**25 while the
    # stored row said 0 is the disagreement that makes storing a bound for it
    # worse than storing nothing.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_codex_log(tmp_path, input_tokens=10**25)
    entries = CodexParser(PricingDatabase())._parse_all()

    live = parse_entries_json({"entries": entries})
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_rows(store, {"s1.jsonl": entries}) is True
    stored = store.aggregate_entries(sources=["codex"])

    assert (live["total_tokens"], live["total_messages"]) == (100, 1)
    assert (stored["total_tokens"], stored["total_messages"]) == (100, 1)
    for key in ("tokens", "tokens_in", "tokens_out", "tokens_cache", "messages"):
        assert stored["all_models"][0][key] == live["all_models"][0][key], key


def test_store_and_live_paths_agree_on_an_ordinary_entry(tmp_path):
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


# --- hash-keyed rows ------------------------------------------------------


def test_hash_keyed_rows_that_read_the_same_collapse_to_one(tmp_path):
    # Without an entry_id a row is keyed by a hash of the fields it stores, and
    # the upsert keeps one row per key. Reading an out-of-range count as 0 makes
    # a row that reported 10**25 indistinguishable from one that reported 0, so
    # they share a key and one of them is dropped -- the same thing two copies
    # of one resumed log already do on purpose. Pinned here so the behaviour is
    # a decision rather than a surprise: a distinct count still keeps its row.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_rows(
        store,
        {
            "a.jsonl": [_entry(entry_id="", input=10**25)],
            "b.jsonl": [_entry(entry_id="", input=0)],
            "c.jsonl": [_entry(entry_id="", input=7)],
        },
    ) is True
    rows = store.query_entries(sources=["codex"])
    assert sorted(row["input"] for row in rows) == [0, 7]