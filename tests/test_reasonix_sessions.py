"""Tests for Reasonix as a session source in sessions.py.

Reasonix turns carry no tokens: nothing in the session file cluster records
usage, and the daily stats log that does carries no session id. These tests
pin the structure Session Explorer does get — ids, titles, project, turn count
and wall-clock timing — plus the guards that keep bad or future files out.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokdash import clientpaths, sessions
from tokdash.sessions import (
    SESSION_TOOLS,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
    _parse_reasonix_session_file,
    _reasonix_session_signatures,
)

DAY1_MS = int(datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
SESSION_ID = "20260601-120000.000000000-MiniMax-M3"


@pytest.fixture(autouse=True)
def _isolated_reasonix_home(monkeypatch, tmp_path):
    home = tmp_path / "reasonix-home"
    monkeypatch.setenv("REASONIX_HOME", str(home))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    reload_pricing_db()
    yield home
    reload_pricing_db()


def _sessions_dir(home: Path, project_key: str = "-mnt-h-project") -> Path:
    path = home / "projects" / project_key / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_session(
    home: Path,
    session_id: str = SESSION_ID,
    *,
    rows=None,
    meta: dict | None = None,
    project_key: str = "-mnt-h-project",
) -> Path:
    directory = _sessions_dir(home, project_key)
    if rows is None:
        rows = [
            {"role": "system", "content": 'You are Reasonix.\nCurrent workspace: "/home/user/project"'},
            {"role": "user", "content": "test prompt", "raw_content": "test prompt", "createdAt": DAY1_MS},
            {"role": "assistant", "content": "test answer", "workDurationMs": 1200},
        ]
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    if meta is not None:
        (directory / f"{session_id}.jsonl.meta").write_text(json.dumps(meta), encoding="utf-8")
    return path


def _default_meta(session_id: str = SESSION_ID, **overrides) -> dict:
    meta = {
        "id": session_id,
        "created_at": "2026-06-01T12:00:00.000Z",
        "updated_at": "2026-06-01T12:00:05.000Z",
        "model": "minimax-cn/MiniMax-M3",
        "preview": "test prompt",
        "schema_version": 2,
    }
    meta.update(overrides)
    return meta


def _parse(path: Path):
    st = path.stat()
    return _parse_reasonix_session_file(str(path), st.st_mtime_ns, st.st_size)


def test_reasonix_is_a_session_tool(_isolated_reasonix_home):
    assert "reasonix" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["reasonix"] == "Reasonix"
    get_sessions_data("reasonix", "all")  # no home dir: empty source, no error


# --- case 10: meta + jsonl round-trip -----------------------------------------


def test_parse_reasonix_session_basic(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    path = _write_session(home, meta=_default_meta())

    raw = _parse(path)

    assert raw is not None
    assert raw["tool"] == "reasonix"
    assert raw["session_id"] == SESSION_ID
    assert raw["display_name"] == "test prompt"
    # The project-key directory name is lossy; the system prompt's workspace
    # line is the authoritative cwd.
    assert raw["project"] == "project"
    assert len(raw["turns"]) == 1
    assert raw["turns"][0]["model"] == "MiniMax-M3"


def test_reasonix_turns_carry_no_tokens(_isolated_reasonix_home):
    """Documented v1 behavior: structure without usage, priced at zero."""
    home = _isolated_reasonix_home
    _write_session(home, meta=_default_meta())

    result = get_sessions_data("reasonix", "all")
    row = result["sessions"][0]

    assert row["tokens"] == 0
    assert row["cost"] == 0.0


def test_get_sessions_data_reasonix(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_session(home, meta=_default_meta(preview="echo hello"))

    res = get_sessions_data("reasonix", "all")

    assert res["tool"] == "reasonix"
    assert res["tool_label"] == "Reasonix"
    assert res["summary"]["session_count"] == 1
    assert len(res["sessions"]) == 1
    assert res["sessions"][0]["session_id"] == SESSION_ID
    assert res["sessions"][0]["display_name"] == "echo hello"


# --- case 11: missing sidecars ------------------------------------------------


def test_jsonl_without_meta_falls_back_to_the_file_stem(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    path = _write_session(home, "20260601-120000.000000000-qwen3.8-27B-FP8", meta=None)

    raw = _parse(path)

    assert raw is not None
    assert raw["session_id"] == "20260601-120000.000000000-qwen3.8-27B-FP8"
    # Without meta there is no session model; the title still comes from the
    # first user message.
    assert raw["turns"][0]["model"] == "unknown"
    assert raw["display_name"] == "test prompt"


def test_meta_without_jsonl_yields_no_session(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    directory = _sessions_dir(home)
    (directory / f"{SESSION_ID}.jsonl.meta").write_text(
        json.dumps(_default_meta()), encoding="utf-8"
    )

    assert _reasonix_session_signatures() == ()
    assert get_sessions_data("reasonix", "all")["summary"]["session_count"] == 0


def test_conversation_without_an_assistant_turn_is_not_a_session(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    path = _write_session(
        home,
        rows=[
            {"role": "system", "content": 'Current workspace: "/home/user/project"'},
            {"role": "user", "content": "hi", "createdAt": DAY1_MS},
        ],
        meta=_default_meta(),
    )

    assert _parse(path) is None
    assert get_sessions_data("reasonix", "all")["summary"]["session_count"] == 0


# --- case 12: discovery excludes the snapshot log -----------------------------


def test_events_log_is_excluded_from_discovery(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_session(home, meta=_default_meta())
    directory = _sessions_dir(home)
    # The events log repeats the whole message array per revision and carries no
    # usage; parsing it would double the session.
    (directory / f"{SESSION_ID}.events.jsonl").write_text(
        json.dumps({"type": "replace", "revision": 1, "messages": []}) + "\n", encoding="utf-8"
    )

    discovered = [Path(p).name for p, _, _ in _reasonix_session_signatures()]

    assert discovered == [f"{SESSION_ID}.jsonl"]
    assert get_sessions_data("reasonix", "all")["summary"]["session_count"] == 1


# --- case 13: schema gating ---------------------------------------------------


def test_future_meta_schema_version_is_skipped_not_parsed_blind(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    path = _write_session(home, meta=_default_meta(schema_version=99))

    assert _parse(path) is None
    assert get_sessions_data("reasonix", "all")["summary"]["session_count"] == 0


def test_known_and_absent_meta_schema_versions_still_parse(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    known = _write_session(home, "known", meta=_default_meta("known", schema_version=2))
    absent = _write_session(home, "absent", meta=_default_meta("absent", schema_version=None))
    del_meta = _default_meta("absent")
    del del_meta["schema_version"]
    (_sessions_dir(home) / "absent.jsonl.meta").write_text(json.dumps(del_meta), encoding="utf-8")

    assert _parse(known) is not None
    assert _parse(absent) is not None


# --- turn timing --------------------------------------------------------------


def test_turn_timestamps_mark_the_end_of_each_assistant_step(_isolated_reasonix_home):
    """_active_intervals reads a timestamp as the instant work finished.

    Stamping the start instead shifts every event earlier by its own duration
    and silently drops the last step's work from the session entirely.
    """
    home = _isolated_reasonix_home
    path = _write_session(
        home,
        rows=[
            {"role": "system", "content": 'Current workspace: "/home/user/project"'},
            {"role": "user", "content": "hi", "createdAt": DAY1_MS},
            {"role": "assistant", "content": "call a tool", "workDurationMs": 5000},
            {"role": "tool", "content": "tool output", "tool_call_id": "t1"},
            {"role": "assistant", "content": "answer", "workDurationMs": 9000},
        ],
        meta=_default_meta(),
    )

    raw = _parse(path)
    stamps = [turn["timestamp_ms"] for turn in raw["turns"]]

    assert stamps == [DAY1_MS + 5000, DAY1_MS + 14000]
    # The user message anchors the first step, whose work would otherwise have
    # no predecessor to be measured against.
    assert raw["_prior_event_ms"] == DAY1_MS
    assert len({turn["_event_key"] for turn in raw["turns"]}) == 2


def test_active_time_counts_every_step_including_the_last(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_session(
        home,
        rows=[
            {"role": "system", "content": 'Current workspace: "/home/user/project"'},
            {"role": "user", "content": "hi", "createdAt": DAY1_MS},
            {"role": "assistant", "content": "one", "workDurationMs": 30_000},
            {"role": "assistant", "content": "two", "workDurationMs": 4_000},
        ],
        meta=_default_meta(),
    )

    row = get_sessions_data("reasonix", "all")["sessions"][0]

    # 30s + 4s of explicit work: neither the first step (no predecessor) nor the
    # last (no successor) may fall out of the total.
    assert row["active_ms"] == 34_000


# --- change detection ---------------------------------------------------------


def test_sidecar_meta_edits_change_the_file_signature(_isolated_reasonix_home):
    """The parser reads the .meta, so a meta-only edit must invalidate caches."""
    home = _isolated_reasonix_home
    _write_session(home, meta=_default_meta())
    before = _reasonix_session_signatures()

    (_sessions_dir(home) / f"{SESSION_ID}.jsonl.meta").write_text(
        json.dumps(_default_meta(preview="a substantially longer preview line")),
        encoding="utf-8",
    )
    after = _reasonix_session_signatures()

    assert before != after
    assert [p for p, _, _ in before] == [p for p, _, _ in after]


def test_appending_a_turn_is_picked_up(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_session(home, meta=_default_meta())
    assert len(get_sessions_data("reasonix", "all")["sessions"][0].keys()) > 0
    assert get_sessions_data("reasonix", "all")["sessions"][0]["token_events"] == 1

    _write_session(
        home,
        rows=[
            {"role": "system", "content": 'Current workspace: "/home/user/project"'},
            {"role": "user", "content": "test prompt", "raw_content": "test prompt", "createdAt": DAY1_MS},
            {"role": "assistant", "content": "test answer", "workDurationMs": 1200},
            {"role": "user", "content": "again", "createdAt": DAY1_MS + 60_000},
            {"role": "assistant", "content": "second answer"},
        ],
        meta=_default_meta(),
    )

    assert get_sessions_data("reasonix", "all")["sessions"][0]["token_events"] == 2


# --- case 15: API smoke -------------------------------------------------------


def test_api_routes_serve_reasonix(_isolated_reasonix_home, monkeypatch):
    from tokdash import api

    home = _isolated_reasonix_home
    _write_session(home, meta=_default_meta())
    # Keep the cross-tool active-time rollup to reasonix only; the other tools
    # would scan this machine's real logs.
    monkeypatch.setattr(sessions, "SESSION_TOOLS", ("reasonix",))

    listing = api.get_sessions(tool="reasonix", period="all")
    assert listing["tool"] == "reasonix"
    assert listing["sessions"][0]["session_id"] == SESSION_ID

    detail = api.get_session(tool="reasonix", session_id=SESSION_ID)
    assert detail["turns"]

    active = api.get_active_time(period="all", refresh=True)
    assert "reasonix" in active["by_tool"]


def test_session_detail_reports_zero_token_turns(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_session(home, meta=_default_meta())

    detail = get_session_detail("reasonix", SESSION_ID)

    assert detail["turns"]
    assert all(turn["tokens"] == 0 for turn in detail["turns"])


def test_reasonix_home_override_points_the_session_reader(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    assert clientpaths.reasonix_projects_dir() == home / "projects"


def test_user_thinking_time_is_excluded_not_capped(_isolated_reasonix_home):
    """Reasonix times each step, so the gap heuristic's cap does not apply.

    A capped gap would bill the 10-minute pause as 5 more minutes of agent work.
    """
    home = _isolated_reasonix_home
    _write_session(
        home,
        rows=[
            {"role": "system", "content": 'Current workspace: "/home/user/project"'},
            {"role": "user", "content": "hi", "createdAt": DAY1_MS},
            {"role": "assistant", "content": "one", "workDurationMs": 10_000},
            {"role": "user", "content": "again", "createdAt": DAY1_MS + 610_000},
            {"role": "assistant", "content": "two", "workDurationMs": 5_000},
        ],
        meta=_default_meta(),
    )

    row = get_sessions_data("reasonix", "all")["sessions"][0]

    assert row["active_ms"] == 15_000
    assert row["span_ms"] == 605_000


def test_turns_without_a_recorded_duration_keep_the_gap_heuristic(_isolated_reasonix_home):
    """Only measured steps opt out; the rest behave like every other tool."""
    home = _isolated_reasonix_home
    _write_session(
        home,
        rows=[
            {"role": "system", "content": 'Current workspace: "/home/user/project"'},
            {"role": "user", "content": "hi", "createdAt": DAY1_MS},
            {"role": "assistant", "content": "one"},
            {"role": "user", "content": "again", "createdAt": DAY1_MS + 20_000},
            {"role": "assistant", "content": "two"},
        ],
        meta=_default_meta(),
    )

    row = get_sessions_data("reasonix", "all")["sessions"][0]

    assert row["active_ms"] == 20_000


def test_a_stale_user_timestamp_never_rewinds_the_clock(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_session(
        home,
        rows=[
            {"role": "system", "content": 'Current workspace: "/home/user/project"'},
            {"role": "user", "content": "hi", "createdAt": DAY1_MS},
            {"role": "assistant", "content": "one", "workDurationMs": 9_000},
            # Older than the work already timed; honoring it would produce a
            # negative gap and a session that ends before it starts.
            {"role": "user", "content": "stale", "createdAt": DAY1_MS - 60_000},
            {"role": "assistant", "content": "two", "workDurationMs": 1_000},
        ],
        meta=_default_meta(),
    )

    row = get_sessions_data("reasonix", "all")["sessions"][0]

    assert row["span_ms"] >= 0
    assert row["active_ms"] == 10_000


def test_single_step_session_still_reports_its_work(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_session(
        home,
        rows=[
            {"role": "system", "content": 'Current workspace: "/home/user/project"'},
            {"role": "user", "content": "hi", "createdAt": DAY1_MS},
            {"role": "assistant", "content": "only", "workDurationMs": 12_000},
        ],
        meta=_default_meta(),
    )

    row = get_sessions_data("reasonix", "all")["sessions"][0]

    assert row["active_ms"] == 12_000


def test_a_session_with_no_timing_information_reports_no_active_time(_isolated_reasonix_home):
    """No createdAt and no workDurationMs: nothing to measure, so claim nothing."""
    home = _isolated_reasonix_home
    path = _write_session(
        home,
        rows=[
            {"role": "system", "content": 'Current workspace: "/home/user/project"'},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "only"},
        ],
        meta=_default_meta(),
    )

    assert "_prior_event_ms" not in _parse(path)
    assert get_sessions_data("reasonix", "all")["sessions"][0]["active_ms"] == 0


def test_cached_and_live_reasonix_payloads_are_identical(_isolated_reasonix_home, monkeypatch):
    """The store must round-trip the first-turn anchor, or active time drifts."""
    home = _isolated_reasonix_home
    _write_session(
        home,
        rows=[
            {"role": "system", "content": 'Current workspace: "/home/user/project"'},
            {"role": "user", "content": "hi", "createdAt": DAY1_MS},
            {"role": "assistant", "content": "one", "workDurationMs": 30_000},
            {"role": "assistant", "content": "two", "workDurationMs": 4_000},
        ],
        meta=_default_meta(),
    )

    stored = get_sessions_data("reasonix", "all")

    reload_pricing_db()
    monkeypatch.setattr(sessions, "persistent_usage_db_enabled", lambda: False)
    live = get_sessions_data("reasonix", "all")

    assert live["sessions"] == stored["sessions"]
    assert live["sessions"][0]["active_ms"] == 34_000
