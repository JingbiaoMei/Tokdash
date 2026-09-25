"""Tests for QoderCliParser (Qoder CLI transcripts + segments)."""
import builtins
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import (
    BaseParser,
    QoderCliParser,
    qoder_cli_unattributed_warning_reset,
)
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
    # The unattributed warning speaks once per (model, reason) per process, so
    # a fixture that does not re-arm it makes the second warning test in the
    # session pass for the wrong reason.
    BaseParser._entry_cache.clear()
    qoder_cli_unattributed_warning_reset()
    yield
    BaseParser._entry_cache.clear()
    qoder_cli_unattributed_warning_reset()


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
            [
                json.loads(line)
                for line in (FIXTURES / transcript).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ],
        )
    if segment:
        _write_lines(
            root / "logs" / "sessions" / "proj1" / "7255bada-539e-4ca0-bcf2-0b23aed031c3" / "segments" / "seg.jsonl",
            [
                json.loads(line)
                for line in (FIXTURES / segment).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ],
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
    assert parser.runtime_config_signature() == {
        "usd_per_credit": None, "context_window": None, "context_windows": ()}
    # explicit 180000 != unset: the override applies to every model
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", "180000")
    assert parser.runtime_config_signature() == {
        "usd_per_credit": None, "context_window": 180000, "context_windows": ()}
    monkeypatch.setenv("QODER_USD_PER_CREDIT", "0.02")
    assert parser.runtime_config_signature() == {
        "usd_per_credit": 0.02, "context_window": 180000, "context_windows": ()}


@pytest.mark.parametrize("rate", ["abc", "nan", "-5", "0", "1e999"])
def test_invalid_rate_falls_back_to_default(monkeypatch, tmp_path, rate):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", credits=1.0, ratio=1000 / 180000)])
    monkeypatch.setenv("QODER_USD_PER_CREDIT", rate)
    parser = _parser(monkeypatch, tmp_path, [root])
    assert parser.runtime_config_signature() == {
        "usd_per_credit": None, "context_window": None, "context_windows": ()}
    entries = parser.collect(None, None)
    assert abs(entries[0]["cost"] - 1.0 * RATE) < 1e-15  # default still applies


@pytest.mark.parametrize("window", ["abc", "nan", "-1", "0", "12.5"])
def test_invalid_window_falls_back_to_unset(monkeypatch, tmp_path, window):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", ratio=0.05)])
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", window)
    parser = _parser(monkeypatch, tmp_path, [root])
    assert parser.runtime_config_signature() == {
        "usd_per_credit": None, "context_window": None, "context_windows": ()}
    entries = parser.collect(None, None)
    assert len(entries) == 1
    assert entries[0]["input"] == 9000  # auto-only default window still applies


# -------------------------------------------------------------------- identity


def test_cli_parser_identity(monkeypatch, tmp_path):
    from tokdash.sources.coding_tools import QoderIdeParser
    from tokdash.usage_store import USAGE_ENTRY_FORMAT_VERSION

    parser = _parser(monkeypatch, tmp_path, [])
    assert parser.persistent_parser_version == 2
    sig = parser.persistent_parser_signature()
    assert sig["object"].endswith("QoderCliParser")
    assert sig["version"] == 2
    assert sig["entry_format"] == USAGE_ENTRY_FORMAT_VERSION
    # the IDE parser stays None: source_native_db, live only
    assert QoderIdeParser.persistent_parser_version is None


def test_tracker_registers_qoder_sources():
    from tokdash.sources.coding_tools import (
        CodingToolsUsageTracker,
        QoderIdeParser,
    )

    tracker = CodingToolsUsageTracker()
    assert isinstance(tracker.parsers["qoder"], QoderIdeParser)
    assert isinstance(tracker.parsers["qoder_cli"], QoderCliParser)
    assert tracker.parsers["qoder_cli"].persistent_parser_version == 2
    assert tracker.parsers["qoder"].persistent_parser_version is None


def test_qoder_frontend_source_labels_distinguish_ide_and_cli():
    source = (
        Path(__file__).parents[1] / "src" / "tokdash" / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert "qoder: 'Qoder IDE'" in source
    assert "qoder_cli: 'Qoder CLI'" in source


# ------------------------------------------------- per-model context windows
#
# Shapes below are captured from Qoder CLI 1.1.28 and 1.1.63 on Linux/WSL.
# The run-log line is faithful in shape (shortened to the fields the parser
# reads): one session reported qfmodel @ 180000 and lite @ 200000. The
# qfmodel ratio 0.11495555555555556 below is a real captured value, and
# 0.11495555555555556 * 180000 is exactly 20692.


def _run_log(root: Path, run_id: str, text: str) -> Path:
    path = Path(root) / "logs" / "runs" / run_id / "qodercli.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _cfg_line(key, window, display="x"):
    return (
        "2026-09-24T17:31:58.738+01:00 INFO  debug.message "
        "[QoderInferRequest details] model_config="
        '{"key":"%s","display_name":"%s","format":"openai",'
        '"api_key":"[redacted]","max_input_tokens":%d}' % (key, display, window)
    )


def test_window_table_reads_qoder_run_logs(monkeypatch, tmp_path):
    from tokdash.sources.coding_tools import qoder_cli_window_table

    root = tmp_path / "root"
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1",
             _cfg_line("qfmodel", 180000, "Qwen3.8-Flash") + "\n"
             + _cfg_line("lite", 200000) + "\n")
    assert qoder_cli_window_table([root]) == {"qfmodel": 180000, "lite": 200000}


def test_pinned_model_recovers_from_run_log_evidence(monkeypatch, tmp_path):
    """The reported bug: a pinned model zero-fills every token field, bills in
    credits, and is not `auto`, so it used to vanish from the dashboard."""
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _t_line("X", "2026-09-24T16:32:31.195Z", model="qfmodel",
                credits=0.18696785714285713, ratio=0.11495555555555556),
    ])
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1",
             _cfg_line("qfmodel", 180000, "Qwen3.8-Flash") + "\n")

    parser = _parser(monkeypatch, tmp_path, [root])
    entries = parser.collect(None, None)
    assert len(entries) == 1
    assert entries[0]["input"] == 20692  # 0.114955... * 180000, exact
    assert entries[0]["model"] == "qfmodel"
    assert entries[0]["costAuthoritative"] is True


