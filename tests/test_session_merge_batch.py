"""The batched session merge must reproduce the pairwise fold exactly.

One Claude session spans every subagent transcript it spawned, so rebuilding it
used to fold the rows pairwise and re-key every turn already merged — quadratic
in the number of files. _merge_raw_session_sequence collapses that to one pass;
these tests pin it to the fold it replaced, because a read that merges
differently is exactly how two paths over the same logs start disagreeing.

Fan-out is not Claude-shaped, only Claude-sized: every loader that can attribute
several files to one session id lands on the same K^2, and the tools that are
never store-backed have no second cache tier to hide behind. So the loaders are
pinned here too — both to the linear key count, and to leaving the sessions they
merge alone afterwards, which is what makes a merged session shareable.
"""
import json
from pathlib import Path

import pytest

from tokdash import sessions
from tokdash.sessions import (
    _drop_codex_subagent_replay_turns,
    _merge_raw_session,
    _merge_raw_session_sequence,
)


def _fold(raws):
    """The pairwise fold the batch merge replaced, kept here as the oracle."""
    acc = raws[0]
    for raw in raws[1:]:
        acc = _merge_raw_session(acc, raw)
    return acc


def _turn(index, stamp, *, event=None, stream=None, tokens=10, cost=0.5):
    turn = {
        "turn_index": index,
        "timestamp_ms": stamp,
        "model": "claude-sonnet-4.5",
        "tokens_in": tokens,
        "tokens_cache": 0,
        "tokens_out": 5,
        "tokens_reasoning": 0,
        "tokens": tokens + 5,
        "cost": cost,
    }
    if event is not None:
        turn["_event_key"] = event
    if stream is not None:
        turn["_stream_id"] = stream
    return turn


def _raw(session_id="s1", *, turns, name=None, project="proj", review=False, explicit=None):
    raw = {
        "tool": "claude",
        "session_id": session_id,
        "project": project,
        "display_name": name,
        "is_review_session": review,
        "turns": turns,
    }
    if explicit is not None:
        raw["_display_name_explicit"] = explicit
    return raw


def _canon(value):
    return json.dumps(value, sort_keys=True, default=str)


def test_single_raw_is_returned_unchanged():
    raw = _raw(turns=[_turn(1, 1000, event="a")])
    assert _merge_raw_session_sequence([raw]) is raw


def test_matches_fold_for_many_files():
    raws = [_raw(turns=[_turn(1, 1000 + i * 10, event=f"e{i}")]) for i in range(40)]
    assert _canon(_merge_raw_session_sequence(raws)) == _canon(_fold(raws))


def test_matches_fold_when_a_later_file_carries_an_earlier_stamp():
    # Same event id, second sighting earlier: the earlier stamp wins in both paths.
    raws = [
        _raw(turns=[_turn(1, 5000, event="dup"), _turn(2, 6000, event="b")]),
        _raw(turns=[_turn(1, 4000, event="dup")]),
        _raw(turns=[_turn(1, 7000, event="dup")]),
    ]
    merged = _merge_raw_session_sequence(raws)
    assert _canon(merged) == _canon(_fold(raws))
    stamps = [turn["timestamp_ms"] for turn in merged["turns"] if turn.get("_event_key") == "dup"]
    assert stamps == [4000]


def test_matches_fold_for_field_identity_and_streams():
    # No event key: identity falls back to fields, and _stream_id keeps two agents
    # reporting the same usage in the same millisecond from collapsing into one.
    raws = [
        _raw(turns=[_turn(1, 2000, stream="main"), _turn(2, 2000, stream="sub")]),
        _raw(turns=[_turn(1, 2000, stream="main"), _turn(2, 2000, stream="other")]),
    ]
    merged = _merge_raw_session_sequence(raws)
    assert _canon(merged) == _canon(_fold(raws))
    assert len(merged["turns"]) == 3


def test_matches_fold_for_duplicate_stamps_across_files():
    # Equal timestamps are where the fold's per-record renumbering decides order.
    raws = [
        _raw(turns=[_turn(3, 1000, event="a"), _turn(1, 1000, event="b")]),
        _raw(turns=[_turn(2, 1000, event="c"), _turn(9, 1000, event="d")]),
        _raw(turns=[_turn(1, 1000, event="e")]),
    ]
    merged = _merge_raw_session_sequence(raws)
    assert _canon(merged) == _canon(_fold(raws))
    assert [turn["turn_index"] for turn in merged["turns"]] == [1, 2, 3, 4, 5]


def test_matches_fold_for_metadata_precedence():
    raws = [
        _raw(turns=[_turn(1, 1000, event="a")], name=None, project="unknown", review=False),
        _raw(turns=[_turn(1, 2000, event="b")], name="Real Title", project="proj", review=True, explicit=True),
        _raw(turns=[_turn(1, 3000, event="c")], name="Later", project="other", review=False),
    ]
    merged = _merge_raw_session_sequence(raws)
    assert _canon(merged) == _canon(_fold(raws))
    assert merged["is_review_session"] is True
    assert merged["project"] == "proj"


