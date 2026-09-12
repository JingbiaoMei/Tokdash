"""Tests for Qoder CLI as a session source (sessions.py).

Qoder CLI bills one request across two streams: the transcript
(projects/<proj>/<session>.jsonl, carries credits + request_id) and the
segment log (logs/sessions/<proj>/<session>/segments/*.jsonl, carries the
token buckets). The harness must reproduce the parser's candidate builders,
the global first-write-wins dedupe per candidate type, the segment-wins-token
merge, and the credits-vs-pricing billing split, so windowed session sums
match QoderCliParser.collect() exactly (fixture-driven parity: this machine
has no transcript files at all, so the transcript side is fixture-only).
"""
from __future__ import annotations

import builtins
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tokdash.sessions as sessions
from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    _raw_sessions_for_tool,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources import coding_tools
from tokdash.sources.coding_tools import BaseParser, QoderCliParser, _sig_cache

# One priced model so billing drift is visible; "qmodel-unpriced" stays absent
# from the pricing file to pin the zero-cost path.
RATES = {
    "qmodel": {"input": 2.0, "output": 4.0, "cache_read": 0.2, "cache_write": 2.0},
}

_LOCAL_TZ = datetime.now().astimezone().tzinfo
_TODAY_LOCAL = datetime.now().astimezone().replace(
    hour=0, minute=0, second=0, microsecond=0
)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso(ts_ms: int) -> str:
    return (
        datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


# Anchored so the named day/month windows behave in any timezone.
T_TODAY = _ms(_TODAY_LOCAL.replace(hour=12))
T_TODAY2 = _ms(_TODAY_LOCAL.replace(hour=13))
T_YESTERDAY = _ms(_TODAY_LOCAL - timedelta(hours=12))
T_OLD = _ms(_TODAY_LOCAL - timedelta(days=200, hours=12))

# The _discovered_files glob is projects/*/[0-9a-f-]*.jsonl, so transcript
# session ids (file stems) must start with hex characters, like the real ones.
SID_A = "aa11bb22-cc33-44dd-88ee-00ff11223344"
SID_B = "deadbeef-0000-1111-2222-333344445555"
SID_C = "cafe0001-9999-8888-7777-666655554444"
PROJ_SEG = "-mnt-h-work-foo-bar"


def _transcript_rec(
    rid, ts_ms, model="qmodel", credits=None, inp=0, outp=0, cr=0, cw=0,
    ratio=None, uuid=None,
):
    usage = {"request_id": rid}
    if credits is not None:
        usage["credits"] = credits
    usage.update({
        "input_tokens": inp,
        "output_tokens": outp,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
    })
    if ratio is not None:
        usage["context_usage_ratio"] = ratio
    return {
        "uuid": uuid or f"u-{rid}",
        "timestamp": _iso(ts_ms),
        "message": {"model": model, "usage": usage},
    }


def _segment_rec(rid, ts_ms, model="qmodel", inp=0, outp=0, cr=0, cw=0):
    return {
        "type": "model.response.completed",
        "request_id": rid,
        "ts": _iso(ts_ms),
        "data": {
            "model": model,
            "input_tokens": inp,
            "output_tokens": outp,
            "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cw,
        },
    }


def _write_jsonl(path: Path, recs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(r if isinstance(r, str) else json.dumps(r) for r in recs) + "\n",
        encoding="utf-8",
    )
    return path


def _transcript(root: Path, project: str, session_id: str, recs) -> Path:
    return _write_jsonl(root / "projects" / project / f"{session_id}.jsonl", recs)


def _segment(root: Path, project: str, session_id: str, recs, run="run.jsonl") -> Path:
    return _write_jsonl(
        root / "logs" / "sessions" / project / session_id / "segments" / run, recs
    )


# --- isolation ---------------------------------------------------------------
#
# qoder_cli_roots() (clientpaths.py) is a UNION, not a switch:
# QODER_CLI_HOME / QODER_CONFIG_DIR are prepended and both default homes
# (~/.qoder, ~/.qoder-cn) are appended anyway. Setting the env to tmp_path
# therefore still scans this machine's real ~/.qoder, so the autouse fixture
# patches the clientpaths function itself — what both _qoder_cli_sessions and
# QoderCliParser.__init__ read. Tests that need roots append them via _setup();
# with none registered every test sees an empty corpus, never the real one.
_ROOTS: list = []


def _clear_caches():
    # This harness has more caches than any other: per-file parser, aggregate,
    # the shared signature TTL cache and the parser's own entry cache. An
    # under-cleared fixture produces a pass that says nothing.
    sessions._parse_qoder_cli_session_file.cache_clear()
    sessions._load_qoder_cli_sessions.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    _ROOTS.clear()
    monkeypatch.setattr(clientpaths, "qoder_cli_roots", lambda: list(_ROOTS))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    monkeypatch.delenv("QODER_USD_PER_CREDIT", raising=False)
    monkeypatch.delenv("QODER_CLI_CONTEXT_WINDOW", raising=False)
    _clear_caches()
    reload_pricing_db()
    yield
    _ROOTS.clear()
    _clear_caches()
    reload_pricing_db()


def _setup(monkeypatch, tmp_path, *roots) -> Path:
    """Register fixture roots and pin pricing; call after writing files.

    The parser MUST be constructed after this (via _parser()) so its
    __init__ snapshots the patched roots, not the real homes.
    """
    _ROOTS[:] = list(roots) or [tmp_path / "qoder-root"]
    override = PricingDatabase().override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps({"version": "test", "aliases": {}, "models": RATES}),
        encoding="utf-8",
    )
    reload_pricing_db()
    return _ROOTS[0]


