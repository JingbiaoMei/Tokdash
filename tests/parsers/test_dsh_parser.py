"""Tests for the DeepSeek Harness (dsh) decoder and usage parser.

Binary zstd fixtures are generated here (independently compressed frames
concatenated, matching how dsh appends) rather than committed as blobs.
Format reference: docs/development/technical-notes/DSH_SUPPORT_DESIGN.md.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from zstandard import ZstdCompressor

from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, DSHParser, _sig_cache
from tokdash.sources.dsh_log import (
    decode_dsh_session_file,
    dsh_file_signatures,
    fold_dsh_usage_samples,
    reset_dsh_diagnostics,
)
from tokdash.usage_store import UsageEntryStore

TS_BASE = 1786735098528  # fixed epoch ms; all test events derive from it


def _header(session_id="session-abc", **overrides):
    header = {
        "type": "session",
        "version": 0,
        "id": session_id,
        "createdAt": TS_BASE,
        "cwd": "/home/howard/project",
    }
    header.update(overrides)
    return header


def _event(seq, event_type, data, time_ms=None):
    return {
        "type": event_type,
        "seq": seq,
        "time": TS_BASE + 1000 * seq if time_ms is None else time_ms,
        "data": data,
    }


def _request_context(seq, model="deepseek-v4-flash", provider="deepseek"):
    return _event(seq, "request/context", {"provider": provider, "model": model})


def _usage_chunk(seq, turn, step, **usage):
    return _event(seq, "assistant/chunk", {"turn": turn, "step": step, "chunk": {"type": "usage", "usage": usage}})


def _assistant_message(seq, turn, step, usage, model="deepseek-v4-flash", provider="deepseek"):
    return _event(
        seq,
        "assistant/message",
        {
            "turn": turn,
            "step": step,
            "message": {
                "id": f"a{seq}",
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "source": {"kind": "model", "provider": provider, "model": model},
            },
            "usage": usage,
        },
    )


def _user_message(seq, text="fix the flaky test"):
    return _event(
        seq,
        "user/message",
        {
            "id": f"u{seq}",
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "source": {"kind": "user"},
        },
    )


def _compaction_summary(seq, time_ms, usage, model="deepseek-v4-flash", provider="deepseek"):
    """A v4 compaction/summary: its own billable model call, turn/step null."""
    return _event(
        seq,
        "compaction/summary",
        {
            "turn": None,
            "step": None,
            "usage": usage,
            "model": model,
            "provider": provider,
            "shadowedTokenCount": 12345,
        },
        time_ms=time_ms,
    )


def _assistant_attempt(seq, turn, step, usage=None):
    """An assistant/attempt whose stream carries the attempt's usage chunk.

    Shape taken from dsh's own snapshot suite
    (``snapshots/session/empty-response-retry-current/session.v3.jsonl``): a v2+
    attempt declares no top-level ``usage`` and embeds the raw stream instead,
    ending with a ``finish`` chunk.
    """
    stream = []
    if usage is not None:
        # A raw record in the embedded compact stream: ``{type, time, chunk}``,
        # exactly what dsh's AssistantStreamAccumulator stores for a usage chunk
        # (packages/llm/llm/src/assistant-stream.ts: it never packs a usage chunk
        # into a delta run -- RawStreamChunkType excludes only the delta types).
        stream.append(
            {
                "type": "chunk",
                "time": TS_BASE + 1000 * seq,
                "chunk": {"type": "usage", "usage": usage},
            }
        )
    stream.append(
        {
            "type": "chunk",
            "time": TS_BASE + 1000 * seq + 1,
            "chunk": {
                "type": "finish",
                "reason": {
                    "kind": "error",
                    "failure": {"message": "empty response", "code": "EMPTY_RESPONSE"},
                },
            },
        }
    )
    return _event(seq, "assistant/attempt", {"turn": turn, "step": step, "stream": stream})


def _retry_started(seq, turn=0, step=0, retry=1):
    return _event(
        seq,
        "llm/retry-started",
        {"retryId": "retry-abc", "turn": turn, "step": step, "retry": retry},
    )


def _write_jsonl(path: Path, rows, trailing="") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows) + trailing, encoding="utf-8")
    return path


def _write_zstd(path: Path, rows, *, torn_tail=False) -> Path:
    """Each row becomes its own independently compressed frame, concatenated.

    dsh writes one framed append batch at a time, so a faithful fixture cannot
    be a single zstd stream of the whole file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    compressor = ZstdCompressor()
    payload = b"".join(compressor.compress((json.dumps(row) + "\n").encode("utf-8")) for row in rows)
    if torn_tail:
        payload += compressor.compress(b'{"type": "assistant/chunk", "seq": 99')[:12]
    path.write_bytes(payload)
    return path


@pytest.fixture(autouse=True)
def _isolated_dsh_home(monkeypatch, tmp_path):
    home = tmp_path / "dsh-home"
    monkeypatch.setenv("DSH_HOME", str(home))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reset_dsh_diagnostics()
    yield home
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reset_dsh_diagnostics()


def _zstd_frames(rows):
    """One independently compressed frame per row, as dsh appends them."""
    compressor = ZstdCompressor()
    return [compressor.compress((json.dumps(row) + "\n").encode("utf-8")) for row in rows]


def _write_zstd_frames(path: Path, rows, corrupt_frame: int = None) -> Path:
    """Write one frame per row; flip a byte inside ``corrupt_frame``'s payload."""
    frames = _zstd_frames(rows)
    payload = b"".join(frames)
    if corrupt_frame is not None:
        offset = sum(len(frame) for frame in frames[:corrupt_frame])
        raw = bytearray(payload)
        raw[offset + 5] ^= 0xFF
        payload = bytes(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _session_path(home: Path, session_id="session-abc", project_dir="--home-howard-project--", suffix=".jsonl") -> Path:
    return home / "sessions" / project_dir / session_id / f"session{suffix}"


def _collect(home: Path):
    parser = DSHParser(PricingDatabase())
    return parser.collect(None, None)


# --- case 1/2: raw JSONL and multi-frame zstd --------------------------------


def test_plain_jsonl_basic(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _request_context(1),
            _user_message(2),
            _assistant_message(3, 0, 0, {"inputTokens": 100, "outputTokens": 12}),
        ],
    )
    entries = _collect(home)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["source"] == "dsh"
    assert entry["model"] == "deepseek-v4-flash"
    assert entry["provider"] == "deepseek"
    assert entry["input"] == 100
    assert entry["output"] == 12
    assert entry["cacheRead"] == 0
    assert entry["cacheWrite"] == 0
    assert entry["reasoning"] == 0
    assert entry["timestamp"] == TS_BASE + 3000
    assert entry["entry_id"] == "dsh:session-abc:0:0"


