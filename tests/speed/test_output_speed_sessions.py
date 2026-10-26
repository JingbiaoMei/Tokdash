"""Output speed in the session drill-down.

Two surfaces have to agree: the Usage Report reads the stored usage rows, and a
session's detail view reads the same log a second time through the session
parser. Both go through the helpers in output_speed.py, and these tests hold the
session side of that promise:

* a turn carries its own paired measurement, or null -- never a borrowed one;
* a turn that merged several calls reports the ratio of their sums;
* the summary counts responses and calls as different questions;
* two measurement kinds inside one session never collapse into one scalar;
* the session *list* stays untouched: no timing scan per row.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

@pytest.fixture(autouse=True)
def _timing_reader_mode():
    from tokdash.speed_mode import collect_timings
    with collect_timings():
        yield


from tokdash import sessions
from tokdash.output_speed import (
    BASIS_INCLUDING,
    BASIS_UNSPECIFIED,
    KIND_POST_FIRST_TOKEN,
    KIND_SERVER_DECODE,
    STATUS_AMBIGUOUS_PAIR,
    STATUS_INVALID_TIMING,
    STATUS_MISSING_TIMING,
    turn_measurement_summary,
)
from tokdash.sessions import (
    _parse_kimi_session_file,
    _parse_omp_session_file,
    _parse_pi_session_file,
    _public_turns,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)

BASE_MS = int(datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _step(kind, ts_ms, *, usage=None, decode_ms=None, agent="main"):
    event = {"type": kind, "uuid": f"{kind}-{ts_ms}", "turnId": "0", "step": 1}
    if usage is not None:
        event["usage"] = usage
        event["llmServerDecodeMs"] = decode_ms
    return {
        "type": "context.append_loop_event",
        "agentId": agent,
        "event": event,
        "time": ts_ms,
    }


def _usage_record(output, *, ts_ms=BASE_MS, usage=None, model="kimi-code/k3", agent="main"):
    """One usage row, in the shape the wire log actually writes.

    ``agentId`` is present on usage.record rows in real logs, and the association
    gate scopes on it, so leaving it out here would put the row in a different
    agent from its own step.
    """
    usage = usage or {"inputOther": 100, "output": output, "inputCacheRead": 0,
                      "inputCacheCreation": 0}
    return {
        "type": "usage.record",
        "agentId": agent,
        "model": model,
        "usage": usage,
        "usageScope": "turn",
        "time": ts_ms,
    }


def _write_kimi(rows, *, workspace="wd_speed_0123456789ab", session="session_speed",
                agent="main"):
    path = (KIMI_ROOT / "sessions" / workspace / session / "agents" / agent / "wire.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolated_kimi(monkeypatch, tmp_path):
    global KIMI_ROOT
    KIMI_ROOT = tmp_path / "kimi-code"
    legacy = tmp_path / "kimi"
    (KIMI_ROOT / "sessions").mkdir(parents=True)
    (legacy / "sessions").mkdir(parents=True)
    monkeypatch.setenv("KIMI_CODE_HOME", str(KIMI_ROOT))
    monkeypatch.setenv("KIMI_SHARE_DIR", str(legacy))
    sessions._SESSION_ASSEMBLY.clear()
    reload_pricing_db()
    yield
    sessions._SESSION_ASSEMBLY.clear()
    reload_pricing_db()


def _kimi_turns(path):
    parsed = _parse_kimi_session_file(str(path), 0, 0, ())
    assert parsed is not None, "the fixture must produce a session"
    return parsed


def _keys(node):
    """Every mapping key anywhere inside a decoded payload."""
    if isinstance(node, dict):
        return set(node) | set().union(*[_keys(v) for v in node.values()]) if node else set()
    if isinstance(node, list):
        return set().union(*[_keys(v) for v in node]) if node else set()
    return set()


# --------------------------------------------------------------------------
# Kimi turns
# --------------------------------------------------------------------------


def test_a_measured_turn_carries_its_own_window_and_the_rest_are_null():
    usage = {"inputOther": 100, "output": 400, "inputCacheRead": 0, "inputCacheCreation": 0}
    path = _write_kimi([
        _step("step.begin", BASE_MS - 2_500),
        _usage_record(400, ts_ms=BASE_MS - 500, usage=usage),
        _step("step.end", BASE_MS + 1_000, usage=usage, decode_ms=2_000),
        # A second call whose bracket never closed inside the file.
        _usage_record(90, ts_ms=BASE_MS + 60_000),
    ])
    turns = _public_turns(_kimi_turns(path)["turns"])
    assert len(turns) == 2
    measured = [t for t in turns if t["output_speed"]]
    unmeasured = [t for t in turns if not t["output_speed"]]
    assert len(measured) == 1 and len(unmeasured) == 1
    speed = measured[0]["output_speed"]
    assert speed["output_tok_per_s"] == pytest.approx(200.0)
    assert speed["speed_calls"] == 1
    assert speed["measurement_kind"] == KIND_SERVER_DECODE
    assert speed["token_basis"] == BASIS_UNSPECIFIED
    # The private key never survives the public projection.
    assert all("_speed" not in t for t in turns)


def test_a_duration_never_crosses_into_a_subagents_row():
    """Agents in one session run at the same time; their windows are separate."""
    main_usage = {"inputOther": 100, "output": 400, "inputCacheRead": 0,
                  "inputCacheCreation": 0}
    sub_usage = {"inputOther": 20, "output": 50, "inputCacheRead": 0,
                 "inputCacheCreation": 0}
    path = _write_kimi([
        _step("step.begin", BASE_MS - 2_500),
        _usage_record(400, ts_ms=BASE_MS - 500, usage=main_usage),
        # The subagent's row sits inside the main agent's bracket, and the
        # subagent has no bracket of its own.
        _usage_record(50, ts_ms=BASE_MS - 400, usage=sub_usage, agent="subagent:1"),
        _step("step.end", BASE_MS + 1_000, usage=main_usage, decode_ms=2_000),
    ])
    public = _public_turns(_kimi_turns(path)["turns"])
    by_tokens = {t["tokens_out"]: t["output_speed"] for t in public}
    assert by_tokens[400] is not None
    assert by_tokens[400]["output_tok_per_s"] == pytest.approx(200.0)
    assert by_tokens[50] is None, "a borrowed window is worse than no window"


def test_a_turn_that_merged_three_calls_reports_the_ratio_of_their_sums():
    """The session-level figure must match the comparison table's rule."""
    rows = []
    specs = [(100, 1_000.0), (200, 2_000.0), (300, 6_000.0)]
    base = BASE_MS
    for i, (tokens, ms) in enumerate(specs):
        usage = {"inputOther": 10, "output": tokens, "inputCacheRead": 0,
                 "inputCacheCreation": 0}
        rows.append(_step("step.begin", base + i * 10_000))
        rows.append(_usage_record(tokens, ts_ms=base + i * 10_000 + 500, usage=usage))
        rows.append(_step("step.end", base + i * 10_000 + 1_000, usage=usage, decode_ms=ms))
    parsed = _kimi_turns(_write_kimi(rows))
    public = _public_turns(parsed["turns"])
    speeds = [t["output_speed"] for t in public if t["output_speed"]]
    # Each usage row is its own turn here, so the merged-turn rule is exercised
    # through the summary, which is where a session actually combines them.
    summary = turn_measurement_summary([t.get("_speed") for t in parsed["turns"]])
    assert summary["responses"] == 3
    assert summary["measured_responses"] == 3
    assert summary["measured_calls"] == 3
    assert len(speeds) == 3
    assert summary["output_tok_per_s"] == pytest.approx(
        round(1000 * 600 / 9000.0, 1)
    )
    assert summary["measurement_kind"] == KIND_SERVER_DECODE


