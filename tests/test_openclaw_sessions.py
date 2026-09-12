"""Tests for OpenClaw as a session source (sessions.py).

OpenClaw's Sessions harness consumes the SAME corpus snapshot Overview parses
(``openclaw.collect_session_corpus`` -> ``_ENTRY_CACHE``): the glob, the
transcript filter, the source-global message-id dedupe and the row set are
shared, not mirrored (the 2026-08-25 spec's per-file re-parse drifted, which is
why parity is scoped live-vs-live and stored-vs-Sessions separately, with the
live-vs-live sum equality as the release gate).

Corpus isolation is load-bearing: ``openclaw_home()`` re-reads
``$OPENCLAW_HOME`` on every call, so the autouse env pin below is what keeps
the real ~/.openclaw (19 GB, 6.32 s cold) out of the suite. A test that
forgets it passes while globbing the machine.
"""
from __future__ import annotations

import ast
import builtins
import json
import logging
import os
import random
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tokdash.sessions as sessions
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    TOOL_LABELS,
    _raw_sessions_for_tool,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources import openclaw
from tokdash.usage_store import UsageEntryStore

RATES = {
    "p1/oc-model": {"input": 2.0, "output": 4.0, "cache_read": 0.2, "cache_write": 2.0},
}

_LOCAL_TZ = datetime.now().astimezone().tzinfo
_TODAY_LOCAL = datetime.now().astimezone().replace(
    hour=0, minute=0, second=0, microsecond=0
)

# Today's local noon is always inside the day/month/all windows, tomorrow's
# local midnight is always the (exclusive) until of the day window.
T_TODAY = int(_TODAY_LOCAL.replace(hour=12).timestamp() * 1000)
T_TODAY2 = int(_TODAY_LOCAL.replace(hour=13).timestamp() * 1000)
T_YESTERDAY = int((_TODAY_LOCAL - timedelta(hours=12)).timestamp() * 1000)
T_TOMORROW_MIDNIGHT = int((_TODAY_LOCAL + timedelta(days=1)).timestamp() * 1000)


def _dt_from_ms(ms: int) -> datetime:
    # _period_range("all") starts a century pre-epoch, and Windows
    # datetime.fromtimestamp raises OSError [Errno 22] on negative values
    # where Linux accepts them. Epoch arithmetic is the same datetime on
    # every platform.
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)


def _clear_caches() -> None:
    openclaw._ENTRY_CACHE.clear()
    sessions._load_openclaw_sessions.cache_clear()
    from tokdash import api

    api._clear_cache()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # NEVER let a test read the real ~/.openclaw: one env pin per test is
    # sufficient because openclaw_home() reads it on each call (unlike the
    # import-time _SIG_TTL in the qwen suite). TOKDASH_DATA_DIR isolates the
    # persistent usage store, and reload_pricing_db() re-anchors the pricing
    # singleton on both sides of every test.
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data"))
    reload_pricing_db()
    _clear_caches()
    yield
    _clear_caches()
    reload_pricing_db()


def _pricing(monkeypatch, tmp_path, models=None) -> Path:
    override = PricingDatabase().override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps({"version": "test", "aliases": {}, "models": models or RATES}),
        encoding="utf-8",
    )
    reload_pricing_db()
    return tmp_path


def _sessions_dir(tmp_path: Path, agent: str) -> Path:
    d = tmp_path / "agents" / agent / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_jsonl(path: Path, rows: list) -> Path:
    path.write_text(
        "\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    return path


def _hdr(sid: str, cwd: str = "/home/howard/.openclaw/workspace") -> dict:
    return {"type": "session", "id": sid, "cwd": cwd, "timestamp": 1, "version": "1"}


def _user(mid: str, ts: int, text: str) -> dict:
    return {
        "id": mid,
        "type": "message",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": text,
            "timestamp": ts,
        },
    }


def _asst(
    mid, ts, inp=0, cw=0, cr=0, out=0,
    provider="p1", model="oc-model", cost=None, usage_override=None,
) -> dict:
    usage = usage_override if usage_override is not None else {
        "input": inp, "cacheWrite": cw, "cacheRead": cr, "output": out,
    }
    if usage is not None and cost is not None:
        usage = dict(usage, cost={"total": cost})
    return {
        "id": mid,
        "type": "message",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "provider": provider,
            "model": model,
            "usage": usage,
            "timestamp": ts,
        },
    }


def _dirs(tmp_path: Path) -> list:
    return sorted(str(p) for p in (tmp_path / "agents").glob("*/sessions"))


def _corpus_for_tests(rows, signature=()):
    """Shared fixture shape (the spec's session_corpus_for_tests helper): meta
    is synthesized from the rows' own ``file`` values, so a fixture that
    forgets the key fails loudly instead of yielding a sessionless panel."""
    meta = {}
    for row in rows:
        meta.setdefault(
            row["file"],
            {"session_id": row["file"], "cwd": "", "agent": "", "preview": ""},
        )
    return openclaw.SessionCorpus(
        rows=list(rows),
        files=list(meta),
        signature=signature,
        meta=meta,
    )


def _raw_row(path, mid, ts, inp=10, cw=0, cr=0, out=2, model="p1/oc-model", payload=0.0):
    return {
        "msg_dt": datetime.fromtimestamp(ts / 1000, timezone.utc),
        "model": model,
        "input_raw": inp,
        "cache_write": cw,
        "output": out,
        "cache_read": cr,
        "payload_cost": payload,
        "entry_id": f"openclaw:{mid}",
        "file": str(path),
    }


