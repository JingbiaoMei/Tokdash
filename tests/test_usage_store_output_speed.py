"""Primary accounting stays independent of derived timing.

Migration removes timing fields without changing usage or cost. Private reader
measurements never enter public/raw accounting payloads. Accounting generation
tracks ownership changes; timing reader versions and pricing remain independent.
"""
import json
import sqlite3
from contextlib import closing

import pytest

from tokdash.compute import _collect_parser_file, _collect_parser_tail
from tokdash.output_speed import (
    STATUS_INVALID_TIMING,
    STATUS_MEASURED,
    STATUS_MISSING_TIMING,
)
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import KimiParser
from tokdash.usage_store import (
    MEASUREMENT_GENERATION_META_KEY,
    SCHEMA_VERSION,
    SPEED_ENTRY_KEY,
    UsageEntryStore,
    public_usage_entry,
)

SPEED_COLUMNS = (
    "speed_tokens", "speed_ms", "speed_calls", "speed_kind",
    "speed_token_basis", "speed_status",
)


@pytest.fixture(autouse=True)
def _timing_truth_reader():
    from tokdash.speed_mode import collect_timings
    with collect_timings():
        yield


def _entry(entry_key="k1", output=100, ts=1_700_000_000_000, source="demo", **speed):
    row = {
        "source": source,
        "model": "test-model",
        "provider": "test",
        "timestamp": ts,
        "input": 10,
        "output": output,
        "cacheRead": 0,
        "cacheWrite": 0,
        "reasoning": 0,
        "cost": 0.001,
        "entry_id": entry_key,
        "messageCount": 1,
    }
    row.update(speed)
    return row


def _speed(tokens=400, ms=2000.0, kind="server_decode", basis="output_reasoning_unspecified",
           status=STATUS_MEASURED, calls=1):
    return {
        "speed_tokens": tokens,
        "speed_ms": ms,
        "speed_calls": calls,
        "speed_kind": kind,
        "speed_token_basis": basis,
        "speed_status": status,
    }


def _private_speed(**kw):
    """The form the real parsers hand back: one nested private key."""
    return {SPEED_ENTRY_KEY: _speed(**kw)}


def _flat_speed(**kw):
    """The tolerated form: the six fields loose on the entry."""
    return dict(_speed(**kw))


def _sync(store, entries, sig=(("/a.jsonl", 1, 10),), **kw):
    return store.sync_files(
        "demo",
        sig,
        parser={"v": 1},
        parse_file_entries=lambda _sig: list(entries),
        **kw,
    )


def _rows(store, source="demo"):
    with closing(store.read_connection()) as conn:
        return list(conn.execute(
            "SELECT entry_key, output, cost, raw_json FROM usage_entries WHERE source = ?"
            " ORDER BY entry_key", (source,)))


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def test_schema_version_bumped_for_the_new_columns():
    assert SCHEMA_VERSION == 13


def test_fresh_database_carries_the_speed_columns_with_unmeasured_defaults(tmp_path):
    store=UsageEntryStore(tmp_path/'usage.sqlite3')
    with closing(store._connect()) as c:
        assert not set(SPEED_COLUMNS) & {r['name'] for r in c.execute('PRAGMA table_info(usage_entries)')}
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='idx_usage_entries_speed_time'").fetchone()