def test_two_measurement_kinds_in_one_session_never_collapse_into_a_scalar():
    summary = turn_measurement_summary([
        {"speed_tokens": 100, "speed_ms": 1000.0, "speed_calls": 1,
         "speed_kind": KIND_SERVER_DECODE, "speed_token_basis": BASIS_UNSPECIFIED,
         "speed_status": "measured"},
        {"speed_tokens": 50, "speed_ms": 500.0, "speed_calls": 1,
         "speed_kind": KIND_POST_FIRST_TOKEN, "speed_token_basis": BASIS_INCLUDING,
         "speed_status": "measured"},
    ])
    assert "output_tok_per_s" not in summary, "incompatible components stay separate"
    assert "measurement_kind" not in summary
    assert summary["status"] == "measured"
    assert summary["measured_calls"] == 2
    kinds = {c["measurement_kind"] for c in summary["components"]}
    assert kinds == {KIND_SERVER_DECODE, KIND_POST_FIRST_TOKEN}
    assert {round(c["output_tok_per_s"], 1) for c in summary["components"]} == {100.0, 100.0}


def test_exclusions_are_counted_by_reason_and_stay_out_of_the_rate():
    summary = turn_measurement_summary([
        {"speed_tokens": 100, "speed_ms": 1000.0, "speed_calls": 1,
         "speed_kind": KIND_SERVER_DECODE, "speed_token_basis": BASIS_UNSPECIFIED,
         "speed_status": "measured"},
        {"speed_tokens": 0, "speed_ms": 0.0, "speed_calls": 0, "speed_kind": "",
         "speed_token_basis": "", "speed_status": STATUS_AMBIGUOUS_PAIR},
        {"speed_tokens": 0, "speed_ms": 0.0, "speed_calls": 0, "speed_kind": "",
         "speed_token_basis": "", "speed_status": STATUS_INVALID_TIMING},
        None,
    ])
    assert summary["responses"] == 4
    assert summary["measured_responses"] == 1
    assert summary["measured_calls"] == 1
    assert summary["output_tok_per_s"] == pytest.approx(100.0)
    assert {e["status"]: e["responses"] for e in summary["excluded"]} == {
        STATUS_AMBIGUOUS_PAIR: 1, STATUS_INVALID_TIMING: 1,
    }