def _entry_sums(entries):
    tokens = sum(
        e["input"] + e["output"] + e["cacheRead"] + e["cacheWrite"] + e["reasoning"]
        for e in entries
    )
    cost = sum(e["cost"] for e in entries)
    return tokens, cost


def _live_entries(tmp_path, db=None):
    """Overview's live (store-off) normalized rows for the fixture tree."""
    return openclaw._collect_normalized_entries(_dirs(tmp_path), db or PricingDatabase())


class _Lock:
    """Patch builtins.open so exactly one path is unopenable while armed."""

    def __init__(self, monkeypatch, path):
        self.path = str(path)
        self.armed = True
        self._real = builtins.open
        monkeypatch.setattr(builtins, "open", self._open)

    def _open(self, path, *args, **kwargs):
        if str(path) == self.path and self.armed:
            raise PermissionError(13, "simulated file lock", path)
        return self._real(path, *args, **kwargs)

    def release(self):
        self.armed = False


# ---------------------------------------------------------------------------
# Parity — the release gate
# ---------------------------------------------------------------------------


def test_two_agents_three_sessions_exact_parity(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "s1.jsonl", [
        _hdr("s1"),
        _user("u1", T_YESTERDAY, "first question"),
        _asst("m1", T_YESTERDAY + 1000, inp=100, cw=20, cr=40, out=10),
        _asst("m2", T_TODAY, inp=200, cw=0, cr=50, out=30),
    ])
    _write_jsonl(a / "s2.jsonl", [
        _hdr("s2"),
        _asst("m3", T_TODAY2, inp=60, cw=5, cr=0, out=7),
    ])
    b = _sessions_dir(tmp_path, "beta")
    _write_jsonl(b / "s3.jsonl", [
        _hdr("s3"),
        _asst("m4", T_TODAY, inp=10, cw=0, cr=300, out=2),
    ])
    _pricing(monkeypatch, tmp_path)

    data = get_sessions_data("openclaw", "all")
    assert data["tool_label"] == "OpenClaw"
    by_id = {s["session_id"]: s for s in data["sessions"]}
    assert set(by_id) == {"s1", "s2", "s3"}
    assert by_id["s1"]["project"] == "alpha"
    assert by_id["s3"]["project"] == "beta"
    assert by_id["s1"]["display_name"] == "first question"

    entries = _live_entries(tmp_path)
    exp_tokens, exp_cost = _entry_sums(entries)
    assert data["summary"]["tokens"] == exp_tokens
    assert data["summary"]["cost"] == pytest.approx(exp_cost)

    # Same window semantics against get_session_usage totals, day/month/all.
    for period in ("day", "month", "all"):
        since_ms, until_ms = sessions._period_range(period)
        since_dt = _dt_from_ms(since_ms)
        until_dt = _dt_from_ms(until_ms)
        live = openclaw.get_session_usage(_dirs(tmp_path), since_dt, until_dt)
        panel = get_sessions_data("openclaw", period)
        assert panel["summary"]["tokens"] == live["total_tokens"], period
        assert panel["summary"]["cost"] == pytest.approx(live["total_cost"]), period


def test_parity_property_randomized(monkeypatch, tmp_path):
    """The release gate: live parse vs Sessions harness over the same fixture
    corpus, store off, WorkBuddy sum formula, five seeds."""
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    _pricing(monkeypatch, tmp_path)
    for seed in range(5):
        rng = random.Random(seed)
        home = tmp_path / f"home-{seed}"
        monkeypatch.setenv("OPENCLAW_HOME", str(home))
        for s in range(rng.randint(2, 4)):  # sessions
            d = home / "agents" / f"agent{s}" / "sessions"
            d.mkdir(parents=True)
            rows = [_hdr(f"sid-{seed}-{s}")]
            if rng.random() < 0.6:
                rows.append(_user(f"q-{seed}-{s}", T_TODAY + s * 1000, "question"))
            for i in range(rng.randint(1, 6)):
                kind = rng.random()
                if kind < 0.15:
                    rows.append(_asst(f"a-{seed}-{s}-{i}", T_TODAY + i * 1000 + s,
                                      usage_override=None))
                elif kind < 0.25:
                    rows.append(_asst(f"a-{seed}-{s}-{i}", T_TODAY + i * 1000 + s,
                                      usage_override={}))
                elif kind < 0.35:
                    rows.append(_asst(f"a-{seed}-{s}-{i}", T_TODAY + i * 1000 + s))  # all zero
                else:
                    rows.append(_asst(
                        f"a-{seed}-{s}-{i}", T_TODAY + i * 1000 + s,
                        inp=rng.randint(10, 5000), cw=rng.randint(0, 900),
                        cr=rng.randint(0, 9000), out=rng.randint(1, 900),
                    ))
            _write_jsonl(d / f"sess-{s}.jsonl", rows)
        # Header-less file, and a duplicate message id across two files.
        loose = home / "agents" / "loose" / "sessions"
        loose.mkdir(parents=True)
        _write_jsonl(loose / "headerless.jsonl", [
            _asst(f"h-{seed}", T_TODAY + 500, inp=40, out=4),
        ])
        dup = _asst(f"d-{seed}", T_TODAY + 700, inp=70, out=7)
        _write_jsonl(loose / "first.jsonl", [dup])
        _write_jsonl(loose / "second.jsonl.reset.9", [dup])

        _clear_caches()

        entries = _live_entries(home)
        exp_tokens, exp_cost = _entry_sums(entries)

        for period in ("day", "all"):
            panel = get_sessions_data("openclaw", period)
            assert panel["summary"]["tokens"] == exp_tokens, (seed, period)
            assert panel["summary"]["cost"] == pytest.approx(exp_cost), (seed, period)


