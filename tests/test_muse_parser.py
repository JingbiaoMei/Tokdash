"""Muse (Meta Muse Code CLI) parser matrix.

Fixtures here are minimal and hand-built to the shape verified in
docs/local/20260909_muse_code_support/evidence/ (the offline echo capture, the
dumped MSP schemas, and the record/frame shape census): every value is inert,
no raw capture content is copied in, per the FINDINGS.md fixture rule. The
decisions that only the signed-in capture campaign can make — the
per-provider cache policy map (open item 1) and the serialized
estimate-marker key (leg (e)) — are set explicitly per test on the parser
instance; production ships the map empty / the key None and the source
unregistered until the capture decides.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tokdash import clientpaths
from tokdash.compute import _collect_parser_file
from tokdash.pricing import PricingDatabase
from tokdash.sources import coding_tools as ct
from tokdash.sources.coding_tools import BaseParser, MuseParser
from tokdash.usage_store import UsageEntryStore

SID = "11111111-1111-4111-8111-111111111111"
CID = "22222222-2222-4222-8222-222222222222"
RUN1 = "33333333-3333-4333-8333-333333333333"
RUN2 = "44444444-4444-4444-8444-444444444444"
# Microseconds, inside the fixed range and safely behind the file-relative
# ceiling a freshly-written fixture file gets (now + 1 day).
BASE_TS = 1_787_600_000_000_000

USAGE = {"input_tokens": 1000, "output_tokens": 200, "cached_tokens": 400, "reasoning_tokens": 50}

# Production ships MUSE_CACHE_POLICIES = {} (every provider unresolved ->
# every entry refused). Tests that don't care about the world use this
# both-included convenience map over the provider names the fixtures
# emit; world-sensitive tests pass their own map explicitly.
DEFAULT_POLICIES = {p: (True, True) for p in (
    "meta", "", "provider-a", "provider-b", "provider-meta", "provider-run1",
)}


def _envelope(rid: str, sid: str, seq: int, ts: int) -> dict:
    return {
        "schema_version": 1,
        "payload_schema_version": 1,
        "durability": "durable",
        "record_type": "event",
        "id": rid,
        "causation_id": None,
        "stream": {"kind": "session", "id": sid},
        "sequence": seq,
        "recorded_at": ts,
    }


def metadata_record(rid="rec-meta", sid=SID, seq=1, ts=BASE_TS, provider_id="meta", model_id=None) -> dict:
    rec = _envelope(rid, sid, seq, ts)
    rec["payload_type"] = "runtime.session.metadata"
    record = {"provider_id": provider_id, "workspace_root": "/work/muse-project"}
    if model_id is not None:
        record["model_id"] = model_id
    rec["payload"] = {"kind": "metadata", "record": record}
    return rec


def usage_record(rid, seq, usage=None, sid=SID, ts=BASE_TS + 1000, model=None, run_id=RUN1, kind="model_completed") -> dict:
    rec = _envelope(rid, sid, seq, ts)
    rec["payload_type"] = "runtime.session"
    event = {"kind": kind, "usage": dict(USAGE if usage is None else usage), "duration_ms": 10}
    if model is not None:
        event["model"] = model
    rec["payload"] = {
        "kind": "run",
        "event": event,
        "run_id": run_id,
        "source_run_record_id": f"{rid}-run",
    }
    return rec


def configured_record(rid, seq, model, run_id, sid=SID, ts=BASE_TS + 500) -> dict:
    rec = _envelope(rid, sid, seq, ts)
    rec["payload_type"] = "runtime.session"
    rec["payload"] = {
        "kind": "run",
        "event": {"kind": "model_request_configured", "model": model},
        "run_id": run_id,
    }
    return rec


def reconfigure_record(rid, seq, model=None, provider=None, sid=SID, ts=BASE_TS + 500, *, event_shape=False) -> dict:
    """The OFFICIAL durable event behind session/modelChanged (SDK
    manifest): runtime.model_reconfigure.completed. Its serialized payload
    positions are fixture-pending (capture leg (d2)), so this
    builds the two shapes the parser tolerates: the manifest name carried
    as payload_type with the new selection at payload level, or as a run
    payload whose event kind names the reconfigure, with the selection
    inside the event. Both are inert hand-built shapes."""
    rec = _envelope(rid, sid, seq, ts)
    fields = {}
    if model is not None:
        fields["model"] = model
    if provider is not None:
        fields["provider_id"] = provider
    if event_shape:
        rec["payload_type"] = "runtime.session"
        rec["payload"] = {
            "kind": "run",
            "event": dict(fields, kind="model_reconfigure.completed"),
            "run_id": "55555555-5555-4555-8555-555555555555",
        }
    else:
        rec["payload_type"] = "runtime.model_reconfigure.completed"
        rec["payload"] = dict(fields, kind="model_reconfigure")
    return rec


def configured_provider_record(rid, seq, model, provider, run_id, sid=SID, ts=BASE_TS + 500) -> dict:
    rec = _envelope(rid, sid, seq, ts)
    rec["payload_type"] = "runtime.session"
    rec["payload"] = {
        "kind": "run",
        "event": {"kind": "model_request_configured", "model": model, "provider_id": provider},
        "run_id": run_id,
    }
    return rec


def frame(outer, children) -> dict:
    """children: iterable of (child_index, record-dict-or-raw-string)."""
    return {
        "retained_frame": "session_permission_transaction",
        "frame_schema_version": 1,
        "outer_log_ordinal": outer,
        "transaction_id": "tx-1",
        "content_sha256": "deadbeef",
        "children": [
            {
                "child_index": idx,
                "record_json": r if isinstance(r, str) else json.dumps(r),
            }
            for idx, r in children
        ],
    }


def write_session(root: Path, uuid: str, records, date: str = "2026-09-09") -> Path:
    d = root.joinpath(*["muse", "sessions"] + date.split("-") + [uuid])
    d.mkdir(parents=True, exist_ok=True)
    path = d / "session.jsonl"
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def muse_parser(monkeypatch, tmp_path, *, policies=None, marker=None, pricing=None):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    BaseParser._entry_cache.clear()
    ct._sig_cache.clear()
    parser = MuseParser(pricing or PricingDatabase())
    parser.cache_policies = dict(DEFAULT_POLICIES if policies is None else policies)
    parser.estimate_marker_key = marker
    return parser


def parse(parser):
    return parser._parse_all()


def by_id(entries):
    return {e["entry_id"]: e for e in entries}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discovery_reaches_old_date_shard_and_skips_dot_dirs(monkeypatch, tmp_path):
    """Every date shard, no window (a first sync must index the whole
    history), while the .msp-view-v1 view cache — which sits alongside real
    logs and holds non-transcript copies — is skipped."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-1", 2)], date="2026-01-01")
    # A view-cache copy under a dot-directory with the SAME stream id: it
    # would pass the stream filter, so discovery itself must exclude it.
    cache_dir = tmp_path / "muse" / "sessions" / ".msp-view-v1" / SID
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "session.jsonl").write_text(
        json.dumps(usage_record("rec-mirror", 9, dict(USAGE), ts=BASE_TS + 1)), encoding="utf-8"
    )
    parser = muse_parser(monkeypatch, tmp_path)
    entries = parse(parser)
    assert [e["entry_id"] for e in entries] == ["muse:rec-1"]