def _parser() -> QoderCliParser:
    return QoderCliParser(sessions._PRICING_DB)


def _entry_sums(entries):
    tokens = sum(
        e["input"] + e["output"] + e["cacheRead"] + e["cacheWrite"]
        + e.get("reasoning", 0)
        for e in entries
    )
    cost = sum(e["cost"] for e in entries)
    return tokens, cost


def _summary_sums(data):
    return data["summary"]["tokens"], data["summary"]["cost"]


def _live_entries(since=None, until=None):
    return _parser().collect(since, until)


def _window_entries(all_entries, period):
    since_ms, until_ms = sessions._period_range(period)
    return [
        e for e in all_entries
        if (since_ms is None or e["timestamp"] >= since_ms)
        and (until_ms is None or e["timestamp"] < until_ms)
    ]


# --- tests ------------------------------------------------------------------


def test_registered():
    assert "qoder_cli" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["qoder_cli"] == "Qoder CLI"
    # Live-parse only: the persistent-store gate stays untouched.
    assert "qoder_cli" not in sessions._SESSION_FILE_PARSER_VERSIONS


def test_transcript_segment_merge_one_rid(monkeypatch, tmp_path):
    """Segment tokens win, transcript credits win, one turn, totals = collect()."""
    root = tmp_path / "qoder-root"
    _transcript(root, "pA", SID_A, [
        _transcript_rec("r1", T_TODAY, credits=7, outp=99),
    ])
    _segment(root, PROJ_SEG, SID_A, [
        _segment_rec("r1", T_TODAY, inp=100, outp=5, cr=7, cw=3),
    ])
    _setup(monkeypatch, tmp_path)

    data = get_sessions_data("qoder_cli", "all")
    assert len(data["sessions"]) == 1
    sess = data["sessions"][0]
    assert sess["session_id"] == SID_A
    turns = get_session_detail("qoder_cli", SID_A)["turns"]
    assert len(turns) == 1
    turn = turns[0]
    # Segment tokens are the truth; tokens_in folds cacheWrite (display map).
    assert turn["tokens_in"] == 103
    assert turn["tokens_cache"] == 7
    assert turn["tokens_out"] == 5
    assert turn["tokens"] == 115
    # Credits are authoritative: 7 credits * $0.01, never repriced.
    assert turn["cost"] == pytest.approx(0.07)

    entries = _live_entries()
    assert len(entries) == 1
    exp_tokens, exp_cost = _entry_sums(entries)
    got_tokens, got_cost = _summary_sums(data)
    assert (got_tokens, got_cost) == (exp_tokens, pytest.approx(exp_cost))