def test_multi_frame_zstd_matches_plain(_isolated_dsh_home):
    home = _isolated_dsh_home
    rows = [
        _header(),
        _request_context(1),
        _user_message(2),
        _assistant_message(3, 0, 0, {"inputTokens": 100, "outputTokens": 12, "cacheReadTokens": 50}),
        _assistant_message(4, 0, 1, {"inputTokens": 200, "outputTokens": 30}),
    ]
    _write_jsonl(_session_path(home, suffix=".jsonl"), rows)
    _write_zstd(_session_path(home, session_id="session-def", suffix=".jsonl.zstd"), [_header("session-def"), *rows[1:]])

    zstd_entries = [e for e in _collect(home) if e["entry_id"].startswith("dsh:session-def:")]
    assert len(zstd_entries) == 2
    assert [e["output"] for e in zstd_entries] == [12, 30]
    assert zstd_entries[0]["cacheRead"] == 50


# --- case 3: torn tail --------------------------------------------------------


def test_trailing_partial_json_line_is_dropped(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _assistant_message(1, 0, 0, {"inputTokens": 10, "outputTokens": 5}),
        ],
        trailing='{"type": "assistant/chunk", "seq": 2, "tim',
    )
    entries = _collect(home)
    assert len(entries) == 1
    assert entries[0]["output"] == 5


def test_torn_final_zstd_frame_keeps_complete_rows(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_zstd(
        _session_path(home, suffix=".jsonl.zstd"),
        [_header(), _assistant_message(1, 0, 0, {"inputTokens": 10, "outputTokens": 5})],
        torn_tail=True,
    )
    entries = _collect(home)
    assert len(entries) == 1
    assert entries[0]["output"] == 5


# --- cases 4/5: the replace-not-add fold --------------------------------------


def test_chunk_replaced_by_final_message_same_step(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _request_context(1),
            _usage_chunk(2, 0, 0, inputTokens=100, outputTokens=10),
            _assistant_message(3, 0, 0, {"inputTokens": 100, "outputTokens": 12}),
        ],
    )
    entries = _collect(home)
    # One row, not two, and it carries the finalized sample's numbers.
    assert len(entries) == 1
    assert entries[0]["output"] == 12
    # The id is stable across the in-file replacement: not a physical line number.
    assert entries[0]["entry_id"] == "dsh:session-abc:0:0"


def test_early_chunk_without_final_message_is_counted(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _request_context(1),
            _usage_chunk(2, 0, 0, inputTokens=100, outputTokens=10),
        ],
    )
    entries = _collect(home)
    assert len(entries) == 1
    assert entries[0]["input"] == 100
    assert entries[0]["model"] == "deepseek-v4-flash"  # latest request/context.model