def test_child_log_counted_and_parent_mirror_dropped(monkeypatch, tmp_path):
    """Children write subagent/<uuid>/session.jsonl and the parent log
    mirrors child run streams under the CHILD's stream id. Excluding nested
    logs loses real spend; keeping the mirrored copies double counts — the
    stream filter (stream.id == parent directory name) is the discriminator
    that does neither: the mirror falls out of the parent, the child's own
    file counts."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [
        metadata_record(),
        usage_record("rec-parent", 2),
        # The mirrored copy of the child's call: same usage, child's stream id.
        usage_record("rec-child-mirror", 3, dict(USAGE), sid=CID),
    ])
    child_dir = tmp_path / "muse" / "sessions" / "2026" / "09" / "09" / SID / "subagent" / CID
    child_dir.mkdir(parents=True, exist_ok=True)
    (child_dir / "session.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [
            metadata_record(rid="rec-child-meta", sid=CID, provider_id="meta"),
            usage_record("rec-child", 2, dict(USAGE), sid=CID),
        ]) + "\n",
        encoding="utf-8",
    )
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert set(entries) == {"muse:rec-parent", "muse:rec-child"}
    # The child log's provider comes from the child's own metadata record.
    assert entries["muse:rec-child"]["provider"] == "meta"


def test_relative_xdg_data_home_is_ignored(monkeypatch, tmp_path):
    """The Base Directory spec says relative XDG paths should be ignored.
    Honouring one would resolve against Tokdash's working directory: the
    real Muse history goes unfound while an unrelated project-relative
    directory gets scanned."""
    home = tmp_path / "fake-home"
    home.mkdir()
    # conftest pins Path.home() (line 87) AND $HOME (line 85) — pin both;
    # expanduser("~/...") reads $HOME, Path.home() reads the pinned attr.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "relative-muse-data")
    assert clientpaths.muse_sessions_root() == home / ".local/share/muse/sessions"
    # Absolute values (including ~) are honoured as before.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "abs-data"))
    assert clientpaths.muse_sessions_root() == tmp_path / "abs-data/muse/sessions"
    monkeypatch.setenv("XDG_DATA_HOME", "~/tilded-data")
    assert clientpaths.muse_sessions_root() == home / "tilded-data/muse/sessions"
    # Unset falls back as before.
    monkeypatch.delenv("XDG_DATA_HOME")
    assert clientpaths.muse_sessions_root() == home / ".local/share/muse/sessions"


def test_discovery_keeps_files_found_before_a_mid_walk_error(monkeypatch, tmp_path):
    """The walk documents "keep the partial walk" on a mid-walk OSError
    (permission, cloud placeholder), but sorted(root.rglob(...)) buffers the
    whole generator before the first result, so the raise escaped with
    nothing collected and the contract held nothing. Iterate first, sort at
    the end (the _rglob_sigs pattern): files found BEFORE the failure are
    returned."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [metadata_record()])
    write_session(tmp_path, "77777777-0000-4000-8000-00000000000b", [
        metadata_record(rid="m2", sid="77777777-0000-4000-8000-00000000000b"),
    ])
    real_rglob = Path.rglob

    def flaky(self, pattern, *args, **kwargs):
        it = real_rglob(self, pattern, *args, **kwargs)
        yield next(it)  # first real file, collected by the walk...
        raise OSError("simulated mid-walk permission failure")  # ...then boom

    monkeypatch.setattr(Path, "rglob", flaky)
    files = clientpaths.muse_session_files()
    assert len(files) == 1  # the partial result survives, not []
    assert files == sorted(files)  # and the return is still sorted


# ---------------------------------------------------------------------------
# Retained frames
# ---------------------------------------------------------------------------


def test_retained_frame_unwrap_counts_usage_inside(monkeypatch, tmp_path):
    """One batch line has no top-level id/payload_type: an envelope filter
    that requires them drops the frame and any usage inside it, silently."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [
        metadata_record(),
        frame(7, [(0, usage_record("rec-in-frame", 2, dict(USAGE)))]),
    ])
    entries = parse(muse_parser(monkeypatch, tmp_path))
    assert [e["entry_id"] for e in entries] == ["muse:rec-in-frame"]


def test_frame_without_outer_ordinal_expands(monkeypatch, tmp_path):
    """The public ccmux Muse 1.0.3 fixture contains a
    session_permission_transaction frame with frame_schema_version and
    indexed children but NO outer_log_ordinal. Expansion position comes from
    (line, child_index) either way, so requiring the ordinal dropped whole
    valid frames — and any usage inside them — over a field nothing orders
    on. Absent ordinal expands; a PRESENT invalid ordinal still kills the
    frame (parametrized case below)."""
    f = frame(7, [(0, usage_record("rec-in-frame", 2, dict(USAGE)))])
    del f["outer_log_ordinal"]
    write_session(tmp_path, SID, [metadata_record(), f])
    entries = parse(muse_parser(monkeypatch, tmp_path))
    assert [e["entry_id"] for e in entries] == ["muse:rec-in-frame"]


def test_frame_expands_in_ascending_child_index(monkeypatch, tmp_path):
    """Expansion order is child_index, not document order: children written
    out of index order still resolve a switch/call pair by stream order —
    the child_index-ordered switch governs the call even though the call
    line physically precedes it."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [
        metadata_record(model_id="model-old"),
        frame(7, [
            (1, reconfigure_record("rec-switch", 10, "model-new")),
            (0, usage_record("rec-call", 10, dict(USAGE), ts=BASE_TS + 2)),
        ]),
    ])
    # child_index order: (0)=call BEFORE (1)=switch on the same sequence ->
    # the call resolves to the pre-switch model.
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert entries["muse:rec-call"]["model"] == "model-old"

    # Same sequence, opposite index order -> switch earlier in stream order
    # governs the call.
    write_session(tmp_path, SID, [
        metadata_record(model_id="model-old"),
        frame(7, [
            (0, reconfigure_record("rec-switch", 10, "model-new")),
            (1, usage_record("rec-call", 10, dict(USAGE), ts=BASE_TS + 2)),
        ]),
    ])
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert entries["muse:rec-call"]["model"] == "model-new"


@pytest.mark.parametrize("breakage", ["version2", "version_missing", "index_dup", "index_bool", "index_neg", "index_missing", "children_not_list", "ordinal_bool"])
def test_malformed_frame_skips_whole_frame(monkeypatch, tmp_path, breakage):
    """Corrupt FRAME structure skips the whole frame: expansion order is
    unknowable and half a frame reintroduces the ordering guesswork the
    validation exists to end. (Corrupt child CONTENT is the per-child rule,
    tested separately.)"""
    muse_parser(monkeypatch, tmp_path)
    rec = usage_record("rec-1", 2, dict(USAGE))
    f = frame(7, [(0, rec), (0, rec)])  # duplicate indices; most cases rebuild child 1
    if breakage == "version2":
        f["frame_schema_version"] = 2
    elif breakage == "version_missing":
        del f["frame_schema_version"]
    elif breakage == "index_dup":
        pass
    elif breakage == "index_bool":
        f["children"][1]["child_index"] = True
    elif breakage == "index_neg":
        f["children"][1]["child_index"] = -1
    elif breakage == "index_missing":
        del f["children"][1]["child_index"]
    elif breakage == "children_not_list":
        f["children"] = "nope"
    elif breakage == "ordinal_bool":
        f["outer_log_ordinal"] = True
    write_session(tmp_path, SID, [metadata_record(), f])
    assert parse(muse_parser(monkeypatch, tmp_path)) == []