def test_each_turn_is_keyed_exactly_once(monkeypatch):
    """The guard against the quadratic coming back.

    The pairwise fold re-keyed every already-merged turn on every record, so this
    count grew with the square of the file count. It must stay linear.
    """
    calls = {"n": 0}
    original = sessions._turn_identity_key

    def counting(turn):
        calls["n"] += 1
        return original(turn)

    monkeypatch.setattr(sessions, "_turn_identity_key", counting)

    files, per_file = 60, 3
    raws = [
        _raw(turns=[_turn(i, 1000 + f * 100 + i, event=f"e{f}-{i}") for i in range(1, per_file + 1)])
        for f in range(files)
    ]
    _merge_raw_session_sequence(raws)
    assert calls["n"] == files * per_file


# Loader name paired with the module attribute its per-file parse reaches through.
# Every one of these once folded pairwise; four of the tools (pi_agent, omp,
# kilocode, workbuddy) are never store-backed, so nothing else caps their cost.
LIVE_LOADERS = [
    ("_load_codex_sessions", "_parse_codex_session_file"),
    ("_load_claude_sessions", "_parse_claude_session_file"),
    ("_load_pi_sessions", "_parse_pi_session_file"),
    ("_load_omp_sessions", "_parse_omp_session_file"),
    ("_load_kimi_sessions", "_parse_kimi_session_file"),
    ("_load_dsh_sessions", "_parse_dsh_session_file"),
    ("_load_reasonix_sessions", "_parse_reasonix_session_file"),
    ("_load_workbuddy_sessions", "_parse_workbuddy_session_file"),
]

_FILES, _PER_FILE = 40, 3


def _fan_out_raw(path_str, per_file=_PER_FILE):
    """One session id spread over many files, the shape subagent fan-out writes."""
    index = int(Path(path_str).stem)
    return _raw(
        turns=[
            _turn(i, 1000 + index * 100 + i, event=f"e{index}-{i}")
            for i in range(1, per_file + 1)
        ]
    )


def _count_identity_keys(monkeypatch):
    calls = {"n": 0}
    original = sessions._turn_identity_key

    def counting(turn):
        calls["n"] += 1
        return original(turn)

    monkeypatch.setattr(sessions, "_turn_identity_key", counting)
    return calls


@pytest.mark.parametrize("loader_name,parser_name", LIVE_LOADERS)
def test_a_live_loader_keys_each_turn_once(monkeypatch, loader_name, parser_name):
    loader = getattr(sessions, loader_name)
    loader.cache_clear()
    monkeypatch.setattr(
        sessions, parser_name, lambda path_str, *_args: _fan_out_raw(path_str)
    )
    calls = _count_identity_keys(monkeypatch)
    signature = tuple((f"/fake/{index}.jsonl", 0, 0) for index in range(_FILES))
    try:
        merged = loader(signature, ())
        assert len(merged["s1"]["turns"]) == _FILES * _PER_FILE
        assert calls["n"] == _FILES * _PER_FILE
    finally:
        loader.cache_clear()


def test_the_kilocode_loader_keys_each_turn_once(monkeypatch, tmp_path):
    # Kilocode reads sqlite rather than transcripts, so it fakes one level up.
    paths = []
    for index in range(_FILES):
        db = tmp_path / f"{index}.vscdb"
        db.write_bytes(b"")
        paths.append(db)
    monkeypatch.setattr(
        sessions,
        "_load_opencode_sessions_scalar",
        lambda db_path, **_kwargs: {"s1": _fan_out_raw(str(db_path))},
    )
    calls = _count_identity_keys(monkeypatch)
    signature = tuple((str(path), 0, 0) for path in paths)
    sessions._load_kilocode_sessions.cache_clear()
    try:
        merged = sessions._load_kilocode_sessions(signature, ())
        assert len(merged["s1"]["turns"]) == _FILES * _PER_FILE
        assert calls["n"] == _FILES * _PER_FILE
    finally:
        sessions._load_kilocode_sessions.cache_clear()


def _fork(session_id, parent_id, events):
    raw = _raw(session_id, turns=[_turn(i, 1000 + i, event=event) for i, event in enumerate(events, 1)])
    raw["tool"] = "codex"
    if parent_id:
        raw["_subagent_parent_id"] = parent_id
    return raw


def test_the_codex_replay_pass_trims_a_fork_without_editing_the_input():
    given = {"p": _fork("p", None, ["a", "b"]), "c": _fork("c", "p", ["a", "c"])}
    before = _canon(given)
    result = _drop_codex_subagent_replay_turns(given)
    assert [turn["_event_key"] for turn in result["c"]["turns"]] == ["c"]
    assert _canon(given) == before


def test_the_codex_replay_pass_drops_an_all_replay_fork_without_editing_the_input():
    given = {"p": _fork("p", None, ["a", "b"]), "c": _fork("c", "p", ["a", "b"])}
    before = _canon(given)
    result = _drop_codex_subagent_replay_turns(given)
    assert "c" not in result and "p" in result
    assert _canon(given) == before


def test_the_codex_sibling_rule_keeps_the_input_intact():
    # Parent absent, so both forks hold the same replay prefix; the later sibling
    # gives it up. That branch also used to trim in place.
    given = {
        "c1": _fork("c1", "gone", ["a", "x"]),
        "c2": _fork("c2", "gone", ["a", "y"]),
    }
    before = _canon(given)
    result = _drop_codex_subagent_replay_turns(given)
    assert [turn["_event_key"] for turn in result["c1"]["turns"]] == ["a", "x"]
    assert [turn["_event_key"] for turn in result["c2"]["turns"]] == ["y"]
    assert _canon(given) == before
