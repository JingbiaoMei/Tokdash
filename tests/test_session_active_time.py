"""Tests for session active time (span minus idle)."""
from datetime import datetime, timezone

import pytest

from tokdash import sessions
from tokdash.sessions import (
    ACTIVE_GAP_CAP_MS_DEFAULT,
    _active_intervals,
    _measured_intervals,
    _merged_interval_ms,
    _session_active_intervals,
    _summarize_session,
    active_gap_cap_ms,
    get_sessions_data,
)

MINUTE = 60_000
HOUR = 60 * MINUTE
BASE_MS = int(datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _turn(offset_ms, tokens=100):
    return {
        "turn_index": 0,
        "timestamp_ms": BASE_MS + offset_ms,
        "model": "test-model",
        "tokens_in": tokens,
        "tokens_cache": 0,
        "tokens_out": tokens,
        "tokens_reasoning": 0,
        "tokens": tokens * 2,
        "cache_hit_rate": 0.0,
        "cost": 0.0,
    }


def _raw(session_id, offsets, project="proj"):
    return {
        "tool": "codex",
        "session_id": session_id,
        "display_name": session_id,
        "project": project,
        "turns": [_turn(offset) for offset in offsets],
    }


def test_active_intervals_cap_long_gaps():
    """A gap longer than the cap contributes only the cap; the rest is idle."""
    cap = 5 * MINUTE
    intervals = _active_intervals([0, MINUTE, MINUTE + 3 * HOUR], cap)

    assert intervals == [(0, MINUTE), (MINUTE + 3 * HOUR - cap, MINUTE + 3 * HOUR)]
    assert sum(end - start for start, end in intervals) == MINUTE + cap


def test_active_intervals_ignore_duplicate_and_unordered_stamps():
    """Out-of-order input is sorted; zero-length gaps add nothing."""
    assert _active_intervals([2 * MINUTE, 0, 2 * MINUTE], 5 * MINUTE) == [(0, 2 * MINUTE)]


def test_active_intervals_single_event_has_no_measurable_time():
    assert _active_intervals([BASE_MS], 5 * MINUTE) == []


def test_measured_intervals_use_the_recorded_duration_not_the_gap():
    """A source that timed its own work needs no cap: idle is simply not in it."""
    cap = 5 * MINUTE
    # 10s of work, the user thinks for 10 minutes, then 5s of work.
    events = [(10_000, 10_000), (10 * MINUTE + 15_000, 5_000)]

    intervals = _measured_intervals(events, cap)

    assert intervals == [(0, 10_000), (10 * MINUTE + 10_000, 10 * MINUTE + 15_000)]
    assert sum(end - start for start, end in intervals) == 15_000


def test_measured_intervals_are_not_capped():
    """The cap strips idle out of an inferred gap; a timed duration has none."""
    assert _measured_intervals([(3 * HOUR, 3 * HOUR)], 5 * MINUTE) == [(0, 3 * HOUR)]


def test_measured_intervals_measure_a_lone_event():
    """Unlike the gap rule, a single timed event is measurable on its own."""
    assert _measured_intervals([(BASE_MS, 7_000)], 5 * MINUTE) == [(BASE_MS - 7_000, BASE_MS)]


def test_measured_intervals_fall_back_to_the_capped_gap():
    """Events with no recorded duration keep the heuristic unchanged."""
    cap = 5 * MINUTE
    plain = [(0, None), (MINUTE, None), (MINUTE + 3 * HOUR, None)]

    assert _measured_intervals(plain, cap) == _active_intervals([0, MINUTE, MINUTE + 3 * HOUR], cap)


def test_measured_and_unmeasured_events_mix_within_one_stream():
    """A timed event still anchors the gap for an untimed one after it."""
    cap = 5 * MINUTE
    events = [(10_000, 10_000), (30_000, None)]

    assert _measured_intervals(events, cap) == [(0, 10_000), (10_000, 30_000)]


def test_merged_interval_counts_overlap_once():
    """Parallel sessions covering the same clock time count once."""
    merged = _merged_interval_ms([(0, 10 * MINUTE), (5 * MINUTE, 15 * MINUTE), (30 * MINUTE, 40 * MINUTE)])
    assert merged == 25 * MINUTE


def test_merged_interval_touching_intervals_do_not_double_count():
    assert _merged_interval_ms([(0, MINUTE), (MINUTE, 2 * MINUTE)]) == 2 * MINUTE


def test_summarize_session_reports_active_and_span():
    """Active time drops the 3h idle stretch that span keeps."""
    summary = _summarize_session(_raw("s1", [0, MINUTE, 2 * MINUTE, 2 * MINUTE + 3 * HOUR]))

    assert summary["span_ms"] == 2 * MINUTE + 3 * HOUR
    assert summary["active_ms"] == 2 * MINUTE + ACTIVE_GAP_CAP_MS_DEFAULT


def test_summarize_session_window_clips_active_time():
    """Events outside the window contribute neither span nor active time."""
    raw = _raw("s1", [0, MINUTE, 2 * MINUTE, 10 * MINUTE])
    summary = _summarize_session(raw, since_ms=BASE_MS, until_ms=BASE_MS + 3 * MINUTE)

    assert summary["span_ms"] == 2 * MINUTE
    assert summary["active_ms"] == 2 * MINUTE


def test_active_gap_cap_env_override(monkeypatch):
    monkeypatch.setenv("TOKDASH_ACTIVE_GAP_CAP_SECONDS", "60")
    assert active_gap_cap_ms() == MINUTE

    summary = _summarize_session(_raw("s1", [0, 3 * HOUR]))
    assert summary["active_ms"] == MINUTE


@pytest.mark.parametrize(
    "value",
    # nan/inf parse as floats and would otherwise reach int(), which raises
    # ValueError/OverflowError and takes the whole sessions response down.
    ["", "0", "-5", "not-a-number", "nan", "NaN", "inf", "-inf", "Infinity"],
)
def test_active_gap_cap_env_falls_back_on_bad_values(monkeypatch, value):
    monkeypatch.setenv("TOKDASH_ACTIVE_GAP_CAP_SECONDS", value)
    assert active_gap_cap_ms() == ACTIVE_GAP_CAP_MS_DEFAULT


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_non_finite_gap_cap_still_serves_sessions(monkeypatch, value):
    """A bad env value must not break the response it feeds."""
    monkeypatch.setenv("TOKDASH_ACTIVE_GAP_CAP_SECONDS", value)
    monkeypatch.setattr(sessions, "_raw_sessions_for_tool", lambda *a, **k: {"a": _raw("a", [0, MINUTE])})

    data = get_sessions_data("codex", "all")
    assert data["sessions"][0]["active_ms"] == MINUTE
    assert data["summary"]["active_gap_cap_ms"] == ACTIVE_GAP_CAP_MS_DEFAULT


def test_active_gap_cap_env_is_clamped(monkeypatch):
    monkeypatch.setenv("TOKDASH_ACTIVE_GAP_CAP_SECONDS", "999999")
    assert active_gap_cap_ms() == 6 * 60 * 60 * 1000


def test_sessions_summary_dedups_parallel_sessions(monkeypatch):
    """Two agents running the same hour are one hour of clock time, two of agent time."""
    parallel = {
        "a": _raw("a", [0, MINUTE, 2 * MINUTE]),
        "b": _raw("b", [MINUTE, 2 * MINUTE, 3 * MINUTE]),
    }
    monkeypatch.setattr(sessions, "_raw_sessions_for_tool", lambda *a, **k: parallel)

    data = get_sessions_data("codex", "all")
    summary = data["summary"]

    assert summary["active_ms"] == 3 * MINUTE
    assert summary["active_ms_sum"] == 4 * MINUTE
    assert summary["span_ms"] == 4 * MINUTE
    assert summary["active_gap_cap_ms"] == ACTIVE_GAP_CAP_MS_DEFAULT


def test_session_payload_hides_interval_internals(monkeypatch):
    """The interval list backing the union is internal and must not be serialized."""
    monkeypatch.setattr(sessions, "_raw_sessions_for_tool", lambda *a, **k: {"a": _raw("a", [0, MINUTE])})

    data = get_sessions_data("codex", "all")
    assert "_active_intervals" not in data["sessions"][0]
    assert data["sessions"][0]["active_ms"] == MINUTE

    detail = sessions.get_session_detail("codex", "a")
    assert "_active_intervals" not in detail["session"]
    assert detail["session"]["active_ms"] == MINUTE


def _stream_turn(offset_ms, stream_id, tokens=100):
    turn = _turn(offset_ms, tokens)
    turn["_stream_id"] = stream_id
    return turn


def _multi_stream_raw(session_id, streams):
    return {
        "tool": "kimi",
        "session_id": session_id,
        "display_name": session_id,
        "project": "proj",
        "turns": [
            _stream_turn(offset, stream_id)
            for stream_id, offsets in streams.items()
            for offset in offsets
        ],
    }


def test_parallel_agents_are_timed_as_separate_streams():
    """Two agents working the same minute: one minute of clock, two of agent time."""
    raw = _multi_stream_raw("s1", {"main": [0, 60_000], "agent-0": [0, 60_000]})

    summary = _summarize_session(raw)

    assert summary["active_ms"] == 60_000
    assert summary["active_ms_sum"] == 120_000


def test_interleaved_agents_are_not_stitched_into_one_timeline():
    """Interleaved events must not shorten each stream's gaps."""
    raw = _multi_stream_raw(
        "s1",
        {"main": [0, 120_000, 240_000], "agent-0": [180_000, 300_000, 360_000]},
    )

    summary = _summarize_session(raw)

    # main covers 0-240s, agent-0 covers 180-360s: 360s of clock, 420s of agent time.
    assert summary["active_ms"] == 360_000
    assert summary["active_ms_sum"] == 420_000


def test_single_stream_sessions_report_equal_clock_and_agent_time():
    summary = _summarize_session(_raw("s1", [0, MINUTE, 2 * MINUTE]))
    assert summary["active_ms"] == summary["active_ms_sum"] == 2 * MINUTE


def test_interval_crossing_the_window_start_is_clipped_not_dropped():
    """A 23:59-00:01 stretch gives the new day its minute, not zero."""
    midnight = BASE_MS + HOUR
    raw = {
        "tool": "codex",
        "session_id": "s1",
        "display_name": "s1",
        "project": "proj",
        "turns": [
            {**_turn(0), "timestamp_ms": midnight - MINUTE},
            {**_turn(0), "timestamp_ms": midnight + MINUTE},
        ],
    }

    summary = _summarize_session(raw, since_ms=midnight, until_ms=midnight + HOUR)

    assert summary["active_ms"] == MINUTE
    assert summary["span_ms"] == 0  # a single event sits inside the window


def test_interval_crossing_the_window_end_is_clipped():
    raw = {
        "tool": "codex",
        "session_id": "s1",
        "display_name": "s1",
        "project": "proj",
        "turns": [
            {**_turn(0), "timestamp_ms": BASE_MS},
            {**_turn(0), "timestamp_ms": BASE_MS + 2 * MINUTE},
        ],
    }

    summary = _summarize_session(raw, since_ms=BASE_MS - HOUR, until_ms=BASE_MS + MINUTE)

    assert summary["active_ms"] == MINUTE


def test_prior_event_seeds_the_first_in_window_interval():
    """Loaders that window at the source hand back the event they held out.

    The held-out event sits 30s before the window and the first in-window event
    30s after it, so half of that stretch belongs to the window.
    """
    raw = _raw("s1", [30_000, 90_000])
    raw["_prior_event_ms"] = BASE_MS - 30_000

    without_prior = _summarize_session(_raw("s1", [30_000, 90_000]), since_ms=BASE_MS, until_ms=BASE_MS + HOUR)
    with_prior = _summarize_session(raw, since_ms=BASE_MS, until_ms=BASE_MS + HOUR)

    assert without_prior["active_ms"] == 60_000
    assert with_prior["active_ms"] == 90_000


def test_summary_reports_the_estimate_contract():
    monkeypatched = {"a": _raw("a", [0, MINUTE])}
    import tokdash.sessions as sessions_module

    original = sessions_module._raw_sessions_for_tool
    sessions_module._raw_sessions_for_tool = lambda *a, **k: monkeypatched
    try:
        summary = get_sessions_data("codex", "all")["summary"]
    finally:
        sessions_module._raw_sessions_for_tool = original

    assert summary["active_time_estimated"] is True
    assert summary["active_time_method"] == "capped-inter-event-gap"

# --- Dashboard wiring -------------------------------------------------------
# The Active column and its labels live in index.html; these guard the parts that
# a Python-only test would otherwise miss.

import re  # noqa: E402
from pathlib import Path  # noqa: E402

import tokdash  # noqa: E402

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"
SESSION_PANELS = ("codex", "claude", "opencode", "pi_agent", "mimo", "kimi", "combined")


def test_every_session_table_has_a_sortable_active_column():
    source = INDEX_HTML.read_text(encoding="utf-8")
    for panel in SESSION_PANELS:
        table = source.split(f'data-panel="{panel}"', 1)[1].split("</table>", 1)[0]
        headers = re.findall(r"<th\s[^>]*>", table)
        assert any('data-sortable="active_ms"' in th for th in headers), panel
        # Column count must match the colgroup or the header row shifts.
        assert len(headers) == len(re.findall(r"<col ", table)), panel


def test_combined_panel_does_not_claim_cross_tool_dedup():
    """Its total sums per-tool actives, so it must not reuse the per-tool hint."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    combined_header = source.split('data-i18n="combinedSessions"', 1)[1].split("</section>", 1)[0]
    assert 'data-i18n-title="activeTimeCombinedHint"' in combined_header
    assert "agentTimePanelHint" not in combined_header


def test_active_time_labels_exist_in_both_languages():
    source = INDEX_HTML.read_text(encoding="utf-8")
    for key in (
        "activeTime",
        "activeTimeColumn",
        "agentTime",
        "idleCap",
        "span",
        "activeTimeHint",
        "activeTimeGroupHint",
        "agentTimePanelHint",
        "activeTimeCombinedHint",
        "activeTimeMultiServerHint",
    ):
        assert len(re.findall(rf"^\s+{key}: ", source, re.MULTILINE)) == 2, key


def test_multi_server_totals_are_relabelled_as_sums():
    """Per-server actives are added up, so the per-tool hint must not be reused."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    for panel in SESSION_PANELS:
        assert f'id="{panel}ActiveLabel"' in source, panel

    panel_fn = source.split("function updateSessionPanel(", 1)[1].split("\n    function ", 1)[0]
    assert "selectedServers().length > 1" in panel_fn
    assert "activeTimeMultiServerHint" in panel_fn
    # The attributes must be updated too, or applyI18n() restores the old wording.
    assert 'setAttribute("data-i18n-title", hintKey)' in panel_fn


def test_panel_kpi_shows_agent_time_only():
    """The header KPI is estimated agent time; the deduplicated clock-time value
    and its sub-span were removed from every panel."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert "ActiveTotal" not in source
    assert "session-panel-kpi-sub" not in source
    labels = re.findall(
        r'id="(\w+)ActiveLabel" class="session-panel-kpi-label" data-i18n="(\w+)"', source)
    panels = set(re.findall(r'data-panel-details="(\w+)"', source))
    # Every rendered panel carries exactly one agent-time KPI label, and the
    # ids must be unique: a duplicated panel block clones its ids too, and a
    # set comparison (labels vs panels) cannot see that — both sides grow
    # together. len == len(set) is what counts the clones.
    assert len(labels) == len({prefix for prefix, _ in labels}), labels
    assert {prefix for prefix, _ in labels} == panels, (labels, panels)
    for prefix, key in labels:
        assert key == "agentTime", (prefix, key)


# --- SQL loaders ------------------------------------------------------------
# OpenCode and Mimo window at the source, so they must hand back the last event
# they held out or a continuing session loses the work leading into the window.

import json as _json  # noqa: E402
import sqlite3  # noqa: E402


def _opencode_db(path, timestamps):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT);
            CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, title TEXT, slug TEXT);
            CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
            """
        )
        conn.execute("INSERT INTO project(id, worktree) VALUES('p1', '/workspace/proj')")
        conn.execute(
            "INSERT INTO session(id, project_id, directory, title, slug)"
            " VALUES('s1', 'p1', '/workspace/proj', 'Session', 'slug')"
        )
        payload = {
            "role": "assistant",
            "modelID": "glm-5.2",
            "providerID": "zai",
            "tokens": {"input": 5, "output": 5, "reasoning": 0, "cache": {"write": 0, "read": 0}},
        }
        for index, item in enumerate(timestamps):
            # Items are timestamps, or (timestamp, role) when the roles matter.
            created, role = item if isinstance(item, tuple) else (item, "assistant")
            data = payload if role == "assistant" else {"role": role}
            conn.execute(
                "INSERT INTO message(id, session_id, time_created, data) VALUES(?, 's1', ?, ?)",
                (f"m{index}", created, _json.dumps(data)),
            )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.mark.parametrize("loader_name", ["_load_opencode_sessions_scalar", "_load_opencode_sessions_raw_json"])