def test_bad_child_record_json_keeps_siblings(monkeypatch, tmp_path):
    """Inside a VALID frame each record_json decodes independently: one
    corrupt child (unparseable, or truncated mid-string) loses only itself."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [
        metadata_record(),
        frame(7, [
            (0, usage_record("rec-ok-1", 2)),
            (1, '{"id": "rec-corrupt", "payload": {"unterminated'),
            (2, usage_record("rec-ok-2", 3)),
            (3, 5),  # record_json not a string: also child-scoped
        ]),
    ])
    entries = parse(muse_parser(monkeypatch, tmp_path))
    assert sorted(e["entry_id"] for e in entries) == ["muse:rec-ok-1", "muse:rec-ok-2"]


# ---------------------------------------------------------------------------
# Live tail
# ---------------------------------------------------------------------------


def test_truncated_last_line_keeps_earlier_calls_and_completes_next_sync(monkeypatch, tmp_path):
    """Muse may be mid-append at read time. A decode failure on the LAST
    line is the ordinary case — skip that line only; the append completing
    changes the file signature and the next sync finds the finished record."""
    meta = json.dumps(metadata_record())
    u1 = json.dumps(usage_record("rec-1", 2))
    u2 = json.dumps(usage_record("rec-2", 3))
    path = write_session(tmp_path, SID, [meta])
    path.write_text(meta + "\n" + u1 + "\n" + '{"id": "rec-2", "recorded_at"', encoding="utf-8")
    assert [e["entry_id"] for e in parse(muse_parser(monkeypatch, tmp_path))] == ["muse:rec-1"]

    # The write completes; the signature changes; the next sync reparses.
    path.write_text(meta + "\n" + u1 + "\n" + u2 + "\n", encoding="utf-8")
    entries = parse(muse_parser(monkeypatch, tmp_path))
    assert sorted(e["entry_id"] for e in entries) == ["muse:rec-1", "muse:rec-2"]


def test_interior_malformed_line_is_line_scoped(monkeypatch, tmp_path):
    muse_parser(monkeypatch, tmp_path)
    good1 = json.dumps(usage_record("rec-1", 2))
    good2 = json.dumps(usage_record("rec-2", 4))
    write_session(tmp_path, SID, [metadata_record(), good1, "not json at all", good2])
    entries = parse(muse_parser(monkeypatch, tmp_path))
    assert sorted(e["entry_id"] for e in entries) == ["muse:rec-1", "muse:rec-2"]


def test_invalid_utf8_is_line_scoped(monkeypatch, tmp_path):
    """A text-mode handle decodes during ITERATION, outside the per-line
    json guard: one truncated multi-byte character — the ordinary state of a
    file Muse is mid-way through writing — raised UnicodeDecodeError and
    lost EVERY earlier valid record in the file. Bytes-mode line reads keep
    decode damage line-scoped: b"\\n" is never part of a valid UTF-8
    sequence, so line boundaries survive any byte-level damage. Trailing
    (mid-append) and interior (corruption) cases both keep their siblings."""
    meta = json.dumps(metadata_record()).encode()
    u1 = json.dumps(usage_record("rec-1", 2)).encode()
    u2 = json.dumps(usage_record("rec-2", 4)).encode()
    d = tmp_path / "muse" / "sessions" / "2026" / "09" / "09" / SID
    d.mkdir(parents=True)
    path = d / "session.jsonl"

    # Trailing truncated multi-byte character (append interrupted).
    path.write_bytes(meta + b"\n" + u1 + b"\n" + b'{"id": "rec-x", "payload": "\xe2')
    assert [e["entry_id"] for e in parse(muse_parser(monkeypatch, tmp_path))] == ["muse:rec-1"]

    # Interior invalid bytes: the corrupt line and only it is lost.
    path.write_bytes(meta + b"\n" + u1 + b"\n" + b'{"id": "rec-x", "payload": "\xff\xfe' + b"\n" + u2 + b"\n")
    entries = parse(muse_parser(monkeypatch, tmp_path))
    assert sorted(e["entry_id"] for e in entries) == ["muse:rec-1", "muse:rec-2"]


# ---------------------------------------------------------------------------
# Classifiers and envelope
# ---------------------------------------------------------------------------


def test_goal_usage_attribution_ignored(monkeypatch, tmp_path):
    """The same counters also appear on goal_usage_attribution as Goal
    budget accounting over the same calls; reading both doubles sessions."""
    muse_parser(monkeypatch, tmp_path)
    goal = usage_record("rec-goal", 3, dict(USAGE), kind="goal_usage_attribution")
    goal["payload"]["quantity"] = dict(USAGE)
    goal["payload"]["main_llm_steps"] = 3
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-1", 2), goal])
    entries = parse(muse_parser(monkeypatch, tmp_path))
    assert [e["entry_id"] for e in entries] == ["muse:rec-1"]


def test_non_event_records_and_bad_envelopes_skipped(monkeypatch, tmp_path):
    muse_parser(monkeypatch, tmp_path)
    keep = usage_record("rec-1", 2)
    no_durable = usage_record("rec-x1", 3)
    no_durable["durability"] = "transient"
    wrong_type = usage_record("rec-x2", 4)
    wrong_type["record_type"] = "marker"
    no_seq = usage_record("rec-x3", 5)
    del no_seq["sequence"]
    bool_seq = usage_record("rec-x4", 6)
    bool_seq["sequence"] = True
    neg_seq = usage_record("rec-x5", 7)
    neg_seq["sequence"] = -1
    write_session(tmp_path, SID, [
        metadata_record(), keep,
        {"id": "rec-frameish", "frame_schema_version": 1, "children": []},  # frame-ish, no recognized type
        no_durable, wrong_type, no_seq, bool_seq, neg_seq,
    ])
    entries = parse(muse_parser(monkeypatch, tmp_path))
    assert [e["entry_id"] for e in entries] == ["muse:rec-1"]


@pytest.mark.parametrize("bad_id", ["", "   ", None, 7])
def test_empty_or_non_string_record_id_malformed(monkeypatch, tmp_path, bad_id):
    """entry_id is "muse:" + id: an empty or whitespace id would map every
    such record to one identical entry_id and silently collapse them."""
    muse_parser(monkeypatch, tmp_path)
    rec = usage_record("rec-1", 2)
    if bad_id is None:
        del rec["id"]
    else:
        rec["id"] = bad_id
    write_session(tmp_path, SID, [metadata_record(), rec])
    assert parse(muse_parser(monkeypatch, tmp_path)) == []


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,value", [
    ("input_tokens", True),
    ("input_tokens", -5),
    ("output_tokens", "12"),
    ("reasoning_tokens", None),
    # With no split pair superseding it, the combined value is validated
    # too — the bad-combined exemption is ONLY the superseded case.
    ("cached_tokens", "bad"),
    ("cached_tokens", True),
    ("cached_tokens", -5),
])
def test_present_but_bad_counter_skips_record(monkeypatch, tmp_path, field, value):
    muse_parser(monkeypatch, tmp_path)
    usage = dict(USAGE)
    usage[field] = value
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-1", 2, usage)])
    assert parse(muse_parser(monkeypatch, tmp_path)) == []


@pytest.mark.parametrize("missing", ["input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens"])
def test_required_counter_absent_skips_record(monkeypatch, tmp_path, missing):
    """The wire schema's TokenUsage required list names all four: a
    required-but-absent field is skipped, because admitting one makes a
    truncated record indistinguishable from a genuine zero — an absent
    cached_tokens reads as 0 and turns cached input into ordinary input."""
    muse_parser(monkeypatch, tmp_path)
    usage = dict(USAGE)
    del usage[missing]
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-1", 2, usage)])
    assert parse(muse_parser(monkeypatch, tmp_path)) == []


def test_split_pair_is_authoritative_and_tolerates_cached_absence(monkeypatch, tmp_path):
    """cache_read_tokens/cache_write_tokens win as the cache buckets when
    both exist; cached_tokens absent while the pair is present is the ONE
    tolerated required-counter absence. And the pair is NOT cross-checked
    against cached_tokens: the official schema defines the split fields as
    raw provider counters with provider-dependent cache conventions, so the
    SASE shape (cached_tokens == read, writes separate, read+write !=
    cached) is BILLABLE, not malformed — dropping it would silently lose
    that provider's spend. Half a pair is still malformed."""
    muse_parser(monkeypatch, tmp_path)
    pair_only = {"input_tokens": 1000, "output_tokens": 200, "reasoning_tokens": 50,
                 "cache_read_tokens": 300, "cache_write_tokens": 100}
    pair_plus_combined = dict(pair_only, cached_tokens=400)
    half_pair = {"input_tokens": 1000, "output_tokens": 200, "reasoning_tokens": 50,
                 "cached_tokens": 400, "cache_read_tokens": 300}
    # SASE layout: cached_tokens is the older read fallback (== read), with
    # writes tracked separately — the equality read+write == cached FAILS.
    sase_shaped = dict(pair_only, cached_tokens=300)
    write_session(tmp_path, SID, [
        metadata_record(),
        usage_record("rec-pair", 2, pair_only),
        usage_record("rec-pair-combined", 3, pair_plus_combined),
        usage_record("rec-half", 4, half_pair),
        usage_record("rec-sase", 5, sase_shaped),
    ])
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert set(entries) == {"muse:rec-pair", "muse:rec-pair-combined", "muse:rec-sase"}
    for key in ("muse:rec-pair", "muse:rec-pair-combined", "muse:rec-sase"):
        assert (entries[key]["cacheRead"], entries[key]["cacheWrite"]) == (300, 100)
        # the default both-included policy subtracts BOTH split members
        assert entries[key]["input"] == 600