def test_fold_is_adjacency_keyed_not_global(_isolated_dsh_home):
    """A later step ends the previous one; the same key may legitimately recur
    in a later turn and must then count as its own row."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _usage_chunk(1, 0, 0, inputTokens=1, outputTokens=1),
            _assistant_message(2, 0, 1, {"inputTokens": 2, "outputTokens": 2}),
            _assistant_message(3, 1, 0, {"inputTokens": 3, "outputTokens": 3}),
        ],
    )
    entries = _collect(home)
    assert len(entries) == 3
    assert [e["entry_id"] for e in entries] == [
        "dsh:session-abc:0:0",
        "dsh:session-abc:0:1",
        "dsh:session-abc:1:0",
    ]


# --- case 6/7: field mapping ---------------------------------------------------


def test_reasoning_tokens_not_double_counted(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _assistant_message(
                1, 0, 0,
                {"inputTokens": 100, "outputTokens": 50, "reasoningTokens": 40},
            ),
        ],
    )
    entries = _collect(home)
    assert len(entries) == 1
    # outputTokens already includes reasoningTokens; the aggregate reasoning
    # field would count them twice.
    assert entries[0]["output"] == 50
    assert entries[0]["reasoning"] == 0


def test_cache_read_write_mapping_and_cost(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _assistant_message(
                1, 0, 0,
                {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 500, "cacheWriteTokens": 60},
            ),
        ],
    )
    entries = _collect(home)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["input"] == 100
    assert entry["cacheRead"] == 500
    assert entry["cacheWrite"] == 60
    assert entry["cost"] == pytest.approx(
        PricingDatabase().get_cost("deepseek-v4-flash", 100, 20, 500, 60)
    )


def test_all_zero_sample_and_absent_usage_emit_nothing(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _assistant_message(1, 0, 0, {"inputTokens": 0, "outputTokens": 0}),
            _event(2, "assistant/message", {"turn": 0, "step": 1, "message": {"id": "a2", "role": "assistant", "content": [], "source": {"kind": "model", "provider": "deepseek", "model": "deepseek-v4-flash"}}}),
            _usage_chunk(3, 0, 2, outputTokens=7),  # missing inputTokens: not a numeric pair
        ],
    )
    assert _collect(home) == []


def test_missing_model_falls_back_to_unknown_never_timestamp(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _event(1, "assistant/message", {
                "turn": 0,
                "step": 0,
                "message": {"id": "a1", "role": "assistant", "content": [], "source": {"kind": "model"}},
                "usage": {"inputTokens": 5, "outputTokens": 5},
            }),
        ],
    )
    entries = _collect(home)
    assert len(entries) == 1
    assert entries[0]["model"] == "unknown"
    assert entries[0]["cost"] == 0.0


# --- cases 8/9: fork and subagent behavior ------------------------------------


def test_fork_seed_boundary_skips_inherited_prefix(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, session_id="session-child"),
        [
            _header("session-child", parentSession="session-parent", seedLength=3),
            # seq 1 and 2 are the cloned parent prefix; the parent owns them.
            _assistant_message(1, 0, 0, {"inputTokens": 900, "outputTokens": 90}),
            _assistant_message(2, 0, 1, {"inputTokens": 800, "outputTokens": 80}),
            _assistant_message(3, 1, 0, {"inputTokens": 100, "outputTokens": 10}),
        ],
    )
    entries = _collect(home)
    assert len(entries) == 1
    assert entries[0]["entry_id"] == "dsh:session-child:1:0"
    assert entries[0]["input"] == 100


def test_fresh_child_with_parent_session_counts_normally(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, session_id="session-subagent"),
        [
            _header("session-subagent", parentSession="session-parent", delegationDepth=1),
            _assistant_message(1, 0, 0, {"inputTokens": 42, "outputTokens": 7}),
        ],
    )
    entries = _collect(home)
    assert len(entries) == 1
    assert entries[0]["input"] == 42


# --- case 11: header gating and malformed-file isolation -----------------------


def test_unsupported_header_version_skips_file(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(version=1),
            _assistant_message(1, 0, 0, {"inputTokens": 10, "outputTokens": 5}),
        ],
    )
    assert _collect(home) == []
    decoded = decode_dsh_session_file(_session_path(home))
    assert decoded.skip_reason == "unsupported-version"


def test_missing_or_invalid_header_skips_file(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(_session_path(home, session_id="no-header"), [_assistant_message(1, 0, 0, {"inputTokens": 1, "outputTokens": 1})])
    _session_path(home, session_id="bad-header").parent.mkdir(parents=True)
    _session_path(home, session_id="bad-header").write_text("not json at all\n", encoding="utf-8")

    assert decode_dsh_session_file(_session_path(home, session_id="no-header")).skip_reason == "missing-header"
    assert decode_dsh_session_file(_session_path(home, session_id="bad-header")).skip_reason == "invalid-header"
    assert _collect(home) == []


def test_one_malformed_file_never_blanks_the_source(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, session_id="good"),
        [_header("good"), _assistant_message(1, 0, 0, {"inputTokens": 10, "outputTokens": 5})],
    )
    bad = _session_path(home, session_id="corrupt", suffix=".jsonl.zstd")
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"\x28\xb5\x2f\xfd corrupt bytes not a real frame")

    entries = _collect(home)
    assert len(entries) == 1
    assert entries[0]["entry_id"] == "dsh:good:0:0"


def test_missing_dsh_home_is_an_empty_source(_isolated_dsh_home):
    # The autouse fixture points DSH_HOME at a directory that does not exist yet.
    assert _collect(_isolated_dsh_home) == []


def test_empty_credential_failure_session_produces_no_rows(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _request_context(1),
            _user_message(2),
            _event(3, "turn/end", {"turn": 0, "reason": {"kind": "error", "failure": {"message": "401 unauthorized", "code": "AUTH"}}}),
        ],
    )
    assert _collect(home) == []


# --- case 12: clientpaths discovery --------------------------------------------


def test_dsh_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "custom"))
    assert clientpaths.dsh_home() == tmp_path / "custom"
    assert clientpaths.dsh_sessions_dir() == tmp_path / "custom" / "sessions"


def test_dsh_home_blank_env_is_unset(monkeypatch):
    monkeypatch.setenv("DSH_HOME", "   ")
    assert clientpaths.dsh_home() == Path.home() / ".dsh"
    monkeypatch.delenv("DSH_HOME")
    assert clientpaths.dsh_sessions_dir() == Path.home() / ".dsh" / "sessions"


def test_dsh_home_expands_leading_tilde_and_makes_absolute(monkeypatch):
    monkeypatch.setenv("DSH_HOME", "~/dsh-alt")
    assert clientpaths.dsh_home() == Path.home() / "dsh-alt"
    monkeypatch.setenv("DSH_HOME", "relative-dsh")
    assert clientpaths.dsh_home().is_absolute()


def test_dsh_home_macos_style(monkeypatch):
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/howard")))
    assert clientpaths.dsh_sessions_dir() == Path("/Users/howard/.dsh/sessions")


def test_windows_style_project_dir_discovery(_isolated_dsh_home):
    """The project dir name is lossy (``--C-Users-...--`` on Windows); discovery
    is recursive and never decodes it."""
    home = _isolated_dsh_home
    path = _session_path(home, project_dir="--C-Users-H1937-project--", suffix=".jsonl.zstd")
    _write_zstd(path, [_header(), _assistant_message(1, 0, 0, {"inputTokens": 3, "outputTokens": 2})])
    sigs = dsh_file_signatures(clientpaths.dsh_sessions_dir())
    assert [Path(s[0]).name for s in sigs] == ["session.jsonl.zstd"]
    assert len(_collect(home)) == 1


# --- case 13: usage-store sync replaces a chunk row with its final row ---------


def test_usage_store_sync_replaces_chunk_row_with_final(_isolated_dsh_home):
    from tokdash.compute import _collect_parser_file

    home = _isolated_dsh_home
    path = _session_path(home)
    _write_jsonl(
        path,
        [_header(), _request_context(1), _usage_chunk(2, 0, 0, inputTokens=100, outputTokens=10)],
    )

    parser = DSHParser(PricingDatabase())
    store = UsageEntryStore()

    def sig():
        stat = path.stat()
        return (str(path), int(stat.st_mtime_ns), int(stat.st_size))

    assert store.sync_files("dsh", [sig()], parser={"v": 1}, parse_file_entries=lambda fs: _collect_parser_file(parser, fs))
    rows = store.query_entries(sources=["dsh"])
    assert [row["output"] for row in rows] == [10]

    # The provider call settles: dsh appends the finalized assistant/message,
    # which replaces the same-(turn, step) chunk row instead of adding to it.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_assistant_message(3, 0, 0, {"inputTokens": 100, "outputTokens": 12})) + "\n")

    assert store.sync_files("dsh", [sig()], parser={"v": 1}, parse_file_entries=lambda fs: _collect_parser_file(parser, fs))
    rows = store.query_entries(sources=["dsh"])
    assert len(rows) == 1
    assert rows[0]["output"] == 12
    assert rows[0]["entry_id"] == "dsh:session-abc:0:0"


# --- fold unit edges ------------------------------------------------------------


def test_fold_ignores_unknown_events_and_non_usage_chunks():
    header = _header()
    events = tuple(
        [
            _event(1, "todo/write", {"todos": []}),
            _event(2, "assistant/chunk", {"turn": 0, "step": 0, "chunk": {"type": "text-delta", "index": 0, "text": "hi"}}),
            _usage_chunk(3, 0, 0, inputTokens=5, outputTokens=5),
        ]
    )
    samples = fold_dsh_usage_samples(header, events)
    assert len(samples) == 1
    assert samples[0]["input"] == 5


# --- review-fix regressions ------------------------------------------------------


def test_duplicate_physical_files_dedup_by_entry_id(_isolated_dsh_home):
    """Both suffixes for one session id — and one id under two project keys —
    must bill once in the live (non-persistent-store) usage path."""
    home = _isolated_dsh_home
    rows = [_header(), _assistant_message(1, 0, 0, {"inputTokens": 10, "outputTokens": 5})]
    _write_jsonl(_session_path(home, suffix=".jsonl"), rows)
    _write_zstd(_session_path(home, suffix=".jsonl.zstd"), rows)
    _write_jsonl(_session_path(home, project_dir="--other-key--"), rows)

    entries = _collect(home)
    assert len(entries) == 1
    assert entries[0]["entry_id"] == "dsh:session-abc:0:0"


def test_usage_sample_without_time_is_skipped(_isolated_dsh_home):
    """A timestamp-0 sample would vanish from date-ranged views yet count in
    unfiltered totals; skip it instead (Pi precedent)."""
    home = _isolated_dsh_home
    event = _assistant_message(1, 0, 0, {"inputTokens": 10, "outputTokens": 5})
    del event["time"]
    _write_jsonl(_session_path(home), [_header(), event])
    assert _collect(home) == []


def test_missing_seq_fails_closed_under_seed_length(_isolated_dsh_home):
    """With seedLength declared, an event whose seq is unreadable cannot prove
    it is not inherited parent history — it is skipped like a below-boundary
    event."""
    home = _isolated_dsh_home
    no_seq = _assistant_message(1, 0, 0, {"inputTokens": 900, "outputTokens": 90})
    del no_seq["seq"]
    _write_jsonl(
        _session_path(home, session_id="session-child"),
        [
            _header("session-child", parentSession="session-parent", seedLength=3),
            no_seq,
            _assistant_message(3, 1, 0, {"inputTokens": 100, "outputTokens": 10}),
        ],
    )
    entries = _collect(home)
    assert [e["entry_id"] for e in entries] == ["dsh:session-child:1:0"]


def test_negative_token_buckets_are_skipped(_isolated_dsh_home):
    """Negative buckets would price below zero, and mixed signs would cancel to
    defeat the all-zero guard."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _assistant_message(1, 0, 0, {"inputTokens": -500, "outputTokens": 10}),
            _assistant_message(2, 0, 1, {"inputTokens": -100, "outputTokens": 100}),
            _assistant_message(3, 0, 2, {"inputTokens": 10, "outputTokens": 5, "cacheWriteTokens": -1}),
            _assistant_message(4, 1, 0, {"inputTokens": 10, "outputTokens": 5}),
        ],
    )
    entries = _collect(home)
    assert [e["entry_id"] for e in entries] == ["dsh:session-abc:1:0"]