def test_store_synced_view_agrees_including_boundary(monkeypatch, tmp_path):
    """Reconciliation scope 2: the store window is half-open and so is the
    Sessions window — with the store on and synced the two agree boundary and
    all, including a row stamped exactly at the day window's until."""
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "s1.jsonl", [
        _hdr("s1"),
        _asst("b1", T_TODAY, inp=100, cw=10, cr=20, out=5),
        _asst("b2", T_TODAY2, inp=1, cw=0, cr=0, out=1),
        _asst("b3", T_TOMORROW_MIDNIGHT, inp=999, cw=0, cr=0, out=999),
    ])

    store = openclaw._sync_openclaw_store(_dirs(tmp_path), PricingDatabase())
    rows = store.query_entries(sources=["openclaw"])
    assert len(rows) == 3  # the boundary row IS stored; the window excludes it

    since_ms, until_ms = sessions._period_range("day")
    since_dt = _dt_from_ms(since_ms)
    until_dt = _dt_from_ms(until_ms)
    live = openclaw.get_session_usage(_dirs(tmp_path), since_dt, until_dt)
    panel = get_sessions_data("openclaw", "day")
    assert live["total_messages"] == 2
    assert panel["summary"]["tokens"] == live["total_tokens"]
    assert panel["summary"]["cost"] == pytest.approx(live["total_cost"])


def test_until_instant_residual_live_fallback_only(monkeypatch, tmp_path):
    """openclaw.py:529 (live fallback) keeps msg_dt <= until_date; sessions'
    _summarize_session drops ts >= until_ms. Several rows stamped exactly at
    the next-day-midnight until-instant are therefore counted by a live
    Overview pass and by NO Sessions window — a residual of the live fallback
    only, never a one-row effect. With the store on, both agree (test above)."""
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "s1.jsonl", [
        _hdr("s1"),
        _asst("x1", T_TODAY, inp=100, out=10),
        _asst("x2", T_TOMORROW_MIDNIGHT, inp=7000, out=70),
        _asst("x3", T_TOMORROW_MIDNIGHT, inp=8000, out=80),
    ])

    since_ms, until_ms = sessions._period_range("day")
    since_dt = _dt_from_ms(since_ms)
    until_dt = _dt_from_ms(until_ms)
    live = openclaw.get_session_usage(_dirs(tmp_path), since_dt, until_dt)
    panel = get_sessions_data("openclaw", "day")
    assert live["total_messages"] == 3
    assert panel["summary"]["session_count"] == 1
    assert panel["summary"]["tokens"] == 100 + 10
    assert panel["summary"]["tokens"] < live["total_tokens"]


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------


def test_cache_write_folded_into_displayed_tokens_in(monkeypatch, tmp_path):
    """>0 cacheWrite must display folded into tokens_in (Overview's own
    mapping, openclaw.py:533-535) while the bill keeps the split. A harness
    passing tokens_in=input_raw displays fewer tokens than Overview."""
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "s1.jsonl", [
        _hdr("s1"),
        _asst("f1", T_TODAY, inp=5, cw=27000, cr=100, out=50),
    ])

    raw = _raw_sessions_for_tool("openclaw")["s1"]
    turn = raw["turns"][0]
    assert turn["tokens_in"] == 5 + 27000
    assert turn["tokens_cache"] == 100
    assert turn["tokens_out"] == 50
    assert turn["tokens"] == 5 + 27000 + 100 + 50
    # Pricing keeps the split: get_cost(model, input_raw, out, cr, cw), not
    # the folded display total.
    assert turn["cost"] == pytest.approx(
        PricingDatabase().get_cost("p1/oc-model", 5, 50, 100, 27000))

    data = get_sessions_data("openclaw", "all")
    live = openclaw.get_session_usage(_dirs(tmp_path))
    assert data["summary"]["tokens"] == live["total_tokens"]


def test_display_name_first_user_message_of_first_file(monkeypatch, tmp_path):
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "base.jsonl", [
        _hdr("s1"),
        _user("u1", T_TODAY, "   collapse   my   whitespace  "),
        _asst("m1", T_TODAY + 1000, inp=10, out=1),
    ])
    # Same session id, later file: the FIRST file's user message names it.
    _write_jsonl(a / "later.jsonl.reset.1", [
        _hdr("s1"),
        _user("u2", T_TODAY + 2000, "should not win"),
        _asst("m2", T_TODAY + 3000, inp=10, out=1),
    ])
    b = _sessions_dir(tmp_path, "beta")
    _write_jsonl(b / "nous.jsonl", [
        _hdr("nous"),
        _asst("m3", T_TODAY, inp=10, out=1),
    ])

    data = get_sessions_data("openclaw", "all")
    by_id = {s["session_id"]: s for s in data["sessions"]}
    assert by_id["s1"]["display_name"] == "collapse my whitespace"
    # No user message: the fallback names it after the project (agent name).
    assert by_id["nous"]["display_name"] == "beta"


def test_user_content_blocks_become_preview(monkeypatch, tmp_path):
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    msg = _user("u1", T_TODAY, "unused")
    msg["message"]["content"] = [
        {"type": "text", "text": "block one"},
        {"type": "tool_result", "content": "ignored"},
    ]
    _write_jsonl(a / "s1.jsonl", [
        _hdr("s1"), msg, _asst("m1", T_TODAY + 1000, inp=10, out=1),
    ])

    data = get_sessions_data("openclaw", "all")
    assert data["sessions"][0]["display_name"] == "block one"