@pytest.mark.parametrize("bad_cached", ["bad", True, -5, 3.5])
def test_bad_combined_cached_ignored_when_split_valid(monkeypatch, tmp_path, bad_cached):
    """A present-but-malformed combined cached_tokens alongside a VALID
    split pair must not poison the record — and, critically, must not
    poison the SOURCE: the split branch validates only read/write, so a
    return path that still ran int(cached) raised ValueError and aborted
    the whole Muse sync over a field nothing reads (review reproduction
    with "cached_tokens": "bad"). The pair is authoritative, so the
    superseded combined value is neither validated nor converted: the
    record prices from the pair. The malformed combined value still
    skips the record when NO pair supersedes it (the field parameter
    below)."""
    muse_parser(monkeypatch, tmp_path)
    usage = {"input_tokens": 1000, "output_tokens": 200, "reasoning_tokens": 50,
             "cache_read_tokens": 300, "cache_write_tokens": 100,
             "cached_tokens": bad_cached}
    write_session(tmp_path, SID, [
        metadata_record(),
        usage_record("rec-pair-bad-combined", 2, usage),
        # Same malformed combined value WITHOUT the superseding pair: still
        # a malformed record (the pair, not the absence of validation, is
        # what rescues the record above).
        usage_record("rec-no-pair", 3, dict(USAGE, cached_tokens=bad_cached)),
    ])
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert set(entries) == {"muse:rec-pair-bad-combined"}
    entry = entries["muse:rec-pair-bad-combined"]
    assert (entry["cacheRead"], entry["cacheWrite"]) == (300, 100)
    assert entry["input"] == 600


def test_all_zero_call_skipped(monkeypatch, tmp_path):
    """The guard every file source uses — the offline echo capture's one
    model_completed record carries all-zero counters and would otherwise
    become a zero-token row of pure noise."""
    muse_parser(monkeypatch, tmp_path)
    zeros = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0}
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-1", 2, zeros)])
    assert parse(muse_parser(monkeypatch, tmp_path)) == []