def test_env_override_wins_over_run_log(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-09-24T16:32:31.195Z", model="qfmodel",
                          credits=1.0, ratio=0.10)])
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1", _cfg_line("qfmodel", 180000) + "\n")
    parser = _parser(monkeypatch, tmp_path, [root])
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", "100000")
    entries = parser.collect(None, None)
    assert entries[0]["input"] == 10000


def test_auto_falls_back_without_any_run_log(monkeypatch, tmp_path):
    """No evidence anywhere: `auto` keeps its documented 180k, nothing else guesses."""
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _t_line("A", "2026-08-21T12:00:00.000Z", model="auto", credits=1.0, ratio=0.05),
        _t_line("B", "2026-08-21T12:00:01.000Z", model="qmodel_38max", credits=1.0, ratio=0.05),
    ])
    entries = _parser(monkeypatch, tmp_path, [root]).collect(None, None)
    assert [e["model"] for e in entries] == ["auto"]
    assert entries[0]["input"] == 9000


def test_unattributed_billed_model_is_named(monkeypatch, tmp_path, caplog):
    """A billed model that has to be dropped must say which one it dropped."""
    import logging
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", model="qmodel_38max",
                          credits=2.0, ratio=0.05)])
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tokdash.sources.coding_tools"):
        assert _parser(monkeypatch, tmp_path, [root]).collect(None, None) == []
    assert "qmodel_38max" in caplog.text


def test_free_records_do_not_raise_the_warning(monkeypatch, tmp_path, caplog):
    """credits: 0 / billable: false is a free request, not a lost one."""
    import logging
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", model="qmodel_38max",
                          credits=0.0, billable=False, ratio=0.05)])
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tokdash.sources.coding_tools"):
        _parser(monkeypatch, tmp_path, [root]).collect(None, None)
    assert "no attributable tokens" not in caplog.text


def test_malformed_run_logs_are_ignored(monkeypatch, tmp_path):
    from tokdash.sources.coding_tools import qoder_cli_window_table

    root = tmp_path / "root"
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1",
             "no model_config here\n"
             'model_config={"key":"broken"\n'                       # truncated
             'model_config={"key":"zero","max_input_tokens":0}\n'   # not positive
             'model_config={"max_input_tokens":180000}\n'           # no key
             'model_config={"key":"ok","max_input_tokens":131072}\n')
    assert qoder_cli_window_table([root]) == {"ok": 131072}


def test_latest_run_wins(monkeypatch, tmp_path):
    from tokdash.sources.coding_tools import qoder_cli_window_table

    root = tmp_path / "root"
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1", _cfg_line("qfmodel", 180000) + "\n")
    _run_log(root, "2026-09-25T15-05-54-438+01-00-b-p2", _cfg_line("qfmodel", 262144) + "\n")
    assert qoder_cli_window_table([root]) == {"qfmodel": 262144}


def test_run_logs_are_never_usage_files(monkeypatch, tmp_path):
    """The run log feeds the window table only; it must never reach the
    candidate builders, which read JSONL."""
    from tokdash.sources.coding_tools import qoder_cli_discovered_files

    root = tmp_path / "root"
    log = _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1", _cfg_line("qfmodel", 180000) + "\n")
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", credits=1.0, in_t=5)])
    found = qoder_cli_discovered_files([root])
    assert log not in found
    assert [f.name for f in found] == ["aaaaaaaa-bbbb.jsonl"]