def test_repeated_message_id_counted_once_in_both_views(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    dup = _asst("dup-1", T_TODAY, inp=100, out=10)
    _write_jsonl(a / "base.jsonl", [_hdr("s1"), dup])
    _write_jsonl(a / "old.jsonl.reset.9", [_hdr("s1"), dup])

    entries = _live_entries(tmp_path)
    assert len(entries) == 1
    data = get_sessions_data("openclaw", "all")
    assert data["summary"]["tokens"] == entries[0]["input"] + entries[0]["output"]
    assert data["sessions"][0]["token_events"] == 1


def test_multi_file_session_merges_without_double_count(monkeypatch, tmp_path):
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "base.jsonl", [
        _hdr("s1"), _asst("t1", T_TODAY, inp=10, out=1),
    ])
    _write_jsonl(a / "more.jsonl.deleted.5", [
        _hdr("s1"), _asst("t2", T_TODAY + 1000, inp=20, out=2),
    ])

    data = get_sessions_data("openclaw", "all")
    assert len(data["sessions"]) == 1
    s = data["sessions"][0]
    assert s["session_id"] == "s1"
    assert s["token_events"] == 2
    assert s["tokens"] == 10 + 1 + 20 + 2


def test_headerless_file_gets_synthetic_id_and_counts(monkeypatch, tmp_path):
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "orphan.jsonl", [
        _asst("o1", T_TODAY, inp=30, out=3),
    ])

    data = get_sessions_data("openclaw", "all")
    assert len(data["sessions"]) == 1
    s = data["sessions"][0]
    assert "orphan" in s["session_id"]
    assert s["project"] == "alpha"
    assert s["tokens"] == 33


def test_project_is_agent_name_not_cwd(monkeypatch, tmp_path):
    """Measured: 5,611 of 5,632 files record the SAME cwd, so cwd grouping
    would collapse the panel into one project; the agent axis is 20 real
    groups. Deliberate deviation from every other harness."""
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "s1.jsonl", [
        _hdr("s1", cwd="/home/howard/.openclaw/workspace"),
        _asst("m1", T_TODAY, inp=10, out=1),
    ])
    b = _sessions_dir(tmp_path, "news")
    _write_jsonl(b / "s2.jsonl", [
        _hdr("s2", cwd="/home/howard/.openclaw/workspace"),
        _asst("m2", T_TODAY, inp=10, out=1),
    ])

    data = get_sessions_data("openclaw", "all")
    assert {s["project"] for s in data["sessions"]} == {"alpha", "news"}


def test_agentless_path_falls_back_to_cwd_project():
    """Only when the path has no agents segment may cwd name the project."""
    row = _raw_row("/x/y/a.jsonl", "r1", T_TODAY)
    corpus = openclaw.SessionCorpus(
        rows=[row], files=["/x/y/a.jsonl"], signature=(),
        meta={"/x/y/a.jsonl": {"session_id": "s", "cwd": "/repos/demo-proj",
                               "agent": "", "preview": ""}},
    )
    built = sessions.build_openclaw_sessions(corpus)
    assert built["s"]["project"] == "demo-proj"


def test_windows_path_agent_name_has_no_backslashes():
    # A Windows tree read from WSL: os.sep alone cannot split it, and an
    # os.sep-only split would hand back the whole path tail as the agent.
    assert openclaw._agent_from_path(
        "C:\\Users\\dev\\.openclaw\\agents\\news\\sessions\\a.jsonl"
    ) == "news"
    assert openclaw._agent_from_path("/x/y/a.jsonl") == ""


# ---------------------------------------------------------------------------
# Cost semantics
# ---------------------------------------------------------------------------


def test_unresolved_model_falls_back_to_payload_cost_and_reprices(monkeypatch, tmp_path):
    """Tokdash pricing wins; the source-recorded cost is the fallback only
    when the model resolves to nothing (the split-cache-write-payload rule).
    Surviving a pricing-file edit is the reason the rule exists at all —
    repricing runs through _BILLING_RULES, not through the source."""
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "s1.jsonl", [
        _hdr("s1"),
        _asst("g1", T_TODAY, inp=1000, out=100, provider="ghost", model="unpriced",
              cost=0.42),
    ])

    data = get_sessions_data("openclaw", "all")
    assert data["summary"]["cost"] == pytest.approx(0.42)

    # Edit the pricing file: the model now resolves, so the DB price wins and
    # the payload fallback is gone. (pricing_sig is in the aggregate key, so
    # the rebuild misses on its own.)
    _pricing(monkeypatch, tmp_path, models=dict(
        RATES, **{"ghost/unpriced": {"input": 1.0, "output": 2.0,
                                     "cache_read": 0.0, "cache_write": 0.0}},
    ))
    reload_pricing_db()
    data2 = get_sessions_data("openclaw", "all")
    expected = PricingDatabase().get_cost("ghost/unpriced", 1000, 100, 0, 0)
    assert expected > 0
    assert data2["summary"]["cost"] == pytest.approx(expected)


def test_billing_rule_and_repricing():
    """The rule exists and _repriced_turns (the repricing path) honors both
    branches: db cost when it resolves, payload_fallback when it does not.
    This mirrors usage_billing_pricing(..., fallback=...) on the store side —
    duplicated deliberately, see the code comment."""
    rule = sessions._BILLING_RULES["split-cache-write-payload"]

    class _Db:
        def get_cost(self, model, i, o, cr, cw):
            return 0.0 if model == "ghost/x" else i + o + cr + cw

    bill_hit = sessions._billing_record(
        "m", "split-cache-write-payload",
        input_tokens=1, output_tokens=2, cache_read=3, cache_write=4,
        payload_fallback=9.99,
    )
    bill_miss = sessions._billing_record(
        "ghost/x", "split-cache-write-payload",
        input_tokens=1, output_tokens=2, cache_read=3, cache_write=4,
        payload_fallback=9.99,
    )
    assert rule(_Db(), bill_hit) == 10  # db wins over the payload
    assert rule(_Db(), bill_miss) == pytest.approx(9.99)

    turn = {
        "model": "ghost/x", "tokens_in": 1, "tokens_cache": 3,
        "tokens_out": 2, "tokens": 6, "_bill": bill_miss,
    }
    assert sessions._repriced_turns([turn])[0]["cost"] == pytest.approx(9.99)