def test_all_zero_call_needs_no_provider_policy(monkeypatch, tmp_path):
    """The ccmux public fixture's shape: an all-zero model_completed under
    the "echo" provider — a built-in test backend no capture will ever
    establish a cache policy for. The all-zero guard reads only raw buckets
    and MUST run before the provider-keyed policy lookup: checking the
    policy first would make this harmless historical session raise
    "unresolved for provider 'echo'" and abort the ENTIRE Muse source on
    every sync, registered under whatever Meta-only map the capture ships."""
    zeros = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0}
    write_session(tmp_path, SID, [
        metadata_record(provider_id="echo"),
        usage_record("rec-zero", 2, zeros),
    ])
    parser = muse_parser(monkeypatch, tmp_path, policies={"meta": (True, True)})
    assert parse(parser) == []  # dropped by the guard, not raised by the policy


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ts", [1_500_000_000_000_000, 5_000_000_000_000_000])
def test_timestamp_outside_fixed_range_skipped(monkeypatch, tmp_path, ts):
    """The fixed range catches a field denominated in seconds/milliseconds/
    nanoseconds, which would read as 1970 or the far future after // 1000."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [
        metadata_record(ts=BASE_TS),
        usage_record("rec-1", 2, ts=ts),
    ])
    assert parse(muse_parser(monkeypatch, tmp_path)) == []


def test_file_relative_ceiling(monkeypatch, tmp_path):
    """A stamp more than a day past the FILE's own last write is not
    trustworthy: one hour ahead of a known mtime counts, two days ahead does
    not. Skip, never clamp — rewriting a timestamp invents a value the source
    never wrote."""
    muse_parser(monkeypatch, tmp_path)
    known_mtime_ns = (BASE_TS + 3 * 86_400_000_000) * 1000  # file written 3 days after base
    path = write_session(tmp_path, SID, [
        metadata_record(ts=BASE_TS),
        usage_record("rec-hour", 2, ts=known_mtime_ns // 1000 + 3_600_000_000),
        usage_record("rec-two-days", 3, ts=known_mtime_ns // 1000 + 2 * 86_400_000_000),
    ])
    os.utime(path, ns=(known_mtime_ns, known_mtime_ns))
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert set(entries) == {"muse:rec-hour"}


# ---------------------------------------------------------------------------
# Cache policy (capture-gated; tests pin the STRUCTURE around the map)
# ---------------------------------------------------------------------------


def test_cache_policy_is_a_provider_keyed_bit_pair(monkeypatch, tmp_path):
    """A provider's policy is (read_included, write_included), settled bit by
    bit (ccusage/BurnBar support reads inside Meta's input_tokens; nothing
    proves writes are) and keyed by PROVIDER (the MSP schema: counters are
    not summable across providers, inclusion is provider-dependent). All
    four combinations are legal — a read-inclusive / write-beside policy
    must pass writes through untouched, or every cache-write token would be
    subtracted from fresh input (an input AND cost undercount). The
    promptTokens oracle lives only on the live notification; the capture's
    notification-join test proves which pairs hold, and these fixtures pin
    the per-bucket arithmetic per policy."""
    pair = {"input_tokens": 1000, "output_tokens": 200, "reasoning_tokens": 50,
            "cache_read_tokens": 300, "cache_write_tokens": 100}
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-1", 2, pair)])

    def entries_for(policy):
        return by_id(parse(muse_parser(monkeypatch, tmp_path, policies={"meta": policy})))["muse:rec-1"]

    both = entries_for((True, True))
    read_only = entries_for((True, False))
    write_only = entries_for((False, True))
    neither = entries_for((False, False))
    assert both["input"] == 600        # subtract reads AND writes
    assert read_only["input"] == 700   # writes stay in fresh input — NOT 600
    assert write_only["input"] == 900
    assert neither["input"] == 1000
    for e in (both, read_only, write_only, neither):
        assert (e["cacheRead"], e["cacheWrite"]) == (300, 100)  # buckets never move


def test_provider_switch_applies_each_provider_policy(monkeypatch, tmp_path):
    """Inclusion is resolved per call from the CALL's provider, not one
    global pair: a session switching providers mid-way must apply each
    provider's own subtraction, or the second provider's calls get the
    first provider's convention (the MSP schema's whole reason for the
    provider caveat). provider-a includes writes; provider-b keeps writes
    beside — same counters, different fresh input."""
    pair = {"input_tokens": 1000, "output_tokens": 200, "reasoning_tokens": 50,
            "cache_read_tokens": 300, "cache_write_tokens": 100}
    write_session(tmp_path, SID, [
        metadata_record(provider_id="provider-a", model_id="model-a"),
        usage_record("rec-under-a", 2, pair),
        reconfigure_record("rec-switch", 3, "model-b", provider="provider-b", ts=BASE_TS + 2),
        usage_record("rec-under-b", 4, pair, ts=BASE_TS + 3),
    ])
    entries = by_id(parse(muse_parser(
        monkeypatch, tmp_path,
        policies={"provider-a": (True, True), "provider-b": (True, False)},
    )))
    assert entries["muse:rec-under-a"]["input"] == 600  # (True, True)
    assert entries["muse:rec-under-b"]["input"] == 700  # (True, False)
    assert entries["muse:rec-under-a"]["provider"] == "provider-a"
    assert entries["muse:rec-under-b"]["provider"] == "provider-b"


def test_reasoning_split_and_billing_uses_full_completion(monkeypatch, tmp_path):
    """Reasoning is INSIDE output_tokens (wire schema) and compute.py adds
    reasoning on top of output, so display output = completion - reasoning
    while billing sees the full completion (WorkBuddy convention)."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-1", 2)])
    entry = by_id(parse(muse_parser(monkeypatch, tmp_path)))["muse:rec-1"]
    assert entry["output"] == 150 and entry["reasoning"] == 50
    assert entry["_billing"]["output"] == 200  # full completion billed
    assert entry["timestamp"] == (BASE_TS + 1000) // 1000


def test_reasoning_above_completion_clamped(monkeypatch, tmp_path):
    muse_parser(monkeypatch, tmp_path)
    usage = {"input_tokens": 100, "output_tokens": 30, "cached_tokens": 0, "reasoning_tokens": 90}
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-1", 2, usage)])
    entry = by_id(parse(muse_parser(monkeypatch, tmp_path)))["muse:rec-1"]
    assert entry["output"] == 0 and entry["reasoning"] == 30


@pytest.mark.parametrize("policies", [
    {},                                     # production posture: nothing established
    {"other-provider": (True, True)},       # established... but not for THIS provider
])
def test_unresolved_provider_policy_raises(monkeypatch, tmp_path, policies):
    """A provider with no entry in the policy map is a build blocker: the
    parser raises instead of guessing, and instead of BORROWING another
    provider's established policy (the MSP schema says conventions are
    provider-dependent, so another provider's entry says nothing about this
    one). The registry keeps the source out until every emittable provider
    has an entry (asserted in test_usage_store)."""
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-1", 2)])
    parser = muse_parser(monkeypatch, tmp_path, policies=policies)
    with pytest.raises(RuntimeError, match="unresolved for provider"):
        parser._parse_all()


def test_model_and_provider_resolve_as_one_selection(monkeypatch, tmp_path):
    """Two rules, one per record kind. (1) A SELECTION record is atomic
    (review reproduction): metadata provider-a/model-a, then a switch
    declaring model-b with NO provider — independent per-field lookups would
    fabricate provider-a/model-b, a selection that never existed. The
    winning record supplies both fields, so the call gets model-b with an
    explicit empty provider, never the previous provider. (2) A usage
    event's bare model is an OBSERVATION, not a selection (real captures:
    model on model_completed, provider_id in session metadata) — it
    overrides the model field only and RETAINS the governing selection's
    provider, or the common meta + bare-model case would price under "" and
    spuriously abort on the provider-keyed policy map."""
    write_session(tmp_path, SID, [
        metadata_record(provider_id="provider-a", model_id="model-a"),
        usage_record("rec-percall", 2, model="model-c", ts=BASE_TS + 1),
        reconfigure_record("rec-switch", 3, "model-b", ts=BASE_TS + 2),
        usage_record("rec-after", 4, ts=BASE_TS + 3),
    ])
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    percall = entries["muse:rec-percall"]
    assert percall["model"] == "model-c"
    assert percall["provider"] == "provider-a"  # model-only override, provider retained
    after = entries["muse:rec-after"]
    assert after["model"] == "model-b"
    assert after["provider"] == ""  # atomic selection record: explicit no-provider


def test_model_reconfigure_completed_switches_selection(monkeypatch, tmp_path):
    """The OFFICIAL durable switch event must split the selection chain.
    Per the SDK manifest the durable event behind session/modelChanged is
    runtime.model_reconfigure.completed. Ignoring that record leaves every
    later call attributed to
    the superseded model AND provider (wrong pricing, no skipped record to
    notice). Payload positions are fixture-pending, so both plausible
    serializations are pinned: manifest name as payload_type with the
    selection at payload level, and as a run event kind with the selection
    inside the event. Atomicity carries over from the directive shape:
    a model-only switch yields an explicit empty provider; a switch
    naming both moves both; calls before either switch keep the old
    selection."""
    write_session(tmp_path, SID, [
        metadata_record(provider_id="provider-a", model_id="model-a"),
        usage_record("rec-early", 2, ts=BASE_TS + 1),
        # Shape 1: manifest name as payload_type, model only.
        reconfigure_record("rec-sw1", 3, model="model-b", ts=BASE_TS + 2),
        usage_record("rec-mid", 4, ts=BASE_TS + 3),
        # Shape 2: run payload, reconfigure event kind, selection in event.
        reconfigure_record("rec-sw2", 5, model="model-c", provider="provider-c",
                           ts=BASE_TS + 4, event_shape=True),
        usage_record("rec-late", 6, ts=BASE_TS + 5),
    ])
    entries = by_id(parse(muse_parser(
        monkeypatch, tmp_path,
        policies={"provider-a": (True, True), "": (True, True), "provider-c": (True, True)},
    )))
    assert len(entries) == 3
    early = entries["muse:rec-early"]
    assert (early["model"], early["provider"]) == ("model-a", "provider-a")  # before any switch
    mid = entries["muse:rec-mid"]
    assert (mid["model"], mid["provider"]) == ("model-b", "")  # atomic: model-only -> no provider
    late = entries["muse:rec-late"]
    assert (late["model"], late["provider"]) == ("model-c", "provider-c")  # both move together


