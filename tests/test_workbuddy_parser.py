"""WorkBuddy transcript parser tests (phase 1 spec).

Fixtures are the verbatim captured rows from
docs/local/20260821_workbuddy_support/evidence (Windows + macOS, working +
coding modes).
"""

import json
from pathlib import Path

from tokdash.compute import _collect_parser_tail
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import WorkBuddyParser


def _isolate_home(monkeypatch, tmp_path):
    """Keep tests hermetic (and Windows-safe) by faking the user home dir."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _raw_usage(prompt, completion, cached, missed, credit=0.0, reasoning=0, write=0,
               cache_read=0, cache_creation=0, include_miss=True):
    """Verbatim-shape rawUsage block (field set from the captured transcripts)."""
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {
            "accepted_prediction_tokens": 0,
            "audio_tokens": 0,
            "reasoning_tokens": 0,
            "rejected_prediction_tokens": 0,
            "cached_tokens": cached,
        },
        "completion_tokens_details": {
            "accepted_prediction_tokens": 0,
            "audio_tokens": 0,
            "reasoning_tokens": reasoning,
            "rejected_prediction_tokens": 0,
            "cached_tokens": 0,
        },
        "prompt_cache_hit_tokens": cached,
        "prompt_cache_write_tokens": write,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "completion_thinking_tokens": 0,
        "credit": credit,
        "cached_tokens": 0,
        **({"prompt_cache_miss_tokens": missed} if include_miss else {}),
    }


def _fallback_usage(prompt, completion, cached, reasoning=0):
    """Captured camel-case providerData.usage schema (no cache-write column)."""
    return {
        "requests": 1,
        "inputTokens": prompt,
        "outputTokens": completion,
        "totalTokens": prompt + completion,
        "inputTokensDetails": [{"cached_tokens": cached}] if cached else [],
        "outputTokensDetails": [{"reasoning_tokens": reasoning}] if reasoning else [],
    }


def _assistant_row(message_id, ts, model, raw_usage=None, usage=None,
                   omit_message_id=False, omit_id=False):
    pd = {
        "model": model,
        "requestModelId": model,
        "requestModelName": "Auto",
        "agent": "cli",
    }
    if not omit_message_id:
        pd["messageId"] = message_id
    if raw_usage is not None:
        pd["rawUsage"] = raw_usage
    if usage is not None:
        pd["usage"] = usage
    row = {
        "timestamp": ts,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "providerData": pd,
        "sessionId": "d2f054aa-6d2a-4953-acc6-485f1d9f7c9a",
    }
    if not omit_id:
        row["id"] = message_id
    return row


def _write_transcript(root: Path, slug: str, session_id: str, rows) -> Path:
    """Write rows (dicts, or raw strings for malformed-line cases) to a transcript."""
    project_dir = root / "projects" / slug
    project_dir.mkdir(parents=True)
    path = project_dir / f"{session_id}.jsonl"
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _collect(parser: WorkBuddyParser):
    return {e["entry_id"]: e for e in parser.collect(None, None)}


def test_workbuddy_windows_fixture(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    root = home / ".workbuddy-ai"
    rows = [
        # Non-usage rows from the real transcript (must be ignored).
        {
            "id": "5ff45aa6-f284-49d6-af0b-4c2bcf142cc2",
            "timestamp": 1787267148990,
            "type": "message",
            "role": "user",
            "content": [],
            "providerData": {"agent": "cli"},
            "sessionId": "d2f054aa-6d2a-4953-acc6-485f1d9f7c9a",
            "cwd": "c:\\Users\\H1937\\WorkBuddy AI\\2026-08-21-00-05-48",
        },
        {
            "id": "snapshot-1",
            "timestamp": 1787267160000,
            "type": "file-history-snapshot",
            "isSnapshotUpdate": False,
            "snapshot": {},
            "cwd": "c:\\Users\\H1937\\WorkBuddy AI",
        },
        {
            "timestamp": 1787267160500,
            "type": "ai-title",
            "aiTitle": "test",
            "sessionId": "d2f054aa-6d2a-4953-acc6-485f1d9f7c9a",
            "cwd": "c:\\Users\\H1937\\WorkBuddy AI",
        },
        # Verbatim Windows capture: 34417 / 128 / cached 12288 / miss 22129.
        _assistant_row(
            "f0eb23d31e314b25919902f51b49a282",
            1787267160894,
            "default-model",
            raw_usage=_raw_usage(34417, 128, 12288, 22129, credit=2.12),
        ),
    ]
    _write_transcript(
        root,
        "c-Users-H1937-WorkBuddy AI-2026-08-21-00-05-48",
        "d2f054aa-6d2a-4953-acc6-485f1d9f7c9a",
        rows,
    )

    parser = WorkBuddyParser(PricingDatabase())
    entries = _collect(parser)

    assert len(entries) == 1
    e = entries["workbuddy:f0eb23d31e314b25919902f51b49a282"]
    assert e["source"] == "workbuddy"
    assert e["model"] == "default-model"
    assert e["provider"] == ""
    assert e["input"] == 22129  # prompt_cache_miss_tokens
    assert e["output"] == 128
    assert e["cacheRead"] == 12288
    assert e["cacheWrite"] == 0
    assert e["reasoning"] == 0
    assert e["cost"] == 0.0  # router alias is absent from the pricing DB
    assert e["workbuddy_credit"] == 2.12
    assert e["timestamp"] == 1787267160894


def test_workbuddy_macos_coding_fixture(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    root = home / ".workbuddy-ai"
    # Verbatim macOS coding-mode capture: 32309 / 759 / cached 28160 / miss 4149.
    _write_transcript(
        root,
        "Users-margaret-WorkBuddy AI-2026-08-21-13-20-34",
        "a2f52b49-26d3-492e-80b1-d260f64e9f3c",
        [
            _assistant_row(
                "75680327bc0744c0bce77b9d4bd3b9cc",
                1787314852053,
                "default-model",
                raw_usage=_raw_usage(32309, 759, 28160, 4149, credit=1.13),
            ),
        ],
    )

    parser = WorkBuddyParser(PricingDatabase())
    entries = _collect(parser)

    assert len(entries) == 1
    e = entries["workbuddy:75680327bc0744c0bce77b9d4bd3b9cc"]
    assert e["input"] == 4149
    assert e["output"] == 759
    assert e["cacheRead"] == 28160
    assert e["cost"] == 0.0
    assert e["workbuddy_credit"] == 1.13
    assert e["timestamp"] == 1787314852053


def test_workbuddy_working_and_coding_modes_parse_identically(monkeypatch, tmp_path):
    """Working and coding mode rows are shape-identical; one code path for both."""
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    root = home / ".workbuddy-ai"
    # Verbatim macOS working-mode capture: 32311 / 204 / cached 9984 / miss 22327.
    _write_transcript(
        root,
        "Users-margaret-WorkBuddy AI-2026-08-21-13-20-18",
        "c1a1a972-29b4-491a-ae36-ba13ac528727",
        [
            _assistant_row(
                "fce3e9f476b4421097972553e5dfcc18",
                1787314833130,
                "default-model",
                raw_usage=_raw_usage(32311, 204, 9984, 22327, credit=2.11),
            ),
        ],
    )
    _write_transcript(
        root,
        "Users-margaret-WorkBuddy AI-2026-08-21-13-20-34",
        "a2f52b49-26d3-492e-80b1-d260f64e9f3c",
        [
            _assistant_row(
                "75680327bc0744c0bce77b9d4bd3b9cc",
                1787314852053,
                "default-model",
                raw_usage=_raw_usage(32309, 759, 28160, 4149, credit=1.13),
            ),
        ],
    )

    parser = WorkBuddyParser(PricingDatabase())
    entries = _collect(parser)

    assert len(entries) == 2
    working = entries["workbuddy:fce3e9f476b4421097972553e5dfcc18"]
    coding = entries["workbuddy:75680327bc0744c0bce77b9d4bd3b9cc"]
    assert working["input"] == 22327
    assert working["output"] == 204
    assert working["cacheRead"] == 9984
    assert coding["input"] == 4149
    assert coding["output"] == 759
    assert coding["cacheRead"] == 28160
    assert set(working.keys()) == set(coding.keys())


def test_workbuddy_malformed_rows_skip_without_dropping_file(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    root = home / ".workbuddy-ai"
    good_id = "d" * 32
    rows = [
        '{"broken": ',  # bad JSON
        "",  # blank line
        _assistant_row(
            "a" * 32,
            1787314800000,
            "default-model",
            raw_usage=_raw_usage(100, 10, 0, 100),
            omit_message_id=True,
            omit_id=True,
        ),  # no stable identity
        _assistant_row(
            "b" * 32,
            0,
            "default-model",
            raw_usage=_raw_usage(100, 10, 0, 100),
        ),  # zero timestamp
        _assistant_row(
            "c" * 32,
            1787314800001,
            "default-model",
            raw_usage=_raw_usage(0, 0, 0, 0),
        ),  # zero usage
        _assistant_row(
            good_id,
            1787314800002,
            "default-model",
            raw_usage=_raw_usage(500, 50, 200, 300, credit=0.5),
        ),
    ]
    _write_transcript(root, "slug", "s1", rows)

    parser = WorkBuddyParser(PricingDatabase())
    entries = _collect(parser)

    assert list(entries) == [f"workbuddy:{good_id}"]
    assert entries[f"workbuddy:{good_id}"]["input"] == 300


def test_workbuddy_multi_root_discovery(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    root_a = home / "wb-a"
    root_b = home / "wb-b"
    _write_transcript(
        root_a,
        "slug-a",
        "s1",
        [_assistant_row("a" * 32, 1787314800000, "default-model",
                        raw_usage=_raw_usage(1000, 100, 400, 600, credit=1.0))],
    )
    _write_transcript(
        root_b,
        "slug-b",
        "s2",
        [_assistant_row("b" * 32, 1787314800001, "default-model",
                        raw_usage=_raw_usage(2000, 200, 0, 2000, credit=2.0))],
    )
    monkeypatch.setenv("WORKBUDDY_DATA_DIR", f"{root_a} , {root_b}")

    parser = WorkBuddyParser(PricingDatabase())
    entries = _collect(parser)

    assert set(entries) == {f"workbuddy:{'a' * 32}", f"workbuddy:{'b' * 32}"}
    assert entries[f"workbuddy:{'a' * 32}"]["input"] == 600
    assert entries[f"workbuddy:{'b' * 32}"]["input"] == 2000


def test_workbuddy_fallback_usage_schema(monkeypatch, tmp_path):
    """rawUsage absent: providerData.usage (camel-case + details arrays) is used,
    with the same totals as the primary adapter on the same numbers."""
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    root = home / ".workbuddy-ai"
    _write_transcript(
        root,
        "slug",
        "s1",
        [
            _assistant_row(
                "f0eb23d31e314b25919902f51b49a282",
                1787267160894,
                "default-model",
                usage=_fallback_usage(34417, 128, 12288),
            ),
        ],
    )

    parser = WorkBuddyParser(PricingDatabase())
    entries = _collect(parser)

    assert len(entries) == 1
    e = entries["workbuddy:f0eb23d31e314b25919902f51b49a282"]
    assert e["input"] == 22129  # prompt - cached (no miss column in fallback)
    assert e["output"] == 128
    assert e["cacheRead"] == 12288
    assert e["cacheWrite"] == 0
    assert e["reasoning"] == 0
    assert e["cost"] == 0.0
    assert e["workbuddy_credit"] == 0.0


def test_workbuddy_reasoning_split_billed_as_completion(monkeypatch, tmp_path):
    """Nonzero reasoning: output excludes it and the full completion is priced.

    With zero cache writes the headline total equals
    fresh + cacheRead + completion == prompt + completion (prompt already
    includes the cached slice, so cache must not be added on top).
    """
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    root = home / ".workbuddy-ai"
    _write_transcript(
        root,
        "slug",
        "s1",
        [_assistant_row("e" * 32, 1787314800000, "gpt-5.5",
                        raw_usage=_raw_usage(1000, 500, 400, 600, reasoning=200))],
    )

    parser = WorkBuddyParser(PricingDatabase())
    entries = _collect(parser)

    e = entries[f"workbuddy:{'e' * 32}"]
    assert e["input"] == 600
    assert e["output"] == 300
    assert e["reasoning"] == 200
    assert e["cacheRead"] == 400
    assert e["input"] + e["cacheRead"] + e["output"] + e["reasoning"] == 1000 + 500
    assert e["cost"] == PricingDatabase().get_cost("gpt-5.5", 600, 500, 400, 0)


def test_workbuddy_reasoning_thinking_fallback(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    root = home / ".workbuddy-ai"
    ru = _raw_usage(100, 100, 0, 100)
    ru["completion_thinking_tokens"] = 77  # details.reasoning_tokens stays 0
    _write_transcript(
        root,
        "slug",
        "s1",
        [_assistant_row("t" * 32, 1787314800000, "default-model", raw_usage=ru)],
    )

    parser = WorkBuddyParser(PricingDatabase())
    entries = _collect(parser)

    e = entries[f"workbuddy:{'t' * 32}"]
    assert e["reasoning"] == 77
    assert e["output"] == 23


def test_workbuddy_cache_precedence_and_clamping(monkeypatch, tmp_path):
    def ru(prompt, **fields):
        base = {
            "prompt_tokens": prompt,
            "completion_tokens": 10,
            "total_tokens": prompt + 10,
            "credit": 0,
        }
        base.update(fields)
        return base

    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    root = home / ".workbuddy-ai"
    rows = [
        # Read chain, one field at a time (WorkBuddy UsageUtils order).
        _assistant_row("r1", 1787314800001, "default-model",
                       raw_usage=ru(1000, cache_read_input_tokens=111)),
        _assistant_row("r2", 1787314800002, "default-model",
                       raw_usage=ru(1000, cacheReadInputTokens=222)),
        _assistant_row("r3", 1787314800003, "default-model",
                       raw_usage=ru(1000, prompt_tokens_details={"cached_tokens": 333})),
        _assistant_row("r4", 1787314800004, "default-model",
                       raw_usage=ru(1000, prompt_cache_hit_tokens=444)),
        # Precedence: earlier chain position wins.
        _assistant_row("r5", 1787314800005, "default-model",
                       raw_usage=ru(1000, cache_read_input_tokens=111,
                                    prompt_cache_hit_tokens=999)),
        # Write chain.
        _assistant_row("w1", 1787314800006, "default-model",
                       raw_usage=ru(1000, cache_creation_input_tokens=55)),
        _assistant_row("w2", 1787314800007, "default-model",
                       raw_usage=ru(1000, cacheCreationInputTokens=66)),
        _assistant_row("w3", 1787314800008, "default-model",
                       raw_usage=ru(1000, prompt_cache_write_tokens=77)),
        # Clamps: cached cannot exceed prompt, fresh cannot go negative.
        _assistant_row("c1", 1787314800009, "default-model",
                       raw_usage=ru(100, prompt_cache_hit_tokens=150,
                                    prompt_cache_miss_tokens=0)),
    ]
    _write_transcript(root, "slug", "s1", rows)

    parser = WorkBuddyParser(PricingDatabase())
    entries = _collect(parser)

    assert entries["workbuddy:r1"]["cacheRead"] == 111
    assert entries["workbuddy:r2"]["cacheRead"] == 222
    assert entries["workbuddy:r3"]["cacheRead"] == 333
    assert entries["workbuddy:r4"]["cacheRead"] == 444
    assert entries["workbuddy:r5"]["cacheRead"] == 111
    # No miss column: fresh falls back to prompt - cached.
    assert entries["workbuddy:r1"]["input"] == 1000 - 111
    assert entries["workbuddy:w1"]["cacheWrite"] == 55
    assert entries["workbuddy:w2"]["cacheWrite"] == 66
    assert entries["workbuddy:w3"]["cacheWrite"] == 77
    assert entries["workbuddy:c1"]["cacheRead"] == 100
    assert entries["workbuddy:c1"]["input"] == 0


def test_workbuddy_model_verbatim_and_pricing(monkeypatch, tmp_path):
    """Router alias: counted, costs 0.00. Explicit id: priced, kept verbatim."""
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    root = home / ".workbuddy-ai"
    rows = [
        _assistant_row("a" * 32, 1787314800000, "default-model",
                       raw_usage=_raw_usage(1000, 100, 0, 1000)),
        _assistant_row("b" * 32, 1787314800001, "gpt-5.5",
                       raw_usage=_raw_usage(1000, 100, 0, 1000)),
    ]
    _write_transcript(root, "slug", "s1", rows)

    parser = WorkBuddyParser(PricingDatabase())
    entries = _collect(parser)

    alias = entries[f"workbuddy:{'a' * 32}"]
    explicit = entries[f"workbuddy:{'b' * 32}"]
    assert alias["model"] == "default-model"
    assert alias["cost"] == 0.0
    assert alias["input"] == 1000
    assert explicit["model"] == "gpt-5.5"
    assert explicit["cost"] > 0.0


def test_workbuddy_append_tail_ingests_only_new_rows(monkeypatch, tmp_path):
    """Persistent append sync path: appended rows in an otherwise-unchanged
    file are parsed from the tail offset, deduped by entry_id downstream."""
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)
    root = home / ".workbuddy-ai"
    first = _assistant_row("a" * 32, 1787314800000, "default-model",
                           raw_usage=_raw_usage(1000, 100, 0, 1000, credit=1.0))
    path = _write_transcript(root, "slug", "s1", [first])

    parser = WorkBuddyParser(PricingDatabase())
    assert [e["entry_id"] for e in parser._parse_all()] == [f"workbuddy:{'a' * 32}"]
    start_offset = path.stat().st_size

    second = _assistant_row("b" * 32, 1787314800001, "default-model",
                            raw_usage=_raw_usage(2000, 200, 0, 2000, credit=2.0))
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(second) + "\n")

    file_sig = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    tail_entries, safe_offset = _collect_parser_tail(parser, file_sig, start_offset)

    assert [e["entry_id"] for e in tail_entries] == [f"workbuddy:{'b' * 32}"]
    assert safe_offset == path.stat().st_size
    # Full reparse sees both rows exactly once each.
    assert [e["entry_id"] for e in parser._parse_all()] == [
        f"workbuddy:{'a' * 32}",
        f"workbuddy:{'b' * 32}",
    ]
