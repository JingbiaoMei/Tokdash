"""Cached Claude session reads must match live parsing."""
import json
from pathlib import Path

import pytest

from tokdash import sessions
from tokdash.sessions import get_sessions_data, reload_pricing_db

TS = "2026-05-19T12:00:00Z"


@pytest.fixture(autouse=True)
def _isolated_claude_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    reload_pricing_db()
    yield tmp_path
    reload_pricing_db()


def _assistant_row(session_id, message_id, tokens, timestamp=TS):
    return {
        "sessionId": session_id,
        "cwd": "/work/proj",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "id": message_id,
            "model": "claude-sonnet-4.5",
            "usage": {"input_tokens": tokens, "output_tokens": 5},
        },
    }


def _write_session_file(home, file_stem, rows, project_dir="projects/proj"):
    path = home / ".claude" / project_dir / f"{file_stem}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _subagent_row(session_id, agent_id, message_id, tokens, timestamp):
    row = _assistant_row(session_id, message_id, tokens, timestamp)
    row["agentId"] = agent_id
    row["isSidechain"] = True
    return row


def _write_subagent_file(home, session_id, agent_id, rows):
    return _write_session_file(
        home, f"agent-{agent_id}", rows, project_dir=f"projects/proj/{session_id}/subagents"
    )


def _at(offset_s):
    return f"2026-05-19T12:{offset_s // 60:02d}:{offset_s % 60:02d}Z"


def test_explicit_title_in_a_later_file_survives_a_cached_read(monkeypatch, _isolated_claude_home):
    """A renamed session keeps its title when the rename lands in a sibling log.

    The first file yields only a fallback name; the second carries the custom
    title. Live parsing merges and prefers the explicit title, so a cached read
    that kept the first row's name would show the project name instead.
    """
    home = _isolated_claude_home
    _write_session_file(home, "a-first", [_assistant_row("shared", "msg-1", 11)])
    _write_session_file(
        home,
        "b-second",
        [
            {"type": "custom-title", "sessionId": "shared", "customTitle": "Renamed session"},
            _assistant_row("shared", "msg-2", 22, timestamp="2026-05-19T12:05:00Z"),
        ],
    )

    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    reload_pricing_db()
    live = get_sessions_data("claude", "all")

    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    reload_pricing_db()
    cached = get_sessions_data("claude", "all")
    warm = get_sessions_data("claude", "all")

    assert live["sessions"][0]["display_name"] == "Renamed session"
    for payload in (live, cached, warm):
        payload.pop("timestamp")
    assert cached == live
    assert warm == live


def test_windowed_cached_read_keeps_a_title_from_an_out_of_window_file(monkeypatch, _isolated_claude_home):
    """The renaming file can sit outside the window the dashboard asks for."""
    home = _isolated_claude_home
    _write_session_file(
        home,
        "a-titled",
        [
            {"type": "custom-title", "sessionId": "shared", "customTitle": "Renamed session"},
            _assistant_row("shared", "msg-1", 11, timestamp="2026-05-19T12:00:00Z"),
        ],
    )
    _write_session_file(
        home,
        "b-later",
        [_assistant_row("shared", "msg-2", 22, timestamp="2026-05-23T12:00:00Z")],
    )
    window = {"period": "custom", "date_from": "2026-05-23", "date_to": "2026-05-23"}

    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    reload_pricing_db()
    live = get_sessions_data("claude", **window)

    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    reload_pricing_db()
    cached = get_sessions_data("claude", **window)

    assert live["sessions"][0]["display_name"] == "Renamed session"
    assert cached["sessions"][0]["display_name"] == live["sessions"][0]["display_name"]
    # Only the in-window turn counts, from both paths.
    assert cached["sessions"][0]["token_events"] == live["sessions"][0]["token_events"] == 1


def test_stored_and_live_agree_for_a_single_file_session(monkeypatch, _isolated_claude_home):
    home = _isolated_claude_home
    _write_session_file(home, "solo", [_assistant_row("solo", "msg-1", 11)])

    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    reload_pricing_db()
    live = get_sessions_data("claude", "all")
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    reload_pricing_db()
    cached = get_sessions_data("claude", "all")

    live.pop("timestamp")
    cached.pop("timestamp")
    assert cached == live
    assert sessions.persistent_usage_db_enabled()