def test_nonterminal_model_reconfigure_does_not_switch_selection(monkeypatch, tmp_path):
    """Only the evidenced terminal event changes durable selection state.
    An intent, start, or pending runtime.model_reconfigure record must not
    attribute a later call to a selection that never completed. The invented
    underscore spelling is unsupported too."""
    pending_type = reconfigure_record(
        "rec-pending-type", 2, model="model-pending", provider="provider-pending",
        ts=BASE_TS + 1,
    )
    pending_type["payload_type"] = "runtime.model_reconfigure"
    pending_type["payload"]["kind"] = "model_reconfigure"
    pending_event = reconfigure_record(
        "rec-pending-event", 3, model="model-pending", provider="provider-pending",
        ts=BASE_TS + 2, event_shape=True,
    )
    pending_event["payload"]["event"]["kind"] = "model_reconfigure"
    invented_event = reconfigure_record(
        "rec-invented-event", 4, model="model-invented", provider="provider-invented",
        ts=BASE_TS + 3, event_shape=True,
    )
    invented_event["payload"]["event"]["kind"] = "model_reconfigure_completed"
    write_session(tmp_path, SID, [
        metadata_record(provider_id="provider-a", model_id="model-a"),
        pending_type,
        pending_event,
        invented_event,
        usage_record("rec-after", 5, ts=BASE_TS + 4),
    ])
    entry = by_id(parse(muse_parser(
        monkeypatch, tmp_path,
        policies={"provider-a": (True, True)},
    )))["muse:rec-after"]
    assert (entry["model"], entry["provider"]) == ("model-a", "provider-a")


# ---------------------------------------------------------------------------
# Model chain and provider
# ---------------------------------------------------------------------------


def test_per_call_model_precedence_and_session_switch(monkeypatch, tmp_path):
    """Per-call event.model wins over metadata; a completed reconfiguration
    splits the session by composite stream position, never by
    recorded_at: the call stamped microseconds AFTER the switch but with a
    LOWER sequence resolves to the pre-switch model, and the call sharing the
    switch's exact recorded_at with a higher sequence resolves after it."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [
        metadata_record(model_id="model-meta"),
        # Lower sequence than the switch though its recorded_at is later —
        # stream position is the truth, recorded_at is not.
        usage_record("rec-before", 3, ts=BASE_TS + 9_000),
        reconfigure_record("rec-switch", 4, "model-new", ts=BASE_TS + 2),
        # Same recorded_at as the switch: the higher sequence resolves after it.
        usage_record("rec-after", 5, ts=BASE_TS + 2),
        usage_record("rec-percall", 6, model="model-percall", ts=BASE_TS + 3),
    ])
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert entries["muse:rec-before"]["model"] == "model-meta"
    assert entries["muse:rec-after"]["model"] == "model-new"
    assert entries["muse:rec-percall"]["model"] == "model-percall"


def test_run_scope_wins_inside_run_and_no_bleed_between_runs(monkeypatch, tmp_path):
    """model_request_configured configures a RUN. A newer matching run
    record wins over older session metadata; a second run that configures
    nothing must fall through to session scope — an unscoped nearest-
    preceding lookup would carry run 1's configuration into run 2 and price
    calls against a model never in force for them."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [
        metadata_record(model_id="model-meta"),
        configured_record("rec-cfg", 2, "model-run1", RUN1),
        usage_record("rec-r1", 3, run_id=RUN1, ts=BASE_TS + 2),
        usage_record("rec-r2", 4, run_id=RUN2, ts=BASE_TS + 3),
    ])
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert entries["muse:rec-r1"]["model"] == "model-run1"
    assert entries["muse:rec-r2"]["model"] == "model-meta"


def test_later_session_switch_supersedes_run_configuration(monkeypatch, tmp_path):
    """session/setModel admitted during a run applies at the next model-call
    boundary, so a later completed session switch supersedes that run's
    earlier model_request_configured record."""
    write_session(tmp_path, SID, [
        metadata_record(provider_id="provider-a", model_id="model-a"),
        configured_provider_record(
            "rec-cfg", 2, "model-run", "provider-run", RUN1,
            ts=BASE_TS + 1,
        ),
        reconfigure_record(
            "rec-switch", 3, model="model-new", provider="provider-new",
            ts=BASE_TS + 2,
        ),
        usage_record("rec-after", 4, run_id=RUN1, ts=BASE_TS + 3),
    ])
    entry = by_id(parse(muse_parser(
        monkeypatch, tmp_path,
        policies={"provider-new": (True, True)},
    )))["muse:rec-after"]
    assert (entry["model"], entry["provider"]) == ("model-new", "provider-new")


def test_model_unknown_at_zero_cost_when_nothing_resolves(monkeypatch, tmp_path):
    """No fourth link: reminder_roster agent models are auxiliary, and
    Muse's own posture calls a null-model usage an unpriced leg, not a
    reason to drop tokens (Qwen Code precedent)."""
    muse_parser(monkeypatch, tmp_path)
    meta = metadata_record()  # echo capture shape: metadata carries no model_id
    rec = usage_record("rec-1", 2)
    rec["payload"]["event"]["reminder_roster"] = {"agents": [{"model": "same-as-main"}]}
    write_session(tmp_path, SID, [meta, rec])
    entry = by_id(parse(muse_parser(monkeypatch, tmp_path)))["muse:rec-1"]
    assert entry["model"] == "unknown"
    assert entry["cost"] == 0.0
    assert entry["input"] > 0  # tokens kept


