"""Tests for Grok Build (Grok CLI) as a session source (sessions.py).

Grok writes one global append-only log (unified.jsonl) with a sid on each
inference row, plus per-session summary.json metadata. The harness must not
re-implement the row rules: it consumes the same iter_grok_usage_rows
generator GrokParser uses, so parity is by construction. This file pins that
parity bucket for bucket, in total and per model, over all-time and date
windows.
"""
from __future__ import annotations

import json
import random
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokdash import sessions
from tokdash.dateutil import parse_date_range
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    _grok_sessions,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources.coding_tools import BaseParser, GrokParser

from test_grok_parser import _inference, _model_event, _write_log

# Two fixed days for window tests.
DAY1_MS = int(datetime(2026, 7, 22, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
DAY2_MS = int(datetime(2026, 7, 23, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _infer(pid, ts, prompt, completion, reasoning=0, cached=0, loop_index=1, sid=None):
    row = _inference(pid, ts, prompt, completion,
                     reasoning=reasoning, cached=cached, loop_index=loop_index)
    if sid is not None:
        row["sid"] = sid
    return row


def _write_summary(grok_home: Path, cwd: str, session_id: str, **overrides) -> Path:
    encoded = urllib.parse.quote(cwd, safe="")
    path = grok_home / "sessions" / encoded / session_id / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "info": {"id": session_id, "cwd": cwd},
        "session_summary": "",
        "created_at": "2026-07-24T15:41:57.235041407Z",
        "updated_at": "2026-07-24T16:03:59.648885846Z",
        "num_messages": 2,
        "current_model_id": "grok-4.5",
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolated_grok_home(monkeypatch, tmp_path):
    home = tmp_path / ".grok"
    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    sessions._load_grok_sessions.cache_clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()
    yield home
    sessions._load_grok_sessions.cache_clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()


def _parser_totals(entries):
    by_model = {}
    for e in entries:
        b = by_model.setdefault(
            e["model"], {"input": 0, "output": 0, "cache": 0, "cost": 0.0, "n": 0}
        )
        b["input"] += e["input"]
        b["output"] += e["output"]
        b["cache"] += e["cacheRead"]
        b["cost"] += e["cost"]
        b["n"] += 1
    return by_model


def _harness_totals(since_ms=None, until_ms=None):
    """Turn sums clipped half-open [since, until), like _summarize_session."""
    by_model = {}
    for s in _grok_sessions().values():
        for t in s["turns"]:
            ts = t["timestamp_ms"]
            if since_ms is not None and ts < since_ms:
                continue
            if until_ms is not None and ts >= until_ms:
                continue
            b = by_model.setdefault(
                t["model"], {"input": 0, "output": 0, "cache": 0, "cost": 0.0, "n": 0}
            )
            b["input"] += t["tokens_in"]
            b["output"] += t["tokens_out"]
            b["cache"] += t["tokens_cache"]
            b["cost"] += t["cost"]
            b["n"] += 1
    return by_model


def test_grok_registered():
    assert "grok" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["grok"] == "Grok Build"
    listing = get_sessions_data("grok", "all")  # no home: empty, no error
    assert listing["sessions"] == []
    with pytest.raises(ValueError):
        get_sessions_data("not_a_tool", "all")


def test_mapping(_isolated_grok_home):
    home = _isolated_grok_home
    rows = [
        _model_event(101, "grok-4.5"),
        _model_event(102, "grok-4.3"),
        _infer(101, "2026-07-22T12:00:00Z", prompt=15584, completion=245,
               reasoning=68, cached=1024, sid="sid-alpha"),
        _infer(102, "2026-07-22T12:05:00Z", prompt=1000, completion=50,
               cached=0, sid="sid-beta"),
    ]
    _write_log(home.parent, rows)
    _write_summary(home, "/work/alpha", "sid-alpha", generated_title="Fix the parser")
    _write_summary(home, "/work/beta", "sid-beta", session_summary="Second session")

    raw = _grok_sessions()
    assert set(raw) == {"sid-alpha", "sid-beta"}
    alpha = raw["sid-alpha"]
    assert alpha["tool"] == "grok"
    assert alpha["project"] == "alpha"  # basename of the summary cwd
    assert alpha["display_name"] == "Fix the parser"
    assert alpha["_display_name_explicit"] is True
    # generated_title absent -> session_summary.
    assert raw["sid-beta"]["display_name"] == "Second session"
    assert raw["sid-beta"]["project"] == "beta"

    turn = alpha["turns"][0]
    # prompt=15584/cached=1024/completion=245/reasoning=68.
    assert turn["tokens_in"] == 15584 - 1024
    assert turn["tokens_cache"] == 1024
    assert turn["tokens_out"] == 245 + 68  # reasoning folded into output
    assert turn["tokens_reasoning"] == 0
    assert turn["tokens"] == (15584 - 1024) + 1024 + 245 + 68
    # Model attributed per pid.
    assert turn["model"] == "grok-4.5"
    assert raw["sid-beta"]["turns"][0]["model"] == "grok-4.3"
    bill = turn["_bill"]
    assert bill["rule"] == "fresh-input"
    assert "fixed" not in bill
    assert turn["cost"] == pytest.approx(
        PricingDatabase().get_cost("grok-4.5", 14560, 313, 1024, 0)
    )
    assert turn["_event_key"].startswith("grok:101:")


def test_date_windows(_isolated_grok_home):
    home = _isolated_grok_home
    rows = [
        _model_event(101, "grok-4.5"),
        _infer(101, _iso(DAY1_MS + 60_000), prompt=100, completion=10, sid="s1"),
        _infer(101, _iso(DAY1_MS + 120_000), prompt=200, completion=20, sid="s1"),
        _infer(101, _iso(DAY2_MS + 60_000), prompt=300, completion=30, sid="s2"),
    ]
    _write_log(home.parent, rows)

    all_listing = get_sessions_data("grok", "all")
    assert {s["session_id"] for s in all_listing["sessions"]} == {"s1", "s2"}
    assert sum(s["token_events"] for s in all_listing["sessions"]) == 3

    day1 = get_sessions_data("grok", "range", date_from="2026-07-22", date_to="2026-07-22")
    assert {s["session_id"] for s in day1["sessions"]} == {"s1"}
    assert day1["sessions"][0]["token_events"] == 2
    day2 = get_sessions_data("grok", "range", date_from="2026-07-23", date_to="2026-07-23")
    assert {s["session_id"] for s in day2["sessions"]} == {"s2"}
    assert day2["sessions"][0]["token_events"] == 1


def test_parity_property(_isolated_grok_home):
    """The load-bearing property: the harness is a partition of the parser's
    survivor set, so totals match in total and per model over all-time and
    date windows."""
    home = _isolated_grok_home
    rng = random.Random(20260825)
    pids = [101, 102, 103]
    sids = ["s-a", "s-b", "s-c"]
    rows = [
        _model_event(101, "grok-4.5"),
        _model_event(102, "grok-4.5"),
        _model_event(103, "grok-4.3"),
        # pid 201 never gets a model event: its rows are unattributable.
    ]
    written = []
    for _ in range(200):
        day = DAY1_MS if rng.random() < 0.5 else DAY2_MS
        ts = _iso(day + rng.randrange(0, 86_400_000, 1000))
        pid = rng.choice(pids + [201])
        sid = rng.choice(sids + [None, None])
        prompt = rng.choice([0, rng.randrange(10, 5000)])
        cached = rng.randrange(0, prompt + 1) if prompt else 0
        completion = rng.choice([0, rng.randrange(1, 500)])
        reasoning = rng.choice([0, rng.randrange(0, 100)])
        loop_index = rng.choice([1, 1, 1, 2])
        row = _infer(pid, ts, prompt, completion,
                     reasoning=reasoning, cached=cached, loop_index=loop_index, sid=sid)
        written.append(row)
        rows.append(row)
    # A duplicate entry_id (same pid/ts/loop_index) counts once on both sides.
    rows.append(written[5])
    # A corrupt line both sides must skip.
    rows.append("{this line is not json")

    _write_log(home.parent, rows)
    parser = GrokParser(PricingDatabase())

    for label, kwargs in (
        ("all", {}),
        ("day1", ("2026-07-22", "2026-07-22")),
        ("day2", ("2026-07-23", "2026-07-23")),
    ):
        if label == "all":
            since_ms = until_ms = None
            entries = parser.collect(None, None)
            listing = get_sessions_data("grok", "all")
        else:
            date_from, date_to = kwargs
            since_dt, until_dt = parse_date_range(date_from, date_to)
            since_ms = int(since_dt.timestamp() * 1000)
            until_ms = int(until_dt.timestamp() * 1000)
            entries = parser.collect(since_dt, until_dt)
            listing = get_sessions_data("grok", "range", date_from=date_from,
                                        date_to=date_to)
        p = _parser_totals(entries)
        h = _harness_totals(since_ms=since_ms, until_ms=until_ms)
        assert set(h) == set(p), label
        for model in p:
            for field in ("n", "input", "output", "cache"):
                assert h[model][field] == p[model][field], (label, model, field)
            assert h[model]["cost"] == pytest.approx(p[model]["cost"], abs=1e-12), (label, model)
        assert sum(s["token_events"] for s in listing["sessions"]) == len(entries), label
        # The summary rows price the same survivors.
        assert sum(s["tokens_in"] for s in listing["sessions"]) == sum(
            e["input"] for e in entries
        ), label
        assert sum(s["tokens_cache"] for s in listing["sessions"]) == sum(
            e["cacheRead"] for e in entries
        ), label
        assert sum(s["tokens_out"] for s in listing["sessions"]) == sum(
            e["output"] for e in entries
        ), label
        assert sum(s["cost"] for s in listing["sessions"]) == pytest.approx(
            sum(e["cost"] for e in entries), abs=1e-12
        ), label
        # No summary.json anywhere: projects unknown, no titles, no crash.
        assert all(s["project"] == "unknown" for s in listing["sessions"]), label


def test_unattributable_rows_excluded(_isolated_grok_home):
    home = _isolated_grok_home
    # No model events at all: every row is unattributable and dies on both
    # sides rather than bucketing under an unpriceable unknown model.
    _write_log(home.parent, [
        _infer(201, "2026-07-22T12:00:00Z", prompt=100, completion=10, sid="s1"),
    ])
    assert _grok_sessions() == {}
    parser = GrokParser(PricingDatabase())
    assert parser.collect(None, None) == []


def test_missing_sid_lands_in_unattributed_bucket(_isolated_grok_home):
    home = _isolated_grok_home
    _write_log(home.parent, [
        _model_event(101, "grok-4.5"),
        _infer(101, "2026-07-22T12:00:00Z", prompt=100, completion=10),  # no sid
    ])
    raw = _grok_sessions()
    assert set(raw) == {"grok:unattributed"}
    assert raw["grok:unattributed"]["turns"][0]["tokens_in"] == 100
    # The parser bills the same row, so totals still reconcile.
    entries = GrokParser(PricingDatabase()).collect(None, None)
    assert len(entries) == 1


def test_duplicate_entry_id_counted_once(_isolated_grok_home):
    home = _isolated_grok_home
    row = _infer(101, "2026-07-22T12:00:00Z", prompt=100, completion=10, sid="s1")
    _write_log(home.parent, [
        _model_event(101, "grok-4.5"),
        row,
        dict(row),  # identical pid/ts/loop_index -> same entry_id
    ])
    raw = _grok_sessions()
    assert len(raw["s1"]["turns"]) == 1
    assert len(GrokParser(PricingDatabase()).collect(None, None)) == 1