def test_shared_corpus_rows_stay_cost_free():
    """The collector is cost-free by design: pricing happens afterwards, in
    _normalized_entry / the sessions turn. A cost key on a collector row
    would pin the pricing generation into _ENTRY_CACHE."""
    row_keys = None

    def probe(files, signature):
        nonlocal row_keys
        corpus = _orig_parse_corpus(files, signature)
        if corpus.rows:
            row_keys = set(corpus.rows[0])
        return corpus

    from tokdash.sources import openclaw as oc
    _orig_parse_corpus = oc._parse_corpus
    home = Path(os.environ["OPENCLAW_HOME"])
    d = home / "agents" / "alpha" / "sessions"
    d.mkdir(parents=True)
    _write_jsonl(d / "s.jsonl", [_asst("c1", T_TODAY, inp=10, out=1)])
    oc._parse_corpus = probe
    try:
        oc.collect_session_corpus([str(d)])
    finally:
        oc._parse_corpus = _orig_parse_corpus
    assert row_keys is not None
    assert "cost" not in row_keys
    assert "file" in row_keys  # Integration 1's grouping key


# ---------------------------------------------------------------------------
# Corpus contract + caching
# ---------------------------------------------------------------------------


def test_row_shape_file_key_and_meta_coverage(monkeypatch, tmp_path):
    """The round-3 regression: the first draft grouped on row["file"] while
    nothing produced it — a KeyError on the first real request."""
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "s1.jsonl", [
        _hdr("s1"),
        _user("u1", T_TODAY, "hello"),
        _asst("m1", T_TODAY + 1, inp=10, out=1),
        _asst("z1", T_TODAY + 2),  # zero usage: no row
    ])
    corpus = openclaw.collect_session_corpus(_dirs(tmp_path))
    assert len(corpus.rows) == 1
    for row in corpus.rows:
        assert row["file"] in corpus.meta
        meta = corpus.meta[row["file"]]
        assert meta["session_id"] == "s1"
        assert meta["agent"] == "alpha"
        assert meta["preview"] == "hello"
    assert corpus.failed == frozenset()


def test_collect_entries_still_returns_rows_only_and_intercepts():
    """_collect_entries keeps its name, its one-argument signature and its
    rows-only return; the live fallback still calls it. The rename warning in
    the spec keeps full force: a rename breaks the patched sites outright and
    an alias silently defangs them."""
    assert callable(openclaw._collect_entries)
    sig = inspect.signature(openclaw._collect_entries)
    assert list(sig.parameters) == ["session_dirs"]
    a = _sessions_dir(Path(os.environ["OPENCLAW_HOME"]), "alpha")
    _write_jsonl(a / "s.jsonl", [_asst("r1", T_TODAY, inp=10, out=1)])
    rows = openclaw._collect_entries([str(a)])
    assert isinstance(rows, list) and rows and isinstance(rows[0], dict)
    assert not isinstance(rows, openclaw.SessionCorpus)
    # Still the live fallback's seam:
    a = _sessions_dir(Path(os.environ["OPENCLAW_HOME"]), "beta")
    rows2 = openclaw._collect_normalized_entries(
        [str(a)], PricingDatabase()
    )  # empty dir
    assert rows2 == []


def test_private_patch_does_not_intercept_sessions_path(monkeypatch, tmp_path):
    """The four existing _collect_entries patches feed Overview's row
    builders; the Sessions harness reads through collect_session_corpus.
    A sessions-side test that patches the private name silently gets the REAL
    corpus while appearing to control it — this test names that split."""
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "real.jsonl", [
        _hdr("real"), _asst("m1", T_TODAY, inp=10, out=1),
    ])
    monkeypatch.setattr(
        openclaw, "_collect_entries",
        lambda _dirs: pytest.fail("_collect_entries ran on the Sessions path"),
    )
    data = get_sessions_data("openclaw", "all")
    assert {s["session_id"] for s in data["sessions"]} == {"real"}


def test_corpus_change_invalidates_via_mtime_bump(monkeypatch, tmp_path):
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    path = _write_jsonl(a / "s1.jsonl", [
        _hdr("s1"), _asst("p1", T_TODAY, inp=100, out=10),
    ])

    data1 = get_sessions_data("openclaw", "all")
    assert data1["summary"]["tokens"] == 110

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_asst("p2", T_TODAY + 5000, inp=200, out=20)) + "\n")
    # This box quantizes st_mtime_ns at 10 ms: bump the clock explicitly.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns + 20_000_000, st.st_mtime_ns + 20_000_000))

    misses_before = sessions._load_openclaw_sessions.cache_info().misses
    data2 = get_sessions_data("openclaw", "all")
    # Assert a MISS plus an updated result — never that a cache came back
    # "cleared": that assertion can only pass on a broken cache.
    assert sessions._load_openclaw_sessions.cache_info().misses > misses_before
    assert data2["summary"]["tokens"] == 330