def test_duplicate_rid_across_roots(monkeypatch, tmp_path):
    root1 = tmp_path / "root1"
    root2 = tmp_path / "root2"
    _transcript(root1, "pA", SID_A, [
        _transcript_rec("dup", T_TODAY, credits=2, inp=1000, outp=20),
    ])
    _transcript(root2, "pB", SID_A, [
        _transcript_rec("dup", T_TODAY, credits=9, inp=5, outp=5),
    ])
    _setup(monkeypatch, tmp_path, root1, root2)

    data = get_sessions_data("qoder_cli", "all")
    assert len(data["sessions"]) == 1
    assert len(get_session_detail("qoder_cli", data["sessions"][0]["session_id"])["turns"]) == 1
    entries = _live_entries()
    assert len(entries) == 1
    exp_tokens, exp_cost = _entry_sums(entries)
    got_tokens, got_cost = _summary_sums(data)
    assert got_tokens == exp_tokens
    assert got_cost == pytest.approx(exp_cost)


def test_transcript_only_and_segment_only(monkeypatch, tmp_path):
    root = tmp_path / "qoder-root"
    _transcript(root, "pT", SID_A, [
        _transcript_rec("t1", T_TODAY, credits=1, inp=50, outp=6),
    ])
    _segment(root, PROJ_SEG, SID_B, [
        _segment_rec("s1", T_TODAY, inp=70, outp=8, cr=4, cw=1),
    ])
    _setup(monkeypatch, tmp_path)

    data = get_sessions_data("qoder_cli", "all")
    assert {s["session_id"] for s in data["sessions"]} == {SID_A, SID_B}
    ta = get_session_detail("qoder_cli", SID_A)["turns"][0]
    assert ta["tokens_in"] == 50 and ta["tokens_out"] == 6
    tb = get_session_detail("qoder_cli", SID_B)["turns"][0]
    assert tb["tokens_in"] == 71 and tb["tokens_cache"] == 4 and tb["tokens_out"] == 8

    entries = {e["entry_id"]: e for e in _live_entries()}
    assert set(entries) == {"qoder-cli:t1", "qoder-cli:s1"}
    exp_tokens, _ = _entry_sums(entries.values())
    got_tokens, _ = _summary_sums(data)
    assert got_tokens == exp_tokens


def test_credit_row_survives_pricing_edit(monkeypatch, tmp_path):
    """Credit-priced cost is fixed; a DB-priced row reprices on the same edit."""
    root = tmp_path / "qoder-root"
    _transcript(root, "pA", SID_A, [
        _transcript_rec("cr1", T_TODAY, credits=7, inp=10, outp=2),
    ])
    _segment(root, PROJ_SEG, SID_B, [
        _segment_rec("pr1", T_TODAY, inp=100, outp=50),
    ])
    _setup(monkeypatch, tmp_path)

    data1 = get_sessions_data("qoder_cli", "all")
    assert {s["session_id"] for s in data1["sessions"]} == {SID_A, SID_B}
    credit_cost1 = get_session_detail("qoder_cli", SID_A)["turns"][0]["cost"]
    priced_cost1 = get_session_detail("qoder_cli", SID_B)["turns"][0]["cost"]
    assert credit_cost1 == pytest.approx(0.07)

    edited = {k: dict(v) for k, v in RATES.items()}
    edited["qmodel"]["output"] = 8.0
    override = PricingDatabase().override_path()
    override.write_text(
        json.dumps({"version": "test", "aliases": {}, "models": edited}),
        encoding="utf-8",
    )
    reload_pricing_db()
    data2 = get_sessions_data("qoder_cli", "all")
    assert get_session_detail("qoder_cli", SID_A)["turns"][0]["cost"] == pytest.approx(credit_cost1)
    assert get_session_detail("qoder_cli", SID_B)["turns"][0]["cost"] > priced_cost1
    # and nothing was left out of the rebuild (a cleared cache that came back
    # EMPTY would satisfy both cost asserts above and still be broken)
    assert sessions._load_qoder_cli_sessions.cache_info().currsize >= 1
    assert len(data2["sessions"]) == 2


