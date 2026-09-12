"""Tests for Qwen Code as a session source (sessions.py).

Qwen Code's token store is the append-only per-session chat JSONL
(<base>/projects/<id>/chats/*.jsonl, plus the pre-rename tmp/ layout). The
harness must reproduce the parser's file set, per-record skip rules, the
cache-inclusive prompt split, and the source-global "qwen:<uuid>" fork dedupe
so windowed session sums match QwenCodeParser's entries exactly (the parity
gate is fixture-driven: the local install holds one 8-record chat file).
"""
from __future__ import annotations

import ast
import builtins
import json
import os
import random
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tokdash.sessions as sessions
from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    _raw_sessions_for_tool,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources import coding_tools
from tokdash.sources.coding_tools import (
    BaseParser,
    QwenCodeParser,
    _sig_cache,
)
from tokdash.compute import CodingToolsUsageTracker

# One priced model so billing drift is visible; "qwen-unpriced" stays absent
# from the pricing file to pin the zero-cost path.
RATES = {
    "qwen-model": {"input": 2.0, "output": 4.0, "cache_read": 0.2, "cache_write": 2.0},
}

_LOCAL_TZ = datetime.now().astimezone().tzinfo
_TODAY_LOCAL = datetime.now().astimezone().replace(
    hour=0, minute=0, second=0, microsecond=0
)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# Anchored so the named day/month windows behave in any timezone: today's noon
# is always inside the day window, the first-of-month noon always inside the
# month window.
T_TODAY = _ms(_TODAY_LOCAL.replace(hour=12))
T_TODAY2 = _ms(_TODAY_LOCAL.replace(hour=13))
T_YESTERDAY = _ms(_TODAY_LOCAL - timedelta(hours=12))
T_MONTH_START = _ms(
    _TODAY_LOCAL.replace(day=1, hour=12)
)
T_OLD = _ms(_TODAY_LOCAL - timedelta(days=200, hours=12))