def test_window_table_changes_the_cache_identity(monkeypatch, tmp_path):
    """A run log that appears must invalidate the stored rows, like the override."""
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-08-21T12:00:00.000Z", model="qfmodel",
                          credits=1.0, ratio=0.05)])
    parser = _parser(monkeypatch, tmp_path, [root])
    assert parser.runtime_config_signature()["context_windows"] == ()
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1", _cfg_line("qfmodel", 180000) + "\n")
    parser.roots = [root]
    assert parser.runtime_config_signature()["context_windows"] == (("qfmodel", 180000),)


# -------------------------------------------- session-declared context window
#
# `--context-window <size>` reaches disk as a runtime-config record in the
# transcript, and the captured ratio follows it: 20783 input tokens at ratio
# 0.15856170654296875 is exactly 131072. An unset window arrives as null.


def _rc_line(window, model="vllm-hpc/qwen3.8-flash-next", ts="2026-09-25T14:10:00.000Z"):
    return {"type": "runtime-config", "sessionId": "s1", "model": model,
            "reasoningEffort": None, "contextWindow": window, "timestamp": ts}


# input/ratio pair captured from a real custom-provider request
_REAL_IN = 20783
_REAL_RATIO_AT_131072 = 0.15856170654296875


def test_session_declared_window_wins_over_run_log(monkeypatch, tmp_path):
    """A window the session declared itself outranks the run-log table."""
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _rc_line(131072),
        _t_line("X", "2026-09-25T14:10:01.000Z", model="qfmodel",
                credits=1.0, ratio=_REAL_RATIO_AT_131072),
    ])
    _run_log(root, "2026-09-25T15-05-54-438+01-00-b-p2", _cfg_line("qfmodel", 180000) + "\n")
    entries = _parser(monkeypatch, tmp_path, [root]).collect(None, None)
    assert len(entries) == 1
    assert entries[0]["input"] == _REAL_IN  # exactly, at the declared 131072


def test_null_session_window_falls_through_to_the_table(monkeypatch, tmp_path):
    """contextWindow: null means the model's own window, not a zero window."""
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _rc_line(None, model="qfmodel"),
        _t_line("X", "2026-09-24T16:32:31.195Z", model="qfmodel",
                credits=1.0, ratio=0.11495555555555556),
    ])
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1", _cfg_line("qfmodel", 180000) + "\n")
    entries = _parser(monkeypatch, tmp_path, [root]).collect(None, None)
    assert entries[0]["input"] == 20692


def test_session_window_tracks_the_file(monkeypatch, tmp_path):
    """A mid-file window change applies to the requests after it, not before."""
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _rc_line(None, model="qfmodel"),
        _t_line("A", "2026-09-24T16:00:00.000Z", model="qfmodel",
                credits=1.0, ratio=0.11495555555555556),   # table: 180000 -> 20692
        _rc_line(131072, model="qfmodel"),
        _t_line("B", "2026-09-24T16:00:01.000Z", model="qfmodel",
                credits=1.0, ratio=_REAL_RATIO_AT_131072),  # declared: -> 20783
    ])
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1", _cfg_line("qfmodel", 180000) + "\n")
    entries = {e["entry_id"]: e["input"] for e in
               _parser(monkeypatch, tmp_path, [root]).collect(None, None)}
    assert entries == {"qoder-cli:A": 20692, "qoder-cli:B": 20783}


def test_env_override_still_beats_a_declared_window(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _rc_line(131072),
        _t_line("X", "2026-09-25T14:10:01.000Z", model="qfmodel", credits=1.0, ratio=0.5),
    ])
    parser = _parser(monkeypatch, tmp_path, [root])
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", "100000")
    assert parser.collect(None, None)[0]["input"] == 50000


def test_declared_window_needs_no_run_log_at_all(monkeypatch, tmp_path):
    """Qoder prunes run logs; the transcript survives, so does the accounting."""
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _rc_line(262144, model="qfmodel"),
        _t_line("X", "2026-09-25T14:10:01.000Z", model="qfmodel",
                credits=1.0, ratio=0.25),
    ])
    assert _parser(monkeypatch, tmp_path, [root]).collect(None, None)[0]["input"] == 65536


# --------------------------------------- run-log evidence: order, caching, advice
#
# These cover the review round on the window table: the sort must decode the
# offset rather than trust the string, the parse must be memoised per file, and
# a dropped record must be told with advice that can actually help.