# --- issue #147: a damaged file must not blank the source or erase stored rows ---


def test_corrupt_interior_frame_keeps_the_frames_around_it(_isolated_dsh_home):
    """dsh frames every append batch independently, so one bad byte costs that
    batch and nothing else. Whole-file decoding lost every frame in the file."""
    home = _isolated_dsh_home
    _write_zstd_frames(
        _session_path(home, suffix=".jsonl.zstd"),
        [
            _header(),
            _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _assistant_message(2, 1, 0, {"inputTokens": 200, "outputTokens": 20}),
            _assistant_message(3, 2, 0, {"inputTokens": 300, "outputTokens": 30}),
        ],
        corrupt_frame=2,
    )

    assert sorted(entry["input"] for entry in _collect(home)) == [100, 300]


def test_corrupt_interior_frame_is_reported(_isolated_dsh_home, caplog):
    """A short read is allowed; a silent one is the bug this replaces."""
    home = _isolated_dsh_home
    _write_zstd_frames(
        _session_path(home, suffix=".jsonl.zstd"),
        [
            _header(),
            _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _assistant_message(2, 1, 0, {"inputTokens": 200, "outputTokens": 20}),
        ],
        corrupt_frame=1,
    )

    _collect(home)

    assert "frames-lost" in caplog.text


def test_damaged_header_frame_fails_closed(_isolated_dsh_home):
    """No header means no version to gate and no session id, so the file is
    skipped whole instead of billed under a guessed format generation."""
    home = _isolated_dsh_home
    _write_zstd_frames(
        _session_path(home, suffix=".jsonl.zstd"),
        [_header(), _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10})],
        corrupt_frame=0,
    )

    assert _collect(home) == []


def test_unreadable_file_keeps_its_stored_rows(_isolated_dsh_home, caplog):
    """Under file_replace an empty parse means "this file now has zero entries",
    so a file that merely became unreadable must raise rather than return []."""
    from tokdash.compute import _collect_parser_file

    home = _isolated_dsh_home
    path = _session_path(home)
    _write_jsonl(path, [_header(), _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10})])

    parser = DSHParser(PricingDatabase())
    store = UsageEntryStore()

    def sig():
        stat = path.stat()
        return (str(path), int(stat.st_mtime_ns), int(stat.st_size))

    assert store.sync_files(
        "dsh", [sig()], parser={"v": 1}, parse_file_entries=lambda fs: _collect_parser_file(parser, fs)
    )
    assert len(store.query_entries(sources=["dsh"])) == 1

    # The same path now carries a header generation this build does not read.
    _write_jsonl(
        path, [_header(version=99), _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10})]
    )
    store.sync_files(
        "dsh", [sig()], parser={"v": 1}, parse_file_entries=lambda fs: _collect_parser_file(parser, fs)
    )

    assert len(store.query_entries(sources=["dsh"])) == 1
    assert "skip:unsupported-version" in caplog.text