def test_reload_pricing_db_clears_openclaw_aggregate(monkeypatch, tmp_path):
    """reload_pricing_db() must list this loader like every other. The honest
    assertion is a miss after the clear plus the same correct total — not an
    empty cache (which only a broken cache can pass)."""
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    _write_jsonl(a / "s1.jsonl", [
        _hdr("s1"), _asst("r1", T_TODAY, inp=100, out=10),
    ])

    data1 = get_sessions_data("openclaw", "all")
    assert data1["summary"]["cost"] == pytest.approx(
        PricingDatabase().get_cost("p1/oc-model", 100, 10, 0, 0))

    reload_pricing_db()
    misses_before = sessions._load_openclaw_sessions.cache_info().misses
    data2 = get_sessions_data("openclaw", "all")
    assert sessions._load_openclaw_sessions.cache_info().misses > misses_before
    assert data2["summary"]["cost"] == pytest.approx(
        PricingDatabase().get_cost("p1/oc-model", 100, 10, 0, 0))


# ---------------------------------------------------------------------------
# Transient reads: detection, delivery, recovery (Integration 2a)
# ---------------------------------------------------------------------------


def test_two_read_variants(monkeypatch, tmp_path):
    """One-argument parse_session_file keeps swallowing everything; the
    unavailable variant raises only for losing the stream. A torn JSON line
    stays a silent skip in BOTH variants — a raising variant that failed on
    decode errors would break on every half-written transcript."""
    d = _sessions_dir(Path(os.environ["OPENCLAW_HOME"]), "alpha")
    torn = _write_jsonl(d / "torn.jsonl", [
        _asst("t1", T_TODAY, inp=1, out=1), "NOT JSON AT ALL", _asst("t2", T_TODAY, inp=1, out=1),
    ])
    locked = _write_jsonl(d / "locked.jsonl", [_asst("l1", T_TODAY, inp=1, out=1)])
    lock = _Lock(monkeypatch, locked)

    assert openclaw.parse_session_file(str(torn)) != []
    assert openclaw.parse_session_file(str(torn), unavailable=openclaw._OpenClawReadUnavailable) != []
    assert openclaw.parse_session_file(str(locked)) == []
    with pytest.raises(openclaw._OpenClawReadUnavailable):
        openclaw.parse_session_file(str(locked), unavailable=openclaw._OpenClawReadUnavailable)
    # The default one-argument call is byte-for-byte the shipped behavior.
    assert openclaw.parse_session_file(str(d / "missing.jsonl")) == []


def test_transient_lock_end_to_end_degrades_and_recovers(monkeypatch, tmp_path):
    """Integration 2a end to end. The shipped behavior cached the hole under
    a never-changing signature and never recovered; asserting only "the
    locked file is absent" passes either way, so the recovery assertion is
    the regression."""
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    locked = _write_jsonl(a / "shotgun.jsonl", [
        _hdr("shotgun"), _asst("l1", T_TODAY, inp=100, out=10),
    ])
    _write_jsonl(a / "survivor.jsonl", [
        _hdr("survivor"), _asst("v1", T_TODAY, inp=200, out=20),
    ])
    sig = openclaw._signature(openclaw._session_files(_dirs(tmp_path)))
    lock = _Lock(monkeypatch, locked)

    data1 = get_sessions_data("openclaw", "all")
    assert {s["session_id"] for s in data1["sessions"]} == {"survivor"}
    # The incomplete corpus was never cached...
    assert openclaw.corpus_for_signature(sig) is None
    # ...and no aggregate entry memoized the partial panel.
    assert sessions._load_openclaw_sessions.cache_info().currsize == 0

    lock.release()
    # Nothing else changed: the next request must recover the session.
    data2 = get_sessions_data("openclaw", "all")
    assert {s["session_id"] for s in data2["sessions"]} == {"shotgun", "survivor"}
    assert sessions._load_openclaw_sessions.cache_info().currsize == 1
    assert openclaw.corpus_for_signature(sig) is not None


def test_dedupe_survives_a_partial_read(monkeypatch, tmp_path):
    """A duplicate id's first occurrence (sorted file order) is the LOCKED
    file. While locked the survivor's copy is counted once; when the lock
    clears the corpus is re-parsed whole, the locked file's copy wins, and
    the survivor's copy drops — counted once again. A partial parse is never
    "the full answer minus one file", so the recovered totals must be the
    WINNER's, never the sum of both copies."""
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    winner = _asst("dup-1", T_TODAY, inp=999, out=99)
    loser = _asst("dup-1", T_TODAY + 100, inp=11, out=1)  # same id, other file
    first = _write_jsonl(a / "a-first.jsonl", [_hdr("s1"), winner])
    _write_jsonl(a / "b-second.jsonl", [
        _hdr("s1"), loser, _asst("own-2", T_TODAY + 600, inp=20, out=2),
    ])
    _write_jsonl(a / "c-archive.jsonl.deleted.1", [
        _hdr("s1"), _asst("own-1", T_TODAY + 500, inp=10, out=1),
    ])
    lock = _Lock(monkeypatch, first)

    data1 = get_sessions_data("openclaw", "all")
    s1 = data1["sessions"][0]
    assert s1["token_events"] == 3
    assert s1["tokens"] == (11 + 1) + (10 + 1) + (20 + 2)

    lock.release()
    data2 = get_sessions_data("openclaw", "all")
    s2 = data2["sessions"][0]
    assert s2["token_events"] == 3
    assert s2["tokens"] == (999 + 99) + (10 + 1) + (20 + 2)