def test_reload_pricing_db_clears_qoder_cli_caches(monkeypatch, tmp_path):
    """reload_pricing_db() must clear BOTH qoder_cli caches. Reloaded without
    touching the file, so pricing_sig is unchanged and a cleared cache is the
    ONLY way the next request misses; the assertion is miss + refilled result,
    never an empty cache as the goal."""
    root = tmp_path / "qoder-root"
    _segment(root, PROJ_SEG, SID_A, [
        _segment_rec("rr1", T_TODAY, inp=100, outp=10),
    ])
    _setup(monkeypatch, tmp_path)

    get_sessions_data("qoder_cli", "all")  # populate
    reload_pricing_db()
    # cache_clear() resets lru stats, so the first post-reload request
    # missing (from zero) proves the clear ran: same pricing_sig means a
    # stale hit was the only other possibility.
    data = get_sessions_data("qoder_cli", "all")
    assert sessions._load_qoder_cli_sessions.cache_info().misses == 1
    assert sessions._parse_qoder_cli_session_file.cache_info().misses == 1
    assert data["summary"]["tokens"] == 110  # miss + full result, not an empty cache


def test_runtime_config_in_cache_keys(monkeypatch, tmp_path):
    """Rate moves merge time; window moves the PER-FILE cache, not just the aggregate."""
    root = tmp_path / "qoder-root"
    # ratio recovery: zero input recovered at the window, transcript-only rid.
    # model=auto is load-bearing: recovery applies to auto only when the
    # window override is unset.
    _transcript(root, "pA", SID_A, [
        _transcript_rec("w1", T_TODAY, model="auto", credits=3, ratio=0.5, outp=4),
    ])
    _setup(monkeypatch, tmp_path)

    data1 = get_sessions_data("qoder_cli", "all")
    tok1 = data1["summary"]["tokens"]
    # unset window -> auto recovery at 180k: int(round(0.5*180000)) + 4
    assert tok1 == 90004
    assert _entry_sums(_live_entries())[0] == tok1

    # Window change: BOTH views move and the per-file candidate cache is
    # invalidated, not merely the aggregate. cache_info().misses is the only
    # way to see whether the window reached the per-file key.
    misses_before = sessions._parse_qoder_cli_session_file.cache_info().misses
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", "100000")
    data2 = get_sessions_data("qoder_cli", "all")
    assert sessions._parse_qoder_cli_session_file.cache_info().misses > misses_before
    assert data2["summary"]["tokens"] == 50004  # int(round(0.5*100000)) + 4
    assert _entry_sums(_live_entries())[0] == 50004

    # Rate change: credit cost moves in both views at merge time.
    monkeypatch.delenv("QODER_CLI_CONTEXT_WINDOW")
    monkeypatch.setenv("QODER_USD_PER_CREDIT", "0.02")
    cost1 = _summary_sums(get_sessions_data("qoder_cli", "all"))[1]
    assert _entry_sums(_live_entries())[1] == pytest.approx(cost1)


def test_session_id_from_path_shapes(monkeypatch, tmp_path):
    root = tmp_path / "qoder-root"
    _transcript(root, "pT", SID_A, [
        _transcript_rec("tt1", T_TODAY, inp=10, outp=1),
    ])
    # segment id is the directory above segments/, not the run stamp
    _segment(root, PROJ_SEG, SID_C, [
        _segment_rec("ss1", T_TODAY, inp=20, outp=2),
    ], run="2026-08-21T13-10-56-043+01-00-p1999852.jsonl")
    # the GUI copy subdir must stay out of discovery: usage-less duplicates
    _write_jsonl(root / "projects" / "pT" / "transcript" / "deadbeef02.jsonl", [
        _transcript_rec("gui-clone", T_TODAY, credits=1000, inp=10**9, outp=10**9),
    ])
    _setup(monkeypatch, tmp_path)

    data = get_sessions_data("qoder_cli", "all")
    assert {s["session_id"] for s in data["sessions"]} == {SID_A, SID_C}
    assert len(_live_entries()) == 2


def test_project_label_verbatim(monkeypatch, tmp_path):
    """The sanitized segment project ships verbatim; two same-named sessions group."""
    root = tmp_path / "qoder-root"
    _segment(root, PROJ_SEG, SID_A, [
        _segment_rec("p1", T_TODAY, inp=10, outp=1),
    ])
    _segment(root, PROJ_SEG, SID_B, [
        _segment_rec("p2", T_TODAY, inp=20, outp=2),
    ])
    _setup(monkeypatch, tmp_path)

    data = get_sessions_data("qoder_cli", "all")
    by_id = {s["session_id"]: s for s in data["sessions"]}
    assert set(by_id) == {SID_A, SID_B}
    # no guessed inverse: the label is the sanitized dir, never a rebuilt path
    for sess in by_id.values():
        assert sess["project"] == PROJ_SEG
        assert "/" not in sess["project"].replace("-mnt-h-work-foo-bar", "")


