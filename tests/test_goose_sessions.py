"""Tests for Goose as a session source (sessions.py).

The Sessions panel and Overview read the same usage_ledger through the same
snapshot helper, so a windowed session sum must equal the parser's entries for
that window, row for row. The tests below pin that parity, the seconds-to-ms
window, the ordinal elapsedMs match behind active time, and the two places the
surfaces are allowed to differ: Goose's own internal sessions, which are
billed but never listed.

The DDL and the seed builders come from test_goose_parser.py so both files
describe one database shape (the captured v1.51.0 schema, version 16).
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from test_goose_parser import T0, _make_db, _parser

from tokdash import sessions
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    TOOL_LABELS,
    GooseReadError,
    _goose_sessions,
    _session_active_intervals,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources.coding_tools import BaseParser, GooseParser, _sig_cache

# One priced model so cost drift is visible; anything else stays at 0.00.
RATES = {"qwen-test": {"input": 2.0, "output": 4.0, "cache_read": 0.2, "cache_write": 2.0}}

SECOND = 1_000
MINUTE = 60_000


def _reset_goose_reads():
    # One corpus per signature per honoured window, held in the module: leaving
    # it between tests would let one test answer for another.
    sessions._clear_goose_windows()
    sessions._goose_sessions_cache_sig = ()
    sessions._goose_corpus_cache = None
    sessions._goose_corpus_cache_sig = ()
    sessions._goose_corpus_cache_path = ""
    sessions._goose_corpus_cache_at = 0.0
    sessions._goose_corpus_cache_life = 0.0


@pytest.fixture(autouse=True)
def _clean_caches():
    _reset_goose_reads()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()
    yield
    _reset_goose_reads()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()


def _setup(monkeypatch, tmp_path, *, sessions=(), ledger=(), messages=()):
    """A Goose database under $XDG_DATA_HOME plus a pricing override.

    The parameter shadows the sessions module inside this function only, which
    is why nothing here reaches for sessions.* — the tests do that.
    """
    root = tmp_path / "xdg"
    db = root / "goose" / "sessions" / "sessions.db"
    _make_db(db, sessions=sessions, ledger=ledger)
    if messages:
        conn = sqlite3.connect(db)
        try:
            conn.executemany(
                "INSERT INTO messages (session_id, role, content_json, "
                "created_timestamp, metadata_json) VALUES (?, ?, ?, ?, ?)",
                messages,
            )
            conn.commit()
        finally:
            conn.close()
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    override = PricingDatabase().override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps({"version": "test", "aliases": {}, "models": RATES}),
        encoding="utf-8",
    )
    _parser(monkeypatch, xdg=root)
    monkeypatch.setenv("XDG_DATA_HOME", str(root))
    reload_pricing_db()
    return db


def _session(sid, name="CLI Session", stype="user", working_dir="/tmp/goose-work"):
    # (id, name, session_type, working_dir, total, input, output, cache_read,
    #  accumulated_input, accumulated_output)
    return (sid, name, stype, working_dir, 0, 0, 0, 0, 0, 0)


def _ledger(row_id, sid, offset, model="qwen-test", inp=1000, out=20,
            cache_read=0, cache_write=0):
    # (id, session_id, created_timestamp, model, input, output, total,
    #  cache_read, cache_write, cost, is_compaction)
    return (row_id, sid, T0 + offset, model, inp, out, inp + out,
            cache_read, cache_write, 99.0, 0)


def _user_prompt(sid, offset, text, *, user_visible=True):
    return (
        sid,
        "user",
        json.dumps([{"type": "text", "text": text}]),
        T0 + offset,
        json.dumps({"userVisible": user_visible, "agentVisible": True}),
    )


def _assistant_usage(sid, offset, elapsed_ms, *, input_tokens=0, output_tokens=0):
    return (
        sid,
        "assistant",
        json.dumps([{"type": "text", "text": "done"}]),
        T0 + offset,
        json.dumps({
            "userVisible": True,
            "agentVisible": True,
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "elapsedMs": elapsed_ms,
            },
        }),
    )


def _assistant_no_usage(sid, offset):
    """An assistant reply that arrived without a usage block (interrupted)."""
    return (
        sid,
        "assistant",
        json.dumps([{"type": "text", "text": "interrupted"}]),
        T0 + offset,
        json.dumps({"userVisible": True, "agentVisible": True}),
    )


def _listing(tool="goose", period="all"):
    return get_sessions_data(tool, period)


# --- registry ----------------------------------------------------------------


def test_goose_is_a_session_tool():
    assert "goose" in SESSION_TOOLS
    assert TOOL_LABELS["goose"] == "Goose"


def test_api_label_is_not_the_title_fallback(monkeypatch, tmp_path):
    """TOOL_LABELS feeds tool_label; without it Roo_Code-style fallbacks ship."""
    _setup(monkeypatch, tmp_path, sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0)])
    assert _listing()["tool_label"] == "Goose"


def test_unknown_session_tool_is_refused():
    with pytest.raises(ValueError):
        get_sessions_data("goose_not_here", "all")


# --- the listing -------------------------------------------------------------


def test_one_turn_per_ledger_row(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1"), _session("s2")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 5), _ledger(3, "s2", 9)])
    data = _listing()
    assert data["summary"]["session_count"] == 2
    by_id = {row["session_id"]: row for row in data["sessions"]}
    assert by_id["s1"]["token_events"] == 2
    assert by_id["s2"]["token_events"] == 1


def test_project_and_display_name_come_from_the_sessions_row(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1", name="Summarise the changelog")],
           ledger=[_ledger(1, "s1", 0)])
    row = _listing()["sessions"][0]
    assert row["display_name"] == "Summarise the changelog"
    assert row["project"] == "goose-work"


def test_generic_cli_name_falls_back_to_the_first_visible_prompt(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1", name="CLI Session")],
           ledger=[_ledger(1, "s1", 0)],
           messages=[
               _user_prompt("s1", -5, "hidden context", user_visible=False),
               _user_prompt("s1", -4, "Count the files in this repo"),
               _user_prompt("s1", 1, "a later prompt"),
           ])
    assert _listing()["sessions"][0]["display_name"] == "Count the files in this repo"


def test_an_empty_name_also_falls_back(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1", name="")],
           ledger=[_ledger(1, "s1", 0)],
           messages=[_user_prompt("s1", -1, "Fix the flaky test")])
    assert _listing()["sessions"][0]["display_name"] == "Fix the flaky test"


# --- the two surfaces and the one documented difference ----------------------


def test_parity_with_the_usage_parser(monkeypatch, tmp_path):
    """Same rows in, same totals out: tokens and cost must agree exactly."""
    rows = [
        _ledger(1, "s1", 0, inp=8327, out=172),
        _ledger(2, "s1", 4, inp=8527, out=132, cache_read=8320),
        _ledger(3, "s2", 30, inp=500, out=7, cache_write=11),
    ]
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1"), _session("s2")], ledger=rows)

    parser = GooseParser(PricingDatabase())
    entries = parser._parse_all()
    data = _listing()

    assert len(entries) == sum(row["token_events"] for row in data["sessions"])
    # The parser stores fresh input; the panel shows input plus the cache write.
    assert sum(e["input"] for e in entries) + sum(e["cacheWrite"] for e in entries) == sum(
        row["tokens_in"] for row in data["sessions"]
    )
    assert sum(e["cacheRead"] for e in entries) == sum(
        row["tokens_cache"] for row in data["sessions"]
    )
    assert sum(e["output"] for e in entries) == sum(
        row["tokens_out"] for row in data["sessions"]
    )
    assert sum(e["cost"] for e in entries) == pytest.approx(
        sum(row["cost"] for row in data["sessions"]), abs=1e-9
    )


def test_turn_event_keys_are_the_parser_entry_ids(monkeypatch, tmp_path):
    """One identity on both surfaces, so a row cannot be counted twice."""
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 5)])
    entries = GooseParser(PricingDatabase())._parse_all()
    keys = {e["entry_id"] for e in entries}
    raw = _goose_sessions()
    turns = [t for session in raw.values() for t in session["turns"]]
    assert {t["_event_key"] for t in turns} == keys


def test_hidden_sessions_are_billed_but_not_listed(monkeypatch, tmp_path):
    """Goose's internal sessions are charged and not shown, on purpose.

    Overview counts them because the tokens were billed; the panel omits them
    because they are not sessions a user started. This is the one documented
    place where the two surfaces differ for Goose.
    """
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1"), _session("h1", stype="hidden")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "h1", 5)])
    ids = {row["session_id"] for row in _listing()["sessions"]}
    assert ids == {"s1"}
    assert len(GooseParser(PricingDatabase())._parse_all()) == 2


def test_a_child_of_another_session_stays_out(monkeypatch, tmp_path):
    db = _setup(monkeypatch, tmp_path,
                sessions=[_session("p1"), _session("k1")],
                ledger=[_ledger(1, "p1", 0), _ledger(2, "k1", 5)])
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE sessions SET parent_session_id = 'p1' WHERE id = 'k1'")
        conn.commit()
    finally:
        conn.close()
    sessions._clear_goose_windows()
    sessions._goose_sessions_cache_sig = ()
    assert {row["session_id"] for row in _listing()["sessions"]} == {"p1"}
    # Billed either way: the subagent's tokens were spent, so Overview keeps them.
    assert len(GooseParser(PricingDatabase())._parse_all()) == 2


# --- the read budget ---------------------------------------------------------


def _counting_snapshot(monkeypatch, calls):
    """Wrap the shared snapshot helper so copies of the DB are countable."""
    real = sessions.zcode_snapshot

    def counting(db_path):
        calls.append(str(db_path))
        return real(db_path)

    monkeypatch.setattr(sessions, "zcode_snapshot", counting)
    return calls


def test_one_snapshot_serves_every_window(monkeypatch, tmp_path):
    """A Report tab asks for six periods and active time for two more.

    Each of those used to be a whole-copy snapshot of sessions.db, so one
    database change cost eight copies of a file that can pass a gigabyte. The
    corpus is read once and the windows are arithmetic.
    """
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1"), _session("s2")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s2", 5)],
           messages=[_assistant_usage("s1", 1, 1200, input_tokens=1000, output_tokens=20)])
    calls = _counting_snapshot(monkeypatch, [])

    windows = [
        (None, None),
        (T0 * SECOND, (T0 + 60) * SECOND),
        ((T0 - 3600) * SECOND, (T0 + 1) * SECOND),
        ((T0 - 86400) * SECOND, (T0 + 86400) * SECOND),
        (0, (T0 - 1) * SECOND),
        (None, (T0 + 30) * SECOND),
    ]
    listed = [len(_goose_sessions(since_ms=lo, until_ms=hi)) for lo, hi in windows]

    assert len(calls) == 1, f"{len(calls)} copies of sessions.db for one signature"
    # And every window still answers, which is the part that must not regress
    # for the saving: the windows disagree about both sessions, on purpose.
    assert listed == [2, 2, 1, 2, 0, 2], listed
    # A repeat asks nothing of the filesystem at all.
    calls.clear()
    assert len(_goose_sessions()) == 2
    assert calls == []


def test_a_database_change_costs_one_more_read_once_the_window_closes(
    monkeypatch, tmp_path
):
    """The corpus is not a permanent cache: a moved database does re-read.

    The bound is TOKDASH_SIG_TTL, the same window every other source gets, so
    this closes it rather than waiting on it. What is under test is that a
    change reaches the reader, not how long the reader is allowed to take.
    """
    db = _setup(monkeypatch, tmp_path,
                sessions=[_session("s1")], ledger=[_ledger(1, "s1", 0)])
    calls = _counting_snapshot(monkeypatch, [])
    monkeypatch.setattr(sessions, "_sig_lifetime", lambda cost: 0.0)
    assert len(_goose_sessions()) == 1
    assert len(calls) == 1

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO usage_ledger (id, session_id, created_timestamp, model, "
        "input_tokens, output_tokens, total_tokens, cache_read_tokens, "
        "cache_write_tokens, cost, is_compaction) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        _ledger(88, "s1", 300),
    )
    conn.commit()
    conn.close()
    # Coarse mtime clocks make this a test of the rule rather than the clock.
    stat = db.stat()
    os.utime(db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    sessions._clear_goose_windows()
    sessions._goose_sessions_cache_sig = ()
    assert len(_goose_sessions()) == 1
    assert len(calls) == 2
    turns = [t for raw in _goose_sessions().values() for t in raw["turns"]]
    assert len(turns) == 2, "the second read must carry the row the first one missed"


def test_an_unreadable_schema_is_not_re_copied_for_every_request(tmp_path, monkeypatch):
    """A Goose older than usage_ledger failed on every read, and re-COPIED the
    whole database to discover the same thing each time: for usage, for
    sessions, for insights. The verdict belongs to the file, so it is remembered
    against the file's signature -- and only against that signature.
    """
    db = tmp_path / "xdg" / "goose" / "sessions" / "sessions.db"
    _make_db(db, with_ledger=False, sessions=[_session("s1")], ledger=[])
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    _parser(monkeypatch, xdg=tmp_path / "xdg")
    calls = _counting_snapshot(monkeypatch, [])

    for _ in range(4):
        with pytest.raises(GooseReadError):
            _goose_sessions()
    assert len(calls) == 1, "the schema verdict was re-derived by copying the file"

    # Upgrading Goose writes the database, the signature moves, and the next
    # read probes for real instead of trusting the memo.
    db.unlink()
    _make_db(db, sessions=[_session("s2")], ledger=[_ledger(9, "s2", 0)])
    sessions._clear_goose_windows()
    sessions._goose_sessions_cache_sig = ()
    assert {row["session_id"] for row in _listing()["sessions"]} == {"s2"}


# --- windowing ---------------------------------------------------------------


def test_the_window_is_half_open_and_in_seconds(monkeypatch, tmp_path):
    """created_timestamp is epoch SECONDS; the ms window must not drift a row."""
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 10), _ledger(3, "s1", 20)])

    lo = (T0 + 10) * SECOND
    hi = (T0 + 20) * SECOND
    raw = _goose_sessions(since_ms=lo, until_ms=hi)
    turns = [t for session in raw.values() for t in session["turns"]]
    assert [t["timestamp_ms"] for t in turns] == [(T0 + 10) * SECOND]

    # Both edges, exact: a one-second window that starts on the row keeps it,
    # and the same instant as an exclusive upper bound drops it.
    assert len([t for s in _goose_sessions(since_ms=(T0 + 10) * SECOND,
                                           until_ms=(T0 + 11) * SECOND).values()
                for t in s["turns"]]) == 1
    assert _goose_sessions(since_ms=(T0 + 1) * SECOND, until_ms=(T0 + 10) * SECOND) == {}


def test_the_parser_is_never_windowed(monkeypatch, tmp_path):
    """The store replaces the whole corpus, so a windowed parse would persist a
    partial corpus. Only the session loader may window."""
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 10), _ledger(3, "s1", 20)])
    assert len(GooseParser(PricingDatabase())._parse_all()) == 3
    assert len([t for s in _goose_sessions(since_ms=(T0 + 5) * SECOND,
                                           until_ms=(T0 + 15) * SECOND).values()
                for t in s["turns"]]) == 1


def test_ordinal_work_match_survives_a_window_that_opens_mid_session(monkeypatch, tmp_path):
    """The request rank is over the whole ledger, not over the window."""
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 10), _ledger(3, "s1", 20)],
           messages=[
               _assistant_usage("s1", 1, 111, input_tokens=1000, output_tokens=20),
               _assistant_usage("s1", 11, 222, input_tokens=1000, output_tokens=20),
               _assistant_usage("s1", 21, 333, input_tokens=1000, output_tokens=20),
           ])
    raw = _goose_sessions(since_ms=(T0 + 15) * SECOND, until_ms=(T0 + 25) * SECOND)
    turns = [t for session in raw.values() for t in session["turns"]]
    assert len(turns) == 1
    assert turns[0]["_work_ms"] == 333  # the THIRD request, not the first


def test_a_carried_forward_row_is_not_a_turn(monkeypatch, tmp_path):
    """The panel excludes Goose's own backfill for the same reason Overview does.

    It also protects the duration pairing: a backfilled row has no assistant
    message behind it at all, so ranking past it would displace the durations of
    every request that follows.
    """
    db = _setup(monkeypatch, tmp_path,
                sessions=[_session("s1")],
                ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 60)],
                messages=[_assistant_usage("s1", 1, 1111, input_tokens=1000,
                                           output_tokens=20)])
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE usage_ledger SET cost_source = 'carried_forward' WHERE id = 2"
    )
    conn.commit()
    conn.close()
    sessions._clear_goose_windows()
    sessions._goose_sessions_cache_sig = ()

    raw = _goose_sessions()
    turns = [t for session in raw.values() for t in session["turns"]]
    assert len(turns) == 1
    assert turns[0]["_work_ms"] == 1111     # the other row's duration, unmatched


def test_an_estimated_row_is_an_ordinary_turn(monkeypatch, tmp_path):
    """The filter names one value; Goose's own 'estimated' rows are real requests."""
    db = _setup(monkeypatch, tmp_path,
                sessions=[_session("s1")],
                ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 60)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE usage_ledger SET cost_source = 'estimated' WHERE id = 2")
    conn.commit()
    conn.close()
    sessions._clear_goose_windows()
    sessions._goose_sessions_cache_sig = ()

    raw = _goose_sessions()
    turns = [t for session in raw.values() for t in session["turns"]]
    assert len(turns) == 2