def test_unreadable_file_rows_return_when_it_reads_again(_isolated_dsh_home):
    """Keeping the rows is a hold, not a freeze: the file is reparsed once it is
    readable, so a newer turn lands instead of being shadowed forever."""
    from tokdash.compute import _collect_parser_file

    home = _isolated_dsh_home
    path = _session_path(home)
    _write_jsonl(path, [_header(), _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10})])

    parser = DSHParser(PricingDatabase())
    store = UsageEntryStore()

    def sig():
        stat = path.stat()
        return (str(path), int(stat.st_mtime_ns), int(stat.st_size))

    store.sync_files(
        "dsh", [sig()], parser={"v": 1}, parse_file_entries=lambda fs: _collect_parser_file(parser, fs)
    )
    _write_jsonl(path, [_header(version=99)])
    store.sync_files(
        "dsh", [sig()], parser={"v": 1}, parse_file_entries=lambda fs: _collect_parser_file(parser, fs)
    )
    _write_jsonl(
        path,
        [
            _header(),
            _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _assistant_message(2, 1, 0, {"inputTokens": 200, "outputTokens": 20}),
        ],
    )
    store.sync_files(
        "dsh", [sig()], parser={"v": 1}, parse_file_entries=lambda fs: _collect_parser_file(parser, fs)
    )

    rows = store.query_entries(sources=["dsh"])
    assert sorted(row["input"] for row in rows) == [100, 200]


# --- format generation 4 (dsh >= 0.2.0) ----------------------------------------


def _end_seed(seq, inherited=False):
    return _event(seq, "session/end-seed", {"inherited": True} if inherited else {})


def test_v4_generation_is_read_and_billed(_isolated_dsh_home):
    """dsh 0.2.0 writes session.v4.jsonl.zstd with header version 4. The usage
    event keeps the v0 shape, so only the gate had to grow -- discovery already
    matched the versioned filename on its suffix."""
    home = _isolated_dsh_home
    _write_zstd(
        _session_path(home, suffix=".v4.jsonl.zstd"),
        [
            _header(version=4, isSeeded=False, delegationDepth=0),
            _assistant_message(1, 1, 1, {"inputTokens": 11309, "outputTokens": 2}, model="deepseek-flash"),
        ],
    )

    entries = _collect(home)
    assert [(entry["input"], entry["output"]) for entry in entries] == [(11309, 2)]
    assert entries[0]["entry_id"] == "dsh:session-abc:1:1"


def test_v4_seeded_session_skips_the_inherited_prefix(_isolated_dsh_home):
    """v4 dropped header seedLength: the cut is the tagged session/end-seed
    marker, and the events before it are the parent's already-billed prefix."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=True),
            _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _end_seed(2, inherited=True),
            _assistant_message(3, 1, 0, {"inputTokens": 300, "outputTokens": 30}),
        ],
    )

    entries = _collect(home)
    assert [entry["input"] for entry in entries] == [300]


def test_v4_plain_resume_marker_does_not_move_the_fork_cut(_isolated_dsh_home):
    """A forked session that is later resumed must keep billing its own work.

    dsh appends a plain session/end-seed (empty data) on every resume of a
    session, seeded or not, and it lands at the END of the log. Taking the last marker of
    either kind therefore moves the cut past everything the fork did on its own --
    silently dropping it from the first resume onward, and more on each resume.
    Only an ``inherited: true`` marker is a cut.
    """
    home = _isolated_dsh_home
    rows = [_header(version=4, isSeeded=True), _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10})]
    rows.append(_end_seed(2, inherited=True))  # the fork cut
    rows += [
        _assistant_message(seq, seq, 0, {"inputTokens": 300, "outputTokens": 30}) for seq in (3, 4, 5)
    ]
    rows.append(_end_seed(6))  # a later resume appends a plain marker here
    _write_jsonl(_session_path(home, suffix=".v4.jsonl"), rows)

    entries = _collect(home)
    # Own work after the inherited cut bills in full; only the inherited prefix
    # (100 tokens) is skipped.
    assert sorted(entry["input"] for entry in entries) == [300, 300, 300]


def test_v4_plain_resume_marker_alone_bills_everything(_isolated_dsh_home):
    """An unseeded session has no cut to find: plain markers are resume
    bookkeeping, and its whole log is its own already-billed work."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=False),
            _assistant_message(1, 1, 1, {"inputTokens": 100, "outputTokens": 10}),
            _end_seed(2),
            _assistant_message(3, 2, 1, {"inputTokens": 300, "outputTokens": 30}),
        ],
    )

    assert sorted(entry["input"] for entry in _collect(home)) == [100, 300]


def test_v4_seeded_without_marker_bills_nothing(_isolated_dsh_home, caplog):
    """A seeded log with no INHERITED marker cannot be split safely: billing all
    of it double-bills the parent, so nothing is billed and the reason is named.
    A plain resume marker does not count as a boundary."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=True),
            _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _end_seed(2),
        ],
    )

    assert _collect(home) == []
    assert "seed-unprovable" in caplog.text


def test_v3_generation_is_read_with_the_v4_seed_rule(_isolated_dsh_home):
    """Earlier dsh Desktop builds left generation-3 logs with the v4 header key
    set, and dsh's own v3 reader cuts a seeded log at the final inherited
    end-seed marker exactly as v4 does. So v3 is the gate plus the v4 rule: the
    inherited prefix is skipped and own work after a plain resume marker bills."""
    home = _isolated_dsh_home
    _write_zstd(
        _session_path(home, suffix=".v3.jsonl.zstd"),
        [
            _header(version=3, isSeeded=True, delegationDepth=0),
            _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _end_seed(2, inherited=True),
            _assistant_message(3, 1, 1, {"inputTokens": 300, "outputTokens": 30}),
            _end_seed(4),
            _assistant_message(5, 2, 1, {"inputTokens": 500, "outputTokens": 50}),
        ],
    )

    assert sorted(entry["input"] for entry in _collect(home)) == [300, 500]


def test_unknown_generation_is_skipped_and_named(_isolated_dsh_home, caplog):
    """A future dsh bump must show up as a named skip, not as a user who appears
    to have stopped using the tool."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v9.jsonl"),
        [_header(version=9), _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10})],
    )

    assert _collect(home) == []
    assert "skip:unsupported-version" in caplog.text