def test_zero_bucket_candidate_absent(monkeypatch, tmp_path):
    root = tmp_path / "qoder-root"
    _transcript(root, "pA", SID_A, [
        _transcript_rec("z1", T_TODAY, credits=5),  # nothing attributable
        _transcript_rec("z2", T_TODAY, inp=3, outp=1),
    ])
    _segment(root, PROJ_SEG, SID_B, [
        _segment_rec("z3", T_TODAY),  # all-zero event
        _segment_rec("z4", T_TODAY, inp=4),
        '{"type": "model.response.completed", "request_id": "to',  # torn final line
    ])
    _setup(monkeypatch, tmp_path)

    data = get_sessions_data("qoder_cli", "all")
    turns = [
        t for s in data["sessions"]
        for t in get_session_detail("qoder_cli", s["session_id"])["turns"]
    ]
    assert len(turns) == 2
    assert len(_live_entries()) == 2
    assert data["summary"]["tokens"] == _entry_sums(_live_entries())[0]


def test_transient_read_partial_view(monkeypatch, tmp_path):
    root = tmp_path / "qoder-root"
    seg_path = _segment(root, PROJ_SEG, SID_B, [
        _segment_rec("l1", T_TODAY, inp=100, outp=10),
    ])
    _segment(root, PROJ_SEG, SID_C, [
        _segment_rec("v1", T_TODAY, inp=200, outp=20),
    ])
    _setup(monkeypatch, tmp_path)

    state = {"left": 1}
    real_open = builtins.open

    def flaky_open(path, *args, **kwargs):
        if str(path) == str(seg_path) and state["left"]:
            state["left"] -= 1
            raise PermissionError(13, "simulated file lock", path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)

    data1 = get_sessions_data("qoder_cli", "all")
    ids = {s["session_id"] for s in data1["sessions"]}
    assert ids == {SID_C}  # the readable session still renders...
    # ...and the partial aggregate was NOT memoized against the signature.
    assert sessions._load_qoder_cli_sessions.cache_info().currsize == 0

    # Lock released, nothing else changed: the next request recovers.
    data2 = get_sessions_data("qoder_cli", "all")
    ids = {s["session_id"] for s in data2["sessions"]}
    assert ids == {SID_B, SID_C}
    assert sessions._load_qoder_cli_sessions.cache_info().currsize == 1


def test_cache_invalidation_on_append(monkeypatch, tmp_path):
    root = tmp_path / "qoder-root"
    path = _segment(root, PROJ_SEG, SID_A, [
        _segment_rec("a1", T_TODAY, inp=100, outp=10),
    ])
    _setup(monkeypatch, tmp_path)

    data1 = get_sessions_data("qoder_cli", "all")
    assert data1["summary"]["tokens"] == 110

    # WSL quantizes st_mtime_ns at 10 ms: bump the mtime explicitly, or the
    # signature says "unchanged" and this test passes for the wrong reason.
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_segment_rec("a2", T_TODAY + 5000, inp=50, outp=5)) + "\n")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns + 20_000_000, st.st_mtime_ns + 20_000_000))
    _sig_cache.clear()

    misses_before = sessions._parse_qoder_cli_session_file.cache_info().misses
    data2 = get_sessions_data("qoder_cli", "all")
    assert sessions._parse_qoder_cli_session_file.cache_info().misses > misses_before
    assert data2["summary"]["tokens"] == 165  # 110 + (50 + 5)


def test_no_roots_empty_view(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, tmp_path / "does-not-exist")
    data = get_sessions_data("qoder_cli", "all")
    assert data["sessions"] == []
    assert data["summary"]["tokens"] == 0


