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


@pytest.fixture(autouse=True)
def _clean_caches():
    sessions._goose_sessions_cache.clear()
    sessions._goose_sessions_cache_sig = ()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()
    yield
    sessions._goose_sessions_cache.clear()
    sessions._goose_sessions_cache_sig = ()
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
    sessions._goose_sessions_cache.clear()
    sessions._goose_sessions_cache_sig = ()
    assert {row["session_id"] for row in _listing()["sessions"]} == {"p1"}
    # Billed either way: the subagent's tokens were spent, so Overview keeps them.
    assert len(GooseParser(PricingDatabase())._parse_all()) == 2


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


def test_active_time_uses_the_measured_durations(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 60)],
           messages=[_assistant_usage("s1", 1, 5000), _assistant_usage("s1", 61, 4000)])
    row = _listing()["sessions"][0]
    assert row["active_ms"] == 9000


def test_a_missing_usage_block_earns_no_measured_duration(monkeypatch, tmp_path):
    """An interrupted request used to shift every LATER duration by one.

    The rank runs over billed ledger rows; the durations come only from the
    assistant messages that carry a usage block. Once one of those is missing
    the two sequences are different lengths and position k is no longer row k:
    the positional read charged row 2 the 3333 ms that belonged to row 3, a
    wrong number rather than a missing one. A session whose counts disagree now
    keeps the capped inter-event-gap contract the design already promises.
    """
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 10), _ledger(3, "s1", 20)],
           messages=[
               _assistant_usage("s1", 1, 1111, input_tokens=1000, output_tokens=20),
               _assistant_no_usage("s1", 11),
               _assistant_usage("s1", 21, 3333, input_tokens=1000, output_tokens=20),
           ])
    turns = [t for s in _goose_sessions().values() for t in s["turns"]]
    assert len(turns) == 3
    assert all("_work_ms" not in t for t in turns)

    row = _listing()["sessions"][0]
    assert row["active_ms"] > 0              # the fallback still measures work
    assert row["active_ms"] != 1111 + 3333   # and never the mispaired sum


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
    assert all("_work_ms" not in t for t in turns)       # three durations, two rows


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
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 200)],
           messages=[_assistant_usage("s1", 1, 5_000),
                     _assistant_usage("s1", 201, 120_000)])
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
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 100)],
           messages=[_assistant_usage("s1", 1, 60_000),
                     _assistant_usage("s1", 101, 5_000)])
    lo, hi = (T0 + 30) * SECOND, (T0 + 200) * SECOND
    edge = next(iter(_goose_sessions(since_ms=lo, until_ms=hi).values()))
    assert "_prior_event_ms" not in edge

    whole = next(iter(_goose_sessions().values()))

    def total(raw):
        return sum(e - s for s, e in _session_active_intervals(raw, 30 * MINUTE, lo, hi))

    assert total(edge) == total(whole)


def test_an_unmeasured_boundary_request_is_not_charged_the_gap(monkeypatch, tmp_path):
    """No elapsedMs for the row past the bound means no boundary event at all.

    A stamp with no measured duration is charged the CAPPED GAP to the next
    event, which bills idle time the source never measured. The count guard that
    protects the in-window pairing protects this edge the same way.
    """
    _setup(monkeypatch, tmp_path,
           sessions=[_session("s1")],
           ledger=[_ledger(1, "s1", 0), _ledger(2, "s1", 200)],
           # One duration for two billed rows: the second request ended without
           # a usage block, so nothing here says how long it took.
           messages=[_assistant_usage("s1", 1, 5_000)])
    edge = next(iter(_goose_sessions(since_ms=T0 * SECOND,
                                     until_ms=(T0 + 150) * SECOND).values()))
    assert "_next_event_ms" not in edge
    assert "_next_work_ms" not in edge
    assert _listing()["sessions"][0]["active_ms"] > 0   # the fallback still runs


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
    assert _listing()["sessions"][0]["token_events"] == 2


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