def test_active_time_uses_the_measured_durations(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0, inp=1000, out=20),
                   _ledger(2, "s1", 60, inp=2000, out=30)],
           messages=[_assistant_usage("s1", 1, 5000, input_tokens=1000, output_tokens=20),
                     _assistant_usage("s1", 61, 4000, input_tokens=2000, output_tokens=30)])
    row = _listing()["sessions"][0]
    assert row["active_ms"] == 9000


def test_a_missing_usage_block_earns_no_measured_duration(monkeypatch, tmp_path):
    """An interrupted request costs ITS OWN duration and nobody else's.

    The durations come only from the assistant messages that carry a usage
    block, so a session can easily hold three billed rows and two durations.
    Pairing them by position charged row 2 the 3333 ms that belonged to row 3,
    a wrong number rather than a missing one. Each row now looks up its own
    token counts, so the two rows that were billed identically still get 1111
    and 3333 in request order and the interrupted row gets nothing.
    """
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0, inp=1000, out=20),
                   _ledger(2, "s1", 10, inp=700, out=9),
                   _ledger(3, "s1", 20, inp=1500, out=30)],
           messages=[
               _assistant_usage("s1", 1, 1111, input_tokens=1000, output_tokens=20),
               _assistant_no_usage("s1", 11),
               _assistant_usage("s1", 21, 3333, input_tokens=1500, output_tokens=30),
           ])
    turns = [t for s in _goose_sessions().values() for t in s["turns"]]
    assert len(turns) == 3
    assert [t.get("_work_ms") for t in turns] == [1111, None, 3333]

    row = _listing()["sessions"][0]
    assert row["active_ms"] > 0              # the fallback covers the gap
    assert row["active_ms"] != 1111 + 3333   # and never a mispaired sum


