"""The Overview's active-time KPI: one clock across every session tool."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import tokdash
from tokdash import sessions
from tokdash.sessions import get_active_time_data

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
MINUTE = 60_000


def _turn(offset_ms):
    return {
        "turn_index": 1,
        "timestamp_ms": BASE_MS + offset_ms,
        "model": "m",
        "tokens_in": 10,
        "tokens_cache": 0,
        "tokens_out": 5,
        "tokens_reasoning": 0,
        "tokens": 15,
        "cache_hit_rate": None,
        "cost": 0.01,
    }


def _raw(tool, session_id, offsets, **extra):
    return {
        "tool": tool,
        "session_id": session_id,
        "display_name": session_id,
        "project": "proj",
        "turns": [_turn(offset) for offset in offsets],
        **extra,
    }


@pytest.fixture
def fake_tools(monkeypatch):
    """Serve canned raw sessions per tool, so timing is the only variable."""
    by_tool: dict[str, object] = {}

    def loader(tool, since_ms=None, until_ms=None):
        value = by_tool.get(tool, {})
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(sessions, "_raw_sessions_for_tool", loader)
    return by_tool


def test_overlapping_tools_count_once_on_the_clock(fake_tools):
    """Codex and Claude sharing a half minute: 90s of clock, 120s of agent time."""
    fake_tools["codex"] = {"c1": _raw("codex", "c1", [0, MINUTE])}
    fake_tools["claude"] = {"a1": _raw("claude", "a1", [MINUTE // 2, MINUTE + MINUTE // 2])}

    data = get_active_time_data("all")

    assert data["active_ms"] == 90_000
    assert data["active_ms_sum"] == 120_000


def test_each_tool_is_reported_separately(fake_tools):
    fake_tools["codex"] = {"c1": _raw("codex", "c1", [0, MINUTE])}
    fake_tools["kimi"] = {"k1": _raw("kimi", "k1", [0, 2 * MINUTE])}

    by_tool = get_active_time_data("all")["by_tool"]

    assert by_tool["codex"]["active_ms"] == MINUTE
    assert by_tool["codex"]["session_count"] == 1
    assert by_tool["kimi"]["active_ms"] == 2 * MINUTE
    assert by_tool["opencode"]["session_count"] == 0
    assert by_tool["claude"]["tool_label"] == "Claude Code"


def test_parallel_agents_within_a_tool_add_agent_time_only(fake_tools):
    """The per-session split survives the aggregate: one minute clock, two agent."""
    raw = _raw("kimi", "k1", [])
    raw["turns"] = [
        {**_turn(0), "_stream_id": "main"},
        {**_turn(MINUTE), "_stream_id": "main"},
        {**_turn(0), "_stream_id": "agent-0"},
        {**_turn(MINUTE), "_stream_id": "agent-0"},
    ]
    fake_tools["kimi"] = {"k1": raw}

    data = get_active_time_data("all")

    assert data["active_ms"] == MINUTE
    assert data["active_ms_sum"] == 2 * MINUTE


def test_one_unreadable_tool_does_not_blank_the_rest(fake_tools):
    fake_tools["codex"] = {"c1": _raw("codex", "c1", [0, MINUTE])}
    fake_tools["claude"] = RuntimeError("database is locked")

    data = get_active_time_data("all")

    assert data["active_ms"] == MINUTE
    assert data["unavailable_tools"] == ["claude"]
    assert "claude" not in data["by_tool"]


def test_a_malformed_stored_session_does_not_blank_the_rest(fake_tools):
    """Summarizing has to be inside the guard, not just loading.

    A single unparseable timestamp in one stored session raises well after the
    loader has returned; uncaught, it takes the whole KPI down with a 500.
    """
    broken = _raw("claude", "a1", [0, MINUTE])
    broken["turns"][1]["timestamp_ms"] = "not-a-timestamp"
    fake_tools["codex"] = {"c1": _raw("codex", "c1", [0, MINUTE])}
    fake_tools["claude"] = {"a1": broken}

    data = get_active_time_data("all")

    assert data["active_ms"] == MINUTE
    assert data["unavailable_tools"] == ["claude"]
    assert "claude" not in data["by_tool"]
    assert data["by_tool"]["codex"]["session_count"] == 1


def test_codex_review_sessions_follow_the_toggle(fake_tools):
    fake_tools["codex"] = {"c1": _raw("codex", "c1", [0, MINUTE], is_review_session=True)}

    assert get_active_time_data("all", include_review_sessions=False)["active_ms"] == 0
    assert get_active_time_data("all", include_review_sessions=True)["active_ms"] == MINUTE


def test_the_window_clips_the_totals(monkeypatch, fake_tools):
    fake_tools["codex"] = {"c1": _raw("codex", "c1", [0, MINUTE])}
    since = BASE_MS + MINUTE // 2
    seen: list[tuple] = []

    def loader(tool, since_ms=None, until_ms=None):
        seen.append((since_ms, until_ms))
        return fake_tools.get(tool, {})

    monkeypatch.setattr(sessions, "_raw_sessions_for_tool", loader)
    monkeypatch.setattr(sessions, "_window_bounds", lambda *a, **k: (since, since + 10 * MINUTE))
    monkeypatch.setattr(sessions, "_previous_window_bounds", lambda *a, **k: (since - 10 * MINUTE, since))

    data = get_active_time_data("today")

    # The window reaches the loaders, and only the half minute inside it counts.
    assert (since, since + 10 * MINUTE) in seen
    assert data["active_ms"] == MINUTE // 2
    # The comparison window is read the same way, and no other.
    assert set(seen) == {(since, since + 10 * MINUTE), (since - 10 * MINUTE, since)}


def _two_window_loader(current, previous, current_offsets, previous_offsets):
    def loader(tool, since_ms=None, until_ms=None):
        if tool != "codex":
            return {}
        if (since_ms, until_ms) == current:
            return {"now": _raw("codex", "now", current_offsets)}
        if (since_ms, until_ms) == previous:
            return {"before": _raw("codex", "before", previous_offsets)}
        return {}

    return loader


def test_the_payload_compares_against_the_previous_window(monkeypatch, fake_tools):
    """The KPI carries its own delta, from the same window /api/usage compares to."""
    current = (BASE_MS + 10 * MINUTE, BASE_MS + 20 * MINUTE)
    previous = (BASE_MS, BASE_MS + 10 * MINUTE)
    monkeypatch.setattr(
        sessions,
        "_raw_sessions_for_tool",
        _two_window_loader(current, previous, [10 * MINUTE, 13 * MINUTE], [0, 2 * MINUTE]),
    )
    monkeypatch.setattr(sessions, "_window_bounds", lambda *a, **k: current)
    monkeypatch.setattr(sessions, "_previous_window_bounds", lambda *a, **k: previous)

    data = get_active_time_data("today")

    assert data["active_ms_sum"] == 3 * MINUTE
    assert data["comparison"]["active_ms_sum_prev"] == 2 * MINUTE
    assert data["comparison"]["active_ms_prev"] == 2 * MINUTE
    assert data["comparison"]["active_ms_sum_pct"] == 50.0
    assert data["comparison"]["active_ms_pct"] == 50.0


def test_an_empty_previous_window_has_no_percentage(monkeypatch, fake_tools):
    """Nothing to compare with is not a hundred-percent rise."""
    current = (BASE_MS + 10 * MINUTE, BASE_MS + 20 * MINUTE)
    previous = (BASE_MS, BASE_MS + 10 * MINUTE)
    monkeypatch.setattr(
        sessions, "_raw_sessions_for_tool", _two_window_loader(current, previous, [10 * MINUTE, 13 * MINUTE], [])
    )
    monkeypatch.setattr(sessions, "_window_bounds", lambda *a, **k: current)
    monkeypatch.setattr(sessions, "_previous_window_bounds", lambda *a, **k: previous)

    comparison = get_active_time_data("today")["comparison"]

    assert comparison["active_ms_sum_prev"] == 0
    assert comparison["active_ms_sum_pct"] is None


def test_a_failing_comparison_does_not_take_the_runtime_with_it(fake_tools, monkeypatch):
    """The delta is the extra; losing it must not blank the figure it annotates."""
    fake_tools["codex"] = {"c1": _raw("codex", "c1", [0, MINUTE])}

    def explode(*_args, **_kwargs):
        raise RuntimeError("no previous window")

    monkeypatch.setattr(sessions, "_previous_window_bounds", explode)

    data = get_active_time_data("all")

    assert data["active_ms_sum"] == MINUTE
    assert data["comparison"] is None


def test_the_comparison_window_is_the_one_the_other_kpis_use():
    from tokdash import compute

    prev_since, prev_until = compute.previous_period_range("today")
    assert sessions._previous_window_bounds("today") == (
        int(prev_since.timestamp() * 1000),
        int(prev_until.timestamp() * 1000),
    )

    # An explicit range is compared with the range of equal length before it.
    current = sessions._window_bounds("custom", "2026-01-08", "2026-01-14")
    previous = sessions._previous_window_bounds("custom", "2026-01-08", "2026-01-14")
    assert previous[1] == current[0]
    assert previous[1] - previous[0] == current[1] - current[0]


def test_the_payload_states_that_the_figures_are_estimates(fake_tools):
    data = get_active_time_data("all")

    assert data["active_time_estimated"] is True
    assert data["active_time_method"] == "capped-inter-event-gap"
    assert data["active_gap_cap_ms"] == sessions.active_gap_cap_ms()


# --- API route --------------------------------------------------------------

pytest.importorskip("fastapi")
import tokdash.api as api  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_api_cache(monkeypatch):
    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    api._clear_cache()
    yield
    api._clear_cache()


def test_route_returns_the_aggregate(monkeypatch):
    monkeypatch.setattr(
        api,
        "get_active_time_data",
        lambda period, date_from, date_to, include_review_sessions=None: {
            "period": period,
            "active_ms": 1,
            "active_ms_sum": 2,
            "by_tool": {},
        },
    )

    payload = api.get_active_time(period="week")

    assert payload["period"] == "week"
    assert payload["active_ms_sum"] == 2


def test_refresh_bypasses_the_response_cache(monkeypatch):
    """Without this the Refresh button redraws the same figure for the whole TTL."""
    calls = []

    def fake(period, date_from, date_to, include_review_sessions=None):
        calls.append(period)
        return {"period": period, "active_ms": len(calls), "active_ms_sum": 0, "by_tool": {}}

    monkeypatch.setattr(api, "get_active_time_data", fake)

    assert api.get_active_time(period="today")["active_ms"] == 1
    assert api.get_active_time(period="today")["active_ms"] == 1  # served from cache
    assert api.get_active_time(period="today", refresh=True)["active_ms"] == 2
    assert len(calls) == 2


def test_route_rejects_a_malformed_date():
    with pytest.raises(api.HTTPException) as excinfo:
        api.get_active_time(date_from="not-a-date", date_to="2026-01-02")
    assert excinfo.value.status_code == 400


def test_the_warmer_and_the_route_share_a_cache_key(monkeypatch):
    """Drifting key construction is how the warmer silently stops warming.

    Capture the keys each side actually uses — the dashboard's default load sends
    an explicit date pair rather than period=today, so the two are easy to drift.
    """
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    warmed: list[str] = []
    routed: list[str] = []

    monkeypatch.setattr(api, "get_cached_or_fetch", lambda key, fetch, **kw: warmed.append(key))
    api._warm_caches()

    monkeypatch.setattr(
        api,
        "_cached_route",
        lambda route, key, fetch, **kw: routed.append(key) or {},
    )
    api.get_active_time(period="today", date_from=today, date_to=today)

    assert routed, "the route must go through the response cache"
    assert routed[0] in warmed, f"warmer keys {warmed!r} miss the route's {routed[0]!r}"


# --- Dashboard wiring -------------------------------------------------------


def _extract_js_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start >= 0, f"{signature} not found"
    depth = 0
    # Start at the signature's own brace: a default like `options = {}` would
    # otherwise close the body before it opens.
    body_start = start + len(signature) - 1 if signature.endswith("{") else src.find("{", start)
    for index in range(body_start, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


def test_the_card_sits_after_total_messages_in_a_six_column_row():
    source = INDEX_HTML.read_text(encoding="utf-8")
    kpis = source.split('<!-- KPI cards -->', 1)[1].split("</section>", 1)[0]

    assert "lg:grid-cols-6" in kpis
    assert len(re.findall(r'<div class="surface p-5">', kpis)) == 6
    assert kpis.index('id="messagesDelta"') < kpis.index('id="overviewActiveTime"')
    assert kpis.index('id="overviewActiveTime"') < kpis.index('data-i18n="avgCacheHitRate"')
    # A delta line like the cards beside it, and a line for the tools it could not read.
    assert 'id="overviewActiveDelta"' in kpis
    assert 'id="overviewActiveMeta"' in kpis
    # A one-line label, like every other card in the row.
    assert 'data-i18n="agentTimeCard"' in kpis
    assert 'data-i18n-title="agentTimeOverviewHint"' in kpis


def test_the_total_tokens_value_carries_no_unit():
    """The card's own label says "Total Tokens"; the value only costs width."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    renderer = _extract_js_function(source, "function renderOverviewTokenTotal(value = overviewTotalTokensRaw) {")

    assert "formatCompactTokenCount(overviewTotalTokensRaw)" in renderer
    assert "formatReadableTokenCount" not in source
    # The exact count keeps the unit for the tooltip and the screen reader.
    assert "${formatNumber(overviewTotalTokensRaw)} ${t('tokensUnit')}" in renderer
    assert "valueElement.setAttribute('aria-label', exact)" in renderer