def test_dst_fall_back_orders_runs_by_utc_instant(monkeypatch, tmp_path):
    """A fall-back transition must not hand "latest run wins" to the stale window.

    Both ids read 02:30 local, one hour apart in real time: at +02:00 that is
    00:30Z, and after the clocks go back +01:00 makes it 01:30Z. A
    lexicographic sort compares the offset digits and puts the +01-00 id first
    ("1" < "2"), so the OLDER run sorts last and wins the table. The mtimes are
    reversed by hand below, so an mtime fallback loses here too: only a decoded
    instant picks the newer window.
    """
    import os
    from tokdash.sources.coding_tools import qoder_cli_run_log_files, qoder_cli_window_table

    root = tmp_path / "root"
    earlier = _run_log(root, "2026-10-25T02-30-00-000+02-00-a-p1",
                       _cfg_line("qfmodel", 180000) + "\n")   # 00:30Z
    later = _run_log(root, "2026-10-25T02-30-00-000+01-00-a-p2",
                     _cfg_line("qfmodel", 262144) + "\n")     # 01:30Z
    # Reverse the mtimes: an mtime-ordered fallback picks the WRONG winner, so
    # a passing test proves the offset in the id is what decided.
    st = later.stat()
    os.utime(later, ns=(st.st_atime_ns - 3_600_000_000_000,
                        st.st_mtime_ns - 3_600_000_000_000))

    assert [p.parent.name for p in qoder_cli_run_log_files([root])] == [
        "2026-10-25T02-30-00-000+02-00-a-p1",
        "2026-10-25T02-30-00-000+01-00-a-p2",
    ]
    assert qoder_cli_window_table([root]) == {"qfmodel": 262144}


def test_unreadable_run_id_falls_back_to_mtime(monkeypatch, tmp_path):
    """An id we cannot parse still lands in mtime order, not an arbitrary slot."""
    from tokdash.sources.coding_tools import qoder_cli_run_log_files

    root = tmp_path / "root"
    _run_log(root, "not-a-timestamp-a-p1", _cfg_line("qfmodel", 180000) + "\n")
    _run_log(root, "not-a-timestamp-a-p2", _cfg_line("qfmodel", 262144) + "\n")
    # lexicographically p1 < p2 and the mtimes are in the same order, so this
    # asserts only that the fallback is stable and total, which is the promise.
    assert [p.parent.name for p in qoder_cli_run_log_files([root])] == [
        "not-a-timestamp-a-p1", "not-a-timestamp-a-p2",
    ]


def _counting_scans(monkeypatch, ct):
    """Wrap the run-log reader so tests count ACTUAL re-reads, not cache hits.

    The reader is looked up through the module on every call, which is what
    makes this patch visible to the memo -- and the memo is cleared first so
    each test starts from a cold, own-corpus state.
    """
    scans: list = []
    real = ct._qoder_cli_scan_run_log_windows

    def counting(path_str):
        scans.append(Path(path_str).parent.name)
        return real(path_str)

    monkeypatch.setattr(ct, "_qoder_cli_scan_run_log_windows", counting)
    ct.qoder_cli_run_log_window_memo_clear()
    return scans


def test_unchanged_run_logs_are_not_re_read(monkeypatch, tmp_path):
    """A live session grows ONE log; the others must not be re-scanned.

    The table used to be memoised on the whole file set, so every refresh while
    Qoder was running re-read every retained log. Per-file memoisation is the
    fix, and the scan counter is the direct evidence of it.
    """
    import os
    from tokdash.sources import coding_tools as ct

    root = tmp_path / "root"
    live = _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1",
                    _cfg_line("qfmodel", 180000) + "\n")
    _run_log(root, "2026-09-25T15-05-54-438+01-00-b-p2",
             _cfg_line("lite", 200000) + "\n")
    scans = _counting_scans(monkeypatch, ct)

    assert ct.qoder_cli_window_table([root]) == {"qfmodel": 180000, "lite": 200000}
    assert sorted(scans) == ["2026-09-24T17-29-24-649+01-00-a-p1",
                             "2026-09-25T15-05-54-438+01-00-b-p2"]

    scans.clear()
    assert ct.qoder_cli_window_table([root]) == {"qfmodel": 180000, "lite": 200000}
    assert scans == [], "an unchanged log must not be re-read"

    with live.open("a", encoding="utf-8") as handle:
        handle.write(_cfg_line("qmodel_38max", 180000) + "\n")
    st = live.stat()
    os.utime(live, ns=(st.st_atime_ns + 20_000_000, st.st_mtime_ns + 20_000_000))
    assert ct.qoder_cli_window_table([root]) == {
        "qfmodel": 180000, "lite": 200000, "qmodel_38max": 180000,
    }
    assert scans == ["2026-09-24T17-29-24-649+01-00-a-p1"], "only the log that changed"