def test_opencode_loaders_hand_back_the_event_before_the_window(tmp_path, loader_name):
    since, until = 1_000_000, 2_000_000
    db_path = _opencode_db(tmp_path / f"{loader_name}.db", [since - 30_000, since + 30_000, since + 90_000])

    loader = getattr(sessions, loader_name)
    loaded = loader(db_path, since_ms=since, until_ms=until)["s1"]

    assert loaded["_prior_event_ms"] == since - 30_000
    # Only in-window events are loaded as turns; the held-out one just seeds time.
    assert [turn["timestamp_ms"] for turn in loaded["turns"]] == [since + 30_000, since + 90_000]

    summary = _summarize_session(loaded, since_ms=since, until_ms=until)
    assert summary["active_ms"] == 90_000

    loaded.pop("_prior_event_ms")
    assert _summarize_session(loaded, since_ms=since, until_ms=until)["active_ms"] == 60_000


def test_opencode_loader_omits_prior_event_for_a_session_that_starts_in_window(tmp_path):
    since, until = 1_000_000, 2_000_000
    db_path = _opencode_db(tmp_path / "fresh.db", [since + 30_000, since + 90_000])

    loaded = sessions._load_opencode_sessions_scalar(db_path, since_ms=since, until_ms=until)["s1"]

    assert "_prior_event_ms" not in loaded
    assert _summarize_session(loaded, since_ms=since, until_ms=until)["active_ms"] == 60_000