def test_the_card_is_rendered_on_every_path_that_can_change_it():
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert "renderOverviewActiveTime();" in _extract_js_function(source, "function renderOverviewTab(data) {")
    assert "renderOverviewActiveTime();" in _extract_js_function(source, "function applyI18n() {")
    assert "loadOverviewActiveTime(customDays, dateFrom, dateTo, { force: forceRefresh })" in source
    # Review sessions count towards active time, so the toggle must refetch.
    toggle = source.split("getElementById('codexReviewToggle')?.addEventListener", 1)[1].split("});", 1)[0]
    assert "loadOverviewActiveTime(" in toggle


def test_a_stale_reply_cannot_overwrite_a_newer_range():
    source = INDEX_HTML.read_text(encoding="utf-8")
    loader = _extract_js_function(source, "async function loadOverviewActiveTime(customDays, dateFrom, dateTo, options = {}) {")

    assert "const requestId = ++overviewActiveTimeState.requestId;" in loader
    assert loader.count("if (requestId !== overviewActiveTimeState.requestId) return;") == 2


def test_a_new_range_blanks_the_card_before_it_loads():
    """A cold read takes seconds; the old range's figure must not sit under the new one."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    invalidate = _extract_js_function(source, "function invalidateOverviewActiveTime(customDays, dateFrom, dateTo) {")
    loader = _extract_js_function(source, "async function loadOverviewActiveTime(customDays, dateFrom, dateTo, options = {}) {")

    assert "if (overviewActiveTimeState.key === requestKey && overviewActiveTimeState.data) return false;" in invalidate
    assert "overviewActiveTimeState.data = null;" in invalidate
    assert "renderOverviewActiveTime();" in invalidate
    # A load already running for the old range must not land under the new one.
    assert "overviewActiveTimeState.requestId += 1;" in invalidate
    # The clear happens before this request takes its id, or it would cancel itself.
    assert loader.index("invalidateOverviewActiveTime(") < loader.index("const requestId =")
    # A same-key forced refresh that fails keeps the figure it already had.
    assert "overviewActiveTimeState.status = overviewActiveTimeState.data ? 'ready' : 'error';" in loader


def test_the_card_is_cleared_the_moment_the_range_changes():
    """Its fetch waits behind /api/usage; the clear must not wait with it."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    updater = _extract_js_function(source, "async function updateDashboard(customDays = null, dateFrom = null, dateTo = null, options = {}) {")
    before_fetch = updater.split("try {", 1)[0]

    assert "invalidateOverviewActiveTime(customDays, dateFrom, dateTo);" in before_fetch
    # Still deferred: the expensive request stays after the primary paint.
    assert "loadOverviewActiveTime(customDays, dateFrom, dateTo, { force: forceRefresh })" in updater.split("} finally {", 1)[1]


def test_a_range_change_during_a_load_is_queued_not_dropped():
    """The picker has already relabelled, so dropping leaves stale numbers on screen.

    Structure only; the queue's behaviour is driven for real against deferred
    fetches in tests/test_dashboard_queue_frontend.py.
    """
    source = INDEX_HTML.read_text(encoding="utf-8")
    updater = _extract_js_function(source, "async function updateDashboard(customDays = null, dateFrom = null, dateTo = null, options = {}) {")
    guard = updater.split("let usageApiUrl;", 1)[0]
    finally_block = updater.split("} finally {", 1)[1]

    # Only work the running load is already doing is dropped, and picking that
    # range again drops whatever was queued behind it.
    assert "if (windowKey === inFlightDashboardKey && !forceRefresh && !inFlightResultDiscarded) {" in guard
    assert "pendingDashboardRequest = null;" in guard
    assert "pendingDashboardRequest = { customDays, dateFrom, dateTo, options };" in guard
    assert "invalidateOverviewActiveTime(customDays, dateFrom, dateTo);" in guard

    # The queued range runs next, and the superseded one skips its deferred work.
    assert "const queued = pendingDashboardRequest;" in finally_block
    assert "pendingDashboardRequest = null;" in finally_block
    assert "updateDashboard(queued.customDays, queued.dateFrom, queued.dateTo, queued.options);" in finally_block
    assert "inFlightDashboardKey = null;" in finally_block
    deferred = finally_block.split("if (queued) {", 1)[1]
    assert deferred.index("} else {") < deferred.index("loadActivityInsights(")
    assert deferred.index("} else {") < deferred.index("scheduleStatsWarm(")


def test_manual_refresh_reaches_the_server_cache():
    source = INDEX_HTML.read_text(encoding="utf-8")
    loader = _extract_js_function(source, "async function loadOverviewActiveTime(customDays, dateFrom, dateTo, options = {}) {")

    assert "const refreshParam = force ? '&refresh=1' : '';" in loader
    assert "${reviewParam}${refreshParam}" in loader


def test_an_unchanged_range_still_refetches():
    """The 5-minute auto-refresh must move this card too, not freeze it."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    loader = _extract_js_function(source, "async function loadOverviewActiveTime(customDays, dateFrom, dateTo, options = {}) {")
    before_request = loader.split("try {", 1)[0]

    assert "return;" not in before_request


def test_the_request_key_identifies_the_servers():
    """Selections of equal size must not collide on "[object Object]"."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    key_fn = _extract_js_function(source, "function activeTimeRequestKey(customDays, dateFrom, dateTo) {")

    assert "server.id" in key_fn
    assert ".sort()" in key_fn
    assert "selectedServers().join" not in key_fn


def test_active_time_overview_labels_exist_in_both_languages():
    source = INDEX_HTML.read_text(encoding="utf-8")
    for key in ("agentTime", "agentTimeCard", "agentTimeOverviewHint", "activeTimeToolsUnavailable", "idleCap"):
        assert len(re.findall(rf"^\s+{key}: ", source, re.MULTILINE)) == 2, key
    # The wording the card no longer uses is gone rather than left to rot.
    assert "activeTimeOverviewMultiServerHint" not in source


def test_the_card_shows_agent_time_and_its_delta():
    """Agent time is additive, so several servers need no relabelling."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    renderer = _extract_js_function(source, "function renderOverviewActiveTime() {")

    assert "formatDuration(data.active_ms_sum)" in renderer
    assert "formatDuration(data.active_ms)" not in renderer, "the union figure is no longer shown"
    assert "renderDelta('overviewActiveDelta', data.comparison?.active_ms_sum_pct ?? null)" in renderer
    # Nothing is left of the multi-server relabelling the union needed.
    assert "activeTimeCombined" not in renderer
    assert "_server_count" not in renderer


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_equal_sized_server_selections_get_different_keys(tmp_path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    harness = tmp_path / "active-time-key.js"
    harness.write_text(
        "let includeCodexReviewSessions = null;\n"
        "let SERVERS = [];\n"
        "function windowKeyFor() { return 'w'; }\n"
        "function selectedServers() { return SERVERS; }\n"
        + _extract_js_function(source, "function activeTimeRequestKey(customDays, dateFrom, dateTo) {")
        + "\nconst input = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify(input.map((selection) => {\n"
        + "  SERVERS = selection;\n"
        + "  return activeTimeRequestKey(null, '2026-01-01', '2026-01-02');\n"
        + "})));\n",
        encoding="utf-8",
    )
    selections = [
        [{"id": "a", "url": "http://a"}, {"id": "b", "url": "http://b"}],
        [{"id": "c", "url": "http://c"}, {"id": "d", "url": "http://d"}],
        [{"id": "b", "url": "http://b"}, {"id": "a", "url": "http://a"}],
    ]
    first, other, reordered = json.loads(
        subprocess.run(
            ["node", str(harness), json.dumps(selections)],
            check=True,
            capture_output=True,
            encoding="utf-8",
        ).stdout
    )

    assert "[object Object]" not in first
    assert first != other  # same size, different servers
    assert first == reordered  # selection order is not part of the identity


def _combine(tmp_path, payloads):
    source = INDEX_HTML.read_text(encoding="utf-8")
    harness = tmp_path / "active-time.js"
    harness.write_text(
        _extract_js_function(source, "function pctChange(current, previous) {")
        + "\n"
        + _extract_js_function(source, "function combineActiveTimePayloads(payloads) {")
        + "\nconst input = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify(combineActiveTimePayloads(input)));\n",
        encoding="utf-8",
    )
    return json.loads(
        subprocess.run(
            ["node", str(harness), json.dumps(payloads)],
            check=True,
            capture_output=True,
            encoding="utf-8",
        ).stdout
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_server_payloads_are_added_up(tmp_path):
    payloads = [
        {"active_ms": 100, "active_ms_sum": 150, "active_gap_cap_ms": 300_000, "unavailable_tools": ["mimo"]},
        {"active_ms": 60, "active_ms_sum": 120, "active_gap_cap_ms": 600_000, "unavailable_tools": ["mimo", "kimi"]},
    ]
    result = _combine(tmp_path, payloads)

    assert result["active_ms"] == 160
    assert result["active_ms_sum"] == 270
    assert result["active_gap_cap_ms"] == 600_000
    assert result["unavailable_tools"] == ["mimo", "kimi"]
    assert result["_server_count"] == 2
    assert result["comparison"] is None, "no server reported one"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_delta_is_recomputed_from_the_combined_totals(tmp_path):
    """Averaging the servers' percentages would weight a quiet one like a busy one."""
    payloads = [
        {
            "active_ms": 100,
            "active_ms_sum": 150,
            "unavailable_tools": [],
            # +50% on its own
            "comparison": {"active_ms_prev": 50, "active_ms_sum_prev": 100, "active_ms_pct": 100.0, "active_ms_sum_pct": 50.0},
        },
        {
            "active_ms": 60,
            "active_ms_sum": 50,
            "unavailable_tools": [],
            # -50% on its own
            "comparison": {"active_ms_prev": 50, "active_ms_sum_prev": 100, "active_ms_pct": 20.0, "active_ms_sum_pct": -50.0},
        },
    ]
    result = _combine(tmp_path, payloads)

    assert result["comparison"]["active_ms_sum_prev"] == 200
    assert result["comparison"]["active_ms_sum_pct"] == 0.0, "200 -> 200 is no change, not the mean of +50 and -50"
    assert result["comparison"]["active_ms_prev"] == 100
    assert result["comparison"]["active_ms_pct"] == 60.0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_zero_previous_window_has_no_percentage(tmp_path):
    """First day of use: the card shows a runtime and no delta, not an infinite one."""
    payloads = [
        {
            "active_ms": 100,
            "active_ms_sum": 150,
            "unavailable_tools": [],
            "comparison": {"active_ms_prev": 0, "active_ms_sum_prev": 0, "active_ms_pct": None, "active_ms_sum_pct": None},
        }
    ]
    result = _combine(tmp_path, payloads)

    assert result["comparison"]["active_ms_sum_pct"] is None
    assert result["comparison"]["active_ms_pct"] is None
