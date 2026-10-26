"""The Overview's active-time KPI: one clock across every session tool.

The aggregate and its route are tested here directly. The card's renderer and
the browser-side merge of several servers' payloads run under node below; the
card's loader, which is driven by the range queue, runs in
test_dashboard_queue_frontend.py.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from tokdash import sessions
from tokdash.sessions import get_active_time_data

from support.frontend import extract_js_function, index_source, needs_node, run_node


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


def test_all_time_agent_headline_avoids_windows_pre_epoch_timestamp_conversion(fake_tools, monkeypatch):
    class WindowsDateTime(datetime):
        @classmethod
        def fromtimestamp(cls, stamp, tz=None):
            if stamp < 0:
                raise OSError("Windows cannot convert a pre-epoch timestamp")
            return super().fromtimestamp(stamp, tz)

    monkeypatch.setattr(sessions, "datetime", WindowsDateTime)
    fake_tools["codex"] = {"c1": _raw("codex", "c1", [0, MINUTE])}
    data = get_active_time_data("all")
    assert data["active_ms_sum"] == 60_000
    assert data["sparkline"] is None


def test_hourly_agent_buckets_use_the_same_additive_intervals(fake_tools):
    start = 119 * MINUTE
    fake_tools["codex"] = {"c1": _raw("codex", "c1", [start, start + 2 * MINUTE])}
    fake_tools["claude"] = {"a1": _raw("claude", "a1", [start, start + 2 * MINUTE])}
    data = get_active_time_data("today", "2026-01-01", "2026-01-01")
    assert data["active_ms"] == 120_000
    assert data["active_ms_sum"] == 240_000
    assert data["sparkline"]["granularity"] == "hour"
    assert [row["agent_ms"] for row in data["sparkline"]["buckets"]] == [120_000, 120_000]
    assert sum(row["agent_ms"] for row in data["sparkline"]["buckets"]) == data["active_ms_sum"]


def test_daily_agent_buckets_follow_review_session_selection(fake_tools):
    fake_tools["codex"] = {
        "main": _raw("codex", "main", [0, MINUTE]),
        "review": _raw("codex", "review", [0, 2 * MINUTE], is_review_session=True),
    }
    for include, expected in ((True, 180_000), (False, 60_000)):
        data = get_active_time_data("week", "2026-01-01", "2026-01-07", include_review_sessions=include)
        assert data["sparkline"]["granularity"] == "day"
        assert sum(row["agent_ms"] for row in data["sparkline"]["buckets"]) == data["active_ms_sum"] == expected


def test_yearly_agent_buckets_follow_review_session_selection(fake_tools):
    fake_tools["codex"] = {
        "main": _raw("codex", "main", [0, MINUTE]),
        "review": _raw("codex", "review", [0, 2 * MINUTE], is_review_session=True),
    }
    for include, expected in ((True, 180_000), (False, 60_000)):
        data = get_active_time_data("year", "2026-01-01", "2026-12-31", include_review_sessions=include)
        assert data["sparkline"] == {"granularity": "month", "buckets": [{"key": "2026-01", "agent_ms": expected}]}
        assert data["active_ms_sum"] == expected


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


def test_the_card_sits_after_total_messages_in_a_six_column_row():
    kpis = index_source().split('<!-- KPI cards -->', 1)[1].split("</section>", 1)[0]

    assert "lg:grid-cols-6" in kpis
    assert len(re.findall(r'<div class="surface p-5">', kpis)) == 6
    assert kpis.index('id="messagesDelta"') < kpis.index('id="overviewActiveTime"')
    assert kpis.index('id="overviewActiveTime"') < kpis.index('data-i18n="avgCacheHitRate"')
    # A delta line like the cards beside it. renderDelta takes its id as an
    # argument, so the generic id check cannot see this one.
    assert 'id="overviewActiveDelta"' in kpis
    # A one-line label, like every other card in the row.
    assert 'data-i18n="agentTime"' in kpis
    assert 'data-i18n-title="agentTimeOverviewHint"' in kpis


def test_a_language_switch_repaints_the_card():
    """The card's loading label and its unavailable-tools line are written by script.

    applyI18n retranslates only the markup's data-i18n labels, so it has to call
    the card's renderer for the rest. Nothing runs applyI18n under test yet, so
    this one call is checked in its source. Every other path that repaints the
    card runs for real: renderOverviewTab in
    test_overview_empty_states_frontend.py::test_the_kpi_readouts_follow_the_range,
    and the loader, the range queue and the Codex review toggle in
    test_dashboard_queue_frontend.py.
    """
    i18n = extract_js_function(index_source(), "function applyI18n() {")

    assert "renderOverviewActiveTime();" in i18n


@needs_node
def test_the_card_says_loading_rather_than_answering_with_a_dash(tmp_path):
    """The card's own request runs behind /api/usage, so it answers a beat later.

    A dash in that gap reads as "no agent time in this range" — an answer the card
    has not got yet. It shares the shimmer the rest of the tab's deferred surfaces
    use, and keeps the dash for the states that really are an answer.

    Once the figure lands the card shows agent time, the additive one, with that
    figure's own delta. Summing agent time across servers is the same quantity, so
    a two-server payload is printed as it is, with no caveat of its own.
    """
    source = index_source()
    harness = tmp_path / "active-time-render.js"
    harness.write_text(
        """
