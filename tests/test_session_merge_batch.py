"""The batched session merge must reproduce the pairwise fold exactly.

One Claude session spans every subagent transcript it spawned, so rebuilding it
from the store used to fold the rows pairwise and re-key every turn already
merged — quadratic in the number of files. _merge_raw_session_sequence collapses
that to one pass; these tests pin it to the fold it replaced, because the live
loaders still fold pairwise and a cached read that merges differently is exactly
how cached and live start disagreeing.
"""
import json

from tokdash import sessions
from tokdash.sessions import _merge_raw_session, _merge_raw_session_sequence


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