@pytest.mark.parametrize("loader_name", ["_load_opencode_sessions_scalar", "_load_opencode_sessions_raw_json"])
def test_opencode_loaders_hand_back_the_event_after_the_window(tmp_path, loader_name):
    """Work spanning the right edge belongs partly to the window that ends there."""
    since, until = 1_000_000, 2_000_000
    db_path = _opencode_db(tmp_path / f"{loader_name}-right.db", [until - 30_000, until + 30_000])

    loaded = getattr(sessions, loader_name)(db_path, since_ms=since, until_ms=until)["s1"]

    assert loaded["_next_event_ms"] == until + 30_000
    assert [turn["timestamp_ms"] for turn in loaded["turns"]] == [until - 30_000]

    summary = _summarize_session(loaded, since_ms=since, until_ms=until)
    assert summary["active_ms"] == 30_000

    loaded.pop("_next_event_ms")
    assert _summarize_session(loaded, since_ms=since, until_ms=until)["active_ms"] == 0


def _both_loaders(db_path, since, until):
    return (
        sessions._load_opencode_sessions_scalar(db_path, since_ms=since, until_ms=until)["s1"],
        sessions._load_opencode_sessions_raw_json(db_path, since_ms=since, until_ms=until)["s1"],
    )


def test_a_user_row_after_the_window_is_not_a_token_event(tmp_path):
    """The session's last token event is in the window; only a user turn follows.

    The loader without JSON1 reads roles in Python, so it must skip that user row
    rather than treat it as the next token event and invent activity the scalar
    loader reports as none.
    """
    since, until = 1_000_000, 2_000_000
    db_path = _opencode_db(
        tmp_path / "user-after.db",
        [(until - 10_000, "assistant"), (until + 1_000, "user")],
    )

    scalar, raw = _both_loaders(db_path, since, until)

    assert "_next_event_ms" not in scalar
    assert "_next_event_ms" not in raw
    for loaded in (scalar, raw):
        assert _summarize_session(loaded, since_ms=since, until_ms=until)["active_ms"] == 0