def _setup(monkeypatch, tmp_path) -> Path:
    """Point the harness + pricing at a hermetic tree; call after writing files."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    override = PricingDatabase().override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps({"version": "test", "aliases": {}, "models": RATES}),
        encoding="utf-8",
    )
    reload_pricing_db()
    return tmp_path


def _rec(uuid, ts_ms, rtype="assistant", model="qwen-model", usage=None,
         session_id="s1", cwd=None, **extra):
    rec = {
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": session_id,
        "timestamp": _iso(datetime.fromtimestamp(ts_ms / 1000, timezone.utc)),
        "type": rtype,
        "version": "0.1.0",
    }
    if cwd:
        rec["cwd"] = cwd
    if model is not None:
        rec["model"] = model
    if usage is not None:
        rec["usageMetadata"] = usage
    rec.update(extra)
    return rec


def _user_rec(text, ts_ms, session_id="s1", cwd=None):
    rec = _rec(f"user-{ts_ms}", ts_ms, rtype="user", session_id=session_id, cwd=cwd)
    rec["message"] = {"role": "user", "parts": [{"text": text}]}
    return rec


def _usage(prompt=100, cached=0, candidates=20, thoughts=0):
    return {
        "promptTokenCount": prompt,
        "cachedContentTokenCount": cached,
        "candidatesTokenCount": candidates,
        "thoughtsTokenCount": thoughts,
    }


def _write_chat(base: Path, project: str, session_file: str, records) -> Path:
    chats = base / "projects" / project / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    path = chats / f"{session_file}.jsonl"
    path.write_text("\n".join(
        r if isinstance(r, str) else json.dumps(r) for r in records
    ) + "\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch, tmp_path):
    # qwen_file_signatures sits behind _timed_sigs with a 5 s module-global
    # TTL; clearing it plus the per-file/aggregate caches and the shared
    # per-source entry cache keeps every test on a fresh scan of its own
    # unique tmp_path tree. QWEN_RUNTIME_DIR is the clientpaths switch, so one
    # env pin per test keeps the real ~/.qwen out of the suite.
    monkeypatch.setenv("QWEN_RUNTIME_DIR", str(tmp_path / "qwen-root"))
    sessions._parse_qwen_code_session_file.cache_clear()
    sessions._load_qwen_code_sessions.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()
    yield
    sessions._parse_qwen_code_session_file.cache_clear()
    sessions._load_qwen_code_sessions.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()


def _qwen_only_tracker() -> CodingToolsUsageTracker:
    tracker = CodingToolsUsageTracker()
    tracker.parsers = {"qwen_code": tracker.parsers["qwen_code"]}
    return tracker


def _entry_sums(entries):
    tokens = sum(
        e["input"] + e["output"] + e["cacheRead"] + e["cacheWrite"] + e["reasoning"]
        for e in entries
    )
    cost = sum(e["cost"] for e in entries)
    return tokens, cost


def _summary_sums(data):
    tokens = data["summary"]["tokens"]
    cost = data["summary"]["cost"]
    return tokens, cost


def test_registered():
    assert "qwen_code" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["qwen_code"] == "Qwen Code"


def test_two_sessions_exact_totals(monkeypatch, tmp_path):
    base = tmp_path / "qwen-root"
    _write_chat(base, "proj-a", "s1", [
        _user_rec("first question", T_YESTERDAY, session_id="s1", cwd="/work/demo"),
        _rec("a1", T_YESTERDAY + 1000, usage=_usage(prompt=100, cached=40, candidates=20, thoughts=5), session_id="s1", cwd="/work/demo"),
        _rec("a2", T_TODAY, usage=_usage(prompt=200, cached=50, candidates=30), session_id="s1", cwd="/work/demo"),
    ])
    _write_chat(base, "proj-b", "s2", [
        _rec("b1", T_TODAY2, usage=_usage(prompt=60, cached=0, candidates=10, thoughts=7), session_id="s2", cwd="/work/other"),
    ])
    _setup(monkeypatch, tmp_path)

    data = get_sessions_data("qwen_code", "all")
    assert data["tool_label"] == "Qwen Code"
    sessions_by_id = {s["session_id"]: s for s in data["sessions"]}
    assert set(sessions_by_id) == {"s1", "s2"}

    # 1:1 bucket mapping: tokens_in is the fresh prompt share, cache is the
    # cached share, reasoning is thoughts; cacheWrite is permanently 0.
    s1 = sessions_by_id["s1"]
    assert s1["tokens_in"] == (100 - 40) + (200 - 50)
    assert s1["tokens_cache"] == 40 + 50
    assert s1["tokens_out"] == 20 + 30
    assert s1["tokens_reasoning"] == 5
    # cacheWrite is permanently 0 for Qwen: the total is prompt + output +
    # thoughts across both turns.
    assert s1["tokens"] == 100 + 200 + 20 + 30 + 5
    assert s1["project"] == "demo"
    assert s1["display_name"] == "first question"
    assert sessions_by_id["s2"]["project"] == "other"

    tracker = _qwen_only_tracker()
    tracker.collect(None, None)
    entries = [e for e in tracker.entries if e["source"] == "qwen_code"]
    exp_tokens, exp_cost = _entry_sums(entries)
    got_tokens, got_cost = _summary_sums(data)
    assert got_tokens == exp_tokens
    assert got_cost == pytest.approx(exp_cost)


def test_window_parity_named_periods(monkeypatch, tmp_path):
    base = tmp_path / "qwen-root"
    _write_chat(base, "proj", "s-win", [
        _rec("w1", T_OLD, usage=_usage(prompt=111, candidates=11)),
        _rec("w2", T_MONTH_START, usage=_usage(prompt=222, cached=22, candidates=22)),
        _rec("w3", T_YESTERDAY, usage=_usage(prompt=333, candidates=33)),
        _rec("w4", T_TODAY, usage=_usage(prompt=444, cached=44, candidates=44, thoughts=4)),
    ])
    _setup(monkeypatch, tmp_path)

    tracker = _qwen_only_tracker()
    tracker.collect(None, None)
    all_entries = [e for e in tracker.entries if e["source"] == "qwen_code"]

    for period in ("day", "month", "all"):
        since_ms, until_ms = sessions._period_range(period)
        data = get_sessions_data("qwen_code", period)
        expected = [
            e for e in all_entries
            if (since_ms is None or e["timestamp"] >= since_ms)
            and (until_ms is None or e["timestamp"] < until_ms)
        ]
        exp_tokens, exp_cost = _entry_sums(expected)
        got_tokens, got_cost = _summary_sums(data)
        assert got_tokens == exp_tokens, period
        assert got_cost == pytest.approx(exp_cost), period


def test_branch_fork_parent_owns_copied_turns(monkeypatch, tmp_path):
    """/branch copies the parent's records (same uuids) into the fork file with
    later timestamps: the earliest copy wins globally, so the fork's panel
    shows only the turns it newly generated. Totals stay right either way."""
    base = tmp_path / "qwen-root"
    _write_chat(base, "proj", "parent", [
        _rec("u1", T_TODAY, usage=_usage(prompt=100, candidates=10), session_id="parent"),
        _rec("u2", T_TODAY + 1000, usage=_usage(prompt=200, candidates=20), session_id="parent"),
    ])
    _write_chat(base, "proj", "fork", [
        # Same uuids, restamped later: the parent copy stays the winner.
        _rec("u1", T_TODAY + 2000, usage=_usage(prompt=100, candidates=10), session_id="fork"),
        _rec("u2", T_TODAY + 3000, usage=_usage(prompt=200, candidates=20), session_id="fork"),
        _rec("u3", T_TODAY + 4000, usage=_usage(prompt=300, candidates=30), session_id="fork"),
    ])
    _setup(monkeypatch, tmp_path)

    tracker = _qwen_only_tracker()
    tracker.collect(None, None)
    entries = [e for e in tracker.entries if e["source"] == "qwen_code"]
    assert {e["entry_id"] for e in entries} == {"qwen:u1", "qwen:u2", "qwen:u3"}

    data = get_sessions_data("qwen_code", "all")
    sessions_by_id = {s["session_id"]: s for s in data["sessions"]}
    assert set(sessions_by_id) == {"parent", "fork"}
    # Copied turns belong to the parent; the fork keeps only its own u3.
    assert sessions_by_id["parent"]["token_events"] == 2
    assert sessions_by_id["fork"]["token_events"] == 1
    exp_tokens, exp_cost = _entry_sums(entries)
    got_tokens, got_cost = _summary_sums(data)
    assert got_tokens == exp_tokens
    assert got_cost == pytest.approx(exp_cost)


def test_fork_winner_keeps_own_file_provenance(monkeypatch, tmp_path):
    """The global winner fold must carry each winner's own file metadata: two
    files whose winning records interleave. A fold that groups by whichever
    file ran last keeps every total correct and misattributes the sessions."""
    base = tmp_path / "qwen-root"
    # File A owns x1 and x3 (earlier), file B owns x2 (earlier).
    _write_chat(base, "proj", "alpha", [
        _rec("x1", T_TODAY + 1000, usage=_usage(prompt=10, candidates=1), session_id="sa"),
        _rec("x2", T_TODAY + 4000, usage=_usage(prompt=290, candidates=29), session_id="sa"),
        _rec("x3", T_TODAY + 5000, usage=_usage(prompt=30, candidates=3), session_id="sa"),
    ])
    _write_chat(base, "proj", "beta", [
        _rec("x1", T_TODAY + 6000, usage=_usage(prompt=190, candidates=19), session_id="sb"),
        _rec("x2", T_TODAY + 2000, usage=_usage(prompt=20, candidates=2), session_id="sb"),
        _rec("x3", T_TODAY + 7000, usage=_usage(prompt=390, candidates=39), session_id="sb"),
    ])
    _setup(monkeypatch, tmp_path)

    data = get_sessions_data("qwen_code", "all")
    sessions_by_id = {s["session_id"]: s for s in data["sessions"]}
    assert set(sessions_by_id) == {"sa", "sb"}
    # sa owns x1 (11 tok) + x3 (33); sb owns x2 (22). Grouping after the fold
    # by the last-seen meta would dump all three turns into one session.
    assert sessions_by_id["sa"]["token_events"] == 2
    assert sessions_by_id["sa"]["tokens"] == (10 + 1) + (30 + 3)
    assert sessions_by_id["sb"]["token_events"] == 1
    assert sessions_by_id["sb"]["tokens"] == 20 + 2


def test_equal_timestamp_fork_first_file_wins(monkeypatch, tmp_path):
    """The fold is a strict less-than (coding_tools QwenCodeParser._parse_all),
    so equal-stamp copies keep the first-encountered winner in discovery
    order: a harness that writes <= silently re-attributes the turn."""
    base = tmp_path / "qwen-root"
    # "a-*" sorts before "b-*": file order settles the tie the same way for
    # both views.
    _write_chat(base, "proj", "a-parent", [
        _rec("u1", T_TODAY, usage=_usage(prompt=100, candidates=10), session_id="sa"),
    ])
    _write_chat(base, "proj", "b-fork", [
        _rec("u1", T_TODAY, usage=_usage(prompt=500, candidates=50), session_id="sb"),
    ])
    _setup(monkeypatch, tmp_path)

    tracker = _qwen_only_tracker()
    tracker.collect(None, None)
    entries = [e for e in tracker.entries if e["source"] == "qwen_code"]
    assert len(entries) == 1
    assert entries[0]["input"] == 100  # the earlier-discovered copy won

    data = get_sessions_data("qwen_code", "all")
    sessions_by_id = {s["session_id"]: s for s in data["sessions"]}
    assert set(sessions_by_id) == {"sa"}  # the fork has no turn of its own
    assert sessions_by_id["sa"]["tokens"] == 110


def test_deleted_canonical_file_promotes_survivor(monkeypatch, tmp_path):
    base = tmp_path / "qwen-root"
    parent = _write_chat(base, "proj", "parent", [
        _rec("u1", T_TODAY, usage=_usage(prompt=100, candidates=10), session_id="parent"),
    ])
    _write_chat(base, "proj", "fork", [
        _rec("u1", T_TODAY + 1000, usage=_usage(prompt=100, candidates=10), session_id="fork"),
        _rec("u2", T_TODAY + 2000, usage=_usage(prompt=300, candidates=30), session_id="fork"),
    ])
    _setup(monkeypatch, tmp_path)

    tracker = _qwen_only_tracker()
    tracker.collect(None, None)
    before = [e for e in tracker.entries if e["source"] == "qwen_code"]
    before_total, _ = _entry_sums(before)

    os.remove(parent)
    _sig_cache.clear()
    tracker.collect(None, None)
    after = [e for e in tracker.entries if e["source"] == "qwen_code"]
    after_total, _ = _entry_sums(after)

    data = get_sessions_data("qwen_code", "all")
    assert data["summary"]["tokens"] == after_total == before_total
    assert {s["session_id"] for s in data["sessions"]} == {"fork"}


def test_skip_rules_never_produce_turns(monkeypatch, tmp_path):
    base = tmp_path / "qwen-root"
    _write_chat(base, "proj", "skipper", [
        _user_rec("no usage here", T_TODAY),
        _rec("k1", T_TODAY, usage=None),                       # no usageMetadata
        _rec("k2", T_TODAY, usage={"promptTokenCount": "x", "candidatesTokenCount": 5}),
        _rec("k3", T_TODAY, usage={"promptTokenCount": 100}),  # candidates missing
        _rec("k4", T_TODAY, usage=_usage(prompt=0, candidates=0)),  # all zero
        {**_rec("k5", T_TODAY, usage=_usage(prompt=10, candidates=1)),
         "timestamp": "not-a-date"},  # unparseable timestamp
        "torn json line",
        _rec("sys", T_TODAY, rtype="system", usage=_usage(prompt=10, candidates=1)),
    ])
    _write_chat(base, "proj", "useronly", [
        _user_rec("just chatting", T_TODAY),
    ])
    _setup(monkeypatch, tmp_path)

    tracker = _qwen_only_tracker()
    tracker.collect(None, None)
    assert tracker.entries == []

    data = get_sessions_data("qwen_code", "all")
    assert data["sessions"] == []
    assert data["summary"]["session_count"] == 0


def test_project_from_windows_cwd(monkeypatch, tmp_path):
    base = tmp_path / "qwen-root"
    _write_chat(base, "proj", "swin", [
        _rec("w1", T_TODAY, usage=_usage(prompt=10, candidates=1),
             session_id="swin", cwd="C:\\Users\\dev\\proj-win"),
    ])
    _setup(monkeypatch, tmp_path)

    data = get_sessions_data("qwen_code", "all")
    s = data["sessions"][0]
    # #56's helper splits on backslashes too: a Windows cwd read from WSL must
    # not become one giant project name.
    assert s["project"] == "proj-win"


def test_display_name_fallback_without_user_record(monkeypatch, tmp_path):
    base = tmp_path / "qwen-root"
    _write_chat(base, "proj", "sname", [
        _rec("d1", T_TODAY, usage=_usage(prompt=10, candidates=1), session_id="sname",
             cwd="/repos/named-proj"),
    ])
    _setup(monkeypatch, tmp_path)

    data = get_sessions_data("qwen_code", "all")
    s = data["sessions"][0]
    # No user record: the fallback names it after the project.
    assert s["display_name"] == "named-proj"


def test_subagent_record_becomes_turn(monkeypatch, tmp_path):
    base = tmp_path / "qwen-root"
    _write_chat(base, "proj", "sside", [
        _rec("m1", T_TODAY, usage=_usage(prompt=10, candidates=1), session_id="sside"),
        _rec("m2", T_TODAY + 1000, usage=_usage(prompt=20, candidates=2),
             session_id="sside", isSidechain=True, agentId="agent-7"),
    ])
    _setup(monkeypatch, tmp_path)

    tracker = _qwen_only_tracker()
    tracker.collect(None, None)
    entries = [e for e in tracker.entries if e["source"] == "qwen_code"]
    assert len(entries) == 2

    data = get_sessions_data("qwen_code", "all")
    s = data["sessions"][0]
    # Subagent turns roll up into the same session file's totals: Overview
    # counts them like any other assistant record, so Sessions must too.
    assert s["token_events"] == 2
    assert s["tokens"] == sum(
        e["input"] + e["output"] + e["cacheRead"] + e["cacheWrite"] + e["reasoning"]
        for e in entries
    )


def test_cache_invalidation_on_append(monkeypatch, tmp_path):
    base = tmp_path / "qwen-root"
    path = _write_chat(base, "proj", "sapp", [
        _rec("p1", T_TODAY, usage=_usage(prompt=100, candidates=10), session_id="sapp"),
    ])
    _setup(monkeypatch, tmp_path)

    data1 = get_sessions_data("qwen_code", "all")
    assert data1["summary"]["tokens"] == 110

    # Two clocks can mask the write: this box quantizes st_mtime_ns at 10 ms
    # and qwen_file_signatures sits behind a 5 s TTL, so bump the mtime
    # explicitly and clear the signature cache the way the antigravity suite
    # does. Without the clear this test would pass for the wrong reason.
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_rec("p2", T_TODAY + 5000, usage=_usage(prompt=200, candidates=20), session_id="sapp")) + "\n")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns + 20_000_000, st.st_mtime_ns + 20_000_000))
    _sig_cache.clear()

    misses_before = sessions._parse_qwen_code_session_file.cache_info().misses
    data2 = get_sessions_data("qwen_code", "all")
    # A cache that came back EMPTY after a change would be a broken cache; the
    # assertion is a miss plus an updated result.
    assert sessions._parse_qwen_code_session_file.cache_info().misses > misses_before
    assert data2["summary"]["tokens"] == 330


def test_transient_lock_degrades_and_recovers(monkeypatch, tmp_path):
    base = tmp_path / "qwen-root"
    locked = str(base / "projects" / "proj" / "chats" / "shotgun.jsonl")
    _write_chat(base, "proj", "shotgun", [
        _rec("l1", T_TODAY, usage=_usage(prompt=100, candidates=10), session_id="shotgun"),
    ])
    _write_chat(base, "proj", "survivor", [
        _rec("v1", T_TODAY, usage=_usage(prompt=200, candidates=20), session_id="survivor"),
    ])
    _setup(monkeypatch, tmp_path)

    state = {"left": 1}
    real_open = builtins.open

    def flaky_open(path, *args, **kwargs):
        if str(path) == locked and state["left"]:
            state["left"] -= 1
            raise PermissionError(13, "simulated file lock", path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)

    data1 = get_sessions_data("qwen_code", "all")
    ids = {s["session_id"] for s in data1["sessions"]}
    assert ids == {"survivor"}  # the readable session still renders...
    # ...and the incomplete aggregate was NOT memoized against the signature.
    assert sessions._load_qwen_code_sessions.cache_info().currsize == 0

    # Lock released (state consumed), no other file changed: the next request
    # must recover the session. The shipped hazard was caching the hole.
    data2 = get_sessions_data("qwen_code", "all")
    ids = {s["session_id"] for s in data2["sessions"]}
    assert ids == {"shotgun", "survivor"}
    assert sessions._load_qwen_code_sessions.cache_info().currsize == 1


def test_parity_property_randomized(monkeypatch, tmp_path):
    """The release gate: live parse vs Sessions harness over the same fixture
    corpus, store off, WorkBuddy sum formula, five seeds."""
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    for seed in range(5):
        rng = random.Random(seed)
        base = tmp_path / f"root-{seed}"
        for s in range(rng.randint(2, 5)):  # sessions
            records = []
            n = rng.randint(1, 6)
            for i in range(n):
                if rng.random() < 0.3:
                    records.append(_user_rec(f"q{i}", T_TODAY + i * 1000, session_id=f"sp{s}"))
                kind = rng.random()
                usage = _usage(prompt=rng.randint(10, 5000),
                               cached=rng.randint(0, 9), candidates=rng.randint(1, 900),
                               thoughts=rng.randint(0, 60))
                if kind < 0.15:
                    usage["promptTokenCount"] = "seven"      # non-int count
                elif kind < 0.25:
                    usage = None                             # missing usageMetadata
                elif kind < 0.32:
                    usage = _usage(prompt=0, candidates=0)   # all-zero bucket
                uuid = f"s{seed}r{i}" if rng.random() > 0.2 else None
                rec = _rec(uuid, T_TODAY + i * 1000 + s, usage=usage, session_id=f"sp{s}",
                           cwd=f"/work/proj{s}")
                if uuid is None:
                    rec.pop("uuid")
                records.append(rec)
            _write_chat(base, f"proj{s}", f"sess-{s}", records)
        # A fork of the first session: copied uuids, equal and later stamps.
        fork_records = []
        for i in range(3):
            ts = T_TODAY + i * 1000  # equal stamps: first discovery wins
            fork_records.append(_rec(f"s{seed}r{i}", ts, usage=_usage(prompt=100, candidates=5),
                                     session_id=f"fork-{seed}"))
        _write_chat(base, "proj0", f"forkfile-{seed}", fork_records)

        _setup(monkeypatch, tmp_path)

        tracker = _qwen_only_tracker()
        tracker.collect(None, None)
        all_entries = [e for e in tracker.entries if e["source"] == "qwen_code"]

        for window in (
            ("all", None, None),
            ("day", None, None),
        ):
            period, dfrom, dto = window
            since_ms, until_ms = sessions._period_range(period)
            data = get_sessions_data("qwen_code", period)
            expected = [
                e for e in all_entries
                if (since_ms is None or e["timestamp"] >= since_ms)
                and (until_ms is None or e["timestamp"] < until_ms)
            ]
            exp_tokens, exp_cost = _entry_sums(expected)
            got_tokens, got_cost = _summary_sums(data)
            assert got_tokens == exp_tokens, (seed, period)
            assert got_cost == pytest.approx(exp_cost), (seed, period)


def test_read_failure_split_between_parser_and_reader(monkeypatch, tmp_path):
    """The shared reader raises when given the caller's exception class while
    the Overview parser's per-file call keeps swallowing: QwenCodeParser
    ._parse_file passes unavailable=None and returns [] on the same lock."""
    base = tmp_path / "qwen-root"
    path = _write_chat(base, "proj", "slock", [
        _rec("o1", T_TODAY, usage=_usage(prompt=10, candidates=1)),
    ])
    _setup(monkeypatch, tmp_path)

    real_open = builtins.open

    def locked_open(p, *args, **kwargs):
        if str(p) == str(path):
            raise PermissionError(13, "simulated file lock", p)
        return real_open(p, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", locked_open)

    with pytest.raises(sessions._SessionFileUnavailable):
        coding_tools.qwen_session_file(str(path), unavailable=sessions._SessionFileUnavailable)

    parser = QwenCodeParser(PricingDatabase())
    assert parser._parse_file(str(path)) == []

    # Dependency direction: coding_tools must never import sessions.py.
    src = inspect.getsource(inspect.getmodule(coding_tools))
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module and "sessions" in node.module:
            raise AssertionError(f"coding_tools imports {node.module}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "sessions" not in alias.name, alias.name


def test_shared_reader_stays_cost_free(monkeypatch, tmp_path):
    """The module reader returns unpriced rows; each consumer prices with the
    database it holds. If a PricingDatabase were optimized into the shared
    reader, the two sides below would agree when they must not."""
    base = tmp_path / "qwen-root"
    _write_chat(base, "proj", "sprice", [
        _rec("c1", T_TODAY, usage=_usage(prompt=1000, cached=100, candidates=100)),
    ])
    _setup(monkeypatch, tmp_path)

    custom = tmp_path / "custom_pricing.json"
    custom.write_text(json.dumps({
        "version": "custom", "aliases": {},
        "models": {"qwen-model": {"input": 42.0, "output": 42.0,
                                  "cache_read": 42.0, "cache_write": 42.0}},
    }), encoding="utf-8")
    # override_path points at a file that never exists: the data-dir override
    # is a full replacement, and leaving it in place would silently replace
    # the custom rates with the test's own.
    custom_db = PricingDatabase(db_path=custom, override_path=tmp_path / "no-override.json")

    parser = QwenCodeParser(custom_db)
    entries = parser.collect(None, None)
    assert entries[0]["cost"] == pytest.approx(
        custom_db.get_cost("qwen-model", 900, 100, 100, 0))

    data = get_sessions_data("qwen_code", "all")
    sessions_cost = data["summary"]["cost"]
    assert sessions_cost == pytest.approx(
        PricingDatabase().get_cost("qwen-model", 900, 100, 100, 0))
    assert sessions_cost != pytest.approx(entries[0]["cost"])


def test_no_root_empty_view(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    _setup(monkeypatch, tmp_path)

    data = get_sessions_data("qwen_code", "all")
    assert data["sessions"] == []
    assert data["summary"]["session_count"] == 0


def test_registration_smoke():
    assert "qwen_code" in SESSION_TOOLS
    assert "qwen_code" in sessions.TOOL_LABELS
    assert _raw_sessions_for_tool("qwen_code") == {}


def test_frontend_session_registry_includes_qwen_code():
    index = Path(sessions.__file__).parent / "static" / "index.html"
    source = index.read_text(encoding="utf-8")
    assert "'workbuddy', 'qoder', 'qwen_code'" in source
    assert "qoder: null, qwen_code: null, openclaw: null, qoder_cli: null, combined: null" in source
    assert 'updateSessionPanel("qwen_code", lastSessionsResponses.qwen_code);' in source
    assert 'initSortHeaders("qwen_code", renderSessionsTab);' in source
    assert "qwen_code: { ...DEFAULT_SORT }," in source
    assert "qwenCodeSessions: 'Qwen Code Sessions'," in source
    assert "qwenCodeSessions: 'Qwen Code \u4f1a\u8bdd'," in source
    assert 'id="qwen_codeSessionsTable"' in source
    assert 'data-panel-details="qwen_code"' in source
    brand = source.split("const TOOL_BRAND_META = Object.freeze({", 1)[1].split("});", 1)[0]
    assert "qwen_code:" in brand


def test_reload_pricing_db_clears_qwen_caches(monkeypatch, tmp_path):
    """reload_pricing_db() lists every pricing-dependent session cache; this
    harness must be on that list. Priced turns live in the per-file parser
    cache, so the honest assertion is: after the clear the same signature
    misses and reparses -- never that the cache came back empty."""
    base = tmp_path / "qwen-root"
    _write_chat(base, "proj", "sreprice", [
        _rec("r1", T_TODAY, usage=_usage(prompt=100, candidates=10)),
    ])
    _setup(monkeypatch, tmp_path)

    data1 = get_sessions_data("qwen_code", "all")
    assert data1["summary"]["cost"] == pytest.approx(
        PricingDatabase().get_cost("qwen-model", 100, 10, 0, 0))

    reload_pricing_db()
    misses_before = sessions._parse_qwen_code_session_file.cache_info().misses
    get_sessions_data("qwen_code", "all")
    assert sessions._parse_qwen_code_session_file.cache_info().misses > misses_before