def test_provider_split_fallback_and_empty(monkeypatch, tmp_path):
    """A qualified provider/model splits into BOTH fields (split only the
    provider and aggregate_entries composes meta/meta/model, grouped apart
    from the same model's unqualified rows); unqualified falls back to
    metadata provider_id; neither yields ""."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [
        metadata_record(provider_id="meta"),
        usage_record("rec-qualified", 2, model="meta/llama-4"),
        usage_record("rec-bare", 3),
    ])
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert (entries["muse:rec-qualified"]["provider"], entries["muse:rec-qualified"]["model"]) == ("meta", "llama-4")
    assert (entries["muse:rec-bare"]["provider"], entries["muse:rec-bare"]["model"]) == ("meta", "unknown")

    # No metadata provider at all -> "".
    muse_parser(monkeypatch, tmp_path)
    meta = metadata_record()
    del meta["payload"]["record"]["provider_id"]
    write_session(tmp_path, SID, [meta, usage_record("rec-x", 2)])
    entry = by_id(parse(muse_parser(monkeypatch, tmp_path)))["muse:rec-x"]
    assert entry["provider"] == ""


def test_provider_switch_does_not_leak_backward(monkeypatch, tmp_path):
    """Official MSP model selection carries providerId alongside modelId, so
    provider resolves through the SAME position chain as the model. A
    scan-wide last-wins metadata_provider would let the provider change at
    seq 3 rewrite rec-early's provider — conceptually relabelling a call
    that happened under a different provider (and its pricing candidate).
    After the switch both fields move together; a qualified per-call model
    still splits first."""
    write_session(tmp_path, SID, [
        metadata_record(provider_id="provider-a", model_id="model-a"),
        usage_record("rec-early", 2),
        reconfigure_record("rec-switch", 3, "model-b", provider="provider-b", ts=BASE_TS + 2),
        usage_record("rec-late", 4, ts=BASE_TS + 3),
    ])
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert (entries["muse:rec-early"]["provider"], entries["muse:rec-early"]["model"]) == ("provider-a", "model-a")
    assert (entries["muse:rec-late"]["provider"], entries["muse:rec-late"]["model"]) == ("provider-b", "model-b")


def test_provider_resolves_run_scope_before_session(monkeypatch, tmp_path):
    """model_request_configured carries provider data too — ignoring it and
    falling straight to metadata would price a run's calls under a provider
    never in force for them. Run-scoped provider wins inside its run; a
    second run with no configuration falls through to the session provider,
    with no bleed (mirror of the model chain)."""
    write_session(tmp_path, SID, [
        metadata_record(provider_id="provider-meta", model_id="model-meta"),
        configured_provider_record("rec-cfg", 2, "model-run1", "provider-run1", RUN1),
        usage_record("rec-r1", 3, run_id=RUN1, ts=BASE_TS + 2),
        usage_record("rec-r2", 4, run_id=RUN2, ts=BASE_TS + 3),
    ])
    entries = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert (entries["muse:rec-r1"]["provider"], entries["muse:rec-r1"]["model"]) == ("provider-run1", "model-run1")
    assert (entries["muse:rec-r2"]["provider"], entries["muse:rec-r2"]["model"]) == ("provider-meta", "model-meta")


# ---------------------------------------------------------------------------
# Estimate marker (capture-gated key; tests set it explicitly)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker,expected", [
    ("tokenizer_estimate", True),
    ("heuristic_estimate", True),
    ("provider_reported", False),
])
def test_estimate_marker_mapping(monkeypatch, tmp_path, marker, expected):
    """estimated=True for the two estimate sources (dropping an estimated
    call undercounts real spend — Grok precedent), False for
    provider_reported, and the key OMITTED when no marker is present: an
    invented False asserts a provenance the source never stated. While the
    serialized key is unset (production default) nothing is ever emitted."""
    muse_parser(monkeypatch, tmp_path)
    usage = dict(USAGE, estimate_source=marker)
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-1", 2, usage)])
    entry = by_id(parse(muse_parser(monkeypatch, tmp_path, marker="estimate_source")))["muse:rec-1"]
    assert entry["estimated"] is expected

    # Unmarked fixture omits the key rather than inventing False.
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-2", 2)])
    entry = by_id(parse(muse_parser(monkeypatch, tmp_path, marker="estimate_source")))["muse:rec-2"]
    assert "estimated" not in entry

    # Key unset (production posture): marker in the data changes nothing.
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-3", 2, usage)])
    entry = by_id(parse(muse_parser(monkeypatch, tmp_path, marker=None)))["muse:rec-3"]
    assert "estimated" not in entry


# ---------------------------------------------------------------------------
# Store integration: DB modes, billing reprice, marker round-trip, races
# ---------------------------------------------------------------------------


def _sync_muse(store, parser, sigs, durable=None):
    return store.sync_files(
        "muse",
        sigs,
        parser=parser.persistent_parser_signature(),
        pricing_identity=None,
        parse_file_entries=lambda fs: _collect_parser_file(parser, fs),
        durable=durable,
    )


def test_db_off_race_keeps_other_files_and_survives_source(tmp_path, monkeypatch):
    """A file enumerated then deleted must not blank the Muse source: the
    source-wide _parse_all() skips ONLY the vanished file (the tracker's
    per-source except would otherwise discard every Muse entry for the
    request, and this parser is exactly where that raise contract bites)."""
    parser = muse_parser(monkeypatch, tmp_path)
    a = write_session(tmp_path, SID, [metadata_record(), usage_record("rec-a", 2)])
    b = write_session(tmp_path, "33333333-0000-4000-8000-00000000000b", [
        metadata_record(rid="meta-b", sid="33333333-0000-4000-8000-00000000000b"),
        usage_record("rec-b", 2, sid="33333333-0000-4000-8000-00000000000b"),
    ])
    sigs = parser._file_signatures()
    assert len(sigs) == 2
    b.unlink()  # the race: B enumerated, then Muse pruned it
    stale_parser = muse_parser(monkeypatch, tmp_path)
    monkeypatch.setattr(stale_parser, "_file_signatures", lambda: sigs)
    entries = stale_parser._parse_all()
    ids = {e["entry_id"] for e in entries}
    assert "muse:rec-a" in ids and "muse:rec-b" not in ids  # A survives; B gone


def test_vanished_file_rows_survive_e2e(monkeypatch, tmp_path):
    """End-to-end missing-file race through the strict callback and the real
    store (durable=True): a file that changed on disk (so its signature
    forces a re-parse) and then vanished before the parse opened it must
    keep its stored rows — the response assertion that pins the fix — and a
    following clean durable sync retains them while marking the file missing.
    An UNCHANGED deleted file never reaches the parse at all (unchanged files
    are not reparsed), so growth-then-deletion is the race's real shape."""
    parser = muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-a", 2)])
    b_uuid = "44444444-0000-4000-8000-00000000000b"
    b = write_session(tmp_path, b_uuid, [
        metadata_record(rid="meta-b", sid=b_uuid),
        usage_record("rec-b", 2, sid=b_uuid),
    ])
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_muse(store, parser, parser._file_signatures(), durable=True) is True
    assert {e["entry_id"] for e in store.query_entries(sources=["muse"])} == {"muse:rec-a", "muse:rec-b"}

    # B grows (signature changes -> forced re-parse next sync)...
    b.write_text(b.read_text() + json.dumps(usage_record("rec-b2", 3, sid=b_uuid)) + "\n", encoding="utf-8")
    grew = muse_parser(monkeypatch, tmp_path)
    sigs2 = grew._file_signatures()
    # ...and vanishes before the parse opens it.
    b.unlink()
    raced = muse_parser(monkeypatch, tmp_path)
    _sync_muse(store, raced, sigs2, durable=True)
    rows = {e["entry_id"] for e in store.query_entries(sources=["muse"])}
    assert "muse:rec-b" in rows  # raced file's rows kept, not silently deleted

    # Next clean sync: B is out of discovery; durable retention keeps its rows.
    fresh = muse_parser(monkeypatch, tmp_path)
    assert _sync_muse(store, fresh, fresh._file_signatures(), durable=True) is True
    rows = {e["entry_id"] for e in store.query_entries(sources=["muse"])}
    assert rows == {"muse:rec-a", "muse:rec-b"}