def test_a_user_row_before_the_window_is_not_a_token_event(tmp_path):
    """Nothing precedes the window but a user turn, so nothing leads into it."""
    since, until = 1_000_000, 2_000_000
    db_path = _opencode_db(
        tmp_path / "user-before.db",
        [(since - 1_000, "user"), (since + 30_000, "assistant")],
    )

    scalar, raw = _both_loaders(db_path, since, until)

    assert "_prior_event_ms" not in scalar
    assert "_prior_event_ms" not in raw
    for loaded in (scalar, raw):
        assert _summarize_session(loaded, since_ms=since, until_ms=until)["active_ms"] == 0


def test_a_user_row_does_not_shadow_the_token_event_behind_it(tmp_path):
    """With a real token event further back, that one is the prior event."""
    since, until = 1_000_000, 2_000_000
    earlier = since - ACTIVE_GAP_CAP_MS_DEFAULT - 60_000
    db_path = _opencode_db(
        tmp_path / "user-shadow.db",
        [(earlier, "assistant"), (since - 1_000, "user"), (since + 30_000, "assistant")],
    )

    scalar, raw = _both_loaders(db_path, since, until)

    assert scalar["_prior_event_ms"] == raw["_prior_event_ms"] == earlier
    for loaded in (scalar, raw):
        # The work leading up to the in-window event, from the window's start.
        assert _summarize_session(loaded, since_ms=since, until_ms=until)["active_ms"] == 30_000