def test_equal_counts_do_not_guarantee_the_same_request(monkeypatch, tmp_path):
    """The narrower form of the same trap, which a count check cannot see.

    A duration-bearing message with no billing row (an import's backfilled
    usage answers no request of this session) and a billed row whose response
    never arrived leave the two sequences the SAME LENGTH. The old length guard
    waved that through and paired positionally: the first real request was
    charged the orphan's 999 ms and read as a 999-second turn. Matching on
    counts leaves the row unmeasured instead, which is a missing number.
    """
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0, inp=1000, out=20),
                   _ledger(2, "s1", 10, inp=500, out=7)],
           messages=[
               # Nobody billed these counts: no ledger row owns them.
               _assistant_usage("s1", 1, 999, input_tokens=9999, output_tokens=999),
               _assistant_usage("s1", 11, 5000, input_tokens=500, output_tokens=7),
           ])
    turns = [t for s in _goose_sessions().values() for t in s["turns"]]
    assert [t.get("_work_ms") for t in turns] == [None, 5000]


def test_a_zero_token_row_does_not_shift_the_later_durations(monkeypatch, tmp_path):
    """A row both surfaces skip must not occupy a slot in the duration rank."""
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0),
                   _ledger(2, "s1", 10, inp=0, out=0),
                   _ledger(3, "s1", 20)],
           messages=[
               _assistant_usage("s1", 1, 1111, input_tokens=1000, output_tokens=20),
               _assistant_usage("s1", 11, 2222),
               _assistant_usage("s1", 21, 3333, input_tokens=1000, output_tokens=20),
           ])
    turns = [t for s in _goose_sessions().values() for t in s["turns"]]
    assert len(turns) == 2                               # the zero row is no turn
    # Two rows, three durations: the middle message answered the zero-token
    # row, so its 2222 ms has no billing row to sit on. Matching on counts
    # leaves it unclaimed and hands each real row its own.
    assert [t.get("_work_ms") for t in turns] == [1111, 3333]