def test_non_vanished_parser_bug_still_fails_the_store_sync(monkeypatch, tmp_path):
    parser = muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [metadata_record(), usage_record("rec-a", 2)])

    def broken(fs):
        raise TypeError("parser bug")

    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    with pytest.raises(TypeError):
        store.sync_files(
            "muse", parser._file_signatures(),
            parser=parser.persistent_parser_signature(),
            parse_file_entries=broken,
        )


def test_fork_pair_db_modes_agree_on_the_winner(monkeypatch, tmp_path):
    """DB=0 (parser by_id) and DB=1 (store stable-key upsert) must pick the
    SAME copy of a fork-restamped record when the copies tie to the
    microsecond — the level the agreement can actually fail, not aggregate
    totals. The winner is identifiable by the sentinel model each file
    prices, never by leaking a path into a public entry."""
    parser = muse_parser(monkeypatch, tmp_path)
    # Fork pair: same record id, same recorded_at, different files; each
    # file's copy carries a sentinel per-call model so the winner is visible.
    write_session(tmp_path, "55555555-0000-4000-8000-00000000000a", [
        metadata_record(rid="meta-a", sid="55555555-0000-4000-8000-00000000000a"),
        usage_record("dup-1", 2, model="sentinel-a", sid="55555555-0000-4000-8000-00000000000a"),
    ])
    write_session(tmp_path, "66666666-0000-4000-8000-00000000000b", [
        metadata_record(rid="meta-b", sid="66666666-0000-4000-8000-00000000000b"),
        usage_record("dup-1", 2, model="sentinel-b", sid="66666666-0000-4000-8000-00000000000b"),
    ])

    off = by_id(parse(muse_parser(monkeypatch, tmp_path)))
    assert set(off) == {"muse:dup-1"}
    assert off["muse:dup-1"]["model"] == "sentinel-a"  # smaller path wins the tie

    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    parser2 = muse_parser(monkeypatch, tmp_path)
    assert store.sync_files(
        "muse", parser2._file_signatures(),
        parser=parser2.persistent_parser_signature(),
        parse_file_entries=lambda fs: _collect_parser_file(parser2, fs),
        cross_file_stable_keys=True,  # the fork world open item 3 may enable
    ) is True
    import sqlite3
    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        rows = conn.execute(
            "SELECT entry_key, file_path, model FROM usage_entries WHERE source='muse' AND entry_key='muse:dup-1'"
        ).fetchall()
    assert len(rows) == 1
    entry_key, file_path, model = rows[0]
    assert model == "sentinel-a"
    stored_path = Path(file_path)
    assert stored_path.name == "session.jsonl"
    assert stored_path.parent.name == "55555555-0000-4000-8000-00000000000a"


def test_qualified_rate_prices_row_and_reprice_moves_it(monkeypatch, tmp_path):
    """A rate registered only under the qualified provider/model name must
    price the row nonzero, and editing that rate must move the stored row's
    cost — repricing walks _billing.models, so the qualified candidate must
    survive into provenance (the only check that _billing is populated)."""
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"version": "t", "models": {
        "meta/mmodel": {"input": 3.0, "output": 15.0, "cache_read": 0.3},
    }}), encoding="utf-8")
    db = PricingDatabase(db_path=prices, override_path=prices)
    parser = muse_parser(monkeypatch, tmp_path, pricing=db)
    write_session(tmp_path, SID, [
        metadata_record(),
        usage_record("rec-1", 2, model="meta/mmodel"),
    ])
    entry = by_id(parse(parser))["muse:rec-1"]
    assert entry["model"] == "mmodel" and entry["provider"] == "meta"
    assert entry["cost"] > 0  # priced under the qualified name, not the bare miss
    assert entry["_billing"]["models"] == ["meta/mmodel", "mmodel"]

    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    # The store reads through its OWN PricingDatabase() (the read path's
    # pricing-generation repair); point it at the same custom rates so the
    # qualified candidate — not a default-DB miss — is what the row carries.
    store._pricing_db = lambda: db
    assert _sync_muse(store, muse_parser(monkeypatch, tmp_path, pricing=db),
                      muse_parser(monkeypatch, tmp_path, pricing=db)._file_signatures()) is True
    before = store.query_entries(sources=["muse"])[0]["cost"]
    assert before > 0

    # Edit the qualified rate; repricing through apply_pricing must move the
    # stored row's cost.
    prices.write_text(json.dumps({"version": "t2", "models": {
        "meta/mmodel": {"input": 6.0, "output": 30.0, "cache_read": 0.6},
    }}), encoding="utf-8")
    db2 = PricingDatabase(db_path=prices, override_path=prices)
    assert store.apply_pricing(db2.content_signature(), db2) is True
    after = store.query_entries(sources=["muse"])[0]["cost"]
    assert after == pytest.approx(before * 2)


def test_aggregate_display_name_is_not_doubly_qualified(monkeypatch, tmp_path):
    """The Provider bullet splits BOTH fields; aggregate_entries composes
    f"{provider}/{model}" — a qualified model kept verbatim next to the
    extracted prefix would surface meta/meta/llama-4 grouped apart from the
    same model's unqualified rows. This asserts the aggregate-level name."""
    muse_parser(monkeypatch, tmp_path)
    write_session(tmp_path, SID, [
        metadata_record(),
        usage_record("rec-1", 2, model="meta/llama-4"),
    ])
    parser = muse_parser(monkeypatch, tmp_path)
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_muse(store, parser, parser._file_signatures()) is True
    agg = store.aggregate_entries(sources=["muse"])
    names = []

    def walk(obj):
        if isinstance(obj, dict):
            if "name" in obj:
                names.append(obj["name"])
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(agg)
    assert "meta/llama-4" in names
    assert "meta/meta/llama-4" not in names


def test_estimate_marker_round_trips_and_aggregates_indistinguishably(monkeypatch, tmp_path):
    """Row-level provenance only: the marker survives query_entries (it rides
    in raw_json; PRIVATE_ENTRY_KEYS is just _billing) but aggregate_entries
    has no estimated dimension and never looks at it — an estimated row sums
    into totals exactly like an unmarked one. If totals ever become
    estimate-aware this test changes with that store feature, not before."""
    muse_parser(monkeypatch, tmp_path)
    usage = dict(USAGE, estimate_source="tokenizer_estimate")
    write_session(tmp_path, SID, [
        metadata_record(),
        usage_record("rec-est", 2, usage),
        usage_record("rec-plain", 3),
    ])
    parser = muse_parser(monkeypatch, tmp_path, marker="estimate_source")
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    assert _sync_muse(store, parser, parser._file_signatures()) is True
    rows = by_id(store.query_entries(sources=["muse"]))
    assert rows["muse:rec-est"]["estimated"] is True
    assert "estimated" not in rows["muse:rec-plain"]

    agg = store.aggregate_entries(sources=["muse"])
    # Both rows land in the same meta/unknown group with exactly the totals
    # a world without markers would produce: no estimated slice, no separate
    # bucket (tokens_in = input + cache_write = 600 each with the default
    # both-included policy).
    tokens_in = 0
    for ref in agg["all_models"]:
        if ref["name"] == "meta/unknown":
            tokens_in += ref["tokens_in"]
    assert tokens_in == 1200  # 600 + 600: estimated row sums exactly like the plain one