# --- issue #148: one corpus, one winner, on every surface -----------------------


def test_non_adjacent_repeated_turn_step_folds_to_one_sample():
    """The fold replaced only the previous sample, so a key repeating after a
    different one produced two samples sharing one entry id -- which Overview
    collapsed and Sessions kept."""
    samples = fold_dsh_usage_samples(
        _header(),
        (
            _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _assistant_message(2, 1, 0, {"inputTokens": 300, "outputTokens": 30}),
            _assistant_message(3, 0, 0, {"inputTokens": 111, "outputTokens": 22}),
        ),
    )

    assert [(s["turn"], s["step"], s["input"]) for s in samples] == [(0, 0, 111), (1, 0, 300)]


def test_duplicate_files_winner_does_not_depend_on_parse_order(_isolated_dsh_home):
    """Same session id under two project keys with different usage for one
    (turn, step): the earliest (timestamp, path) copy wins, which is the rule
    sync_files and the Sessions merge already apply. Previously the later file
    parsed overwrote it, so Overview and Sessions billed different copies."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [_header(), _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10})],
    )
    _write_zstd(
        _session_path(home, project_dir="--other-key--", suffix=".jsonl.zstd"),
        [_header(), _assistant_message(1, 0, 0, {"inputTokens": 999, "outputTokens": 99})],
    )

    entries = _collect(home)
    assert len(entries) == 1
    assert entries[0]["input"] == 100


def test_duplicate_copies_bill_once_in_the_persistent_store(_isolated_dsh_home):
    """cross_file_stable_keys makes the store keep the earliest (timestamp,
    path) copy instead of whichever file was committed last."""
    from tokdash.compute import _collect_parser_file

    home = _isolated_dsh_home
    first = _write_jsonl(
        _session_path(home),
        [_header(), _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10})],
    )
    second = _write_zstd(
        _session_path(home, project_dir="--other-key--", suffix=".jsonl.zstd"),
        [_header(), _assistant_message(1, 0, 0, {"inputTokens": 999, "outputTokens": 99})],
    )

    parser = DSHParser(PricingDatabase())
    store = UsageEntryStore()

    def sigs():
        return tuple(
            (str(path), int(path.stat().st_mtime_ns), int(path.stat().st_size))
            for path in (first, second)
        )

    store.sync_files(
        "dsh",
        sigs(),
        parser={"v": 1},
        parse_file_entries=lambda fs: _collect_parser_file(parser, fs),
        cross_file_stable_keys=True,
    )

    rows = store.query_entries(sources=["dsh"])
    assert len(rows) == 1
    assert rows[0]["input"] == 100


# --- issue #171: compaction usage, attempt usage, recurring diagnostics ---------


def test_compaction_summary_usage_is_counted(_isolated_dsh_home):
    """A compaction/summary is its own billable model call with turn/step null,
    so it never went through the (turn, step) fold. It now bills under its own
    time-keyed entry id (#171)."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=False),
            _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _compaction_summary(
                2,
                TS_BASE + 5000,
                {
                    "inputTokens": 3,
                    "outputTokens": 4429,
                    "cacheReadTokens": 5130,
                    "cacheWriteTokens": 139885,
                },
            ),
        ],
    )

    entries = _collect(home)
    assert [entry["entry_id"] for entry in entries] == [
        "dsh:session-abc:0:0",
        f"dsh:session-abc:c:{TS_BASE + 5000}",
    ]
    compaction = entries[1]
    assert (compaction["input"], compaction["output"]) == (3, 4429)
    assert compaction["cacheRead"] == 5130
    assert compaction["cacheWrite"] == 139885
    assert compaction["model"] == "deepseek-v4-flash"
    assert compaction["provider"] == "deepseek"


def test_compaction_usage_absent_is_not_zero(_isolated_dsh_home):
    """``compaction/summary.usage`` is optional; an absent object stays absent
    rather than becoming a zero row."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=False),
            _event(
                1,
                "compaction/summary",
                {"turn": None, "step": None, "model": "deepseek-v4-flash", "provider": "deepseek"},
                time_ms=TS_BASE + 5000,
            ),
        ],
    )
    assert _collect(home) == []


def test_compaction_copies_dedup_by_time_not_seq(_isolated_dsh_home):
    """Two physical copies of one session share timestamps even when a format
    generation renumbers their dense seq positions, so the compaction entry id
    is keyed on time and the earliest-(timestamp, path) copy wins, exactly as
    #148 requires. A seq-keyed id would bill such a pair twice."""
    home = _isolated_dsh_home
    # Same session id and compaction time, different seq -- what a renumbered
    # generation of one session presents.
    _write_jsonl(
        _session_path(home),
        [_header(), _compaction_summary(2, TS_BASE + 5000, {"inputTokens": 3, "outputTokens": 100})],
    )
    _write_jsonl(
        _session_path(home, project_dir="--other-key--", suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=False),
            _compaction_summary(9, TS_BASE + 5000, {"inputTokens": 3, "outputTokens": 100}),
        ],
    )

    entries = _collect(home)
    assert len(entries) == 1
    assert entries[0]["entry_id"] == f"dsh:session-abc:c:{TS_BASE + 5000}"