def test_a_large_log_corpus_does_not_thrash_the_memo(monkeypatch, tmp_path):
    """Past any cap, a live log must still cost ONE re-read per refresh.

    The memo was an lru_cache(maxsize=256) over (path, mtime_ns, size), and a
    growing log produces a new signature for the SAME path on every poll. With
    more retained logs than the cap, those generations evicted the logs the
    merge had not reached yet, so one touch of one log re-read all 300 -- the
    per-poll full re-scan this memo exists to remove, resurrected by corpus
    size. 300 is above the old cap; the promise is about ANY count.
    """
    import os
    from tokdash.sources import coding_tools as ct

    root = tmp_path / "big"
    for i in range(300):
        _run_log(root, "2026-01-%02dT10-00-00-000+00-00-p%03d" % (i % 28 + 1, i),
                 _cfg_line("model_%03d" % i, 180000) + "\n")
    scans = _counting_scans(monkeypatch, ct)

    table = ct.qoder_cli_window_table([root])
    assert len(table) == 300
    assert len(scans) == 300, "cold read is one per file"

    live = sorted((root / "logs" / "runs").glob("*/qodercli.log"))[-1]
    for i in range(5):
        with live.open("a", encoding="utf-8") as handle:
            handle.write(_cfg_line("qfmodel", 180000) + "\n")
        st = live.stat()
        os.utime(live, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000 * (i + 1)))
        scans.clear()
        table = ct.qoder_cli_window_table([root])
        assert table["qfmodel"] == 180000
        assert len(scans) == 1, "one changed log must cost one re-read, not 300"


def test_pruned_run_logs_leave_the_memo(monkeypatch, tmp_path):
    """Qoder prunes run logs; the memo cannot outgrow the corpus it mirrors."""
    from tokdash.sources import coding_tools as ct

    root = tmp_path / "root"
    kept = _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1",
                    _cfg_line("qfmodel", 180000) + "\n")
    pruned = _run_log(root, "2026-09-25T15-05-54-438+01-00-b-p2",
                      _cfg_line("lite", 200000) + "\n")
    _counting_scans(monkeypatch, ct)
    assert len(ct.qoder_cli_window_table([root])) == 2
    assert len(ct._QODER_RUN_LOG_WINDOW_MEMO) == 2

    pruned.unlink()
    pruned.parent.rmdir()
    assert ct.qoder_cli_window_table([root]) == {"qfmodel": 180000}
    assert list(ct._QODER_RUN_LOG_WINDOW_MEMO) == [str(kept)]


def test_prompt_text_cannot_become_a_context_window(monkeypatch, tmp_path):
    """The scan stops at the config object, never at end of line.

    A real line ends `...}, custom_model=null` and the surrounding log line
    carries prompt text, so a search past the object could read a prompt that
    quotes "max_input_tokens" as the window -- and every token count divided by
    that window would look like a measurement.
    """
    from tokdash.sources.coding_tools import qoder_cli_window_table

    root = tmp_path / "root"
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1",
             # no max_input_tokens in the config; the payload quotes one
             '2026-09-24T17:31:58.738+01:00 INFO debug.message [QoderInferRequest '
             'details] model_config={"key":"qfmodel","display_name":"Qwen3.8-Flash"}, '
             'prompt="the docs say \"max_input_tokens\": 999999 here", custom_model=null\n'
             # a brace inside a string must not end the object early
             + _cfg_line("lite", 200000, display="a{b}c") + ", custom_model=null\n"
             # truncated payload: no close, so no evidence, so nothing
             + 'model_config={"key":"truncated","max_input_tokens":12345\n')
    assert qoder_cli_window_table([root]) == {"lite": 200000}


def test_unreadable_context_window_value_keeps_the_declaration(monkeypatch, tmp_path):
    """contextWindow: "131072" is an unreadable value, not a claim of "unset".

    A present-but-unparseable value used to read as an explicit null and clear
    the --context-window declared earlier in the file, dropping the recovered
    input to the run-log window or to nothing. That is the same lost
    declaration the absent-key case was fixed for.
    """
    root = tmp_path / "root"
    typed = {"type": "runtime-config", "sessionId": "s1", "model": "qfmodel",
             "contextWindow": "131072", "timestamp": "2026-09-25T14:10:00.500Z"}
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _rc_line(131072, model="qfmodel"),
        _t_line("A", "2026-09-25T14:10:01.000Z", model="qfmodel",
                credits=1.0, ratio=_REAL_RATIO_AT_131072),
        typed,
        _t_line("B", "2026-09-25T14:10:02.000Z", model="qfmodel",
                credits=1.0, ratio=_REAL_RATIO_AT_131072),
        # a real null still means "no window is set", straight to the table
        _rc_line(None, model="qfmodel"),
        _t_line("C", "2026-09-25T14:10:03.000Z", model="qfmodel",
                credits=1.0, ratio=0.11495555555555556),
    ])
    _run_log(root, "2026-09-25T15-05-54-438+01-00-b-p2", _cfg_line("qfmodel", 180000) + "\n")
    entries = {e["entry_id"]: e["input"] for e in
               _parser(monkeypatch, tmp_path, [root]).collect(None, None)}
    assert entries == {
        "qoder-cli:A": _REAL_IN,      # declared 131072
        "qoder-cli:B": _REAL_IN,      # the typed value did not clear it
        "qoder-cli:C": 20692,         # the null did: table window 180000
    }