def test_loader_without_raising_attribute(monkeypatch, tmp_path):
    """Regression for the *extra forwarding: a replaced plain callable (no
    .raising) must still get the five forwarded arguments and parse."""
    root = tmp_path / "qoder-root"
    _segment(root, PROJ_SEG, SID_A, [
        _segment_rec("f1", T_TODAY, inp=10, outp=2),
    ])
    _setup(monkeypatch, tmp_path)

    calls = []

    def plain_stub(path_str, mtime_ns, size, pricing_sig, window):
        calls.append((path_str, mtime_ns, size, pricing_sig, window))
        return coding_tools.qoder_cli_file_candidates(Path(path_str), window)

    # the autouse teardown clears this attribute after the test, while the
    # stub is still installed; give it the one hook the clear needs
    plain_stub.cache_clear = lambda: None
    monkeypatch.setattr(sessions, "_parse_qoder_cli_session_file", plain_stub)
    data = get_sessions_data("qoder_cli", "all")  # must not AttributeError
    assert data["summary"]["tokens"] == 12
    assert calls and len(calls[0]) == 5


def test_merged_entry_carries_source_name(monkeypatch, tmp_path):
    db = sessions._PRICING_DB
    tcand = {
        "has_credits": True, "credits": 3.0,
        "input": 10, "output": 2, "cacheRead": 0, "cacheWrite": 0,
        "model": "qmodel", "ts": T_TODAY,
    }
    entry = coding_tools.qoder_cli_merged_entry(db, "qoder_cli", "m1", tcand, None, 0.01)
    assert entry["source"] == "qoder_cli"
    entry2 = coding_tools.qoder_cli_merged_entry(db, "other", "m1", tcand, None, 0.01)
    assert entry2["source"] == "other"
    # the harness view (via the parser) says qoder_cli too
    _setup(monkeypatch, tmp_path)
    assert {e["source"] for e in _live_entries()} <= {"qoder_cli"}


def test_display_name_is_short_id_not_project(monkeypatch, tmp_path):
    """The conventional fallback would name every session after its sanitized
    project dir; this harness deliberately passes an EMPTY project."""
    root = tmp_path / "qoder-root"
    _segment(root, PROJ_SEG, SID_A, [
        _segment_rec("d1", T_TODAY, inp=10, outp=1),
    ])
    _segment(root, PROJ_SEG, SID_B, [
        _segment_rec("d2", T_TODAY, inp=20, outp=2),
    ])
    _setup(monkeypatch, tmp_path)

    data = get_sessions_data("qoder_cli", "all")
    by_id = {s["session_id"]: s for s in data["sessions"]}
    names = {by_id[SID_A]["display_name"], by_id[SID_B]["display_name"]}
    assert names == {SID_A[:8], SID_B[:8]}
    assert PROJ_SEG not in names
    # the project keeps its own column
    assert by_id[SID_A]["project"] == PROJ_SEG


def test_runtime_config_one_implementation(monkeypatch, tmp_path, caplog):
    """Invalid overrides behave and sign like unset, in both views, and the
    warning is pinned, not counted: >=1 and <=2 per cold collect."""
    root = tmp_path / "qoder-root"
    _transcript(root, "pA", SID_A, [
        _transcript_rec("c1", T_TODAY, credits=4, inp=10, outp=2),
    ])
    _segment(root, PROJ_SEG, SID_B, [
        _segment_rec("c2", T_TODAY, inp=30, outp=3),
    ])
    _setup(monkeypatch, tmp_path)

    def fingerprint():
        raws = _raw_sessions_for_tool("qoder_cli")
        return sorted(
            (t["tokens"], round(t["cost"], 9))
            for raw in raws.values() for t in raw["turns"]
        )

    base_sig = coding_tools.qoder_cli_runtime_signature()
    base = fingerprint()
    assert base_sig == (None, None)

    for bad in ("0", "-1", "abc"):
        monkeypatch.setenv("QODER_USD_PER_CREDIT", bad)
        assert coding_tools.qoder_cli_runtime_signature() == base_sig
        assert fingerprint() == base

    # Pin the warning: BaseParser.collect reads the env twice on purpose
    # (cache signature, then parse), so twice on a cold collect is CORRECT and
    # an exact-once assertion would fail against right code.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tokdash.sources.coding_tools"):
        BaseParser._entry_cache.clear()
        _parser().collect(None, None)
    warns = [r for r in caplog.records if "invalid QODER_USD_PER_CREDIT" in r.getMessage()]
    assert 1 <= len(warns) <= 2