def test_incomplete_view_never_500s(monkeypatch, tmp_path):
    """Neither incomplete path may 500: _SessionFileUnavailable escaping an
    aggregate reaches the route guard (OSError/sqlite3.Error only) and the
    panel users get an error page instead of a degraded table."""
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    locked = _write_jsonl(a / "locked.jsonl", [
        _hdr("locked"), _asst("l1", T_TODAY, inp=100, out=10),
    ])
    _write_jsonl(a / "ok.jsonl", [
        _hdr("ok"), _asst("o1", T_TODAY, inp=10, out=1),
    ])
    _Lock(monkeypatch, locked)

    raws = _raw_sessions_for_tool("openclaw")  # must not raise
    assert set(raws) == {"ok"}


# ---------------------------------------------------------------------------
# The persistent store: check and write ONE corpus (Integration 2a)
# ---------------------------------------------------------------------------


def _stored(store):
    rows = store.query_entries(sources=["openclaw"])
    return len(rows), sum(float(r["cost"] or 0.0) for r in rows)


def test_stored_corpus_survives_partial_sync(monkeypatch, tmp_path, caplog):
    """The destructive one: today one locked file plus any signature change
    replaces the whole stored OpenClaw corpus with the partial set (durable
    retention only guards the wholly-empty case). The fix declines the sync
    and leaves the previous rows untouched — and must RECOVER after, which a
    marker-based design fails."""
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    f1 = _write_jsonl(a / "one.jsonl", [
        _hdr("s1"), _asst("m1", T_TODAY, inp=100, out=10),
    ])
    _write_jsonl(a / "two.jsonl", [
        _hdr("s2"), _asst("m2", T_TODAY, inp=200, out=20),
    ])
    store = openclaw._sync_openclaw_store(_dirs(tmp_path), PricingDatabase())
    before_n, before_cost = _stored(store)
    before_sig = store.source_signature("openclaw")
    assert before_n == 2

    # A signature change (new file) while one original is locked.
    _write_jsonl(a / "three.jsonl", [
        _hdr("s3"), _asst("m3", T_TODAY + 1000, inp=300, out=30),
    ])
    lock = _Lock(monkeypatch, f1)
    with caplog.at_level(logging.WARNING, logger="tokdash.sources.openclaw"):
        store2 = openclaw._sync_openclaw_store(_dirs(tmp_path), PricingDatabase())

    assert _stored(store2) == (before_n, pytest.approx(before_cost)), "partial sync replaced stored rows"
    assert store2.source_signature("openclaw") == before_sig, "declined sync recorded a new signature"
    assert any("openclaw" in r.getMessage().lower() for r in caplog.records), \
        "declined sync must be visible in the log, not silently stale"

    # Unlock: the SAME signature mismatch now succeeds — nothing was recorded
    # as current while locked, so the next request retries.
    lock.release()
    store3 = openclaw._sync_openclaw_store(_dirs(tmp_path), PricingDatabase())
    assert _stored(store3)[0] == 3

    # Unchanged corpus: the signature short-circuit must skip collection
    # ENTIRELY — without it every sync pays the 6.3s cold read the guard was
    # written to avoid.
    calls = []
    real_collect = openclaw.collect_session_corpus
    monkeypatch.setattr(
        openclaw, "collect_session_corpus",
        lambda *a, **k: calls.append(1) or real_collect(*a, **k),
    )
    openclaw._sync_openclaw_store(_dirs(tmp_path), PricingDatabase())
    assert calls == []


def test_store_writes_the_corpus_it_verified(monkeypatch, tmp_path):
    """The second-collection regression: while sync_source read the tree a
    second time, a mid-request change could store rows nobody checked. The
    rows written must be the rows the guard checked — the SAME object."""
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    fixture_row = _raw_row(a / "fixture.jsonl", "fx1", T_TODAY, inp=1000, out=1,
                           model="ghost/fixture-row", payload=0.0)
    corpus = _corpus_for_tests([fixture_row])
    monkeypatch.setattr(
        openclaw, "collect_session_corpus",
        lambda dirs, files=None: corpus,
    )
    # A DIFFERENT real corpus lands on disk after the patch was installed:
    _write_jsonl(a / "real.jsonl", [
        _hdr("real"), _asst("r1", T_TODAY, inp=7, out=7, provider="p1",
                            model="oc-model"),
    ])

    store = openclaw._sync_openclaw_store(_dirs(tmp_path), PricingDatabase())
    rows = store.query_entries(sources=["openclaw"])
    assert len(rows) == 1
    assert rows[0]["model"] == "ghost/fixture-row"  # the object, not the tree