def test_absent_context_window_key_keeps_the_declared_window(monkeypatch, tmp_path):
    """A runtime-config record WITHOUT the key says nothing; null says "unset".

    A model-switch record that omits contextWindow used to read as null and
    silently clear a --context-window declared earlier in the file, which is
    the same wrong-window class this parser exists to fix.
    """
    root = tmp_path / "root"
    silent = {"type": "runtime-config", "sessionId": "s1",
              "model": "qfmodel", "timestamp": "2026-09-25T14:10:00.500Z"}
    assert "contextWindow" not in silent
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _rc_line(131072, model="qfmodel"),
        _t_line("A", "2026-09-25T14:10:01.000Z", model="qfmodel",
                credits=1.0, ratio=_REAL_RATIO_AT_131072),
        silent,
        _t_line("B", "2026-09-25T14:10:02.000Z", model="qfmodel",
                credits=1.0, ratio=_REAL_RATIO_AT_131072),
    ])
    _run_log(root, "2026-09-25T15-05-54-438+01-00-b-p2", _cfg_line("qfmodel", 180000) + "\n")
    entries = {e["entry_id"]: e["input"] for e in
               _parser(monkeypatch, tmp_path, [root]).collect(None, None)}
    # Both stay at the declared 131072; had the key's absence cleared it, B
    # would divide at the run-log 180000 instead.
    assert entries == {"qoder-cli:A": _REAL_IN, "qoder-cli:B": _REAL_IN}


def test_no_ratio_record_is_not_advised_the_window_override(monkeypatch, tmp_path, caplog):
    """QODER_CLI_CONTEXT_WINDOW cannot recover a record with no ratio to multiply."""
    import logging
    root = tmp_path / "root"
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _t_line("A", "2026-09-24T16:32:31.195Z", model="qfmodel", credits=1.0),
        _t_line("B", "2026-09-24T16:32:32.195Z", model="unheard-of-model",
                credits=1.0, ratio=0.05),
        _t_line("C", "2026-09-24T16:32:33.195Z", model="missing-both-model",
                credits=1.0),
    ])
    # qfmodel HAS an evidenced window, so its drop is the no-ratio kind; the
    # second model has a ratio and no window, so it stays the settable kind; the
    # third is missing BOTH and must file under the ratio kind, because an
    # override supplies a multiplier to a record that has nothing to multiply.
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1", _cfg_line("qfmodel", 180000) + "\n")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tokdash.sources.coding_tools"):
        assert _parser(monkeypatch, tmp_path, [root]).collect(None, None) == []
    text = caplog.text
    assert "unheard-of-model" in text and "QODER_CLI_CONTEXT_WINDOW=<size>" in text
    assert "qfmodel" in text and "context_usage_ratio" in text
    # exactly one line offers the env var: the two causes split, they merge
    assert text.count("Pass QODER_CLI_CONTEXT_WINDOW=<size> to recover them.") == 1
    # and the compound drop is NOT in it -- setting the variable would change
    # the reason and not the outcome, which reads as a broken setting
    assert "missing-both-model" in text
    window_line = [r.getMessage() for r in caplog.records
                   if "QODER_CLI_CONTEXT_WINDOW=<size>" in r.getMessage()][0]
    assert "missing-both-model" not in window_line
    ratio_line = [r.getMessage() for r in caplog.records
                  if "context_usage_ratio" in r.getMessage()][0]
    assert "missing-both-model" in ratio_line and "qfmodel" in ratio_line


def test_invalid_window_warning_does_not_claim_auto_only_recovery(monkeypatch, tmp_path, caplog):
    """The advice text must match the current policy, not the pre-fix one."""
    import logging
    parser = _parser(monkeypatch, tmp_path, [])
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", "0")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tokdash.sources.coding_tools"):
        assert parser.runtime_config_signature()["context_window"] is None
    assert "auto-only" not in caplog.text
    assert "evidenced window" in caplog.text