def test_attempt_only_usage_is_counted(_isolated_dsh_home):
    """From dsh v2 on, a call that never produced a final message leaves only an
    assistant/attempt, whose stream carries the usage. Without reading it that
    provider-billed usage went missing (#171)."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=False),
            _assistant_attempt(1, 0, 0, {"inputTokens": 250, "outputTokens": 40}),
        ],
    )

    entries = _collect(home)
    assert [(e["entry_id"], e["input"], e["output"]) for e in entries] == [
        ("dsh:session-abc:0:0", 250, 40)
    ]


def test_attempt_usage_is_replaced_by_a_later_final_message(_isolated_dsh_home):
    """An attempt's usage chunk and the finalized message share a (turn, step),
    so the final message still replaces it instead of double-counting."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=False),
            _assistant_attempt(1, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _assistant_message(2, 0, 0, {"inputTokens": 100, "outputTokens": 12}),
        ],
    )

    entries = _collect(home)
    assert len(entries) == 1
    assert entries[0]["output"] == 12
    assert entries[0]["entry_id"] == "dsh:session-abc:0:0"


@pytest.mark.parametrize("version", [3, 4])
@pytest.mark.parametrize("settlement", [_assistant_message, _assistant_attempt], ids=["message", "attempt"])
def test_started_retry_keeps_the_previous_attempts_usage(_isolated_dsh_home, version, settlement):
    """Separate provider calls add across a retry boundary on both generations."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=f".v{version}.jsonl"),
        [
            _header(version=version, isSeeded=False),
            _request_context(1),
            _assistant_attempt(2, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _retry_started(3),
            settlement(4, 0, 0, {"inputTokens": 200, "outputTokens": 20}),
        ],
    )

    entries = _collect(home)
    assert [(e["entry_id"], e["input"], e["output"]) for e in entries] == [
        ("dsh:session-abc:0:0", 100, 10),
        ("dsh:session-abc:0:0:r:1", 200, 20),
    ]
    pricing = PricingDatabase()
    assert sum(e["cost"] for e in entries) == pytest.approx(
        pricing.get_cost("deepseek-v4-flash", 100, 10)
        + pricing.get_cost("deepseek-v4-flash", 200, 20)
    )


def test_multiple_retries_replace_usage_only_within_each_attempt(_isolated_dsh_home):
    """Early chunks and settlements replace each other without erasing retries."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home),
        [
            _header(),
            _request_context(1),
            _usage_chunk(2, 0, 0, inputTokens=100, outputTokens=10),
            _retry_started(3),
            _usage_chunk(4, 0, 0, inputTokens=200, outputTokens=20),
            _assistant_attempt(5, 0, 0, {"inputTokens": 220, "outputTokens": 22}),
            _retry_started(6, retry=2),
            _usage_chunk(7, 0, 0, inputTokens=300, outputTokens=30),
            _assistant_message(8, 0, 0, {"inputTokens": 330, "outputTokens": 33}),
        ],
    )

    entries = _collect(home)
    assert [(e["entry_id"], e["input"], e["output"]) for e in entries] == [
        ("dsh:session-abc:0:0", 100, 10),
        ("dsh:session-abc:0:0:r:1", 220, 22),
        ("dsh:session-abc:0:0:r:2", 330, 33),
    ]


@pytest.mark.parametrize("usage", [None, {"inputTokens": 0, "outputTokens": 0}])
def test_retry_identity_does_not_depend_on_previous_usage(_isolated_dsh_home, usage):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=False),
            _assistant_attempt(1, 0, 0, usage),
            _retry_started(2),
            _assistant_message(3, 0, 0, {"inputTokens": 200, "outputTokens": 20}),
        ],
    )
    assert [e["entry_id"] for e in _collect(home)] == ["dsh:session-abc:0:0:r:1"]


def test_scheduled_retry_does_not_open_a_new_attempt(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=False),
            _assistant_attempt(1, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _event(2, "llm/retry", {"retryId": "retry-abc", "turn": 0, "step": 0, "retry": 1}),
            _assistant_message(3, 0, 0, {"inputTokens": 200, "outputTokens": 20}),
        ],
    )
    assert [(e["entry_id"], e["input"]) for e in _collect(home)] == [("dsh:session-abc:0:0", 200)]


def test_retry_boundaries_are_scoped_to_their_step(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=False),
            _assistant_attempt(1, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _retry_started(2, step=1),
            _assistant_message(3, 0, 0, {"inputTokens": 200, "outputTokens": 20}),
        ],
    )
    assert [(e["entry_id"], e["input"]) for e in _collect(home)] == [("dsh:session-abc:0:0", 200)]


def test_retry_copies_dedup_when_sequence_positions_differ(_isolated_dsh_home):
    home = _isolated_dsh_home
    events = [
        _request_context(1),
        _assistant_attempt(2, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
        _retry_started(3),
        _assistant_message(4, 0, 0, {"inputTokens": 200, "outputTokens": 20}),
    ]
    _write_jsonl(_session_path(home, suffix=".v3.jsonl"), [_header(version=3, isSeeded=False), *events])
    _write_jsonl(
        _session_path(home, project_dir="--other-key--", suffix=".v4.jsonl"),
        [_header(version=4, isSeeded=False), *[dict(e, seq=e["seq"] + 10) for e in events]],
    )
    assert [(e["entry_id"], e["input"]) for e in _collect(home)] == [
        ("dsh:session-abc:0:0", 100),
        ("dsh:session-abc:0:0:r:1", 200),
    ]


def test_inherited_retry_does_not_advance_the_childs_attempt(_isolated_dsh_home):
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=True),
            _request_context(1),
            _assistant_attempt(2, 0, 0, {"inputTokens": 100, "outputTokens": 10}),
            _retry_started(3),
            _end_seed(4, inherited=True),
            _assistant_attempt(5, 0, 0, {"inputTokens": 200, "outputTokens": 20}),
            _retry_started(6, retry=2),
            _assistant_message(7, 0, 0, {"inputTokens": 300, "outputTokens": 30}),
        ],
    )
    assert [(e["entry_id"], e["input"]) for e in _collect(home)] == [
        ("dsh:session-abc:0:0", 200),
        ("dsh:session-abc:0:0:r:1", 300),
    ]