def test_a_zero_token_row_costs_the_pairing_nothing(monkeypatch, tmp_path):
    """What the guarded rank buys: a skipped row no longer displaces the rest.

    Goose billed two requests here and reported two durations, so the pairing is
    sound and must survive. Ranking the unguarded ledger is what broke it.
    """
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0),
                   _ledger(2, "s1", 10, inp=0, out=0),
                   _ledger(3, "s1", 20)],
           messages=[
               _assistant_usage("s1", 1, 1111, input_tokens=1000, output_tokens=20),
               _assistant_usage("s1", 21, 2222, input_tokens=1000, output_tokens=20),
           ])
    turns = [t for s in _goose_sessions().values() for t in s["turns"]]
    assert [t["_work_ms"] for t in turns] == [1111, 2222]

def test_a_compaction_row_never_borrows_a_measured_duration(monkeypatch, tmp_path):
    """Compaction turns go unmeasured, because nothing can prove the pairing.

    Goose's compaction request answers like an ordinary one, and its answer
    carries a usage block like an ordinary one -- but nothing on the MESSAGE side
    says "this came from a compaction". So a count-only rule lets a compaction
    row take the duration of whichever ordinary request billed the same counts.
    Leaving the compaction row unmatched costs that one turn its measured number
    and protects the real request beside it. V6 captured no is_compaction row at
    all, so this is defensive rather than observed.
    """
    db = _setup(monkeypatch, tmp_path,
                sessions=[_session("s1")],
                ledger=[_ledger(1, "s1", 0, inp=1000, out=20),
                        _ledger(2, "s1", 10, inp=2000, out=30)],
                messages=[
                    _assistant_usage("s1", 1, 999, input_tokens=1000, output_tokens=20),
                    _assistant_usage("s1", 11, 1111, input_tokens=2000, output_tokens=30),
                ])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE usage_ledger SET is_compaction = 1 WHERE id = 1")
    conn.commit()
    conn.close()
    sessions._clear_goose_windows()
    sessions._goose_sessions_cache_sig = ()

    turns = [t for s in _goose_sessions().values() for t in s["turns"]]

    assert [t.get("is_compaction") for t in turns] == [True, None]
    assert [t.get("_work_ms") for t in turns] == [None, 1111]