def test_in_class_aliases_forward_every_parameter(monkeypatch, tmp_path):
    """The aliases must not drop session_window / unattributed.

    Nothing in-tree calls them, which is exactly why the drift was silent: a
    future caller through the alias would resolve a different window than the
    module function it forwards to, and tokens would change with no clue.
    """
    parser = _parser(monkeypatch, tmp_path, [])
    line = _t_line("X", "2026-09-25T14:10:01.000Z", model="qfmodel",
                   credits=1.0, ratio=_REAL_RATIO_AT_131072)

    assert parser._window_for("qfmodel", None, {}, 131072) == 131072
    assert parser._window_for("qfmodel", None, {"qfmodel": 180000}, 131072) == 131072
    assert parser._window_for("qfmodel", None, {"qfmodel": 180000}) == 180000

    unattributed: set = set()
    cand = parser._transcript_candidate(line, None, {}, unattributed, 131072)
    assert cand is not None and cand[1]["input"] == _REAL_IN

    # And the no-evidence path still reports through the alias, reason included.
    unattributed.clear()
    assert parser._transcript_candidate(line, None, {}, unattributed, None) is None
    assert unattributed == {("qfmodel", "window")}


# --------------------------------- evidence that refuses rather than breaks
# A field the CLI should never have written has two ways to do damage: it can
# become a number the dashboard reports, or it can raise its way out of the
# source. These cover both, plus the shared gate the three window sources pass.


def test_absent_config_cannot_borrow_a_window_from_the_prompt(monkeypatch, tmp_path):
    """model_config=null leaves no object to read, so nothing downstream is one.

    The anchor is the whole difference. A search for the NEXT brace lands on
    JSON the user pasted into the chat, which hands a model a window it never
    had -- and every later zero-filled record for that model key recovers
    ratio x that borrowed number, which reads as a measurement.
    """
    from tokdash.sources.coding_tools import qoder_cli_window_table

    root = tmp_path / "root"
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1",
             '2026-09-24T17:31:58.738+01:00 INFO debug.message [QoderInferRequest '
             'details] model_config=null, prompt="user pasted '
             '{"key":"qwen","max_input_tokens":8192}", custom_model=null\n')
    assert qoder_cli_window_table([root]) == {}

    # End to end: the borrowed 8192 would have recovered 0.05 x 8192 = 410
    # input tokens for a model that has no evidenced window at all.
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                 [_t_line("X", "2026-09-24T16:32:31.195Z", model="qwen",
                          credits=1.0, ratio=0.05)])
    assert _parser(monkeypatch, tmp_path, [root]).collect(None, None) == []


def test_config_object_after_a_space_is_still_evidence(monkeypatch, tmp_path):
    """Anchoring the payload must not lose a real log that spaced the marker.

    raw_decode does not skip leading whitespace the way json.loads does, so
    decoding has to start at the brace rather than at the gap before it.
    """
    from tokdash.sources.coding_tools import qoder_cli_window_table

    root = tmp_path / "root"
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1",
             'INFO [QoderInferRequest details] model_config= '
             '{"key":"qfmodel","max_input_tokens":180000}, custom_model=null\n')
    assert qoder_cli_window_table([root]) == {"qfmodel": 180000}


def test_hostile_payloads_cost_their_line_not_the_file(monkeypatch, tmp_path):
    """One unreadable line must not silence the run's other evidence.

    A 400-digit max_input_tokens overflows the float conversion (OverflowError,
    not ValueError), and a payload nested a few thousand deep overflows the
    decoder itself (RecursionError). Either one used to be raised out of the
    scan, through the cache signature and into the dashboard request, which is
    the opposite of the contract on qoder_cli_window_table().
    """
    from tokdash.sources.coding_tools import qoder_cli_window_table

    root = tmp_path / "root"
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1",
             'model_config={"key":"huge","max_input_tokens":%s}\n' % ("9" * 400)
             + 'model_config={"key":"deep","meta":{"a":' + "[" * 6000 + "}}\n"
             + _cfg_line("lite", 200000) + "\n")
    assert qoder_cli_window_table([root]) == {"lite": 200000}
    # ...and the ordering must not matter: the good line is kept whether the
    # hostile one came before or after it.
    other = tmp_path / "other"
    _run_log(other, "2026-09-24T17-29-24-649+01-00-a-p1",
             _cfg_line("lite", 200000) + "\n"
             + 'model_config={"key":"huge","max_input_tokens":%s}\n' % ("9" * 400))
    assert qoder_cli_window_table([other]) == {"lite": 200000}