def test_opencode_loader_spanning_both_edges_credits_only_the_window(tmp_path):
    since, until = 1_000_000, 2_000_000
    db_path = _opencode_db(
        tmp_path / "both-edges.db",
        [since - 30_000, since + 30_000, until - 30_000, until + 30_000],
    )

    loaded = sessions._load_opencode_sessions_scalar(db_path, since_ms=since, until_ms=until)["s1"]
    summary = _summarize_session(loaded, since_ms=since, until_ms=until)

    # 30s carried over the left edge, the idle stretch between the two in-window
    # events truncated to the cap, then 30s carried over the right edge.
    assert summary["active_ms"] == 30_000 + ACTIVE_GAP_CAP_MS_DEFAULT + 30_000
    assert summary["active_ms"] == 360_000


def test_zcode_activity_events_and_boundary_work():
    """ZCode activity events and boundary work stamps feed the interval
    builder: an activity event credits its work, and a next-boundary event
    carries the overlapping measured work from outside the window."""
    raw = {
        "tool": "zcode", "session_id": "s", "turns": [],
        "_activity_events": [(BASE_MS + 2 * MINUTE, 60_000)],
        "_next_event_ms": BASE_MS + 5 * MINUTE,
        "_next_work_ms": 4 * MINUTE,
    }
    intervals = _session_active_intervals(raw, 5 * MINUTE, None, None)
    assert (BASE_MS + MINUTE, BASE_MS + 2 * MINUTE) in intervals
    assert (BASE_MS + MINUTE, BASE_MS + 5 * MINUTE) in intervals