def test_a_request_spanning_the_closing_edge_keeps_its_in_window_work(monkeypatch, tmp_path):
    """The row just past the bound can own work that happened INSIDE it.

    A Goose duration runs BACKWARDS from its completion stamp, so the request
    that finished at T0+200 covered [T0+80, T0+200] and a window ending at
    T0+150 owes the 70 s before its own edge. The loader windows at the source,
    so it has to hand that row back the way ZCode does; holding it silently was
    an undercount, not a conservative choice. Reading the same session
    unwindowed and clipping is the answer a non-windowing source would give, and
    the two must now agree.
    """
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0, inp=1000, out=20),
                   _ledger(2, "s1", 200, inp=2000, out=30)],
           messages=[_assistant_usage("s1", 1, 5_000, input_tokens=1000, output_tokens=20),
                     _assistant_usage("s1", 201, 120_000, input_tokens=2000,
                                      output_tokens=30)])
    lo, hi = T0 * SECOND - 10_000, (T0 + 150) * SECOND

    whole = next(iter(_goose_sessions().values()))
    edge = next(iter(_goose_sessions(since_ms=lo, until_ms=hi).values()))
    assert edge["_next_event_ms"] == (T0 + 200) * SECOND
    assert edge["_next_work_ms"] == 120_000

    def total(raw):
        return sum(e - s for s, e in _session_active_intervals(raw, 30 * MINUTE, lo, hi))

    assert total(edge) == total(whole) == 75_000


def test_the_opening_edge_holds_back_nothing_worth_passing(monkeypatch, tmp_path):
    """Why only the closing edge is passed back, and not symmetrically.

    The interval of a row BEFORE the window ends at that row's own stamp, which
    is before the window opened, so the clip discards it and there is no work to
    recover. A _prior_event_ms here would only add an unmeasured stamp.
    """
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0, inp=1000, out=20),
                   _ledger(2, "s1", 100, inp=2000, out=30)],
           messages=[_assistant_usage("s1", 1, 60_000, input_tokens=1000, output_tokens=20),
                     _assistant_usage("s1", 101, 5_000, input_tokens=2000,
                                      output_tokens=30)])
    lo, hi = (T0 + 30) * SECOND, (T0 + 200) * SECOND
    edge = next(iter(_goose_sessions(since_ms=lo, until_ms=hi).values()))
    assert "_prior_event_ms" not in edge

    whole = next(iter(_goose_sessions().values()))

    def total(raw):
        return sum(e - s for s, e in _session_active_intervals(raw, 30 * MINUTE, lo, hi))

    assert total(edge) == total(whole)


def test_an_unmeasured_boundary_request_still_hands_over_its_edge(monkeypatch, tmp_path):
    """The closing edge is owed to the window whether or not it was measured.

    Holding it back until the duration matched looked conservative and was not:
    the fallback path is exactly the path most likely to hold an unmatched row,
    so that was the one path that could not report a session still running when
    the window closed. An unmatched stamp is charged the same CAPPED inter-event
    gap the fallback gives any in-window turn, which is the contract the panel
    already applies - and the test that proves it is the honest one: the same
    session read whole and clipped has no edge to hand over, so the two must
    land on one number.
    """
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0, inp=1000, out=20),
                   _ledger(2, "s1", 200, inp=2000, out=30)],
           # The in-window request measured; the one past the bound ended
           # without a usage block, so nothing says how long IT took.
           messages=[_assistant_usage("s1", 1, 5_000, input_tokens=1000,
                                      output_tokens=20)])
    lo, hi = T0 * SECOND - 10_000, (T0 + 150) * SECOND

    edge = next(iter(_goose_sessions(since_ms=lo, until_ms=hi).values()))
    assert edge["_next_event_ms"] == (T0 + 200) * SECOND
    assert "_next_work_ms" not in edge          # no duration to hand over

    whole = next(iter(_goose_sessions().values()))

    def total(raw):
        return sum(e - s for s, e in _session_active_intervals(raw, 30 * MINUTE, lo, hi))

    # The windowed read may not know how long the boundary request took, but it
    # must not come to a different number than a source that never windowed.
    assert total(edge) > 0
    assert total(edge) <= hi - lo
    assert total(edge) == total(whole)