def test_parity_property_randomized(monkeypatch, tmp_path):
    """The release gate: live parser vs Sessions harness over the same fixture
    corpus, store off, WorkBuddy sum formula, five seeds."""
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    for seed in range(5):
        rng = random.Random(seed)
        root1 = tmp_path / f"root1-{seed}"
        root2 = tmp_path / f"root2-{seed}"
        seen_rids = []
        for s in range(rng.randint(2, 4)):  # sessions
            root = root1 if s % 2 == 0 else root2
            sid = f"{seed}{s}abcdef0123456789"[:12]
            t_recs, s_recs = [], []
            for i in range(rng.randint(1, 5)):
                rid = f"r{seed}-{s}-{i}"
                seen_rids.append((root, sid, rid))
                ts = T_TODAY + i * 1000 + s
                shape = rng.random()
                credits = rng.randint(1, 20) if shape < 0.5 else None
                if shape < 0.7:  # transcript side exists
                    t_recs.append(_transcript_rec(
                        rid, ts,
                        credits=credits,
                        inp=rng.randint(0, 3000) if shape > 0.2 else 0,
                        outp=rng.randint(0, 400),
                        cr=rng.randint(0, 9) if shape > 0.35 else 0,
                        cw=rng.randint(0, 9) if shape > 0.35 else 0,
                        ratio=round(rng.uniform(0.01, 0.9), 3) if shape >= 0.6 else None,
                    ))
                if shape >= 0.3:  # segment side exists
                    s_recs.append(_segment_rec(
                        rid, ts,
                        inp=rng.randint(0, 3000), outp=rng.randint(0, 400),
                        cr=rng.randint(0, 500), cw=rng.randint(0, 500),
                    ))
                if rng.random() < 0.15:  # nothing attributable anywhere
                    t_recs.append(_transcript_rec(f"zz{seed}-{s}-{i}", ts))
                    s_recs.append(_segment_rec(f"zz{seed}-{s}-{i}", ts))
            if t_recs:
                _transcript(root, f"p{s}", sid, t_recs)
            if s_recs:
                _segment(root, f"-p-{s}", sid, s_recs)
        # a true duplicate: last session's first rid replayed in the OTHER root
        if seen_rids:
            other = root2 if root1 in {r for r, _, _ in seen_rids} and seed % 2 == 0 else root1
            _, _, rid = seen_rids[0]
            _transcript(other, "pdup", SID_C, [
                _transcript_rec(rid, T_TODAY, credits=99, inp=999999, outp=999999),
            ])

        _setup(monkeypatch, tmp_path, root1, root2)

        all_entries = _live_entries()
        for period in ("all", "day", "month"):
            expected = _window_entries(all_entries, period)
            exp_tokens, exp_cost = _entry_sums(expected)
            data = get_sessions_data("qoder_cli", period)
            got_tokens, got_cost = _summary_sums(data)
            assert got_tokens == exp_tokens, (seed, period)
            assert got_cost == pytest.approx(exp_cost), (seed, period)


def test_no_500_on_transient_read(monkeypatch, tmp_path):
    """_SessionFileUnavailable must degrade, never escape to a 500."""
    root = tmp_path / "qoder-root"
    path = _segment(root, PROJ_SEG, SID_A, [
        _segment_rec("g1", T_TODAY, inp=10, outp=1),
    ])
    _setup(monkeypatch, tmp_path)
    state = {"on": True}
    real_open = builtins.open

    def always_locked(p, *a, **k):
        if str(p) == str(path) and state["on"]:
            raise OSError(11, "temporarily unavailable", p)
        return real_open(p, *a, **k)

    monkeypatch.setattr(builtins, "open", always_locked)
    assert _raw_sessions_for_tool("qoder_cli") == {}
    state["on"] = False
    assert list(_raw_sessions_for_tool("qoder_cli")) == [SID_A]


def test_frontend_session_registry_includes_qoder_cli():
    html = (Path(__file__).resolve().parent.parent / "src" / "tokdash" / "static" / "index.html").read_text(encoding="utf-8")
    assert "'qoder_cli'" in html
    # the JS looks the tbody up as `${panel}SessionsTable`, raw key
    assert 'id="qoder_cliSessionsTable"' in html
    assert html.count("qoderCliSessions:") == 6  # six i18n dicts
    # the panel tail pins every tool key in load order
    assert "qoder_cli: null, combined: null" in html
    # icon + label for qoder_cli already existed before this harness: verify
    # they were not duplicated (a repeated object key parses and silently
    # keeps the last value, which is how a wrong label ships).
    assert html.count("qoderCliSessions:") == 6
