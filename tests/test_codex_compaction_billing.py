"""A Codex compaction request is billed once, whichever way the log carries it (issue #222).

On OpenAI models a remote compaction is a ``token_usage_record`` answered by a
``compacted`` event, and no ``token_count`` ever carries its usage: the one after
it has zero ``last_token_usage`` and every later total excludes the request.
Other providers' next ``token_count`` carries it. Overview (live and stored) and
Sessions must each bill it exactly once in both cases.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tokdash.compute as compute
from tokdash import sessions
from tokdash.output_speed import STATUS_INCOMPLETE
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import (
    BaseParser,
    CodexParser,
    CodingToolsUsageTracker,
    _sig_cache,
    codex_compaction_event_key,
)
from tokdash.speed_mode import collect_timings

MODEL = "tokdash-codex-test"
BASE = datetime(2026, 5, 19, 12, tzinfo=timezone.utc)

# One request before the compaction, the compaction itself, one request after.
BEFORE = {"input_tokens": 1_000, "cached_input_tokens": 400, "output_tokens": 50, "reasoning_output_tokens": 10}
COMPACTION = {"input_tokens": 5_000, "cached_input_tokens": 3_000, "output_tokens": 700, "reasoning_output_tokens": 0}
AFTER = {"input_tokens": 200, "cached_input_tokens": 100, "output_tokens": 30, "reasoning_output_tokens": 5}
ZERO = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "1")
    yield tmp_path


def _add(*usages):
    return {key: sum(u[key] for u in usages) for key in BEFORE}


def _stamp(seconds):
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _row(seconds, kind, **payload):
    return {"timestamp": _stamp(seconds), "type": kind, "payload": payload}


def _count(seconds, last, total):
    return _row(seconds, "event_msg", type="token_count", info={"last_token_usage": last, "total_token_usage": total})


def _rollout(session_id, *, shape, at=0, provider="openai"):
    """shape: 'uncarried' (OpenAI), 'carried' (custom provider: its token_count sits
    between the record and the `compacted` event), 'carried_after' (the same
    token_count after `compacted`) or 'unrecorded' (old logs)."""
    omitted = shape in ("uncarried", "unrecorded")
    thread = _add(BEFORE, COMPACTION)
    record = {
        "response_id": "resp-compact",
        "turn_id": "turn-1",
        "usage": COMPACTION,
        "thread_token_usage": thread,
    }
    carrier = _count(at + 3, COMPACTION, thread)
    rows = [
        _row(at, "session_meta", id=session_id, cwd="/w", model_provider=provider),
        _row(at + 1, "turn_context", turn_id="turn-1", model=MODEL),
        _count(at + 2, BEFORE, _add(BEFORE)),
    ]
    if shape != "unrecorded":
        rows.append(_row(at + 3, "token_usage_record", **record))
    if shape == "carried":
        rows.append(carrier)
    compacted = {}
    if shape != "unrecorded":
        compacted = {"compaction_response_id": "resp-compact", "latest_token_usage_record": record}
    rows += [
        _row(at + 3, "compacted", **compacted),
        _row(at + 4, "turn_context", turn_id="turn-1", model=MODEL),
    ]
    if shape == "carried_after":
        rows.append(carrier)
    elif omitted:
        rows.append(_count(at + 5, ZERO, _add(BEFORE)))
    rows.append(_count(at + 6, AFTER, _add(BEFORE, AFTER) if omitted else _add(BEFORE, COMPACTION, AFTER)))
    return rows


def _write(home, stem, rows):
    path = home / ".codex" / "sessions" / "2026" / "05" / "19" / f"{stem}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _write_pricing():
    path = PricingDatabase().override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    priced = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.0}
    path.write_text(json.dumps({"version": "test", "aliases": {}, "models": {MODEL: priced}}), encoding="utf-8")
    sessions.reload_pricing_db()


def _fresh(usage):
    return usage["input_tokens"] - usage["cached_input_tokens"]


def _tokens(usage):
    """(fresh input, cache read, output) as every view reports one request."""
    return (_fresh(usage), usage["cached_input_tokens"], usage["output_tokens"])


def _overview_live():
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    tracker = CodingToolsUsageTracker()
    tracker.collect(None, None, ["codex"])
    return sorted((e["input"], e["cacheRead"], e["output"]) for e in tracker.to_json()["entries"])


def _overview_stored():
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    store, _sources = compute._sync_usage_store(CodingToolsUsageTracker())
    return sorted((e["input"], e["cacheRead"], e["output"]) for e in store.query_entries(sources=["codex"]))


def _sessions(monkeypatch, stored):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1" if stored else "0")
    _sig_cache.clear()
    sessions.reload_pricing_db()
    turns = [turn for raw in sessions._raw_sessions_for_tool("codex").values() for turn in raw["turns"]]
    return sorted((t["tokens_in"], t["tokens_cache"], t["tokens_out"]) for t in turns)


def _views(monkeypatch):
    return {
        "live": _overview_live(),
        "stored": _overview_stored(),
        "sessions_live": _sessions(monkeypatch, stored=False),
        "sessions_stored": _sessions(monkeypatch, stored=True),
    }


def _expect(*usages):
    return sorted(_tokens(u) for u in usages)


def test_openai_compaction_no_token_count_carries_is_billed_once(_isolated_home, monkeypatch):
    _write_pricing()
    _write(_isolated_home, "openai", _rollout("sess-openai", shape="uncarried"))

    for view, rows in _views(monkeypatch).items():
        assert rows == _expect(BEFORE, COMPACTION, AFTER), view


@pytest.mark.parametrize("shape", ["carried", "carried_after"])
def test_custom_provider_compaction_a_token_count_carries_is_still_billed_once(_isolated_home, monkeypatch, shape):
    _write_pricing()
    _write(_isolated_home, "custom", _rollout("sess-custom", shape=shape, provider="custom"))

    for view, rows in _views(monkeypatch).items():
        assert rows == _expect(BEFORE, COMPACTION, AFTER), view


def test_resumed_history_copy_of_a_compaction_is_billed_once(_isolated_home, monkeypatch):
    """Codex copies a resumed thread's history into a new file, restamped."""
    _write_pricing()
    _write(_isolated_home, "original", _rollout("sess-resumed", shape="uncarried"))
    _write(_isolated_home, "resumed", _rollout("sess-resumed", shape="uncarried", at=3_600))

    for view, rows in _views(monkeypatch).items():
        assert rows == _expect(BEFORE, COMPACTION, AFTER), view