const nodes = {};
function node(id) {
  if (!nodes[id]) nodes[id] = {
    id, textContent: '', innerHTML: '', title: '', style: {}, attrs: {}, classes: new Set(),
    children: [],
    classList: {
      add: (c) => nodes[id].classes.add(c),
      remove: (c) => nodes[id].classes.delete(c),
      contains: (c) => nodes[id].classes.has(c),
    },
    setAttribute(name, value) { nodes[id].attrs[name] = value; },
    removeAttribute(name) { delete nodes[id].attrs[name]; },
    appendChild(child) { nodes[id].children.push(child); return child; },
    append(...kids) { nodes[id].children.push(...kids); },
  };
  return nodes[id];
}
const document = {
  getElementById: node,
  createElement: () => node('__created_' + Math.random().toString(36).slice(2)),
};
const overviewActiveTimeState = { status: 'idle', data: null, key: null, requestId: 0 };
function t(key) { return key; }
const deltas = [];
function renderDelta(id, pct) { deltas.push([id, pct]); }
function formatDuration(ms) { return `dur:${ms}`; }
function formatToolName(name) { return name; }
function fitKpiValue() {}
"""
        # The card's exact-value readout is written by the shared helper all six
        # KPI cards use, with its own unrounded-duration formatter behind it.
        + extract_js_function(
            source,
            "function setKpiExactReadout(valueId, tooltipId, text, note = '') {",
        )
        + "\n"
        + extract_js_function(source, "function formatExactDuration(ms) {")
        + "\n"
        + extract_js_function(source, "function renderOverviewActiveTime() {")
        + """
const out = {};
const snapshot = () => ({
  text: node('overviewActiveTime').textContent,
  html: node('overviewActiveTime').innerHTML,
  shimmer: node('overviewActiveTime').classes.has('tokdash-loading-placeholder'),
  meta: node('overviewActiveMeta').textContent,
  delta: deltas.at(-1) || null,
});
for (const status of ['loading', 'error', 'idle']) {
  overviewActiveTimeState.status = status;
  overviewActiveTimeState.data = null;
  renderOverviewActiveTime();
  out[status] = snapshot();
}
overviewActiveTimeState.status = 'ready';
// Two servers' payloads merged: the clock (active_ms) and the agent time differ,
// and so do their deltas.
overviewActiveTimeState.data = {
  active_ms: 7, active_ms_sum: 42, unavailable_tools: [], _server_count: 2,
  comparison: { active_ms_pct: 99, active_ms_sum_pct: 12.5 },
};
renderOverviewActiveTime();
out.ready = snapshot();
process.stdout.write(JSON.stringify(out));
""",
        encoding="utf-8",
    )
    out = json.loads(run_node(harness))

    assert out["loading"]["shimmer"] is True, "a pending request must not read as a dash"
    assert "tokdash-loading-label" in out["loading"]["html"]
    assert 'data-i18n="loading"' in out["loading"]["html"], "and must survive a language switch"

    # The states that really are an answer keep the dash.
    assert out["error"]["shimmer"] is False
    assert out["error"]["text"] == "\u2014"
    assert out["error"]["meta"] == "loadFailed"
    assert out["idle"]["shimmer"] is False
    assert out["idle"]["text"] == "\u2014"
    assert out["idle"]["delta"] == ["overviewActiveDelta", None], "no figure, no delta"

    # And the shimmer is dropped once the figure lands, not left under the value.
    assert out["ready"]["shimmer"] is False
    assert out["ready"]["text"] == "dur:42", "the card shows agent time, not the clock"
    assert out["ready"]["delta"] == ["overviewActiveDelta", 12.5], "and agent time's own delta"
    assert out["ready"]["meta"] == "", "two servers' agent time needs no relabelling"


@needs_node
def test_equal_sized_server_selections_get_different_keys(tmp_path):
    source = index_source()
    harness = tmp_path / "active-time-key.js"
    harness.write_text(
        "let includeCodexReviewSessions = null;\n"
        "let SERVERS = [];\n"
        "function windowKeyFor() { return 'w'; }\n"
        "function selectedServers() { return SERVERS; }\n"
        + extract_js_function(source, "function activeTimeRequestKey(customDays, dateFrom, dateTo) {")
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
    first, other, reordered = json.loads(run_node(harness, json.dumps(selections)))

    assert "[object Object]" not in first
    assert first != other  # same size, different servers
    assert first == reordered  # selection order is not part of the identity


def _combine(tmp_path, payloads):
    source = index_source()
    harness = tmp_path / "active-time.js"
    harness.write_text(
        extract_js_function(source, "function pctChange(current, previous) {")
        + "\n"
        + extract_js_function(source, "function combineActiveTimePayloads(payloads) {")
        + "\nconst input = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify(combineActiveTimePayloads(input)));\n",
        encoding="utf-8",
    )
    return json.loads(run_node(harness, json.dumps(payloads)))


@needs_node
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


@needs_node
def test_the_per_tool_rows_are_added_up_the_same_way(tmp_path):
    """The tools table ranks by these rows, so a second server has to add to a
    tool's session count and agent time rather than replace them."""
    payloads = [
        {
            "active_ms": 100,
            "active_ms_sum": 150,
            "by_tool": {
                "codex": {"tool_label": "Codex", "session_count": 3, "active_ms_sum": 90},
                "claude": {"tool_label": "Claude", "session_count": 1, "active_ms_sum": 60},
            },
        },
        {
            "active_ms": 60,
            "active_ms_sum": 30,
            "by_tool": {"codex": {"tool_label": "Codex", "session_count": 2, "active_ms_sum": 30}},
        },
        None,
    ]
    result = _combine(tmp_path, payloads)

    assert result["by_tool"]["codex"] == {
        "tool_label": "Codex",
        "session_count": 5,
        "active_ms_sum": 120,
    }
    assert result["by_tool"]["claude"] == {
        "tool_label": "Claude",
        "session_count": 1,
        "active_ms_sum": 60,
    }, "a tool only one server reported keeps its own row"
    assert result["active_ms_sum"] == 180, "the headline still adds every tool"


@needs_node
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


@needs_node
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