def test_a_junk_token_value_costs_one_number_not_the_whole_panel(monkeypatch, tmp_path):
    """A TEXT token count reads 0 here, as it does in Overview and in SQL.

    int() on the column raised, the raise escaped the loader, and every Goose
    session in the database went with it - the whole panel for one bad cell in
    one row. GooseParser._i already answers 0 for a value it cannot read, and
    so does SQLite's own arithmetic in the keep-guard, so the loader has to
    answer the same way or the two surfaces disagree about the same row.
    """
    db = _setup(monkeypatch, tmp_path,
                sessions=[_session("s1"), _session("s2")],
                ledger=[_ledger(1, "s1", 0, inp=1000, out=20),
                        _ledger(2, "s2", 60, inp=1000, out=20)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE usage_ledger SET output_tokens = 'not-a-number' WHERE id = 2")
    conn.commit()
    conn.close()
    sessions._clear_goose_windows()
    sessions._goose_sessions_cache_sig = ()

    raw = _goose_sessions()                      # must not raise
    turns = sorted((t for s in raw.values() for t in s["turns"]),
                   key=lambda t: t["timestamp_ms"])

    assert len(turns) == 2                       # both sessions still listed
    assert turns[0]["tokens_out"] == 20
    assert turns[1]["tokens_out"] == 0           # the junk cell, read as 0
    assert turns[1]["tokens_in"] == 1000         # and the rest of it still billed


def test_a_named_corpus_never_opens_the_messages_table_for_prompts(monkeypatch, tmp_path):
    """Only the sessions Goose left unnamed may cost a prompt lookup.

    Every in-window session otherwise drags the content_json of all its user
    messages through the reader to produce a string the loader then throws away,
    and the dashboard warms several windows per start. Counting the text
    extractor is the cheap way to see the query never ran.
    """
    calls = []
    real = sessions._goose_content_text
    monkeypatch.setattr(sessions, "_goose_content_text",
                        lambda value: calls.append(value) or real(value))
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1", name="Refactor the parser"),
                     _session("s2", name="Fix the flaky test")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s2", 5)],
           messages=[_user_prompt("s1", 2, "do it"), _user_prompt("s2", 7, "do that")])
    rows = {row["display_name"] for row in _listing()["sessions"]}
    assert rows == {"Refactor the parser", "Fix the flaky test"}
    assert calls == []


def test_a_mix_of_named_and_unnamed_sessions_still_names_both(monkeypatch, tmp_path):
    """The narrowed prompt list must not lose the session that does need it."""
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1", name="Named by the user"),
                     _session("s2", name="CLI Session")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s2", 5)],
           messages=[_user_prompt("s1", 2, "ignored, it has a name"),
                     _user_prompt("s2", 7, "the fallback title")])
    rows = {row["display_name"] for row in _listing()["sessions"]}
    assert rows == {"Named by the user", "the fallback title"}


def test_a_compaction_request_is_billed_and_marked(monkeypatch, tmp_path):
    """Goose flags a compaction request; the flag survives to the API row.

    The row is billed like any other - it carries tokens - so it has to stay
    distinguishable rather than become an ordinary turn in the listing.
    """
    row = list(_ledger(1, "s1", 0, inp=2000, out=10))
    row[-1] = 1  # is_compaction
    _setup(monkeypatch, tmp_path, sessions=[_session("s1")], ledger=[tuple(row)])
    turns = [t for s in _goose_sessions().values() for t in s["turns"]]
    assert turns[0]["is_compaction"] is True
    assert turns[0]["tokens"] == 2010

    listed = get_session_detail("goose", "s1")["turns"][0]
    assert listed["is_compaction"] is True


def test_no_usage_messages_still_lists_the_sessions(monkeypatch, tmp_path):
    """elapsedMs is active time only; without it the turns are still billed."""
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0, inp=4000, out=30)])
    row = _listing()["sessions"][0]
    assert row["tokens"] == 4030
    assert "active_ms" in row


# --- empty, schema, and failure states ---------------------------------------


def test_no_database_is_an_empty_success(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    assert _goose_sessions() == {}


def test_missing_ledger_on_a_goose_database_raises(tmp_path, monkeypatch):
    """A Goose DB whose ledger table vanished is a failure, not an empty list."""
    db = tmp_path / "xdg" / "goose" / "sessions" / "sessions.db"
    _make_db(db, with_ledger=False, sessions=[_session("s1")], ledger=[])
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    _parser(monkeypatch, xdg=tmp_path / "xdg")
    with pytest.raises(GooseReadError):
        _goose_sessions()


def test_a_schema_error_is_not_cached_as_empty(tmp_path, monkeypatch):
    db = tmp_path / "xdg" / "goose" / "sessions" / "sessions.db"
    _make_db(db, with_ledger=False, sessions=[_session("s1")], ledger=[])
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    _parser(monkeypatch, xdg=tmp_path / "xdg")
    with pytest.raises(GooseReadError):
        _goose_sessions()
    assert sessions._goose_sessions_cache == {}

    # Repair the database and the same call must see it, with no cache in the way.
    db.unlink()
    _make_db(db, sessions=[_session("s2")], ledger=[_ledger(9, "s2", 0)])
    assert {row["session_id"] for row in _listing()["sessions"]} == {"s2"}


def test_a_corrupt_database_raises_rather_than_reading_as_empty(tmp_path, monkeypatch):
    db = tmp_path / "xdg" / "goose" / "sessions" / "sessions.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a database, just bytes where a header should be")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    _parser(monkeypatch, xdg=tmp_path / "xdg")
    with pytest.raises(GooseReadError):
        _goose_sessions()


def test_a_broken_read_is_not_flattened_at_the_public_layer(monkeypatch, tmp_path):
    """The route turns this into a 500; the claim here is that no layer between
    the loader and get_sessions_data answers an empty panel instead."""
    db = tmp_path / "xdg" / "goose" / "sessions" / "sessions.db"
    _make_db(db, with_ledger=False, sessions=[_session("s1")], ledger=[])
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    _parser(monkeypatch, xdg=tmp_path / "xdg")
    with pytest.raises(GooseReadError):
        get_sessions_data("goose", "all")


# --- detail, caching, frontend ----------------------------------------------


