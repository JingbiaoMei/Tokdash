"""Tests for QoderCliParser (Qoder CLI transcripts + segments)."""
import builtins
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, QoderCliParser
from tokdash.usage_store import (
    UsageEntryStore,
    build_source_signature,
    public_usage_entry,
    usage_billing_fixed,
    usage_billing_pricing,
)

FIXTURES = Path(__file__).parent / "fixtures" / "qoder"
RATE = 0.01  # default QODER_USD_PER_CREDIT estimate


@pytest.fixture(autouse=True)
def _clear_qoder_cli_caches():
    BaseParser._entry_cache.clear()
    yield
    BaseParser._entry_cache.clear()


def _parser(monkeypatch, tmp_path, roots: list):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    for var in ("QODER_CLI_HOME", "QODER_CONFIG_DIR", "QODER_USD_PER_CREDIT", "QODER_CLI_CONTEXT_WINDOW"):
        monkeypatch.delenv(var, raising=False)
    if roots:
        monkeypatch.setenv("QODER_CLI_HOME", ",".join(str(r) for r in roots))
    return QoderCliParser(PricingDatabase())


def _write_lines(path: Path, lines: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return path


def _t_line(rid, ts, model="auto", credits=None, in_t=0, out_t=0, cache_r=0, cache_w=0, ratio=None, billable=None):
    usage = {
        "input_tokens": in_t,
        "output_tokens": out_t,
        "cache_read_input_tokens": cache_r,
        "cache_creation_input_tokens": cache_w,
        "request_id": rid,
    }
    if credits is not None:
        usage["credits"] = credits
        usage["original_credits"] = credits
        usage["billable"] = True if billable is None else billable
    if ratio is not None:
        usage["context_usage_ratio"] = ratio
    return {
        "type": "assistant",
        "uuid": "uuid-" + rid,
        "timestamp": ts,
        "message": {"role": "assistant", "model": model, "usage": usage},
        "sessionId": "s1",
    }


def _s_line(rid, ts, model="auto", in_t=0, out_t=0, cache_r=0, cache_w=0, data_rid=None):
    data = {
        "request_index": 1,
        "model": model,
        "stop_reason": "end_turn",
        "input_tokens": in_t,
        "output_tokens": out_t,
        "cache_read_input_tokens": cache_r,
        "cache_creation_input_tokens": cache_w,
    }
    line = {"type": "model.response.completed", "data": data, "ts": ts, "seq": 1, "turn_id": "t", "loop_id": "l"}
    if data_rid is not None:
        data["request_id"] = data_rid
    elif rid is not None:
        line["request_id"] = rid
    return line


def _fixture_root(root: Path, transcript=None, segment=None) -> Path:
    if transcript:
        _write_lines(
            root / "projects" / "proj1" / "7255bada-539e-4ca0-bcf2-0b23aed031c3.jsonl",
            [json.loads(l) for l in (FIXTURES / transcript).read_text().splitlines() if l.strip()],
        )
    if segment:
        _write_lines(
            root / "logs" / "sessions" / "proj1" / "7255bada-539e-4ca0-bcf2-0b23aed031c3" / "segments" / "seg.jsonl",
            [json.loads(l) for l in (FIXTURES / segment).read_text().splitlines() if l.strip()],
        )
    return root


def _sync(store: UsageEntryStore, parser: QoderCliParser) -> None:
    sig = build_source_signature(
        files=parser._file_signatures(),
        parser=parser.persistent_parser_signature(),
    )
    store.sync_source("qoder_cli", sig, lambda: parser.collect(None, None))


# ---------------------------------------------------------------- transcripts


def test_interactive_fixture(monkeypatch, tmp_path):
    root = _fixture_root(
        tmp_path / "root",
        transcript="mac_cli_transcript_interactive.jsonl",
        segment="mac_cli_segment_interactive.jsonl",
    )
    entries = _parser(monkeypatch, tmp_path, [root]).collect(None, None)

    assert len(entries) == 2
    by_rid = {e["entry_id"]: e for e in entries}
    e1 = by_rid["qoder-cli:b76b8ce1-12ff-450e-9cec-fb7c199744eb"]
    e2 = by_rid["qoder-cli:09f99852-2d5c-4e87-ab9f-e5da486911c9"]
    for e in (e1, e2):
        assert e["model"] == "auto"
        assert e["output"] == 0  # the international build zero-fills output
        assert e["cacheRead"] == 0
        assert e["costAuthoritative"] is True
    # input recovered exactly from context_usage_ratio * 180000
    assert e1["input"] == 23285
    assert e2["input"] == 23321
    assert abs(e1["cost"] - 0.6484133714285715 * RATE) < 1e-15
    assert abs(e2["cost"] - 0.48029068 * RATE) < 1e-15
    from tokdash.sources.coding_tools import _qoder_cli_iso_ms

    assert e1["timestamp"] == _qoder_cli_iso_ms("2026-08-21T12:15:25.492Z")
    # the all-zero segment events change nothing (verified against the live
    # v1.1.28 capture: segments and transcripts share request_ids)


def test_tool_test_fixture(monkeypatch, tmp_path):
    root = _fixture_root(
        tmp_path / "root",
        transcript="mac_cli_transcript_tool_test.jsonl",
        segment="mac_cli_segment_tool_test.jsonl",
    )
    entries = _parser(monkeypatch, tmp_path, [root]).collect(None, None)
    assert len(entries) == 2
    by_rid = {e["entry_id"]: e for e in entries}
    assert by_rid["qoder-cli:115df0c0-4608-404a-ae75-894304ad57d6"]["input"] == 23392
    assert by_rid["qoder-cli:d265ff5a-eb3f-41a2-a2a0-64901465efc1"]["input"] == 23505
    assert abs(
        by_rid["qoder-cli:115df0c0-4608-404a-ae75-894304ad57d6"]["cost"] - 2.29996199235 * RATE
    ) < 1e-15
    assert abs(
        by_rid["qoder-cli:d265ff5a-eb3f-41a2-a2a0-64901465efc1"]["cost"] - 0.5640312001500001 * RATE
    ) < 1e-15


def test_rate_override_changes_cost_not_tokens(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "abcdef01-2345.jsonl",
                 [_t_line("r1", "2026-08-21T12:00:00.000Z", credits=2.5, ratio=1000 / 180000)])
    parser = _parser(monkeypatch, tmp_path, [root])
    base = parser.collect(None, None)
    monkeypatch.setenv("QODER_USD_PER_CREDIT", "0.02")
    entries = parser.collect(None, None)
    assert len(entries) == 1
    assert entries[0]["input"] == base[0]["input"] == 1000
    assert abs(entries[0]["cost"] - 2.5 * 0.02) < 1e-15


def test_no_roots_is_empty_success(monkeypatch, tmp_path):
    assert _parser(monkeypatch, tmp_path, []).collect(None, None) == []


# -------------------------------------------------------------------- merging


def test_transcript_segment_merge(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", credits=1.0, ratio=1000 / 180000)])
    _write_lines(root / "logs" / "sessions" / "p" / "s" / "segments" / "seg.jsonl",
                 [_s_line("X", "2026-08-21T13:00:00+01:00", in_t=500, out_t=7)])
    entries = _parser(monkeypatch, tmp_path, [root]).collect(None, None)
    assert len(entries) == 1
    e = entries[0]
    # tokens from the segment (finer-grained truth), credits from the transcript
    assert (e["input"], e["output"]) == (500, 7)
    assert e["costAuthoritative"] is True
    assert abs(e["cost"] - 1.0 * RATE) < 1e-15


def test_all_zero_segment_contributes_nothing(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _write_lines(root / "logs" / "sessions" / "p" / "s" / "segments" / "seg.jsonl",
                 [_s_line("X", "2026-08-21T13:00:00+01:00")])
    assert _parser(monkeypatch, tmp_path, [root]).collect(None, None) == []


def test_transcript_subdir_and_non_json_are_ignored(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "abcdef01-2345.jsonl",
                 [_t_line("good", "2026-08-21T12:00:00.000Z", credits=1.0, ratio=1 / 180000)])
    # a GUI session transcript copy in the transcript/ subdir (must not count)
    _write_lines(root / "projects" / "p" / "transcript" / "abcdef01-2345.jsonl",
                 [_t_line("gui-copy", "2026-08-21T12:00:00.000Z", credits=9.0, ratio=1 / 180000)])
    # non-JSON content in a hex-named file (skipped line by line)
    _write_lines(root / "projects" / "p" / "deadbeef.jsonl", ["not json at all", "{broken"])
    entries = _parser(monkeypatch, tmp_path, [root]).collect(None, None)
    assert [e["entry_id"] for e in entries] == ["qoder-cli:good"]


def test_segment_data_level_request_id_still_merges(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", credits=1.0, ratio=1000 / 180000)])
    lines = [_s_line(None, "2026-08-21T13:00:00+01:00", in_t=300, out_t=3, data_rid="X")]
    # a line with no resolvable request_id is skipped
    lines.append(_s_line(None, "2026-08-21T13:00:01+01:00", in_t=999, out_t=9))
    _write_lines(root / "logs" / "sessions" / "p" / "s" / "segments" / "seg.jsonl", lines)
    entries = _parser(monkeypatch, tmp_path, [root]).collect(None, None)
    assert len(entries) == 1
    assert (entries[0]["input"], entries[0]["output"]) == (300, 3)


def test_cost_only_record_is_skipped(monkeypatch, tmp_path):
    # credits > 0 but no tokens and no usable ratio: the aggregator would
    # drop the zero-token row before reading its cost, so skip it by design.
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", credits=5.0)])
    assert _parser(monkeypatch, tmp_path, [root]).collect(None, None) == []


# ------------------------------------------------------------- ratio recovery


def test_pinned_model_recovery_requires_override(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", model="qwen3.8-max", ratio=0.05)])
    parser = _parser(monkeypatch, tmp_path, [root])
    assert parser.collect(None, None) == []
    # explicit override applies to every model, including this pinned one
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", "200000")
    entries = parser.collect(None, None)
    assert len(entries) == 1
    assert entries[0]["input"] == 10000  # 0.05 * 200000, exact


def test_explicit_window_override_applies_to_auto(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", ratio=0.05)])
    parser = _parser(monkeypatch, tmp_path, [root])
    default = parser.collect(None, None)
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", "200000")
    overridden = parser.collect(None, None)
    assert default[0]["input"] == 9000  # 0.05 * 180000
    assert overridden[0]["input"] == 10000  # 0.05 * 200000


def test_recovery_requires_zero_cache_buckets(monkeypatch, tmp_path):
    # non-zero cache + zero input + usable ratio: no recovery (the ratio is
    # the TOTAL prompt; assigning it to input would double-count the cache),
    # but the entry is still emitted with its attributable cache bucket.
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", cache_r=500, ratio=0.1)])
    entries = _parser(monkeypatch, tmp_path, [root]).collect(None, None)
    assert len(entries) == 1
    assert (entries[0]["input"], entries[0]["output"], entries[0]["cacheRead"]) == (0, 0, 500)


# ------------------------------------------------------------------ root dedup


def test_same_type_duplicate_across_roots_first_wins(monkeypatch, tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write_lines(root_a / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", credits=1.0, ratio=1000 / 180000)])
    _write_lines(root_b / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", credits=2.0, ratio=2000 / 180000)])
    entries = _parser(monkeypatch, tmp_path, [root_a, root_b]).collect(None, None)
    assert len(entries) == 1
    assert entries[0]["input"] == 1000
    assert abs(entries[0]["cost"] - 1.0 * RATE) < 1e-15


def test_complementary_split_across_roots_merges(monkeypatch, tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write_lines(root_a / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", credits=1.5, ratio=0.1)])
    _write_lines(root_b / "logs" / "sessions" / "p" / "s" / "segments" / "seg.jsonl",
                 [_s_line("X", "2026-08-21T13:00:00+01:00", in_t=400, out_t=5)])
    entries = _parser(monkeypatch, tmp_path, [root_a, root_b]).collect(None, None)
    assert len(entries) == 1
    assert (entries[0]["input"], entries[0]["output"]) == (400, 5)
    assert abs(entries[0]["cost"] - 1.5 * RATE) < 1e-15


# ------------------------------------------------------------- source replace


def test_source_replace_remerges_on_single_file_change(monkeypatch, tmp_path):
    """Cross-file visibility regression (the file_replace pitfall).

    A segment-only change must re-emit the FULL merged entry (credits still
    visible from the unchanged transcript file), and vice versa.
    """
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", credits=1.0, ratio=1000 / 180000)])
    seg_path = root / "logs" / "sessions" / "p" / "s" / "segments" / "seg.jsonl"
    _write_lines(seg_path, [_s_line("X", "2026-08-21T13:00:00+01:00")])
    os.utime(seg_path, ns=(1787314000_000_000_000, 1787314000_000_000_000))

    parser = _parser(monkeypatch, tmp_path, [root])
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    _sync(store, parser)
    rows = {e["entry_id"]: e for e in store.query_entries()}
    assert len(rows) == 1
    assert rows["qoder-cli:X"]["input"] == 1000  # ratio recovery
    assert abs(rows["qoder-cli:X"]["cost"] - RATE) < 1e-15

    # segment-only change: it now carries real tokens
    _write_lines(seg_path, [_s_line("X", "2026-08-21T13:00:01+01:00", in_t=500, out_t=7)])
    os.utime(seg_path, ns=(1787314100_000_000_000, 1787314100_000_000_000))
    _sync(store, parser)
    rows = {e["entry_id"]: e for e in store.query_entries()}
    assert len(rows) == 1
    # merged from BOTH files: segment tokens, transcript credits
    assert (rows["qoder-cli:X"]["input"], rows["qoder-cli:X"]["output"]) == (500, 7)
    assert abs(rows["qoder-cli:X"]["cost"] - RATE) < 1e-15

    # transcript-only change: a second request appears, the first survives
    tr_path = root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl"
    lines = [json.loads(l) for l in tr_path.read_text().splitlines() if l.strip()]
    lines.append(_t_line("Y", "2026-08-21T12:05:00.000Z", credits=0.5, ratio=2000 / 180000))
    _write_lines(tr_path, lines)
    os.utime(tr_path, ns=(1787314200_000_000_000, 1787314200_000_000_000))
    _sync(store, parser)
    rows = {e["entry_id"]: e for e in store.query_entries()}
    assert set(rows) == {"qoder-cli:X", "qoder-cli:Y"}
    assert rows["qoder-cli:X"]["input"] == 500
    assert rows["qoder-cli:Y"]["input"] == 2000
    assert abs(rows["qoder-cli:Y"]["cost"] - 0.5 * RATE) < 1e-15


def test_unreadable_file_preserves_stored_corpus(monkeypatch, tmp_path):
    """Whole-source correctness: a read failure aborts the sync, and the
    previously stored rows survive (the DELETE never ran)."""
    root = tmp_path / "root"
    tr_path = root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl"
    _write_lines(tr_path, [_t_line("X", "2026-08-21T12:00:00.000Z", credits=1.0, ratio=1000 / 180000)])
    seg_path = root / "logs" / "sessions" / "p" / "s" / "segments" / "seg.jsonl"
    _write_lines(seg_path, [_s_line("X", "2026-08-21T13:00:00+01:00")])
    os.utime(seg_path, ns=(1787314000_000_000_000, 1787314000_000_000_000))

    parser = _parser(monkeypatch, tmp_path, [root])
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    _sync(store, parser)
    assert len(store.query_entries()) == 1

    # the transcript changes (new signature) while the segment becomes
    # unreadable; the parse must raise and the stored corpus must remain
    lines = [json.loads(l) for l in tr_path.read_text().splitlines() if l.strip()]
    lines.append(_t_line("Y", "2026-08-21T12:05:00.000Z", credits=0.5, ratio=2000 / 180000))
    _write_lines(tr_path, lines)
    os.utime(tr_path, ns=(1787314200_000_000_000, 1787314200_000_000_000))

    real_open = builtins.open

    def bad_open(file, *args, **kwargs):
        if str(file) == str(seg_path):
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", bad_open)
    with pytest.raises(PermissionError):
        _sync(store, parser)

    rows = store.query_entries()
    assert len(rows) == 1
    assert rows[0]["entry_id"] == "qoder-cli:X"
    assert rows[0]["input"] == 1000


# ------------------------------------------------- billing + repricing provenance


def test_billing_provenance_per_kind(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _t_line("credits", "2026-08-21T12:00:00.000Z", credits=1.0, ratio=1000 / 180000),
        _t_line("tokens", "2026-08-21T12:01:00.000Z", model="grok-4.5", in_t=1_000_000),
    ])
    entries = {e["entry_id"]: e for e in _parser(monkeypatch, tmp_path, [root]).collect(None, None)}

    fixed = entries["qoder-cli:credits"]["_billing"]
    assert fixed["kind"] == "fixed"
    assert fixed["cost"] == pytest.approx(RATE)
    assert entries["qoder-cli:credits"]["costAuthoritative"] is True

    priced = entries["qoder-cli:tokens"]["_billing"]
    assert priced["kind"] == "pricing"
    assert priced["models"] == ["grok-4.5"]
    assert (priced["input"], priced["output"]) == (1_000_000, 0)
    assert entries["qoder-cli:tokens"]["costAuthoritative"] is False


def test_apply_pricing_moves_token_rows_not_credit_rows(monkeypatch, tmp_path):
    ts = 1_784_900_000_000
    entries = [
        {
            "source": "qoder_cli",
            "model": "auto",
            "entry_id": "qoder-cli:credits",
            "timestamp": ts,
            "input": 1000,
            "output": 1,
            "cost": RATE,
            "costAuthoritative": True,
            "_billing": usage_billing_fixed(RATE),
        },
        {
            "source": "qoder_cli",
            "model": "grok-4.5",
            "provider": "xai",
            "entry_id": "qoder-cli:tokens",
            "timestamp": ts + 1000,
            "input": 1_000_000,
            "output": 0,
            "cost": 2.0,
            "costAuthoritative": False,
            "_billing": usage_billing_pricing(["grok-4.5"], input_tokens=1_000_000),
        },
    ]
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.sync_source(
        "qoder_cli",
        build_source_signature(files=[["a", 1, 1]], parser={"v": 1}),
        lambda: entries,
    )
    # a pricing DB where grok-4.5 input doubles to $4/M
    db_path = tmp_path / "pricing.json"
    db_path.write_text(json.dumps({
        "models": {
            "grok-4.5": {
                "provider": "xai",
                "input": 4.0,
                "output": 6.0,
                "cache_read": 0.5,
                "cache_write": 2.0,
                "unit": "per_million_tokens",
            }
        },
        "aliases": {},
    }), encoding="utf-8")
    assert store.apply_pricing("identity-v2", PricingDatabase(db_path=db_path)) is True

    rows = {e["entry_id"]: e for e in store.query_entries()}
    assert rows["qoder-cli:tokens"]["cost"] == pytest.approx(4.0)
    assert rows["qoder-cli:credits"]["cost"] == pytest.approx(RATE)  # never repriced


# ------------------------------------------------------------------ free rows


def test_free_request_on_every_read_path(monkeypatch, tmp_path):
    """Pinned model, real tokens, credits: 0, billable: false.

    The recorded zero must survive live parsing, the query_entries round
    trip, the production aggregate_entries SQL path, and the Stats
    contribution query -- while a credits-absent twin is priced.
    """
    from tokdash.compute import parse_entries_json

    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _t_line("free", "2026-08-21T12:00:00.000Z", model="grok-4.5", in_t=1_000_000, credits=0, billable=False),
        _t_line("paid", "2026-08-21T12:01:00.000Z", model="grok-4.5", in_t=1_000_000),
    ])
    parser = _parser(monkeypatch, tmp_path, [root])
    raw = parser.collect(None, None)
    by_id = {e["entry_id"]: e for e in raw}
    assert by_id["qoder-cli:free"]["cost"] == 0.0
    assert by_id["qoder-cli:free"]["costAuthoritative"] is True
    assert by_id["qoder-cli:paid"]["cost"] == pytest.approx(2.0)

    # (a) live parse_entries_json
    live = parse_entries_json({"entries": [public_usage_entry(e) for e in raw]})
    model_rows = {m["name"]: m for m in live["apps"]["qoder_cli"]["models"]}
    # one grouped model row: the free row stays 0.0, the paid row prices at $2
    assert model_rows["grok-4.5"]["cost"] == pytest.approx(2.0)
    assert model_rows["grok-4.5"]["tokens_in"] == 2_000_000

    # (b) query_entries round trip + stored column
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    _sync(store, parser)
    rows = {e["entry_id"]: e for e in store.query_entries()}
    assert rows["qoder-cli:free"]["costAuthoritative"] is True
    assert rows["qoder-cli:free"]["cost"] == 0.0
    import sqlite3 as _sqlite3

    with _sqlite3.connect(store.path) as conn:
        conn.row_factory = _sqlite3.Row
        cols = {
            r["entry_key"]: r["cost_authoritative"]
            for r in conn.execute(
                "SELECT entry_key, cost_authoritative FROM usage_entries WHERE source = 'qoder_cli'"
            ).fetchall()
        }
    assert cols["qoder-cli:free"] == 1
    assert cols["qoder-cli:paid"] == 0

    # (c) production aggregate_entries (mixed group, same model)
    data = store.aggregate_entries(sources=["qoder_cli"])
    assert data["apps"]["qoder_cli"]["cost"] == pytest.approx(2.0)
    assert data["apps"]["qoder_cli"]["tokens_in"] == 2_000_000

    # (d) Stats contribution query
    ts = min(e["timestamp"] for e in raw)
    days = store.contribution_days(
        sources=["qoder_cli"],
        since=datetime.fromtimestamp(ts / 1000 - 1, timezone.utc),
        until=datetime.fromtimestamp(ts / 1000 + 120, timezone.utc),
    )
    assert len(days) == 1
    assert days[0]["totals"]["cost"] == pytest.approx(2.0)


# ----------------------------------------------------------- runtime signature


def test_runtime_signature_unset_vs_explicit(monkeypatch, tmp_path):
    parser = _parser(monkeypatch, tmp_path, [])
    assert parser.runtime_config_signature() == {"usd_per_credit": None, "context_window": None}
    # explicit 180000 != unset: the override applies to every model
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", "180000")
    assert parser.runtime_config_signature() == {"usd_per_credit": None, "context_window": 180000}
    monkeypatch.setenv("QODER_USD_PER_CREDIT", "0.02")
    assert parser.runtime_config_signature() == {"usd_per_credit": 0.02, "context_window": 180000}


@pytest.mark.parametrize("rate", ["abc", "nan", "-5", "0", "1e999"])
def test_invalid_rate_falls_back_to_default(monkeypatch, tmp_path, rate):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", credits=1.0, ratio=1000 / 180000)])
    monkeypatch.setenv("QODER_USD_PER_CREDIT", rate)
    parser = _parser(monkeypatch, tmp_path, [root])
    assert parser.runtime_config_signature() == {"usd_per_credit": None, "context_window": None}
    entries = parser.collect(None, None)
    assert abs(entries[0]["cost"] - 1.0 * RATE) < 1e-15  # default still applies


@pytest.mark.parametrize("window", ["abc", "nan", "-1", "0", "12.5"])
def test_invalid_window_falls_back_to_unset(monkeypatch, tmp_path, window):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", ratio=0.05)])
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", window)
    parser = _parser(monkeypatch, tmp_path, [root])
    assert parser.runtime_config_signature() == {"usd_per_credit": None, "context_window": None}
    entries = parser.collect(None, None)
    assert len(entries) == 1
    assert entries[0]["input"] == 9000  # auto-only default window still applies


# -------------------------------------------------------------------- identity


def test_cli_parser_identity(monkeypatch, tmp_path):
    from tokdash.sources.coding_tools import QoderIdeParser
    from tokdash.usage_store import USAGE_ENTRY_FORMAT_VERSION

    parser = _parser(monkeypatch, tmp_path, [])
    assert parser.persistent_parser_version == 1
    sig = parser.persistent_parser_signature()
    assert sig["object"].endswith("QoderCliParser")
    assert sig["version"] == 1
    assert sig["entry_format"] == USAGE_ENTRY_FORMAT_VERSION
    # the IDE parser stays None: source_native_db, live only
    assert QoderIdeParser.persistent_parser_version is None