def test_a_session_with_no_timing_reports_unavailable_not_zero():
    usage = {"inputOther": 10, "output": 40, "inputCacheRead": 0, "inputCacheCreation": 0}
    path = _write_kimi([_usage_record(40, usage=usage)])  # no step bracket at all
    parsed = _kimi_turns(path)
    summary = turn_measurement_summary([t.get("_speed") for t in parsed["turns"]])
    assert summary["status"] == "unavailable"
    assert "output_tok_per_s" not in summary
    assert summary["measured_calls"] == 0
    assert summary["responses"] == 1
    public = _public_turns(parsed["turns"])
    assert [t["output_speed"] for t in public] == [None]


# --------------------------------------------------------------------------
# omp: numerator and denominator arrive on one record
# --------------------------------------------------------------------------


def _omp_file(tmp_path, *, duration=4_000, ttft=1_000, output=150, extra_usage=None):
    usage = {
        "input": 1_000,
        "output": output,
        "cacheRead": 0,
        "cacheWrite": 0,
        "reasoningTokens": 40,
        "totalTokens": 1_000 + output,
    }
    usage.update(extra_usage or {})
    session_id = "11111111-1111-4111-8111-111111111111"
    lines = [
        json.dumps({"type": "title", "v": 1, "title": "speed", "updatedAt": "2026-06-01T12:00:00.000Z"}),
        json.dumps({"type": "session", "version": 3, "id": session_id,
                    "timestamp": "2026-06-01T12:00:00.000Z", "cwd": "/tmp/project"}),
        json.dumps({"type": "model_change", "id": "m1", "parentId": None,
                    "timestamp": "2026-06-01T12:00:00.100Z", "model": "vllm-hpc/selfhosted-qwen"}),
        json.dumps({"type": "message", "id": "u1", "timestamp": "2026-06-01T12:00:00.200Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}),
        json.dumps({"type": "message", "id": "a1", "timestamp": "2026-06-01T12:00:05.000Z",
                    "message": {"role": "assistant", "provider": "vllm-hpc",
                                "model": "selfhosted-qwen", "usage": usage,
                                "duration": duration, "ttft": ttft}}),
    ]
    path = tmp_path / "omp_speed.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path, session_id


def test_an_omp_turn_subtracts_time_to_first_token_from_the_call(tmp_path):
    path, _session_id = _omp_file(tmp_path)
    parsed = _parse_omp_session_file(str(path), 0, 0, ())
    assert parsed is not None
    public = _public_turns(parsed["turns"])
    assistant = [t for t in public if t["output_speed"]]
    assert len(assistant) == 1
    speed = assistant[0]["output_speed"]
    # 150 output tokens over (4000 - 1000) ms of post-first-token window.
    assert speed["output_tok_per_s"] == pytest.approx(50.0)
    assert speed["measurement_kind"] == KIND_POST_FIRST_TOKEN
    assert speed["token_basis"] == BASIS_INCLUDING


def test_omp_timing_does_not_leak_into_pi_agent(tmp_path):
    """pi_agent parses the same format and measures nothing.

    omp's timing is read through a class attribute on the parser, not by the
    shared record shape, so the parent source cannot be upgraded by accident.
    """
    path, _session_id = _omp_file(tmp_path)
    pi_parsed = _parse_pi_session_file(str(path), 0, 0, ())
    if pi_parsed is None:  # the pi layout differs; the promise still holds
        return
    assert [t.get("_speed") for t in pi_parsed["turns"]] == [
        None for _ in pi_parsed["turns"]
    ]
    assert [t["output_speed"] for t in _public_turns(pi_parsed["turns"])] == [
        None for _ in pi_parsed["turns"]
    ]


# --------------------------------------------------------------------------
# The detail payload, and the list payload that must stay as it was
# --------------------------------------------------------------------------


def test_session_detail_publishes_turns_and_a_coverage_line():
    usage = {"inputOther": 100, "output": 400, "inputCacheRead": 0, "inputCacheCreation": 0}
    _write_kimi([
        _step("step.begin", BASE_MS - 2_500),
        _usage_record(400, ts_ms=BASE_MS - 500, usage=usage),
        _step("step.end", BASE_MS + 1_000, usage=usage, decode_ms=2_000),
        _usage_record(60, ts_ms=BASE_MS + 90_000),
    ])
    listed = get_sessions_data("kimi", "all")
    sessions_rows = listed["sessions"]
    assert sessions_rows, "the fixture must be discoverable"
    detail = get_session_detail("kimi", sessions_rows[0]["session_id"])

    summary = detail["speed_measurement"]
    assert summary["responses"] == 2
    assert summary["measured_responses"] == 1
    assert summary["measured_calls"] == 1
    assert summary["output_tok_per_s"] == pytest.approx(200.0)
    assert summary["components"][0]["measurement_kind"] == KIND_SERVER_DECODE

    assert [t["output_speed"] is not None for t in detail["turns"]] == [True, False]
    # Nothing private rode along. A key walk, not a substring: the fixture's own
    # session id contains the word. speed_tokens / speed_ms / speed_calls are in
    # the published contract; the storage-only names (the private key and the
    # status/kind/basis labels) are not.
    assert _keys(detail).isdisjoint(
        {"_speed", "speed_status", "speed_kind", "speed_token_basis"}
    )


def test_the_session_list_payload_carries_no_timing_at_all():
    """Timing costs a scan of the log; the list view must not pay for it."""
    usage = {"inputOther": 100, "output": 400, "inputCacheRead": 0,
             "inputCacheCreation": 0}
    _write_kimi([
        _step("step.begin", BASE_MS - 2_500),
        _usage_record(400, ts_ms=BASE_MS - 500, usage=usage),
        _step("step.end", BASE_MS + 1_000, usage=usage, decode_ms=2_000),
    ])
    listed = get_sessions_data("kimi", "all")
    keys = _keys(listed)
    assert keys.isdisjoint({
        "output_speed", "speed_measurement", "_speed", "speed_status",
        "speed_kind", "speed_token_basis", "speed_calls",
    })


def test_the_parsers_that_gained_timing_declare_a_version_bump():
    """Stored rows and stored turns must be rebuilt, not read as if complete.

    Both caches key on a parser version. If either number stops moving while the
    reader still exists, a corpus parsed before the feature would keep serving
    rows that were never given a timing column, and the report would call that
    "no measurements" forever.
    """
    from tokdash.sessions import _SESSION_FILE_PARSER_VERSIONS
    from tokdash.sources.coding_tools import KimiParser, OmpParser

    assert _SESSION_FILE_PARSER_VERSIONS["_parse_kimi_session_file"] == 3
    assert KimiParser.persistent_parser_version == 4
    assert OmpParser.persistent_parser_version == 3
    assert KimiParser._tracks_output_speed is True
    assert OmpParser._tracks_output_speed is True