def test_session_detail_lists_the_turns(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 5)])
    detail = get_session_detail("goose", "s1")
    assert [t["turn_index"] for t in detail["turns"]] == [1, 2]
    assert detail["session"]["tool"] == "goose"
    assert "_event_key" not in detail["turns"][0]
    with pytest.raises(FileNotFoundError):
        get_session_detail("goose", "nope")


def test_new_ledger_rows_reach_the_panel(monkeypatch, tmp_path):
    db = _setup(monkeypatch, tmp_path,
                sessions=[_session("s1")], ledger=[_ledger(1, "s1", 0)])
    assert _listing()["summary"]["session_count"] == 1

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO usage_ledger (id, session_id, created_timestamp, model, "
            "input_tokens, output_tokens, total_tokens, cache_read_tokens, "
            "cache_write_tokens, cost, is_compaction) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            _ledger(77, "s1", 400),
        )
        conn.commit()
    finally:
        conn.close()
    # The DB's own signature moved, so the cached view must not be reused.
    # The signature is (mtime_ns, size) and this filesystem's mtime clock is
    # coarse (measured ~4 ms), so a write this small does not always move it and
    # the test would then pass by accident or fail by accident. Advance the
    # clock by hand: what is under test is the cache rule, not the clock.
    stat = db.stat()
    os.utime(db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    sessions._goose_corpus_cache_life = 0.0  # the window closed
    assert _listing()["sessions"][0]["token_events"] == 2


def test_a_live_database_is_read_once_per_window_not_per_commit(monkeypatch, tmp_path):
    """A Goose that is merely running moves the signature with every request.

    One refresh asks for eight windows. Trusting the signature absolutely
    re-copies a database that can pass a gigabyte for each of them to pick up
    rows a fraction of a second apart, which is the active-Goose regression:
    the panel got slower the busier Goose was.
    """
    db = _setup(monkeypatch, tmp_path,
                sessions=[_session("s1")], ledger=[_ledger(1, "s1", 0)])
    monkeypatch.setattr(sessions, "_sig_lifetime", lambda cost: 600.0)
    calls = _counting_snapshot(monkeypatch, [])
    assert len(_goose_sessions()) == 1
    assert len(calls) == 1

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO usage_ledger (id, session_id, created_timestamp, model, "
        "input_tokens, output_tokens, total_tokens, cache_read_tokens, "
        "cache_write_tokens, cost, is_compaction) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        _ledger(55, "s1", 300),
    )
    conn.commit()
    conn.close()
    stat = db.stat()
    os.utime(db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    # A later window of the same refresh, asked after that commit: answered from
    # the corpus already in hand, with no second copy of the file.
    sessions._clear_goose_windows()
    sessions._goose_sessions_cache_sig = ()
    assert len(_goose_sessions()) == 1
    assert len(calls) == 1, "a live Goose re-copied the database for one window"

    # The new row is held, not lost: it lands as soon as the window closes.
    sessions._goose_corpus_cache_life = 0.0
    assert len(_goose_sessions()) == 1
    assert len(calls) == 2
    turns = [t for raw in _goose_sessions().values() for t in raw["turns"]]
    assert len(turns) == 2, "the honoured window outlived the row it was holding"


def test_the_honoured_window_never_covers_a_different_database(monkeypatch, tmp_path):
    """A second Goose root is a different corpus, not a stale one.

    The window excuses re-reading a file that moved. It cannot excuse answering
    for a file nobody has read, which is what a signature-blind cache would do
    the moment a second root appears.
    """
    first = _setup(monkeypatch, tmp_path,
                   sessions=[_session("s1")], ledger=[_ledger(1, "s1", 0)])
    monkeypatch.setattr(sessions, "_sig_lifetime", lambda cost: 600.0)
    calls = _counting_snapshot(monkeypatch, [])
    assert {r["session_id"] for r in _listing()["sessions"]} == {"s1"}

    root2 = tmp_path / "xdg2"
    other = root2 / "goose" / "sessions" / "sessions.db"
    _make_db(other, sessions=[_session("s2")], ledger=[_ledger(2, "s2", 0)])
    monkeypatch.setenv("XDG_DATA_HOME", str(root2))
    assert str(other) != str(first)

    assert {r["session_id"] for r in _listing()["sessions"]} == {"s2"}
    assert len(calls) == 2, "one database was answered from another one s corpus"


def test_frontend_session_registry_includes_goose():
    index = Path(sessions.__file__).parent / "static" / "index.html"
    source = index.read_text(encoding="utf-8")
    assert "'openclaw', 'qoder_cli', 'goose', 'roo_code']" in source
    assert "openclaw: null, qoder_cli: null, goose: null, roo_code: null, combined: null" in source
    assert 'updateSessionPanel("goose", lastSessionsResponses.goose);' in source
    assert 'initSortHeaders("goose", renderSessionsTab);' in source
    assert "goose: { ...DEFAULT_SORT }," in source
    assert "goose: 'Goose'," in source
    # The heading key is a label, camelCase; the ids are the raw tool key.
    assert "gooseSessions: 'Goose Sessions'," in source
    assert "gooseSessions: 'Goose 会话'," in source
    assert "gooseSessions: 'Goose セッション'," in source
    assert "gooseSessions: 'Goose 세션'," in source
    assert "gooseSessions: 'Sesiones de Goose'," in source
    assert "gooseSessions: 'Sessões do Goose'," in source
    assert 'id="gooseSessionsTable"' in source
    assert 'data-panel-details="goose"' in source
    assert 'data-panel="goose"' in source
    assert 'id="goosePanelCount"' in source
    assert 'id="gooseLatestSession"' in source
    assert 'id="gooseActiveAgent"' in source
    assert source.count('data-panel="goose"') == 1
    assert (index.parent / "icons" / "agents" / "goose.svg").is_file()


def test_a_junk_timestamp_costs_one_row_not_the_panel(monkeypatch, tmp_path):
    """The row split runs BEFORE the loop that tolerates a junk stamp.

    Goose's DDL says NOT NULL, so NULL is not the realistic junk; a text stamp
    is (SQLite keeps it as TEXT under INTEGER affinity), and
    `_goose_ts_to_ms` is written to refuse that rather than propagate it. The
    split into window rows and boundary rows must not be the one place that
    raises, because raising here costs the whole panel, not the bad row.
    """
    db = _setup(monkeypatch, tmp_path,
                sessions=[_session("s1")],
                ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 60)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE usage_ledger SET created_timestamp = 'not a time' WHERE id = 2")
    conn.commit()
    conn.close()
    sessions._clear_goose_windows()

    turns = [t for raw in _goose_sessions().values() for t in raw["turns"]]
    assert [t["timestamp_ms"] for t in turns] == [T0 * SECOND]   # the good row lists


# ---------------------------------------------------------------------------
# Bounds on the held windows
#
# A window is the whole history sliced by time, so bounding the cache by window
# COUNT bounds nothing that matters: over a large history, the dashboard's own
# working set is several copies of it. Measured on a 200,000-row ledger, an
# eleven-window sweep was kept in full at 1,377 MB over the corpus, 1,404 MB peak.
# ---------------------------------------------------------------------------


def test_a_held_window_counts_its_turns_not_just_itself(monkeypatch, tmp_path):
    """Two windows over a big history are two copies of it, and the cache knows."""
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(i, "s1", i * 10) for i in range(1, 11)])

    sessions._goose_sessions_cache.clear()
    sessions._goose_store(("sig",), (None, None), {
        "s1": {"turns": [{"i": i} for i in range(40)]},
    })
    assert sessions._goose_sessions_cache_turns == 40
    # Overlapping windows of one history are the normal case -- the Report tab
    # asks for day inside week inside month inside year -- so the budget is
    # scaled to the largest of them rather than fixed, and the wide view that
    # cost the most to build is not the one thrown away.
    sessions._goose_store(("sig",), (0, 1), {
        "s1": {"turns": [{"i": i} for i in range(39)]},
    })
    assert len(sessions._goose_sessions_cache) == 2
    assert sessions._goose_sessions_cache_turns == 79