def test_absurd_windows_are_not_evidence_from_any_source(monkeypatch, tmp_path):
    """The ceiling is shared by the run log, the env var and the transcript.

    ratio x window is the recovery, so an unbounded window turns an ordinary
    ratio into a token count no endpoint could have billed: 0.05 x 1e30 is
    5e28 "input tokens". Refusing it costs the record the named warning;
    trusting it costs the dashboard its numbers.
    """
    from tokdash.sources.coding_tools import (
        qoder_cli_runtime_config,
        qoder_cli_window_table,
    )

    root = tmp_path / "root"
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1",
             'model_config={"key":"sentinel","max_input_tokens":1e30}\n'
             + 'model_config={"key":"overflow","max_input_tokens":1e400}\n'
             + 'model_config={"key":"big_but_real","max_input_tokens":1000000}\n')
    assert qoder_cli_window_table([root]) == {"big_but_real": 1000000}

    # The override is gated too: it is the one path a user can set by hand, and
    # an absurd value there multiplies EVERY model.
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", "20000000")
    assert qoder_cli_runtime_config()[1] is None
    monkeypatch.setenv("QODER_CLI_CONTEXT_WINDOW", "256000")
    assert qoder_cli_runtime_config()[1] == 256000

    # A declared window past the ceiling is an unreadable value, so it neither
    # adopts the sentinel nor clears what the file already declared.
    root2 = tmp_path / "root2"
    _run_log(root2, "2026-09-24T17-29-24-649+01-00-a-p1", _cfg_line("qfmodel", 180000) + "\n")
    _write_lines(root2 / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _rc_line(131072),
        _rc_line(1e30),
        _t_line("X", "2026-09-24T16:32:31.195Z", model="qfmodel",
                credits=1.0, ratio=_REAL_RATIO_AT_131072),
    ])
    entries = _parser(monkeypatch, tmp_path, [root2]).collect(None, None)
    assert [e["input"] for e in entries] == [_REAL_IN]  # still the declared 131072


def test_absurd_ratio_is_dropped_rather_than_crashing(monkeypatch, tmp_path):
    """context_usage_ratio: 10**400 is a valid JSON int and not a usable number.

    math.isfinite(10 ** 400) raises rather than returning False, and the
    recovery multiplies as a float, so the value had to be refused upstream: a
    field the CLI should never have written must cost that record, not the
    source.
    """
    root = tmp_path / "root"
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1", _cfg_line("qfmodel", 180000) + "\n")
    _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl", [
        _t_line("X", "2026-09-24T16:32:31.195Z", model="qfmodel",
                credits=1.0, ratio=int("9" * 400)),
    ])
    assert _parser(monkeypatch, tmp_path, [root]).collect(None, None) == []


def test_unattributed_warning_speaks_once_per_cause(monkeypatch, tmp_path, caplog):
    """A live session re-parses every poll; the advice must not scroll away.

    _parse_all runs on every cache miss, and a session in progress changes a
    transcript mtime between polls, so an ungoverned warning is the same block
    again every few seconds for as long as the run lasts -- to the one reader
    it was written for.
    """
    import logging
    import time

    root = tmp_path / "root"
    path = _write_lines(root / "projects" / "p" / "aaaaaaaa-bbbb.jsonl",
                        [_t_line("X", "2026-08-21T12:00:00.000Z", model="qmodel_38max",
                                 credits=2.0, ratio=0.05)])
    parser = _parser(monkeypatch, tmp_path, [root])
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tokdash.sources.coding_tools"):
        for _ in range(4):
            assert parser.collect(None, None) == []
            os.utime(path, ns=(time.time_ns() + 10 ** 9, time.time_ns() + 10 ** 9))
    assert caplog.text.count("Pass QODER_CLI_CONTEXT_WINDOW=<size> to recover them.") == 1


def test_merged_window_table_is_read_under_the_lock(monkeypatch, tmp_path):
    """A warm hit reads the merged table as one value, while holding the lock.

    The slot is a (signature, table) tuple in a module global, and serve and
    the TUI warmers run on their own threads and clear it. Checking element 0
    and returning element 1 as two separate global reads can straddle a clear
    and hand back an empty table for a signature the caller just vouched for,
    so a hit that acquires the lock zero times is reading it unguarded.
    """
    from tokdash.sources import coding_tools
    from tokdash.sources.coding_tools import qoder_cli_window_table

    root = tmp_path / "root"
    _run_log(root, "2026-09-24T17-29-24-649+01-00-a-p1", _cfg_line("qfmodel", 180000) + "\n")
    assert qoder_cli_window_table([root]) == {"qfmodel": 180000}  # warm the merge

    acquires = []
    real = threading.Lock()

    class _Probe:
        def __enter__(self):
            real.acquire()
            acquires.append(1)
            return self

        def __exit__(self, *exc):
            real.release()
            return False

    monkeypatch.setattr(coding_tools, "_QODER_RUN_LOG_WINDOW_LOCK", _Probe())
    assert qoder_cli_window_table([root]) == {"qfmodel": 180000}
    assert len(acquires) == 1