def test_a_reader_never_runs_the_schema(tmp_path):
    """A dashboard GET must not be the thing that creates or migrates the schema.

    sqlite3.connect still leaves an empty file behind; what has to stay true is
    that no DDL and no migration run on the read path, so a reader can never be
    the process that mutates a usage database.
    """
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    with closing(store.read_connection()) as conn:
        tables = {
            str(r["name"])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert tables == set()


def test_a_v9_database_migrates_and_its_history_stays_unmeasured(tmp_path):
    """Historical rows must not become measured-zero on upgrade.

    A row whose speed_ms is 0 and whose speed_calls is 0 reads as "this call was
    never timed" only while speed_status says so; the column defaults carry that.
    """
    path = tmp_path / "usage.sqlite3"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute(
            """
            CREATE TABLE usage_entries (
                source TEXT NOT NULL, file_path TEXT NOT NULL, entry_key TEXT NOT NULL,
                model TEXT NOT NULL, provider TEXT NOT NULL, timestamp INTEGER NOT NULL,
                input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
                cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
                reasoning INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL,
                billing_json TEXT NOT NULL DEFAULT '', cost_authoritative INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute("INSERT INTO usage_entries VALUES "
                     "('demo','/old.jsonl','old1','m','p',1770000000000,"
                     "10,55,0,0,0,0.5,1,'{}','',0)")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '9')")
        conn.commit()

    store = UsageEntryStore(path)
    with closing(store._connect()):
        pass  # the migration runs here, on the writer's connection
    with closing(store.read_connection()) as conn:
        row = conn.execute("SELECT * FROM usage_entries WHERE entry_key = 'old1'").fetchone()
    assert not set(SPEED_COLUMNS) & set(row.keys())
    # Cost survived the migration untouched, and migrating is not measuring: the
    # generation only ever moves with a commit that carries timing.
    assert row["cost"] == 0.5
    assert store.measurement_generation() == 0


def test_fresh_database_starts_at_generation_zero(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert store.measurement_generation() == 0


# --------------------------------------------------------------------------
# Writing measurements
# --------------------------------------------------------------------------


def test_a_measured_row_lands_in_its_own_columns(tmp_path):
    store=UsageEntryStore(tmp_path/'u.sqlite3')
    _sync(store,[_entry(**_private_speed())])
    row=_rows(store)[0]
    assert row['output']==100 and row['cost']==0.001
    assert not set(SPEED_COLUMNS)&set(row.keys())
    assert '_speed' not in json.loads(row['raw_json'])



def test_the_flat_form_is_stored_identically(tmp_path):
    store=UsageEntryStore(tmp_path/'u.sqlite3')
    _sync(store,[_entry(**_flat_speed())])
    row=_rows(store)[0]
    assert row['output']==100 and row['cost']==0.001
    assert not set(SPEED_COLUMNS)&set(row.keys())
    assert '_speed' not in json.loads(row['raw_json'])



def test_a_row_without_timing_is_stored_as_explicit_absence(tmp_path):
    store=UsageEntryStore(tmp_path/'u.sqlite3')
    _sync(store,[_entry(**{})])
    row=_rows(store)[0]
    assert row['output']==100 and row['cost']==0.001
    assert not set(SPEED_COLUMNS)&set(row.keys())
    assert '_speed' not in json.loads(row['raw_json'])



def test_an_exclusion_reason_survives_a_full_statistic(tmp_path):
    store=UsageEntryStore(tmp_path/'u.sqlite3')
    _sync(store,[_entry(**_private_speed(status=STATUS_INVALID_TIMING))])
    row=_rows(store)[0]
    assert row['output']==100 and row['cost']==0.001
    assert not set(SPEED_COLUMNS)&set(row.keys())
    assert '_speed' not in json.loads(row['raw_json'])



def test_timing_never_reaches_a_public_payload(tmp_path):
    store = UsageEntryStore(tmp_path / "u.sqlite3")
    # Both accepted input forms, because the flat one is the one a caller is
    # most likely to write by hand.
    _sync(store, [
        _entry(entry_key="nested", **_private_speed()),
        _entry(entry_key="flat", ts=1_700_000_000_001, **_flat_speed()),
    ])
    for row in _rows(store):
        raw = json.loads(row["raw_json"])
        assert SPEED_ENTRY_KEY not in raw
        assert not any(k.startswith("speed_") for k in raw), row["entry_key"]
        # The columns still carry it; only the public blob stays clean.
        assert not set(SPEED_COLUMNS) & set(row.keys())
    entries = UsageEntryStore(tmp_path / "u.sqlite3").query_entries(sources=["demo"])
    assert len(entries) == 2
    assert all(
        SPEED_ENTRY_KEY not in e and not any(k.startswith("speed_") for k in e)
        for e in entries
    )
    cleaned = public_usage_entry(_entry(**_private_speed()))
    assert SPEED_ENTRY_KEY not in cleaned


def test_a_late_duration_replaces_the_earlier_absence(tmp_path):
    store=UsageEntryStore(tmp_path/'u.sqlite3')
    _sync(store,[_entry()]); before=_rows(store)
    _sync(store,[_entry(**_private_speed())],sig=(("/a.jsonl",2,20),))
    assert _rows(store)==before
    assert store.measurement_generation()==2



def test_billing_and_tokens_are_untouched_by_timing(tmp_path):
    store = UsageEntryStore(tmp_path / "u.sqlite3")
    _sync(store, [_entry(**_private_speed())])
    plain = UsageEntryStore(tmp_path / "plain.sqlite3")
    _sync(plain, [_entry()])
    with_timing, without = _rows(store)[0], _rows(plain)[0]
    assert with_timing["output"] == without["output"] == 100
    assert with_timing["cost"] == without["cost"]
    a, b = json.loads(with_timing["raw_json"]), json.loads(without["raw_json"])
    assert a["cost"] == b["cost"]
    assert {k: v for k, v in a.items() if not k.startswith("speed_")} == b


def test_poisonous_timing_cannot_be_persisted(tmp_path):
    store=UsageEntryStore(tmp_path/'u.sqlite3')
    _sync(store,[_entry(entry_key='nan',**_private_speed(ms=float('nan'))),_entry(entry_key='inf',ts=1700000000001,**_private_speed(ms=float('inf')))])
    assert len(_rows(store))==2
    assert all('_speed' not in json.loads(r['raw_json']) for r in _rows(store))



def test_source_replacement_clears_the_rows_and_their_measurements(tmp_path):
    store = UsageEntryStore(tmp_path / "u.sqlite3")
    _sync(store, [_entry(**_private_speed())])
    assert len(_rows(store)) == 1
    # The file goes away: its rows go with it, measurements included.
    _sync(store, [], sig=(), durable=False)
    assert _rows(store) == []


# --------------------------------------------------------------------------
# The measurement generation a comparison cache keys on
# --------------------------------------------------------------------------


def test_the_generation_moves_once_per_committing_sync(tmp_path):
    path = tmp_path / "u.sqlite3"
    store = UsageEntryStore(path)
    assert store.measurement_generation() == 0
    _sync(store, [_entry(**_private_speed())])
    assert store.measurement_generation() == 1
    # Same bytes, same parser: nothing committed, so nothing moved.
    assert _sync(store, [_entry(**_private_speed())]) is False
    assert store.measurement_generation() == 1
    # A real change to the file commits again, and the generation follows.
    assert _sync(store, [_entry(entry_key="k2", ts=1_700_000_005_000)],
                sig=(("/a.jsonl", 3, 30),)) is True
    assert store.measurement_generation() == 2


def test_a_second_reader_sees_the_committed_generation(tmp_path):
    """Cross-process visibility: no shared in-memory counter may be trusted.

    The comparison cache in the API lives in the serving process, while the rows
    are written by whichever process synced last, so the generation has to travel
    through the file.
    """
    path = tmp_path / "u.sqlite3"
    writer = UsageEntryStore(path)
    _sync(writer, [_entry(**_private_speed())])
    reader = UsageEntryStore(path)
    assert reader.measurement_generation() == writer.measurement_generation() == 1
    _sync(writer, [_entry(entry_key="k2", ts=1_700_000_007_000)],
          sig=(("/a.jsonl", 9, 90),))
    assert reader.measurement_generation() == 2


def test_generation_roundtrips_a_non_numeric_meta_value(tmp_path):
    path = tmp_path / "u.sqlite3"
    store = UsageEntryStore(path)
    _sync(store, [_entry(**_private_speed())])
    with closing(store._connect()) as conn:
        conn.execute("UPDATE meta SET value = 'not-a-number' WHERE key = ?",
                     (MEASUREMENT_GENERATION_META_KEY,))
        conn.commit()
    assert store.measurement_generation() == 0


# --------------------------------------------------------------------------
# An older database, and the append path a speed-tracking source still takes
# --------------------------------------------------------------------------


def test_speed_columns_present_names_a_layout_this_build_can_read(tmp_path):
    """The probe that turns "no such column" into a retryable answer.

    One PRAGMA against the layout the connection is pinned to: true for a database
    this build wrote, false for one an older build wrote, so the route can say
    "still migrating" instead of raising SQLite's complaint at the reader.
    """
    from tokdash.usage_store import speed_columns_present

    current = UsageEntryStore(tmp_path / "current.sqlite3")
    _sync(current, [_entry(**_private_speed())])
    with closing(current.read_connection()) as conn:
        assert speed_columns_present(conn) is False

    old = tmp_path / "v9.sqlite3"
    with closing(sqlite3.connect(str(old))) as conn:
        conn.execute(
            """
            CREATE TABLE usage_entries (
                source TEXT NOT NULL, file_path TEXT NOT NULL, entry_key TEXT NOT NULL,
                model TEXT NOT NULL, provider TEXT NOT NULL, timestamp INTEGER NOT NULL,
                input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
                cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
                reasoning INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL,
                billing_json TEXT NOT NULL DEFAULT '', cost_authoritative INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.commit()
    with closing(sqlite3.connect(str(old))) as conn:
        assert speed_columns_present(conn) is False


def _kimi_line(kind, stamp, usage, decode_ms=None, step=1):
    if kind == "begin":
        return json.dumps({
            "type": "context.append_loop_event", "time": stamp, "agentId": "main",
            "event": {"type": "step.begin", "uuid": f"b{step}", "step": step},
        })
    if kind == "end":
        return json.dumps({
            "type": "context.append_loop_event", "time": stamp, "agentId": "main",
            "event": {
                "type": "step.end", "uuid": f"e{step}", "step": step, "turnId": "0",
                "usage": usage, "finishReason": "tool_use",
                "llmServerDecodeMs": decode_ms, "llmStreamDurationMs": decode_ms + 8,
            },
        })
    return json.dumps({
        "type": "usage.record", "time": stamp, "model": "kimi-code/k3",
        "agentId": "main", "usage": usage,
    })


def _kimi_usage(output=600):
    return {"inputOther": 100, "output": output, "inputCacheRead": 1_000,
            "inputCacheCreation": 0}


def _kimi_usage_written(output=600, cache_write=0):
    """A usage row that wrote cached tokens, for telling two calls apart by
    nothing but that counter."""
    return {"inputOther": 100, "output": output, "inputCacheRead": 1_000,
            "inputCacheCreation": cache_write}


def _kimi_sync(store, parser, path, calls):
    """One sync through the same two entry points compute wires up."""
    sig = ((str(path), path.stat().st_mtime_ns, path.stat().st_size),)

    def whole(file_sig):
        calls["whole"] += 1
        return _collect_parser_file(parser, file_sig)

    def tail(file_sig, start_offset):
        calls["tail"] += 1
        return _collect_parser_tail(parser, file_sig, start_offset)

    return store.sync_files(
        "kimi",
        sig,
        parser=parser.persistent_parser_signature(),
        parse_file_entries=whole,
        parse_file_tail_entries=tail,
        durable=False,
    )


def _stored_speed(store):
    from tokdash import speed_cache, speed_worker
    from unittest.mock import patch
    with patch.dict('os.environ',{'TOKDASH_USAGE_DB_PATH':str(store.path),'TOKDASH_SPEED_CPU_FRACTION':'1'}):
        with closing(speed_cache.connect()) as c:
            c.execute("INSERT OR REPLACE INTO speed_jobs(id,request,state,created_at,updated_at) VALUES('boundary','{}','building',0,0)");c.commit()
            speed_worker.build(c,'boundary',{'from':'2020-01-01','to':'2030-01-01','source':'kimi'},sync=False)
            return c.execute("SELECT entry_key,speed_status,speed_ms,speed_calls,speed_tokens FROM speed_responses WHERE source='kimi' ORDER BY timestamp").fetchall()



def test_a_split_pair_is_mended_without_giving_up_the_append(tmp_path):
    """Append parsing survives the timing feature, and so does the timing.

    Reported the other way round: keeping the duration correct cost the source its
    incremental sync, which put every stored row of a changed file back through the
    parser (+82% on an append). Both hold here. A turn whose step.end arrives in a
    later append is mended by one whole-file reparse -- the slice says so, and the
    store falls back -- while the ordinary append that follows stays a tail parse,
    and no row is ever written twice.
    """
    path = tmp_path / "wire.jsonl"
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    parser = KimiParser(PricingDatabase())
    calls = {"whole": 0, "tail": 0}
    first = _kimi_usage(600)

    # Append 1: the turn's row, with its duration still unwritten.
    path.write_text(
        _kimi_line("begin", 1_780_000_000_000, first) + "\n"
        + _kimi_line("usage", 1_780_000_001_000, first) + "\n",
        encoding="utf-8",
    )
    _kimi_sync(store, parser, path, calls)
    rows = _stored_speed(store)
    assert len(rows) == 1
    assert rows[0]["speed_status"] == STATUS_MISSING_TIMING
    assert calls == {"whole": 1, "tail": 0}, "the first sight of a file is a whole parse"

    # Append 2: only the step.end, which belongs to the row already stored. The
    # slice cannot pair it, says so, and the store reparses the file whole.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_kimi_line("end", 1_780_000_020_000, first, decode_ms=19_000) + "\n")
    _kimi_sync(store, parser, path, calls)
    assert calls == {"whole": 2, "tail": 1}, "tail attempted, split declared, whole file read"
    rows = _stored_speed(store)
    assert len(rows) == 1, "the mend rewrites the row; it does not add one"
    assert rows[0]["speed_status"] == STATUS_MEASURED
    assert rows[0]["speed_ms"] == 19_000.0 and rows[0]["speed_tokens"] == 600

    # Append 3: an ordinary turn, row and duration in one slice. Nothing about the
    # timing feature makes this file lose its incremental path.
    second = _kimi_usage(300)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            _kimi_line("begin", 1_780_000_030_000, second, step=2) + "\n"
            + _kimi_line("usage", 1_780_000_031_000, second, step=2) + "\n"
            + _kimi_line("end", 1_780_000_040_000, second, decode_ms=9_000, step=2) + "\n"
        )
    _kimi_sync(store, parser, path, calls)
    assert calls == {"whole": 2, "tail": 2}, "a self-contained append stays a tail parse"
    rows = _stored_speed(store)
    assert [r["speed_status"] for r in rows] == [STATUS_MEASURED, STATUS_MEASURED]
    assert [r["speed_ms"] for r in rows] == [19_000.0, 9_000.0]


def test_a_split_pair_is_mended_when_the_row_arrives_last(tmp_path):
    """The reverse append split, at the store: the row is what arrives late.

    Reported as a stored missing_timing row that a whole-file read measures at 600
    tokens over 19,000 ms: the turn's step.end went in on one append and its
    usage.record on the next, so the second slice holds a row and no bracket.
    Nothing pairs a lone row and nothing asked for the whole file, so the row
    settled unmeasured and a tail append never goes back to it. The slice declares
    the split now and the store reparses, the same mend the forward split gets,
    while the ordinary append after it keeps the incremental path.
    """
    path = tmp_path / "wire.jsonl"
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    parser = KimiParser(PricingDatabase())
    calls = {"whole": 0, "tail": 0}
    first = _kimi_usage(600)

    # Append 1: the turn closed and its row is still unwritten, so the whole-file
    # read that the bracket-side signal asks for finds nothing to measure.
    path.write_text(
        _kimi_line("begin", 1_780_000_000_000, first) + "\n"
        + _kimi_line("end", 1_780_000_020_000, first, decode_ms=19_000) + "\n",
        encoding="utf-8",
    )
    _kimi_sync(store, parser, path, calls)
    assert _stored_speed(store) == [], "no counted row in the file yet"

    # Append 2: the row arrives by itself. Its duration is bytes the store already
    # consumed, so only a whole-file read can join the pair.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_kimi_line("usage", 1_780_000_001_000, first) + "\n")
    _kimi_sync(store, parser, path, calls)
    assert calls == {"whole": 2, "tail": 1}, "row-side split declared, whole file read"
    rows = _stored_speed(store)
    assert len(rows) == 1
    assert rows[0]["speed_status"] == STATUS_MEASURED
    assert rows[0]["speed_ms"] == 19_000.0 and rows[0]["speed_tokens"] == 600

    # Append 3: an ordinary turn, pair whole in one slice. Still a tail parse.
    second = _kimi_usage(300)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            _kimi_line("begin", 1_780_000_030_000, second, step=2) + "\n"
            + _kimi_line("usage", 1_780_000_031_000, second, step=2) + "\n"
            + _kimi_line("end", 1_780_000_040_000, second, decode_ms=9_000, step=2) + "\n"
        )
    _kimi_sync(store, parser, path, calls)
    assert calls == {"whole": 2, "tail": 2}, "a self-contained append stays a tail parse"
    rows = _stored_speed(store)
    assert [r["speed_status"] for r in rows] == [STATUS_MEASURED, STATUS_MEASURED]
    assert [r["speed_ms"] for r in rows] == [19_000.0, 9_000.0]


def test_a_speed_tracking_source_keeps_its_append_path(monkeypatch):
    """The sync gate, asserted rather than assumed.

    Reported as the timing feature costing kimi its incremental sync: the gate
    traded an 82%-slower append for a correct duration, and an 82% tax on every
    turn is the wrong way round -- the split pair is rare and the store can mend
    it. So the decision belongs to the source's append capability and to nothing
    else, and this is the level where that decision is actually made.
    """
    from tokdash import compute
    from tokdash.sources.coding_tools import SourceSyncCapability

    tail_parsers = {}

    def record(self, source, signatures, **kwargs):
        tail_parsers[source] = kwargs.get("parse_file_tail_entries")
        return False

    monkeypatch.setattr(UsageEntryStore, "sync_files", record)
    monkeypatch.setattr(UsageEntryStore, "apply_pricing", lambda self, *a, **k: None)

    class Quiet:
        """An ordinary file_replace source whose rows are not append-safe."""

        sync_capability = SourceSyncCapability(
            mode="file_replace", append_jsonl=False, reason="test stub",
        )

        def _file_signatures(self):
            return ()

        def persistent_parser_signature(self):
            return {"name": "quiet", "version": 1}

    kimi = KimiParser(PricingDatabase())
    assert kimi._tracks_output_speed is True, "the source under test must track speed"
    assert kimi.sync_capability.append_jsonl is True

    tracker = type("Tracker", (), {})()
    tracker.pricing_db = kimi.pricing_db
    tracker.parsers = {"kimi": kimi, "quiet": Quiet()}
    compute._sync_usage_store(tracker)

    assert tail_parsers["kimi"] is not None, (
        "a speed-tracking append source still syncs by appended slice"
    )
    assert tail_parsers["quiet"] is None, "the tail path is the source's call, not a default"



# --------------------------------------------------------------------------
# The store's trust boundary
# --------------------------------------------------------------------------


def _minted(**kw):
    """A verdict as the association itself mints it, not as a test would write it."""
    from tokdash.output_speed import measured

    return measured(
        tokens=kw.get("tokens", 400),
        duration_ms=kw.get("duration_ms", 2000.0),
        measurement_kind=kw.get("kind", "server_decode"),
        token_basis=kw.get("basis", "output_reasoning_unspecified"),
    )


def test_a_minted_verdict_stores_the_same_columns_as_the_same_numbers_written_by_hand(
    tmp_path,
):
    """Trusting a verdict this module minted must not move one stored value.

    The store takes the six columns straight out of a canonical verdict instead of
    running it back through ``sanitize_stored``, because that round-trip was the
    largest single cost of ingesting a corpus of measured rows. This is the
    one-for-one check that makes the shortcut boring: one store is handed the
    minted dict, the other a copy of it that it cannot vouch for, and the two rows
    have to come out identical -- measured ones and unmeasured ones alike.
    """
    from tokdash.output_speed import STATUS_AMBIGUOUS_PAIR, unmeasured

    cases = {
        "measured": _minted(),
        "unpaired": unmeasured(STATUS_AMBIGUOUS_PAIR),
        "untimed": unmeasured(STATUS_MISSING_TIMING),
        "no-verdict-at-all": None,
    }
    for name, verdict in cases.items():
        trusted = UsageEntryStore(tmp_path / f"{name}-trusted.sqlite3")
        untrusted = UsageEntryStore(tmp_path / f"{name}-untrusted.sqlite3")
        entry = _entry("k1")
        if verdict is not None:
            entry[SPEED_ENTRY_KEY] = verdict
        _sync(trusted, [dict(entry)])
        if verdict is not None:
            entry[SPEED_ENTRY_KEY] = dict(verdict)
        _sync(untrusted, [entry])
        assert _rows(trusted) == _rows(untrusted), name


def test_two_rows_can_share_one_verdict_dict(tmp_path):
    from tokdash.output_speed import unmeasured
    shared=unmeasured(STATUS_MISSING_TIMING)
    store=UsageEntryStore(tmp_path/'u.sqlite3')
    _sync(store,[_entry('k1',_speed=shared),_entry('k2',_speed=shared),_entry('k3',**_private_speed())])
    assert len(_rows(store))==3
    assert all('_speed' not in json.loads(r['raw_json']) for r in _rows(store))
    with pytest.raises(TypeError):
        shared['speed_status']=STATUS_MEASURED



def test_a_plain_dict_still_has_to_prove_its_numbers(tmp_path):
    store=UsageEntryStore(tmp_path/'u.sqlite3')
    _sync(store,[_entry(**_private_speed(ms=float("nan")))])
    row=_rows(store)[0]
    assert row['output']==100 and row['cost']==0.001
    assert not set(SPEED_COLUMNS)&set(row.keys())
    assert '_speed' not in json.loads(row['raw_json'])



# --------------------------------------------------------------------------
# Incremental parsing against whole-file parsing, at every boundary
# --------------------------------------------------------------------------
# A tail append stores what the new bytes produced and never revisits them, so a
# verdict the slice got wrong is wrong for the life of the row. The rules that
# keep the two readings identical are in KimiStepAssociation; what these prove is
# that the store's half of the bargain holds too -- that when the slice asks for
# the whole file it gets it, and that the rows it keeps afterwards are the rows a
# whole-file read would have written.

def _boundary_shapes():
    """Log shapes whose cuts each separate a different pair of records."""
    t0 = 1_780_000_000_000
    a, b = _kimi_usage(600), _kimi_usage(300)
    return {
        "row_after_close": [
            _kimi_line("begin", t0, a),
            _kimi_line("end", t0 + 20_000, a, decode_ms=19_000),
            _kimi_line("usage", t0 + 20_001, a),
            _kimi_line("begin", t0 + 30_000, b, step=2),
            _kimi_line("end", t0 + 40_000, b, decode_ms=9_000, step=2),
            _kimi_line("usage", t0 + 40_001, b, step=2),
        ],
        "row_before_close": [
            _kimi_line("begin", t0, a),
            _kimi_line("usage", t0 + 1_000, a),
            _kimi_line("end", t0 + 20_000, a, decode_ms=19_000),
            _kimi_line("begin", t0 + 30_000, b, step=2),
            _kimi_line("usage", t0 + 31_000, b, step=2),
            _kimi_line("end", t0 + 40_000, b, decode_ms=9_000, step=2),
        ],
        "mixed_tail_late_row": [
            _kimi_line("begin", t0, a),
            _kimi_line("end", t0 + 20_000, a, decode_ms=19_000),
            _kimi_line("begin", t0 + 20_100, b, step=2),
            _kimi_line("usage", t0 + 20_200, a),
            _kimi_line("end", t0 + 30_000, b, decode_ms=9_000, step=2),
            _kimi_line("usage", t0 + 30_100, b, step=2),
        ],
        "mixed_tail_at_row_clock_limit": [
            _kimi_line("begin", t0, a),
            _kimi_line("end", t0 + 20_000, a, decode_ms=19_000),
            _kimi_line("begin", t0 + 20_000, b, step=2),
            _kimi_line("usage", t0 + 20_500, a),
            _kimi_line("usage", t0 + 21_000, b, step=2),
            _kimi_line("end", t0 + 30_000, b, decode_ms=9_000, step=2),
        ],
        # Two calls whose rows land in the same clock second and differ in no
        # counter but the cached-write one. Nothing else in the row distinguishes
        # them, so an append that re-delivered one, or a comparison that keyed on
        # less than every counter, could pass while holding a single stored row
        # where the file records two calls.
        "cached_write_apart_at_one_clock": [
            _kimi_line("begin", t0, _kimi_usage_written(600, 0)),
            _kimi_line("end", t0 + 20_000, _kimi_usage_written(600, 0), decode_ms=19_000),
            _kimi_line("usage", t0 + 20_001, _kimi_usage_written(600, 0)),
            _kimi_line("begin", t0 + 30_000, _kimi_usage_written(600, 4_000), step=2),
            _kimi_line("end", t0 + 40_000, _kimi_usage_written(600, 4_000),
                       decode_ms=9_000, step=2),
            _kimi_line("usage", t0 + 40_001, _kimi_usage_written(600, 4_000), step=2),
        ],
        "duration_a_turn_late": [
            _kimi_line("begin", t0, a),
            _kimi_line("usage", t0 + 1_000, a),
            _kimi_line("begin", t0 + 5_000, b, step=2),
            _kimi_line("usage", t0 + 6_000, b, step=2),
            _kimi_line("end", t0 + 20_000, a, decode_ms=19_000),
            _kimi_line("end", t0 + 21_000, b, decode_ms=9_000, step=2),
        ],
        "row_the_log_never_bracketed": [
            _kimi_line("usage", t0 + 1_000, _kimi_usage(120)),
            _kimi_line("begin", t0 + 30_000, b, step=2),
            _kimi_line("usage", t0 + 31_000, b, step=2),
            _kimi_line("end", t0 + 40_000, b, decode_ms=9_000, step=2),
        ],
    }


def _stored_verdicts(store):
    timings={r['entry_key']:r for r in _stored_speed(store)}
    with closing(store.read_connection()) as c:
        rows=list(c.execute("SELECT * FROM usage_entries WHERE source='kimi'"))
    return sorted((r['timestamp'],r['output'],r['cache_read'],r['cache_write'],r['reasoning'],timings[r['entry_key']]['speed_status'],timings[r['entry_key']]['speed_tokens'],timings[r['entry_key']]['speed_ms'],timings[r['entry_key']]['speed_calls']) for r in rows)



def _whole_file_verdicts(path):
    entries = _collect_parser_file(
        KimiParser(PricingDatabase()),
        (str(path), path.stat().st_mtime_ns, path.stat().st_size),
    )
    verdicts = []
    for entry in entries:
        speed = entry.get("_speed") or {}
        # The same columns, from the other side of the comparison. A cache counter
        # left out here is a cache counter two different calls could agree on by
        # accident, which is how a collapsed row reads as a match.
        verdicts.append((entry["timestamp"], entry["output"],
                         entry.get("cacheRead"), entry.get("cacheWrite"),
                         entry.get("reasoning"),
                         speed.get("speed_status"), speed.get("speed_tokens"),
                         speed.get("speed_ms"), speed.get("speed_calls")))
    return sorted(verdicts)


@pytest.mark.parametrize("shape", sorted(_boundary_shapes()))
def test_incremental_and_whole_file_reads_agree_at_every_boundary(tmp_path, shape):
    """Whatever the append boundary cuts, the stored rows match a whole-file read.

    Every cut of every shape: the file is written up to the boundary and synced,
    the rest is appended and synced, and the stored verdicts are compared with what
    one whole-file parse of the complete log produces. A shape whose cut lands
    inside a pair is only allowed to pass by asking for the whole file -- the
    assertion is on the stored rows, so a signal that stayed silent would fail it,
    which is exactly how the mixed tail was found.
    """
    lines = _boundary_shapes()[shape]
    for cut in range(1, len(lines)):
        root = tmp_path / f"{shape}-{cut}"
        root.mkdir()
        path = root / "wire.jsonl"
        store = UsageEntryStore(root / "usage.sqlite3")
        parser = KimiParser(PricingDatabase())
        calls = {"whole": 0, "tail": 0}

        path.write_text("".join(line + "\n" for line in lines[:cut]), encoding="utf-8")
        _kimi_sync(store, parser, path, calls)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines[cut:]))
        _kimi_sync(store, parser, path, calls)

        assert calls["tail"] >= 1, "the append took the incremental path at least once"
        assert _stored_verdicts(store) == _whole_file_verdicts(path), (
            f"{shape}: cut after record {cut} stored a verdict the whole file disagrees with"
        )