def test_the_window_cache_stops_multiplying_the_history(monkeypatch, tmp_path):
    """Past the ceiling the oldest window goes; the cache is not a second corpus."""
    sessions._clear_goose_windows()
    width = sessions._GOOSE_SESSIONS_TURN_CEILING // 2 + 1
    for i in range(4):
        sessions._goose_store(("sig",), (i, i + 1), {
            "s1": {"turns": [{"i": j} for j in range(width)]},
        })
    assert len(sessions._goose_sessions_cache) < 4, (
        "four windows each holding half the ceiling were all kept: the ceiling "
        "must stop the cache holding several copies of one history")
    # The most recent survives, and the accounting went with what was dropped.
    assert (3, 4) in sessions._goose_sessions_cache
    held = sum(len(session["turns"])
               for window in sessions._goose_sessions_cache.values()
               for session in window.values())
    assert sessions._goose_sessions_cache_turns == held, (
        "the turn accounting no longer matches what is held, so the next budget "
        "decision is made against a history that is not in the cache")


def test_one_window_bigger_than_the_ceiling_is_still_held(monkeypatch, tmp_path):
    """Dropping the view the user is looking at to save nothing is no bound at all."""
    sessions._clear_goose_windows()
    width = sessions._GOOSE_SESSIONS_TURN_CEILING * 2
    sessions._goose_store(("sig",), (None, None), {
        "s1": {"turns": [{"i": i} for i in range(width)]},
    })
    assert len(sessions._goose_sessions_cache) == 1
    assert sessions._goose_sessions_cache_turns == width


def test_a_new_corpus_clears_the_turn_accounting_with_the_windows(monkeypatch, tmp_path):
    """The counts live beside the windows, so a signature change must take them."""
    sessions._clear_goose_windows()
    sessions._goose_store(("old-signature",), (None, None), {
        "s1": {"turns": [{"i": i} for i in range(100)]},
    })
    assert sessions._goose_sessions_cache_turns == 100

    sessions._goose_store(("new-signature",), (None, None), {
        "s1": {"turns": [{"i": 0}]},
    })
    assert sessions._goose_sessions_cache_turns == 1, (
        "the new corpus is being judged against a budget spent on the old one")


def test_a_moved_database_resets_the_window_accounting(monkeypatch, tmp_path):
    """A signature change inside the loader must take the counts with it.

    The turn counts cannot be derived from the windows they price, so the path
    that clears the windows on a moved database has to clear them too: a clear
    that leaves them behind judges the new history against a budget spent on the
    one it replaced, which is a bound that evicts at the wrong moment in both
    directions.
    """
    db = _setup(monkeypatch, tmp_path,
                sessions=[_session("s1")],
                ledger=[_ledger(i, "s1", i * 10) for i in range(1, 4)])
    monkeypatch.setattr(sessions, "_sig_lifetime", lambda cost: 0.0)
    assert len(_goose_sessions()) == 1
    assert sessions._goose_sessions_cache_turns == 3

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM usage_ledger WHERE id = 3")
    conn.commit()
    conn.close()
    stat = db.stat()
    os.utime(db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    assert len(_goose_sessions()) == 1
    held = sum(len(session["turns"])
               for window in sessions._goose_sessions_cache.values()
               for session in window.values())
    assert (held, sessions._goose_sessions_cache_turns) == (2, 2), (
        "the accounting survived the signature change that dropped its windows")