def test_forked_subagent_replaying_the_parents_compaction_does_not_bill_it_again(_isolated_home, monkeypatch):
    _write_pricing()
    parent = _rollout("sess-parent", shape="uncarried")
    _write(_isolated_home, "parent", parent)
    child_meta = _row(
        3_600,
        "session_meta",
        id="sess-child",
        cwd="/w",
        model_provider="openai",
        forked_from_id="sess-parent",
        source={"subagent": {"thread_spawn": {"parent_thread_id": "sess-parent", "depth": 1}}},
    )
    replay = [{**row, "timestamp": _stamp(3_601)} for row in parent]
    _write(_isolated_home, "child", [child_meta, *replay])

    for view, rows in _views(monkeypatch).items():
        assert rows == _expect(BEFORE, COMPACTION, AFTER), view


def test_rollouts_that_name_no_usage_for_their_compaction_are_unchanged(_isolated_home, monkeypatch):
    """Old logs: a bare `compacted` with no token_usage_record says nothing to bill."""
    _write_pricing()
    _write(_isolated_home, "old", _rollout("sess-old", shape="unrecorded"))

    for view, rows in _views(monkeypatch).items():
        assert rows == _expect(BEFORE, AFTER), view


def test_compaction_naming_another_response_bills_nothing(_isolated_home, monkeypatch):
    _write_pricing()
    rows = _rollout("sess-other", shape="uncarried")
    for row in rows:
        if row["type"] == "compacted":
            row["payload"] = {"compaction_response_id": "resp-unseen"}
    _write(_isolated_home, "other", rows)

    for view, got in _views(monkeypatch).items():
        assert got == _expect(BEFORE, AFTER), view


def test_the_compaction_row_is_priced_dated_and_never_timed(_isolated_home):
    _write_pricing()
    _write(_isolated_home, "openai", _rollout("sess-openai", shape="uncarried"))

    _sig_cache.clear()
    with collect_timings():
        rows = CodexParser(PricingDatabase())._parse_all()
    by_output = {row["output"]: row for row in rows}
    compaction = by_output[COMPACTION["output_tokens"]]

    assert len(rows) == 3
    assert compaction["model"] == MODEL
    assert compaction["timestamp"] == int((BASE + timedelta(seconds=3)).timestamp() * 1000)
    assert compaction["cost"] == pytest.approx((2_000 * 3.0 + 3_000 * 0.3 + 700 * 15.0) / 1_000_000)
    assert compaction["_speed"]["speed_status"] == STATUS_INCOMPLETE
    assert compaction["_speed"]["speed_calls"] == 0


def test_compaction_key_is_scoped_to_the_session_and_the_usage_it_leaves():
    record = {"usage": COMPACTION, "thread_token_usage": _add(BEFORE, COMPACTION)}
    key = codex_compaction_event_key("sess", record)

    assert key and key.startswith("codex-compaction-v1:")
    assert key == codex_compaction_event_key("sess", dict(record))
    assert key != codex_compaction_event_key("other", record)
    assert key != codex_compaction_event_key("sess", {**record, "thread_token_usage": _add(BEFORE)})
    assert codex_compaction_event_key("sess", {"usage": COMPACTION}) is None
    assert codex_compaction_event_key(None, record) is None