def test_parallel_subagents_are_timed_as_separate_streams(_isolated_claude_home):
    """A subagent working alongside the main agent doubles agent time, not clock time."""
    home = _isolated_claude_home
    _write_session_file(
        home,
        "shared",
        [_assistant_row("shared", "m1", 11, _at(0)), _assistant_row("shared", "m2", 11, _at(60))],
    )
    _write_subagent_file(
        home,
        "shared",
        "a1",
        [
            _subagent_row("shared", "a1", "s1", 5, _at(0)),
            _subagent_row("shared", "a1", "s2", 5, _at(60)),
        ],
    )

    session = get_sessions_data("claude", "all")["sessions"][0]

    assert session["active_ms"] == 60_000
    assert session["active_ms_sum"] == 120_000


def test_interleaved_subagent_events_are_not_stitched_into_one_timeline(_isolated_claude_home):
    home = _isolated_claude_home
    _write_session_file(
        home,
        "shared",
        [
            _assistant_row("shared", "m1", 11, _at(0)),
            _assistant_row("shared", "m2", 11, _at(120)),
            _assistant_row("shared", "m3", 11, _at(240)),
        ],
    )
    _write_subagent_file(
        home,
        "shared",
        "a1",
        [
            _subagent_row("shared", "a1", "s1", 5, _at(180)),
            _subagent_row("shared", "a1", "s2", 5, _at(300)),
            _subagent_row("shared", "a1", "s3", 5, _at(360)),
        ],
    )

    session = get_sessions_data("claude", "all")["sessions"][0]

    # main covers 0-240s, the subagent 180-360s: 360s of clock, 420s of agent time.
    assert session["active_ms"] == 360_000
    assert session["active_ms_sum"] == 420_000


def test_subagent_streams_survive_a_cached_read(monkeypatch, _isolated_claude_home):
    home = _isolated_claude_home
    _write_session_file(
        home,
        "shared",
        [_assistant_row("shared", "m1", 11, _at(0)), _assistant_row("shared", "m2", 11, _at(60))],
    )
    _write_subagent_file(
        home,
        "shared",
        "a1",
        [
            _subagent_row("shared", "a1", "s1", 5, _at(0)),
            _subagent_row("shared", "a1", "s2", 5, _at(60)),
        ],
    )

    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    reload_pricing_db()
    live = get_sessions_data("claude", "all")

    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    reload_pricing_db()
    cached = get_sessions_data("claude", "all")

    live.pop("timestamp")
    cached.pop("timestamp")
    assert cached == live
    assert live["summary"]["active_ms_sum"] == 120_000


def test_identical_usage_in_two_streams_is_not_deduplicated(monkeypatch, _isolated_claude_home):
    """Two agents can report the same tokens in the same second; both must count.

    Claude has no per-event id, so turns are deduplicated by their fields. Without
    the stream in that identity the subagent's minute is dropped as a duplicate of
    the main agent's, taking its agent time with it.
    """
    home = _isolated_claude_home
    _write_session_file(
        home,
        "shared",
        [_assistant_row("shared", "m1", 11, _at(0)), _assistant_row("shared", "m2", 11, _at(60))],
    )
    _write_subagent_file(
        home,
        "shared",
        "a1",
        [
            _subagent_row("shared", "a1", "m1", 11, _at(0)),
            _subagent_row("shared", "a1", "m2", 11, _at(60)),
        ],
    )

    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    reload_pricing_db()
    live = get_sessions_data("claude", "all")
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    reload_pricing_db()
    cached = get_sessions_data("claude", "all")

    session = live["sessions"][0]
    assert session["token_events"] == 4
    assert session["active_ms"] == 60_000
    assert session["active_ms_sum"] == 120_000
    live.pop("timestamp")
    cached.pop("timestamp")
    assert cached == live


def test_duplicate_rows_within_one_stream_are_still_deduplicated(_isolated_claude_home):
    """The same event logged in two files of one session stays one turn."""
    home = _isolated_claude_home
    row = _assistant_row("shared", "m1", 11, _at(0))
    _write_session_file(home, "a-first", [row])
    _write_session_file(home, "b-second", [row])

    session = get_sessions_data("claude", "all")["sessions"][0]

    assert session["token_events"] == 1


def test_claude_turns_carry_a_stream_id_that_never_reaches_the_api(_isolated_claude_home):
    home = _isolated_claude_home
    _write_session_file(home, "shared", [_assistant_row("shared", "m1", 11, _at(0))])
    _write_subagent_file(home, "shared", "a1", [_subagent_row("shared", "a1", "s1", 5, _at(30))])

    raw = sessions._claude_sessions()["shared"]
    assert {turn["_stream_id"] for turn in raw["turns"]} == {"main", "a1"}

    detail = sessions.get_session_detail("claude", "shared")
    assert all("_stream_id" not in turn for turn in detail["turns"])