def test_attempt_without_a_usage_chunk_is_ignored(_isolated_dsh_home):
    """An attempt that streamed no usage contributes nothing, and does not
    fabricate a zero row."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [_header(version=4, isSeeded=False), _assistant_attempt(1, 0, 0, None)],
    )
    assert _collect(home) == []


def test_message_usage_from_stream_when_data_usage_absent(_isolated_dsh_home):
    """Upstream's token-meter reads `data.usage`, and falls back to the last
    usage chunk in the settlement's `stream` when the adapter omitted it
    (packages/llm/token-meter/src/usage-projection.ts). A finalized message whose
    usage lives only in its stream must still be billed (#171)."""
    home = _isolated_dsh_home
    usage = {"inputTokens": 120, "outputTokens": 30}
    message = _event(
        1,
        "assistant/message",
        {
            "turn": 0,
            "step": 0,
            "message": {
                "id": "a1",
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "source": {"kind": "model", "provider": "deepseek", "model": "deepseek-v4-flash"},
            },
            # No top-level `usage`; only the embedded stream carries it.
            "stream": [
                {"type": "chunk", "time": TS_BASE, "chunk": {"type": "usage", "usage": usage}}
            ],
        },
    )
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [_header(version=4, isSeeded=False), message],
    )

    entries = _collect(home)
    assert [(e["entry_id"], e["input"], e["output"]) for e in entries] == [
        ("dsh:session-abc:0:0", 120, 30)
    ]


def test_compaction_before_the_fork_cut_is_not_billed(_isolated_dsh_home):
    """A compaction inside a forked session's inherited prefix is a call the
    parent already billed. The compaction branch rides the same seed filter as
    every other shape, so the child must not bill it a second time."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=True),
            _compaction_summary(1, TS_BASE + 1000, {"inputTokens": 3, "outputTokens": 4429}),
            _end_seed(2, inherited=True),
            _assistant_message(3, 1, 0, {"inputTokens": 300, "outputTokens": 30}),
        ],
    )

    entries = _collect(home)
    assert [entry["input"] for entry in entries] == [300]


def test_compaction_with_an_invalid_bucket_is_dropped(_isolated_dsh_home):
    """A compaction is billable, but a malformed one is still unusable: a
    negative bucket makes the whole sample unusable rather than billing a
    negative count."""
    home = _isolated_dsh_home
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [
            _header(version=4, isSeeded=False),
            _compaction_summary(1, TS_BASE + 1000, {"inputTokens": -1, "outputTokens": 4429}),
        ],
    )
    assert _collect(home) == []


def test_attempt_takes_the_last_usage_chunk_in_its_stream(_isolated_dsh_home):
    """A provider may report usage more than once in one stream. Upstream's
    `lastAssistantStreamChunk` takes the last one, so this fold must too."""
    home = _isolated_dsh_home
    first = {"inputTokens": 10, "outputTokens": 1}
    last = {"inputTokens": 200, "outputTokens": 20}
    attempt = _event(
        1,
        "assistant/attempt",
        {
            "turn": 0,
            "step": 0,
            "stream": [
                {"type": "chunk", "time": TS_BASE, "chunk": {"type": "usage", "usage": first}},
                {"type": "chunk", "time": TS_BASE + 5, "chunk": {"type": "usage", "usage": last}},
            ],
        },
    )
    _write_jsonl(
        _session_path(home, suffix=".v4.jsonl"),
        [_header(version=4, isSeeded=False), attempt],
    )

    entries = _collect(home)
    assert [(e["entry_id"], e["input"], e["output"]) for e in entries] == [
        ("dsh:session-abc:0:0", 200, 20)
    ]


def test_diagnostics_warn_again_after_the_file_recovers(_isolated_dsh_home, caplog):
    """report_dsh_diagnostic warns once per (path, kind, detail) and nothing in
    production ever cleared the registry, so a file that recovered and then
    broke again stayed silent for the rest of the process (#171). A clean decode
    forgets the path, so the recurrence is reported again -- while a file that
    stays broken still warns only once."""
    from tokdash.sources import dsh_log

    reset_dsh_diagnostics()
    caplog.set_level(logging.WARNING, logger="tokdash.sources.dsh_log")
    home = _isolated_dsh_home
    path = _session_path(home, suffix=".jsonl.zstd")
    rows = [_header(), _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10})]

    def decode_once():
        _sig_cache.clear()
        BaseParser._entry_cache.clear()
        decoded = decode_dsh_session_file(path)
        dsh_log.report_dsh_decode(str(path), decoded)
        return decoded

    _write_zstd_frames(path, rows, corrupt_frame=1)
    assert decode_once().failed_frames == 1
    # Still corrupt: the same problem must not warn a second time.
    assert decode_once().failed_frames == 1

    _write_zstd_frames(path, rows)
    assert decode_once().failed_frames == 0

    _write_zstd_frames(path, rows, corrupt_frame=1)
    assert decode_once().failed_frames == 1

    assert caplog.text.count("frames-lost") == 2


def test_healthy_zstd_never_takes_the_recovery_path(_isolated_dsh_home, monkeypatch):
    """The per-frame recovery path re-slices the remaining buffer once per frame
    and is quadratic in frame count (measured 117x slower than the single
    streaming read on a 20,000-frame log). DSH files are re-read whole on every
    change, so a live session would reintroduce the slow-refresh problem the
    recovery exists to soften: healthy files must not touch it.

    Patching the recovery to explode is the cheapest non-flaky assertion of that;
    a timing assertion would not be.
    """
    from tokdash.sources import dsh_log

    def boom(raw):  # pragma: no cover - reaching this is the failure
        raise AssertionError("healthy file must not use the quadratic recovery path")

    monkeypatch.setattr(dsh_log, "_decode_dsh_zstd_frames", boom)
    home = _isolated_dsh_home
    _write_zstd(
        _session_path(home, suffix=".jsonl.zstd"),
        [_header(), _assistant_message(1, 0, 0, {"inputTokens": 100, "outputTokens": 10})],
    )

    assert [(e["input"], e["output"]) for e in _collect(home)] == [(100, 10)]