def test_upgrade_path_parser_version_bump(monkeypatch, tmp_path, caplog):
    """Integration 2b: without the version bump an install the shipped bug
    already corrupted matches its stored signature, collection never runs,
    and the under-billed store survives the upgrade. This test decides
    whether the fix can reach anyone."""
    from tokdash.usage_store import build_source_signature

    assert openclaw.OPENCLAW_PARSER_VERSION == 2
    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    f1 = _write_jsonl(a / "one.jsonl", [
        _hdr("s1"), _asst("m1", T_TODAY, inp=100, out=10),
    ])
    f2 = _write_jsonl(a / "two.jsonl", [
        _hdr("s2"), _asst("m2", T_TODAY, inp=200, out=20),
    ])
    db = PricingDatabase()
    files = openclaw._session_files(_dirs(tmp_path))
    v1_parser = dict(openclaw._openclaw_parser_signature(), version=1)
    v1_sig = build_source_signature(files=openclaw._signature(files), parser=v1_parser)

    def seed_corrupted_store():
        # Exactly the state the shipped bug leaves: rows for one file (the
        # other was locked mid-sync) committed under the OLD parser version
        # at today's file signature.
        partial = [openclaw._normalized_entry(
            _raw_row(f1, "m1", T_TODAY, inp=100, out=10), db)]
        store = UsageEntryStore()
        store.sync_source("openclaw", v1_sig, lambda: partial)
        return store

    store = seed_corrupted_store()
    assert _stored(store)[0] == 1

    # The fixed sync: version 2 makes the signature mismatch, collection
    # runs, and the complete corpus replaces the short rows.
    fixed = openclaw._sync_openclaw_store(_dirs(tmp_path), db)
    assert _stored(fixed)[0] == 2
    assert fixed.source_signature("openclaw") != v1_sig

    # Repeat on a fresh store with one file still locked: the version-1 rows
    # survive untouched, the warning fires, and the stored signature stays
    # the old one so the next request retries.
    data2 = tmp_path / "data2"
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data2))
    openclaw._ENTRY_CACHE.clear()
    store2 = seed_corrupted_store()
    lock = _Lock(monkeypatch, f2)
    with caplog.at_level(logging.WARNING, logger="tokdash.sources.openclaw"):
        still = openclaw._sync_openclaw_store(_dirs(tmp_path), db)
    assert _stored(still) == _stored(store2) == (1, pytest.approx(
        db.get_cost("p1/oc-model", 100, 10, 0, 0)))
    assert still.source_signature("openclaw") == v1_sig
    assert any("openclaw" in r.getMessage().lower() for r in caplog.records)
    lock.release()

    # The follow-up request the comment above promises: because the declined
    # sync recorded nothing new, the version mismatch still stands and the
    # next sync completes — the corrupted v1 rows are finally replaced and
    # the v2 signature is committed.
    repaired = openclaw._sync_openclaw_store(_dirs(tmp_path), db)
    assert _stored(repaired)[0] == 2
    assert repaired.source_signature("openclaw") != v1_sig


# ---------------------------------------------------------------------------
# Route-level staleness (shared behavior, pinned)
# ---------------------------------------------------------------------------


def test_route_serves_stale_partial_until_cache_cleared(monkeypatch, tmp_path):
    """Characterization test of SHARED behavior, not of this harness: a
    partial panel sits in the route response cache for up to CACHE_TTL, so a
    browser can keep showing a missing session for minutes after the lock
    clears. /api/sessions has NO refresh parameter — recovery here is
    api._clear_cache(), and a test written with ?refresh=1 would pass only
    because the cache happened to expire. If the out-of-scope partial flag
    ever lands, this test must fail loudly."""
    from tokdash import api

    _pricing(monkeypatch, tmp_path)
    a = _sessions_dir(tmp_path, "alpha")
    locked = _write_jsonl(a / "locked.jsonl", [
        _hdr("locked"), _asst("l1", T_TODAY, inp=100, out=10),
    ])
    _write_jsonl(a / "ok.jsonl", [
        _hdr("ok"), _asst("o1", T_TODAY, inp=10, out=1),
    ])
    lock = _Lock(monkeypatch, locked)

    r1 = api.get_sessions("openclaw", "all")
    assert {s["session_id"] for s in r1["sessions"]} == {"ok"}

    lock.release()
    r2 = api.get_sessions("openclaw", "all")
    assert {s["session_id"] for s in r2["sessions"]} == {"ok"}, \
        "the partial payload must still come from the route cache"

    api._clear_cache()
    r3 = api.get_sessions("openclaw", "all")
    assert {s["session_id"] for s in r3["sessions"]} == {"locked", "ok"}


# ---------------------------------------------------------------------------
# Registration + hygiene
# ---------------------------------------------------------------------------


def test_registered_out_of_persistent_gate():
    assert "openclaw" in SESSION_TOOLS
    assert TOOL_LABELS["openclaw"] == "OpenClaw"
    # The store holds per-message rows with no session column: serving OpenClaw
    # sessions from it needs a schema change and stays out of scope.
    assert "openclaw" not in sessions._SESSION_FILE_PARSER_VERSIONS
    assert "openclaw" not in {"codex", "claude", "kimi", "dsh", "reasonix"}
    # Empty tree: empty view, no raise.
    data = get_sessions_data("openclaw", "all")
    assert data["sessions"] == []
    assert data["summary"]["session_count"] == 0


def test_empty_home_no_view(monkeypatch, tmp_path):
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setenv("OPENCLAW_HOME", str(empty))
    data = get_sessions_data("openclaw", "all")
    assert data["sessions"] == []


def test_openclaw_source_never_imports_sessions():
    """Dependency direction: sessions.py translates openclaw's failure class
    at its own boundary; the source owning it must not import the consumer."""
    src = inspect.getsource(inspect.getmodule(openclaw))
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module and "sessions" in node.module:
            raise AssertionError(f"openclaw imports {node.module}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "sessions" not in alias.name, alias.name


def test_frontend_session_registry_includes_openclaw():
    index = Path(sessions.__file__).parent / "static" / "index.html"
    source = index.read_text(encoding="utf-8")
    assert "'qwen_code', 'openclaw'" in source
    assert "qwen_code: null, openclaw: null, combined: null" in source
    assert 'updateSessionPanel("openclaw", lastSessionsResponses.openclaw);' in source
    assert 'initSortHeaders("openclaw", renderSessionsTab);' in source
    assert "openclaw: { ...DEFAULT_SORT }," in source
    assert "openclawSessions: 'OpenClaw Sessions'," in source
    assert "openclawSessions: 'OpenClaw \u4f1a\u8bdd'," in source
    assert 'id="openclawSessionsTable"' in source
    assert 'data-panel-details="openclaw"' in source
    # Brand meta and formatToolName already had openclaw — verify, don't add.
    brand = source.split("const TOOL_BRAND_META = Object.freeze({", 1)[1].split("});", 1)[0]
    assert "openclaw:" in brand
