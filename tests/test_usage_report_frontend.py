"""Tests for the Report tab (usage report) in the single-file dashboard.

The tab owns its own calendar-aligned window and renders from three endpoints,
so these pin the parts that fail silently: a tab nobody can open, a window off
by one day (the printed range line is the only part of a report a reader can
check by hand), a day-map rule that quietly stops matching the Stats tab, and
model-derived text reaching the DOM as markup.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import date, timedelta
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

I18N_LANGS = ("en", "zh", "ja", "ko", "es", "pt")

# Kana and Han, for the copy rules that only apply where words carry no spaces.
CJK = r"[぀-ヿ一-鿿]"

# Every line that makes a claim about one machine: whose activity was recorded,
# whose midnight starts a day, whose tokdash counted, whose other tools these
# are. All of them take the selected server's name rather than assuming local.
MACHINE_KEYS = (
    "usageReportScope",
    "usageReportOtherTools",
    "usageReportFooterDays",
    "usageReportFooterSrc",
    "usageReportCardRange",
)

BUILD_MODEL_SIGNATURE = (
    "function usageReportBuildModel({ insights, usage, activeTime, period, dateFrom,"
    " dateTo, insightsMissing, usageMissing, activeTimeMissing, pending = false }) {"
)

HELPERS = (
    "function formatDateKey(date) {",
    "function parseDateKey(value) {",
    "function startOfWeekMonday(date) {",
    "function usageReportToday() {",
    "function usageReportWindows(period) {",
    "function usageReportPeriodLength(period, fromKey) {",
    "function usageReportDayKeys(fromKey, toKey) {",
    "function usageReportCalendar(period, fromKey, toKey) {",
)


def _source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _report_block(source: str) -> str:
    """The Report tab's own JS, from its first constant to the tab activator."""
    start = source.index("const USAGE_REPORT_FACETS")
    end = source.index("    function activateDashboardTab(tab) {", start)
    return source[start:end]


def _extract_js_function(source: str, signature: str) -> str:
    start = source.find(signature)
    assert start != -1, f"{signature} not found in index.html"
    body_start = start + len(signature) - 1
    assert source[body_start] == "{", f"{signature} must end at the function body"
    depth = 0
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


def _run_node(tmp_path: Path, name: str, body: str, *argv: str) -> str:
    """Run a harness under node and read its stdout as UTF-8.

    The encoding is not optional: node writes UTF-8 whatever the platform, so
    decoding with the locale default turns every em dash and middot this tab
    prints into a replacement character on a Windows runner.
    """
    harness = tmp_path / name
    harness.write_text(body, encoding="utf-8")
    return subprocess.run(
        ["node", str(harness), *argv],
        capture_output=True,
        check=True,
        encoding="utf-8",
    ).stdout


# --- wiring -----------------------------------------------------------------


def test_report_tab_is_registered_between_stats_and_quota() -> None:
    source = _source()
    nav = source[source.index('<nav class="mt-5 flex flex-wrap gap-2"') :]
    tabs = re.findall(r'<button class="tab-btn" data-tab="([^"]+)"', nav)
    assert "report" in tabs
    assert tabs.index("report") == tabs.index("stats") + 1
    assert 'data-i18n="usageReport"' in nav


def test_report_tab_has_a_content_panel_and_one_lazy_load_branch() -> None:
    source = _source()
    assert '<div id="report-content" class="tab-content">' in source
    activate = source[
        source.index("function activateDashboardTab(tab) {") :
        source.index("function openOverviewProfile() {")
    ]
    assert "if (tab === 'report' && !usageReportState.loaded) loadUsageReport();" in activate
    # Loaded once and not re-fetched on every switch, like the pricing tab: the
    # period switcher, the server picker and Retry are what ask the server again.
    assert activate.count("loadUsageReport()") == 1


def test_report_panels_are_painted_from_the_theme_token() -> None:
    """The tab uses its own panel class rather than `.surface`, because the hover
    lift would move text under the cursor on a panel you are reading. That opts
    it out of every per-theme `.surface` override too, so the fill has to come
    from `--color-surface-glass` — a literal would leave the report the only
    slate-blue stack on seventeen themes.
    """
    source = _source()
    panel = next(
        line for line in source.splitlines() if line.strip().startswith(".usage-report-panel {")
    )
    assert "--color-surface-glass" in panel, panel
    assert "rgba(" not in panel, f"a literal fill does not follow the theme: {panel}"
    # No dark-mode twin either: the token already carries each theme's dark value.
    assert "html.dark .usage-report-panel" not in source


def test_report_panel_ids_are_unique() -> None:
    ids = re.findall(r'id="(usageReport[A-Za-z0-9]*)"', _source())
    assert ids
    duplicates = sorted({key for key in ids if ids.count(key) > 1})
    assert not duplicates, f"duplicate element ids: {duplicates}"


def test_report_renders_on_a_language_switch() -> None:
    """Its sentences are built imperatively, so data-i18n cannot carry them."""
    source = _source()
    apply_i18n = source[
        source.index("function applyI18n() {") : source.index("function isCustomRange(")
    ] if "function isCustomRange(" in source else source[
        source.index("function applyI18n() {") : source.index("function applyI18n() {") + 6000
    ]
    assert "usageReportRender()" in apply_i18n


# --- data access ------------------------------------------------------------


def test_report_fetches_three_endpoints_with_explicit_dates() -> None:
    block = _report_block(_source())
    for path in ("/api/insights", "/api/usage", "/api/active-time"):
        assert path in block, path
    assert "date_from=" in block and "date_to=" in block
    assert "facets=${USAGE_REPORT_FACETS}" in block


def test_report_never_asks_for_a_trailing_window_alias() -> None:
    """Spec 3: `period=week` is a trailing seven days, not this calendar week.

    The report labels its window with calendar words, so an alias here would
    put those words around a rolling window.
    """
    assert "period=" not in _report_block(_source())


def test_day_map_reuses_the_dashboard_quantile_rule_not_the_served_intensity() -> None:
    """Spec D3: one shading rule for the product, and it is the Stats tab's."""
    block = _report_block(_source())
    assert "quantileThresholds(" in block
    assert "quantileLevel(" in block
    assert "heatColor(" in block
    assert "intensity" not in block


def test_report_renders_data_text_without_inner_html() -> None:
    """Tool, model and project names are data; they never travel as markup."""
    assert "innerHTML" not in _report_block(_source())


def test_report_names_every_degradation_state() -> None:
    block = _report_block(_source())
    for marker in (
        "usageReportUnreachable",  # neither totals source answered
        "usageReportFacetsMissing",  # server predates the facets
        "usageReportTotalsFallback",  # /api/usage missing, the analytics total stands in
        "usageReportInsightsMissing",  # /api/insights missing
        "usageReportActiveMissing",  # /api/active-time missing
        "usageReportPartial",  # source_errors present
        "usageReportActiveUnavailable",  # active-time served, but not for these tools
        "usageReportEmptyPeriod",  # no rows in the window
    ):
        assert marker in block, marker


def test_every_notice_the_model_records_reaches_the_banner() -> None:
    """A flag the model sets and the banner never reads is a silent zero.

    Each of the three requests can fail on its own, and the banner is the only
    surface that says which one did.
    """
    block = _report_block(_source())
    model = _extract_js_function(block, BUILD_MODEL_SIGNATURE)
    banner = _extract_js_function(block, "function usageReportRenderBanner(model) {")
    notices = set(re.findall(r"\b(\w+Missing),?$", model[model.index("notices: {"):], re.M))
    assert notices == {"insightsMissing", "usageMissing", "activeTimeMissing"}
    for notice in notices:
        assert f"model.notices.{notice}" in banner, notice


def test_report_counts_every_codex_session_whatever_overview_shows() -> None:
    """The scope line claims all agent activity on the machine, so the report
    pins the review-session filter open instead of inheriting the server default
    or the Overview toggle -- either would make the two tabs disagree silently."""
    block = _report_block(_source())
    loader = _extract_js_function(block, "async function loadUsageReport(options = {}) {")
    assert "/api/active-time?include_review_sessions=true" in loader
    assert "includeCodexReviewSessions" not in loader, "the Overview toggle is not this tab's input"


def test_report_is_single_server_and_prints_which_one() -> None:
    """Spec D1: facets have no merge semantics, so no merged report is offered."""
    block = _report_block(_source())
    assert "usageReportServers()" in block
    assert "fetchSelectedServers" not in block


# --- window math, executed for real under node ------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize(
    ("today", "period", "expected", "calendar"),
    [
        # Mid-week: the week runs back to Monday, the month to the 1st.
        (
            "2026-09-03",
            "week",
            {"from": "2026-08-31", "to": "2026-09-03", "length": 7, "elapsed": 4},
            {"first": "2026-08-31", "last": "2026-09-06"},
        ),
        (
            "2026-09-03",
            "month",
            {"from": "2026-09-01", "to": "2026-09-03", "length": 30, "elapsed": 3},
            {"first": "2026-09-01", "last": "2026-09-30"},
        ),
        (
            "2026-09-03",
            "year",
            {"from": "2026-01-01", "to": "2026-09-03", "length": 365, "elapsed": 246},
            {"first": "2026-01-01", "last": "2026-12-31"},
        ),
        # Sunday belongs to the week it ends, not the week it opens.
        (
            "2026-09-06",
            "week",
            {"from": "2026-08-31", "to": "2026-09-06", "length": 7, "elapsed": 7},
            {"first": "2026-08-31", "last": "2026-09-06"},
        ),
        # A month on its first day spans one day but still draws the whole month.
        (
            "2026-03-01",
            "month",
            {"from": "2026-03-01", "to": "2026-03-01", "length": 31, "elapsed": 1},
            {"first": "2026-03-01", "last": "2026-03-31"},
        ),
        # Leap day: February has 29 days and the year has 366.
        (
            "2028-02-29",
            "month",
            {"from": "2028-02-01", "to": "2028-02-29", "length": 29, "elapsed": 29},
            {"first": "2028-02-01", "last": "2028-02-29"},
        ),
        (
            "2028-02-29",
            "year",
            {"from": "2028-01-01", "to": "2028-02-29", "length": 366, "elapsed": 60},
            {"first": "2028-01-01", "last": "2028-12-31"},
        ),
        # The century year that is not a leap year.
        (
            "2100-02-28",
            "year",
            {"from": "2100-01-01", "to": "2100-02-28", "length": 365, "elapsed": 59},
            {"first": "2100-01-01", "last": "2100-12-31"},
        ),
    ],
)
def test_report_windows_are_calendar_aligned(
    tmp_path: Path, today: str, period: str, expected: dict, calendar: dict
) -> None:
    source = _source()
    body = "\n".join(_extract_js_function(source, signature) for signature in HELPERS)
    body += f"""
usageReportToday = () => new Date('{today}T12:00:00');
const periodArg = process.argv[2];
const windows = usageReportWindows(periodArg);
const calendar = usageReportCalendar(periodArg, windows.date_from, windows.date_to);
const periodLength = usageReportPeriodLength(process.argv[2], windows.date_from);
process.stdout.write(JSON.stringify({{
  from: windows.date_from,
  to: windows.date_to,
  length: periodLength,
  elapsed: usageReportDayKeys(windows.date_from, windows.date_to).length,
  calendarFirst: calendar[0],
  calendarLast: calendar[calendar.length - 1],
}}));
"""
    result = json.loads(_run_node(tmp_path, "usage-report-windows.js", body, period))
    assert result == {
        "from": expected["from"],
        "to": expected["to"],
        "length": expected["length"],
        "elapsed": expected["elapsed"],
        "calendarFirst": calendar["first"],
        "calendarLast": calendar["last"],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_an_elapsed_window_never_exceeds_the_period_it_claims(tmp_path: Path) -> None:
    """The in-progress note prints `{d} of {n} days`; both have to be true."""
    source = _source()
    body = "\n".join(_extract_js_function(source, signature) for signature in HELPERS)
    body += """
usageReportToday = () => new Date(process.argv[2] + 'T12:00:00');
const out = [];
for (const period of ['week', 'month', 'year']) {
  const windows = usageReportWindows(period);
  out.push({
    period,
    elapsed: usageReportDayKeys(windows.date_from, windows.date_to).length,
    length: usageReportPeriodLength(period, windows.date_from),
  });
}
process.stdout.write(JSON.stringify(out));
"""
    rows = json.loads(
        _run_node(tmp_path, "usage-report-span.js", body, "2026-09-03")
    )
    assert {row["period"]: row for row in rows} == {
        "week": {"period": "week", "elapsed": 4, "length": 7},
        "month": {"period": "month", "elapsed": 3, "length": 30},
        "year": {"period": "year", "elapsed": 246, "length": 365},
    }


# --- one source down, executed for real under node --------------------------


MODEL_SIGNATURES = HELPERS + (
    "function usageReportProjectShares(facetTotal, rowSum, unattributedTokens) {",
    "function quantileThresholds(values, levels) {",
    "function quantileLevel(value, thresholds) {",
    BUILD_MODEL_SIGNATURE,
)

MODEL_FIXTURE = """
const HEAT_COLORS = new Array(8).fill('#000');
const INSIGHTS = {
  range: { from: '2026-09-01', to: '2026-09-03' },
  totals: { tokens: 900, messages: 90, cost: 9 },
  daily: [{ date: '2026-09-01', tokens: 300, messages: 30 }],
  streaks: { active_days: 2, current_streak: 2, longest_streak: 9 },
  firsts: { busiest_day: '2026-09-01', busiest_day_tokens: 300 },
  tools: { ranked: [{ tool: 'codex', tokens: 600, cost: 6 }, { tool: 'claude', tokens: 300, cost: 3 }] },
  models: { ranked: [] },
};
const USAGE = { total_tokens: 1000, total_messages: 100, total_cost: 10, by_tool: {}, apps: {} };
const ACTIVE = {
  active_ms: 5000,
  active_ms_sum: 7000,
  by_tool: { codex: { session_count: 4, active_ms_sum: 3000 } },
};
const build = (over) => usageReportBuildModel({
  insights: INSIGHTS, usage: USAGE, activeTime: ACTIVE,
  period: 'month', dateFrom: '2026-09-01', dateTo: '2026-09-03',
  insightsMissing: false, usageMissing: false, activeTimeMissing: false,
  ...over,
});
const shape = (model) => ({
  sessions: model.sessions,
  activeMs: model.activeMs,
  primary: model.agentPrimary.map((row) => row.tool),
  others: model.agentOthers.map((row) => row.tool),
  primarySessions: model.agentPrimary.map((row) => row.sessions),
  hasStreaks: !!model.streaks,
  notices: model.notices,
});
process.stdout.write(JSON.stringify({
  whole: shape(build({})),
  noActiveTime: shape(build({ activeTime: { __error: 'boom' }, activeTimeMissing: true })),
  noInsights: shape(build({ insights: { __error: 'boom' }, insightsMissing: true })),
  // The first paint: /api/usage has landed and the other two have not been
  // issued, so the model is built over two literal nulls.
  firstPaint: shape(build({
    insights: null, activeTime: null,
    insightsMissing: true, activeTimeMissing: true, pending: true,
  })),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_failed_active_time_leaves_no_count_rather_than_a_zero(tmp_path: Path) -> None:
    """`/api/active-time` is one of three requests and can fail on its own.

    Summing an absent `by_tool` map yields 0, which the report would then print
    as "you drove 0 sessions" for a month somebody worked. It also used to empty
    the agent table: every row lost its session count, and the rule that parks
    session-less tools in the disclosure row then parked all of them.
    """
    source = _source()
    body = "\n".join(_extract_js_function(source, sig) for sig in MODEL_SIGNATURES) + MODEL_FIXTURE
    report = json.loads(_run_node(tmp_path, "usage-report-model.js", body))

    whole = report["whole"]
    assert whole["sessions"] == 4
    assert whole["primary"] == ["codex"], "a tool with no recorded session waits in the disclosure"
    assert whole["others"] == ["claude"]

    down = report["noActiveTime"]
    assert down["sessions"] is None, "an absent source has no session count, not a count of zero"
    assert down["activeMs"] == 0
    assert down["notices"]["activeTimeMissing"] is True
    assert down["primary"] == ["codex", "claude"], "the ranking still stands on its token columns"
    assert down["primarySessions"] == [None, None]
    assert down["others"] == []

    # The other half of the same class: no streaks facet is no active-day count.
    assert report["noInsights"]["hasStreaks"] is False
    assert report["noInsights"]["sessions"] == 4


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_first_paint_builds_a_model_over_two_absent_sources(tmp_path: Path) -> None:
    """The hero paints off /api/usage before the other two have been issued.

    Every read of `insights` and `activeTime` in the model is guarded by its
    Missing flag, so the first paint can pass literal nulls. Running it is the
    point: an unguarded read is a TypeError that leaves the tab on its spinner
    forever, which no amount of reading the source proves absent.
    """
    source = _source()
    body = "\n".join(_extract_js_function(source, sig) for sig in MODEL_SIGNATURES) + MODEL_FIXTURE
    first = json.loads(_run_node(tmp_path, "usage-report-model.js", body))["firstPaint"]

    assert first["notices"]["pending"] is True, "the banner needs silence told from refusal"
    assert first["sessions"] is None, "no active time yet is not a count of zero"
    assert first["hasStreaks"] is False
    assert first["activeMs"] == 0


def test_the_server_warms_the_exact_facet_string_the_tab_asks_for() -> None:
    """The facet string is part of the insights cache key, character for character.

    These cannot be one constant -- one is Python and one is JavaScript -- so a
    reordered or extended set on either side warms a key the tab never asks for.
    Nothing else would notice: the warm still runs, every test that builds keys
    from the Python constant still passes, and the tab just quietly goes back to
    paying the cold scan it used to.
    """
    from tokdash import api

    block = _report_block(_source())
    match = re.search(r"const USAGE_REPORT_FACETS = '([^']+)';", block)
    assert match, "the tab's facet constant moved or changed quoting"
    assert match.group(1) == api.REPORT_FACETS


LOADER_HARNESS = """
const usageReportState = {
  loading: false, loaded: false, error: null, model: null,
  period: 'month', serverId: 'local', staleReread: false,
};
const calls = [];
let responder = () => null;
function usageReportWire() {}
function usageReportRenderControls() {}
function usageReportRenderLoading() { calls.push('renderLoading'); }
function usageReportServer() { return { id: 'local', label: 'local' }; }
function usageReportLoadVersion() {}
function usageReportWindows() { return { date_from: '2026-09-01', date_to: '2026-09-05' }; }
function usageReportBuildModel(args) { return { notices: {}, args }; }
function usageReportRenderPartial() { calls.push('partial'); }
function usageReportRender() { calls.push('render'); }
function usageReportScheduleStaleReread() {}
function usageReportApplyPendingChoice() {}
function fetchJsonWithRetry() { return Promise.resolve(responder()); }
const USAGE_REPORT_FACETS = 'daily';
__LOADER__
(async () => {
  const out = {};
  // An OK 200 whose body is not JSON: fetchJson resolves to null.
  responder = () => null;
  await loadUsageReport();
  out.afterNull = { loading: usageReportState.loading, loaded: usageReportState.loaded };

  // The tab must still accept a later load. Before the guard existed this second
  // call was refused outright and the tab was dead until a page reload.
  calls.length = 0;
  responder = () => ({ total_tokens: 5 });
  await loadUsageReport();
  out.recovered = calls.includes('render');
  out.loadingAtRest = usageReportState.loading;
  process.stdout.write(JSON.stringify(out));
})();
"""


WINDOWS_HARNESS = """
let CURSOR = null;
function usageReportToday() { return CURSOR; }
__HELPERS__
const out = JSON.parse(process.argv[2]).map((iso) => {
  const [y, m, d] = iso.split('-').map(Number);
  CURSOR = new Date(y, m - 1, d);
  return ['week', 'month', 'year'].map((period) => {
    const w = usageReportWindows(period);
    return [w.date_from, w.date_to];
  });
});
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_warmers_windows_match_the_tabs_own_across_years(tmp_path: Path) -> None:
    """The warmer builds its keys in Python; the tab builds the same windows in
    JavaScript. A mismatch of one day warms a key nobody asks for and the tab
    quietly stays cold -- with every Python-side test still green.

    Run against the real JS rather than hardcoded expectations, over enough days to
    cross two year boundaries and a leap day. The riskiest divergence is the week
    start: JavaScript's getDay() calls Sunday 0, Python's weekday() calls Monday 0.
    """
    from tokdash import api

    days = [date(2024, 1, 1) + timedelta(days=n) for n in range(900)]
    helpers = "\n".join(
        _extract_js_function(_source(), sig)
        for sig in (
            "function formatDateKey(date) {",
            "function startOfWeekMonday(date) {",
            "function usageReportWindows(period) {",
        )
    )
    body = WINDOWS_HARNESS.replace("__HELPERS__", helpers)
    js = json.loads(
        _run_node(tmp_path, "usage-report-windows.js", body,
                  json.dumps([day.isoformat() for day in days]))
    )

    mismatches = [
        (day.isoformat(), from_js, from_py)
        for day, from_js in zip(days, js)
        for from_js, from_py in [([tuple(w) for w in from_js], api._report_windows(day))]
        if from_js != from_py
    ]
    assert not mismatches, mismatches[:5]
    # Guard the comparison itself: a harness that silently produced nothing would
    # otherwise "pass" by having no mismatches to report.
    assert len(js) == len(days)
    assert js[0] == [["2024-01-01", "2024-01-01"]] * 3, "a Monday 1 January collapses all three"


SCHEDULE_HARNESS = """
const usageReportState = { staleTimer: null, loadToken: 3 };
let nextId = 1;
const armed = new Map();
const cleared = [];
setTimeout = (fn, ms) => { const id = nextId++; armed.set(id, { fn, ms }); return id; };
clearTimeout = (id) => { cleared.push(id); armed.delete(id); };
const reread = [];
function usageReportSilentReread(token, period, serverId) { reread.push([token, period, serverId]); }
const USAGE_REPORT_STALE_REREAD_MS = 12000;
__SERVED__
__SCHEDULE__
const fresh = { response_cache: { status: 'hit' } };
const stale = { response_cache: { status: 'stale' } };
const bare = {};                       // a route that serves no cache metadata
const server = { id: 'local' };
const out = {};

// Nothing stale: nothing armed.
usageReportScheduleStaleReread([fresh, fresh, fresh], 'month', server);
out.allFresh = armed.size;

// A route with no metadata must not be guessed at.
usageReportScheduleStaleReread([bare, bare, bare], 'month', server);
out.noMetadata = armed.size;

// Only the SLOW pair is stale -- usage is already fresh. This is the case that
// keying the decision on /api/usage alone used to miss entirely.
usageReportScheduleStaleReread([fresh, stale, fresh], 'month', server);
out.slowPairStale = armed.size;
const firstId = usageReportState.staleTimer;

// A second schedule must retire the first timer, not stack on it.
usageReportScheduleStaleReread([fresh, fresh, stale], 'month', server);
out.afterSecond = { armed: armed.size, clearedFirst: cleared.includes(firstId) };
out.delay = armed.get(usageReportState.staleTimer).ms;

// Firing it hands the silent path the token captured at schedule time.
armed.get(usageReportState.staleTimer).fn();
out.fired = reread;
out.handleCleared = usageReportState.staleTimer === null;
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_only_one_stale_reread_is_ever_armed(tmp_path: Path) -> None:
    """Scheduling is where the first attempt leaked timers.

    It gated on a boolean that every explicit load reset, so old timers stayed
    armed and a reader flipping periods could fire several re-reads for the same
    window -- multiplying request load exactly when the server is contended.
    Executed, because a source assertion cannot see a timer that was never cleared.
    """
    block = _report_block(_source())
    body = (
        SCHEDULE_HARNESS
        .replace("__SERVED__", _extract_js_function(
            block, "function usageReportServedStale(payload) {"))
        .replace("__SCHEDULE__", _extract_js_function(
            block, "function usageReportScheduleStaleReread(payloads, period, server) {"))
    )
    out = json.loads(_run_node(tmp_path, "usage-report-schedule.js", body))

    assert out["allFresh"] == 0, "nothing stale, nothing to re-read"
    assert out["noMetadata"] == 0, "a route that says nothing is treated as fresh"
    assert out["slowPairStale"] == 1, (
        "a stale insights/active-time must arm the re-read even when usage is fresh"
    )
    assert out["afterSecond"] == {"armed": 1, "clearedFirst": True}, "timers stacked"
    assert out["delay"] == 12000
    assert out["fired"] == [[3, "month", "local"]], "the token is captured at schedule time"
    assert out["handleCleared"] is True


REREAD_HARNESS = """
const usageReportState = {
  loading: false, loaded: true, error: null, model: { tag: 'original' },
  period: 'month', serverId: 'local', staleTimer: null, loadToken: 7,
};
let mode = 'ok';
let onRequest = null;
const renders = [];
function usageReportServer() { return { id: 'local' }; }
function usageReportWindows() { return { date_from: '2026-09-01', date_to: '2026-09-05' }; }
function usageReportBuildModel() { return { tag: 'reread' }; }
function usageReportRender() { renders.push(usageReportState.model.tag); }
const USAGE_REPORT_FACETS = 'daily';
function fetchJsonWithRetry() {
  if (onRequest) onRequest();
  if (mode === 'throw') return Promise.reject(new Error('network'));
  if (mode === 'null') return Promise.resolve(null);
  return Promise.resolve({ ok: true });
}
__REREAD__
__WANTS__
const snap = () => ({ model: usageReportState.model.tag, renders: renders.length, loading: usageReportState.loading });
(async () => {
  const out = {};
  mode = 'throw';
  await usageReportSilentReread(7, 'month', 'local');
  out.afterFailure = snap();

  mode = 'null';
  await usageReportSilentReread(7, 'month', 'local');
  out.afterNull = snap();

  // The reader switches period while the round trip is in flight.
  mode = 'ok';
  onRequest = () => { usageReportState.period = 'year'; };
  await usageReportSilentReread(7, 'month', 'local');
  out.afterPeriodSwitch = snap();
  onRequest = null;
  usageReportState.period = 'month';

  // An explicit load ran and bumped the token.
  await usageReportSilentReread(6, 'month', 'local');
  out.afterStaleToken = snap();

  // Nothing changed: it commits.
  await usageReportSilentReread(7, 'month', 'local');
  out.afterSuccess = snap();
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_silent_reread_never_damages_the_report_on_screen(tmp_path: Path) -> None:
    """The re-read is invisible, so every way it can go wrong is silent too.

    Executed rather than asserted against the source: the first attempt at this
    reused `loadUsageReport`, and every one of these cases passed its
    source-string tests while blanking the page, swallowing clicks or leaving a
    half-empty model in state for the share export to capture.
    """
    block = _report_block(_source())
    body = (
        REREAD_HARNESS
        .replace("__REREAD__", _extract_js_function(
            block, "async function usageReportSilentReread(token, period, serverId) {"))
        .replace("__WANTS__", _extract_js_function(
            block, "function usageReportStillWants(token, period, serverId) {"))
    )
    out = json.loads(_run_node(tmp_path, "usage-report-reread.js", body))

    for case in ("afterFailure", "afterNull", "afterPeriodSwitch", "afterStaleToken"):
        assert out[case]["model"] == "original", f"{case} overwrote a good report"
        assert out[case]["renders"] == 0, f"{case} repainted the page"

    assert out["afterSuccess"]["model"] == "reread", "a clean re-read must commit"
    assert out["afterSuccess"]["renders"] == 1

    # It must never take the loading flag: that is what swallowed period clicks and
    # made Retry a no-op during a refresh the reader could not see.
    for case in out.values():
        assert case["loading"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_an_empty_200_does_not_wedge_the_report_tab(tmp_path: Path) -> None:
    """`fetchJson` resolves to null when an OK response carries no JSON body.

    Sequencing moved the first dereference of that value inside the window where
    `loading` is true, so the TypeError left the flag set and the guard at the top
    of `loadUsageReport` then refused every later load -- period buttons, server
    switch and Retry alike -- until a page reload. Executed rather than asserted
    against the source, because the bug is control flow, not a string.
    """
    block = _report_block(_source())
    loader = _extract_js_function(block, "async function loadUsageReport(options = {}) {")
    body = LOADER_HARNESS.replace("__LOADER__", loader)
    out = json.loads(_run_node(tmp_path, "usage-report-loader.js", body))

    assert out["afterNull"]["loading"] is False, "an empty 200 left the tab wedged"
    assert out["afterNull"]["loaded"] is True
    assert out["recovered"] is True, "the tab refused every load after the first failure"
    assert out["loadingAtRest"] is False


def test_a_report_served_from_a_stale_entry_reads_again_once() -> None:
    """The morning's first visit is answered out of the 00:05 warm.

    Past its TTL the server hands the cached value over instantly AND recomputes
    it in the background, so the figures are hours old and nothing on the page
    says so. The tab reads again once when it sees that status -- and must not
    send refresh=1, which would start a second full recompute of the work the
    daemon is already doing.
    """
    block = _report_block(_source())
    schedule = _extract_js_function(
        block, "function usageReportScheduleStaleReread(payloads, period, server) {"
    )
    reread = _extract_js_function(
        block, "async function usageReportSilentReread(token, period, serverId) {"
    )
    loader = _extract_js_function(block, "async function loadUsageReport(options = {}) {")

    # Any of the three, not just the fastest one.
    assert "payloads.some(usageReportServedStale)" in schedule
    assert "usageReportScheduleStaleReread([usage, insights, activeTime], period, server)" in loader

    # The server is already recomputing these keys; forcing doubles the work.
    assert "refresh=1" not in reread
    assert "loadUsageReport" not in reread, (
        "the silent path must not reuse the loader: that one owns `loading`, the "
        "partial model and the blanking paint"
    )

    # A queued re-read describes the window an explicit load is replacing.
    assert "clearTimeout(usageReportState.staleTimer)" in schedule
    assert "clearTimeout(usageReportState.staleTimer)" in loader
    assert "usageReportState.loadToken += 1" in loader

    # Guarded on both sides of the round trip.
    assert reread.count("usageReportStillWants(token, period, serverId)") == 2
    # A failed re-read must leave the report on screen alone.
    assert "return;" in reread.split("catch (_error) {")[1]


def test_the_version_stamp_waits_for_the_full_paint() -> None:
    """/api/version is fired first and answers instantly, so it can land between
    the two paints. Stamping the partial model would render the footer and the
    share cards off a half-empty report -- the one thing the partial paint is
    careful not to do."""
    block = _report_block(_source())
    version = _extract_js_function(block, "function usageReportLoadVersion() {")
    # The whole statement, not the condition and the ordering: asserting those two
    # separately passes for `if (...notices.pending) { }`, which guards nothing.
    assert "if (usageReportState.model.notices.pending) return;" in version
    assert version.index("notices.pending") < version.index("usageReportRenderShare")


def test_a_source_not_asked_for_yet_is_not_reported_as_unavailable() -> None:
    """`pending` is the difference between "not here yet" and "refused".

    Without it the first paint flashes "analytics unavailable" a moment before
    the analytics land, which is a false claim about the server.
    """
    block = _report_block(_source())
    banner = _extract_js_function(block, "function usageReportRenderBanner(model) {")
    assert "const pending = !!model.notices.pending;" in banner
    assert "model.notices.insightsMissing && !pending" in banner
    assert "model.notices.activeTimeMissing && !pending" in banner
    # Usage is the request the first paint is made of: by then it has either
    # landed or failed, so its notice is never suppressed.
    assert "model.notices.usageMissing && !pending" not in banner


def test_the_report_paints_the_hero_before_asking_for_the_slow_two() -> None:
    """The three requests contend for the server's GIL instead of overlapping.

    Fired as one Promise.all, a 2s /api/usage became a 13s one and the paint
    waited on the slowest of the set either way. Usage alone fills the hero, so
    it is awaited and painted before the other two are issued.
    """
    block = _report_block(_source())
    loader = _extract_js_function(block, "async function loadUsageReport(options = {}) {")
    usage_at = loader.index("await settle(request('/api/usage'))")
    paint_at = loader.index("usageReportRenderPartial(")
    insights_at = loader.index("/api/insights?facets=")
    active_at = loader.index("/api/active-time?include_review_sessions=true")
    assert usage_at < paint_at < insights_at, "the hero must paint before the slow two start"
    assert usage_at < paint_at < active_at
    # The slow two still overlap each other -- sequencing them as well would add
    # their times rather than trading a little wall clock for a much earlier paint.
    assert loader.count("await Promise.all([") == 1


def test_the_first_paint_renders_only_what_usage_answers() -> None:
    """Anything else would print an em dash it has to take back a moment later.

    The sections the other two requests feed keep the placeholder
    usageReportRenderLoading() left on them, so the page fills in instead.
    """
    block = _report_block(_source())
    partial = _extract_js_function(block, "function usageReportRenderPartial(model) {")
    full = _extract_js_function(block, "function usageReportRender() {")
    hero = _extract_js_function(block, "function usageReportRenderHero(model) {")
    assert "usageReportRenderHeroTotals(model)" in partial
    # The activity half is reached through the whole-hero wrapper, which only the
    # full paint calls.
    assert "usageReportRenderHeroActivity(model)" not in partial
    assert "usageReportRenderHeroActivity(model)" in hero
    assert "usageReportRenderHero(model)" in full
    for absent in (
        "usageReportRenderSentence",
        "usageReportRenderHeat",
        "usageReportRenderPodium",
        "usageReportRenderWhen",
        "usageReportRenderAgents",
        "usageReportRenderFooter",
        "usageReportRenderShare",  # would export a half-empty card
    ):
        assert absent not in partial, f"{absent} has no data on the first paint"
        assert absent in full, f"{absent} must still run once everything has landed"
    # Both paints take the window lines from one place, so the partial one cannot
    # print a different range or title from the full one that replaces it.
    assert "usageReportRenderHead(model)" in partial
    assert "usageReportRenderHead(model)" in full


def test_an_unknown_count_prints_a_dash_everywhere_it_is_read() -> None:
    """`model.sessions` and `model.streaks` are read in four places. A single
    formatNumber(null) among them puts "0 sessions" back on the page."""
    block = _report_block(_source())
    # The activity half of the hero: split out so the first paint can render the
    # totals half from /api/usage alone, without these two tiles.
    hero = _extract_js_function(block, "function usageReportRenderHeroActivity(model) {")
    assert "empty || !model.streaks" in hero
    assert "String(model.streaks.active_days)" in hero, "the zero fallback is gone"
    sentence = _extract_js_function(block, "function usageReportRenderSentence(model) {")
    assert "sessions: usageReportFigure(model.sessions, formatNumber)" in sentence
    card = _extract_js_function(block, "function usageReportCardModel(model, tier, mode, overrides) {")
    assert "sessions: usageReportFigure(model.sessions, formatNumber)" in card
    assert "const streaks = model.streaks || null;" in card, "a zero-filled streak block prints 0 of N"


# --- delta honesty ----------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_delta_outside_the_data_is_never_printed(tmp_path: Path) -> None:
    """Year to date compares against an equal window that predates the records.

    The server cannot know the history stops, so the report decides: no delta on
    the year view, and a multiple-of-nothing delta is not a percentage to print.
    """
    body = _extract_js_function(_source(), "function usageReportShowDelta(model) {")
    body += """
const cases = [
  { period: 'year', comparison: { tokens_pct: 1274922.4 } },
  { period: 'month', comparison: { tokens_pct: 1274922.4 } },
  { period: 'month', comparison: { tokens_pct: 72.8 } },
  { period: 'week', comparison: { tokens_pct: -41.2 } },
  { period: 'month', comparison: null },
  { period: 'month', comparison: { tokens_pct: null } },
];
process.stdout.write(JSON.stringify(cases.map(usageReportShowDelta)));
"""
    result = json.loads(_run_node(tmp_path, "usage-report-delta.js", body))
    assert result == [False, False, True, True, False, False]


# --- copy deck --------------------------------------------------------------


def test_report_copy_keys_are_prefixed_and_shared_across_languages() -> None:
    """The prefix keeps the report clear of the refresh-report popover (`refreshReport*`)."""
    source = _source()
    assert re.search(r"\brefreshReport[A-Za-z]*\b", source), "popover keys must stay in place"
    start = source.index("const I18N = {")
    literal = source[start : source.index("\n    };", start)]
    report_keys = sorted(set(re.findall(r"^        (usageReport[A-Za-z0-9]*):", literal, re.M)))
    assert report_keys
    assert "report" not in report_keys, "a bare `report` key would collide"
    for language in I18N_LANGS:
        block = literal[literal.index(f"      {language}: {{") :]
        own = set(re.findall(r"^        ([a-zA-Z0-9_]+):", block, re.M))
        missing = set(report_keys) - own
        assert not missing, f"{language} is missing {sorted(missing)}"


def test_every_line_that_names_a_machine_names_the_selected_one() -> None:
    """The report is single-server, and these lines used to be fixed strings
    about "this machine": read from a remote server, the scope line, the footer,
    the agent table and the exported card all described the wrong box.
    """
    source = _source()
    block = _report_block(source)
    for call in (
        "t('usageReportScope', { machine: usageReportMachineName() })",
        "t('usageReportFooterDays', { machine })",
        "t('usageReportOtherTools', { n: tally, machine: usageReportMachineName() })",
    ):
        assert call in block, call
    footer = _extract_js_function(block, "function usageReportRenderFooter(model) {")
    assert "const machine = usageReportMachineName();" in footer
    # Only the nickname fallback may still spell the phrase out.
    for lang in I18N_LANGS:
        copy = _i18n_report_copy(source, lang)
        for key in MACHINE_KEYS:
            assert "{machine}" in copy[key], f"{lang}.{key} lost the placeholder"
        assert copy["usageReportCardNicknameFallback"]


def test_the_machine_placeholder_does_not_split_a_cjk_phrase() -> None:
    """`{machine} 记录的...` renders as `本机 记录的...` under the Chinese
    fallback -- a space between two Han characters, which reads as a typo. The
    space is only right when the neighbour is a middot separator, so the
    placeholder sits flush against CJK text and Korean particles.
    """
    source = _source()
    for lang in ("zh", "ja"):
        copy = _i18n_report_copy(source, lang)
        for key in MACHINE_KEYS:
            value = copy[key]
            assert not re.search(rf"\{{machine\}} {CJK}", value), f"{lang}.{key}: {value}"
            assert not re.search(rf"{CJK} \{{machine\}}", value), f"{lang}.{key}: {value}"


def test_the_version_stamp_is_read_from_the_server_the_report_reads() -> None:
    """`getRuntimeVersion()` is local-only and cached for the whole session, so
    a remote report stamped from it printed the local build in its footer line
    and in the attribution baked into every exported PNG."""
    version = _extract_js_function(_source(), "function usageReportLoadVersion() {")
    assert "getRuntimeVersion" not in version
    assert "fetchJsonWithRetry(server, '/api/version')" in version
    assert "usageReportState.versionServerId" in version, "the answer is keyed to its server"
    assert "usageReportRenderShare(usageReportState.model)" in version


# --- PR-B: podium, rhythm, agent table, caveats ------------------------------


def test_report_asks_for_the_facets_the_new_sections_need() -> None:
    """The sections below the day map are facet-fed; a missing facet is a hole."""
    block = _report_block(_source())
    facets_line = [
        line for line in block.splitlines() if line.strip().startswith("const USAGE_REPORT_FACETS")
    ][0]
    served = facets_line.split("'")[1].split(",")
    for facet in ("hourly", "weekday", "tools", "models", "projects"):
        assert facet in served, facet
    # One request for all of them: the server folds every facet out of one scan, so
    # asking twice costs the machine the scan again for nothing. Both callers -- the
    # explicit load and the silent re-read -- must therefore ask for the WHOLE set.
    # What must never appear is an insights request for a subset.
    asked = re.findall(r"/api/insights\?facets=\$\{(\w+)\}", block)
    assert asked, "no insights request found"
    assert set(asked) == {"USAGE_REPORT_FACETS"}, asked


def test_the_day_map_notice_names_only_the_facets_it_is_missing() -> None:
    """The wide request list must not leak into a notice about a narrow gap."""
    block = _report_block(_source())
    heat = [
        line for line in block.splitlines() if line.strip().startswith("const USAGE_REPORT_HEAT_FACETS")
    ][0]
    assert "hourly" not in heat and "projects" not in heat
    assert "t('usageReportFacetsMissing', { facets: USAGE_REPORT_HEAT_FACETS })" in block
    # The hour strip names its own two facets instead.
    assert "t('usageReportFacetsMissing', { facets: 'hourly,weekday' })" in block


def test_the_new_sections_render_after_the_day_map() -> None:
    block = _report_block(_source())
    render = _extract_js_function(block, "function usageReportRender() {")
    order = [
        "usageReportRenderHeat(model)",
        "usageReportRenderPodium(model)",
        "usageReportRenderWhen(model)",
        "usageReportRenderAgents(model)",
        "usageReportRenderFooter(model)",
    ]
    positions = [render.index(call) for call in order]
    assert positions == sorted(positions), "the sections fell out of the spec's order"


def test_the_new_sections_are_cleared_before_the_next_load() -> None:
    """A stale podium under a fresh hero is a report that lies about the period."""
    block = _report_block(_source())
    loading = _extract_js_function(block, "function usageReportRenderLoading() {")
    for element in (
        "usageReportPodium",
        "usageReportGroupNote",
        "usageReportWhen",
        "usageReportAgentsBody",
        "usageReportFooter",
    ):
        assert f"'{element}'" in loading, element


def test_no_night_window_is_hardcoded_in_the_page() -> None:
    """Spec 5.7: the window is served, so a client-side copy cannot rot quietly."""
    block = _report_block(_source())
    assert "night_hours" in block
    assert "22:00" not in block, "the night window is spelled out in the page"
    for hours in ("22, 23", "[22", "22,23"):
        assert hours not in block, hours


def test_the_project_surface_prints_every_reconciliation_component() -> None:
    """Spec 5.2: the facet's own unattributed bucket is not the whole gap."""
    block = _report_block(_source())
    assert "usageReportPodiumProjectNote" in block
    assert "usageReportProjectGroupNote" in block
    assert "usageReportMissingProjects" in block


def test_the_agent_table_never_invents_a_token_split() -> None:
    """OpenClaw has no output-token column; its row must show a dash, not a guess."""
    block = _report_block(_source())
    agents = _extract_js_function(block, BUILD_MODEL_SIGNATURE)
    assert "tokensOut: app && app.tokens_out !== undefined" in agents
    # Tokens and cost come from the facet scan, the split from /api/usage: the
    # two are different scans and the page must not blur them into one figure.
    assert "usageApps[tool]" in agents
    assert "usageReportFacetCovers" in block


def test_weekday_rows_that_have_not_happened_print_no_figure() -> None:
    """A three-day window has not met Thursday; that is not a zero-usage Thursday."""
    block = _report_block(_source())
    assert "elapsedWeekdays" in block
    when = _extract_js_function(block, "function usageReportRenderWhen(model) {")
    assert "reached ? formatTokenCount(row.tokens) : USAGE_REPORT_EM" in when
    assert "'0%'" in when


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_reconciliation_separates_the_unattributed_share_from_the_gap(
    tmp_path: Path,
) -> None:
    """The one real defect the design review found, pinned as arithmetic.

    A facet that sees 90% of the period's tokens has a 10% gap, and an
    unattributed bucket of 5% is not that gap: printing only the bucket
    understates it by half.
    """
    body = _extract_js_function(
        _source(), "function usageReportProjectShares(facetTotal, rowSum, unattributedTokens) {"
    )
    body += """
const cases = [
  [1000, 850, 50],   // 5% unattributed, 10% the facet never saw at all
  [1000, 500, 500],  // everything inside the facet
  [1000, 1000, 0],   // nothing to reconcile
  [1000, 980, 40],   // rows over-cover the total: clamp, never print a negative
  [0, 0, 0],         // no facet total: no shares, rather than shares of nothing
];
process.stdout.write(JSON.stringify(cases.map(([a, b, c]) => usageReportProjectShares(a, b, c))));
"""
    rows = json.loads(_run_node(tmp_path, "usage-report-reconcile.js", body))
    assert rows == [
        {"unattrShare": 0.05, "gapShare": 0.1},
        {"unattrShare": 0.5, "gapShare": 0.0},
        {"unattrShare": 0.0, "gapShare": 0.0},
        {"unattrShare": 0.04, "gapShare": 0.0},
        {"unattrShare": None, "gapShare": None},
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        # The window the server ships today: one span that walks past midnight.
        ([22, 23, 0, 1], "22:00\u201302:00"),
        # A server that changes its mind must print differently, not identically.
        ([20, 21, 22, 23], "20:00\u201300:00"),
        ([1, 2, 3, 19], "01:00\u201304:00 \u00b7 19:00\u201320:00"),
        ([], "\u2014"),
    ],
)
def test_the_night_window_is_read_from_what_the_server_serves(
    tmp_path: Path, hours: list[int], expected: str
) -> None:
    source = _source()
    body = _em_constant(source) + _extract_js_function(
        source, "function usageReportHourLabel(hour) {"
    ) + _extract_js_function(source, "function usageReportHourWindow(hours) {")
    body += f"""
process.stdout.write(usageReportHourWindow({json.dumps(hours)}));
"""
    assert _run_node(tmp_path, "usage-report-night.js", body) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_small_share_keeps_digits_that_survive_the_eye(tmp_path: Path) -> None:
    """0.4% rounded to 0% reads as a missing slice, which is a different fact."""
    source = _source()
    body = _em_constant(source) + _extract_js_function(source, "function usageReportShare(fraction) {")
    body += """
const cases = [0.004, 0.097, 0.21, 0.5, null, undefined, NaN];
process.stdout.write(JSON.stringify(cases.map(usageReportShare)));
"""
    assert json.loads(_run_node(tmp_path, "usage-report-share.js", body)) == [
        "0.40%",
        "9.7%",
        "21%",
        "50%",
        "\u2014",
        "\u2014",
        "\u2014",
    ]


def _em_constant(source: str) -> str:
    line = next(
        line
        for line in source.splitlines()
        if line.strip().startswith("const USAGE_REPORT_EM = ")
    )
    return line + "\n"


# --- PR-C: share cards, exported as PNG in both themes ----------------------


CARD_SIGNATURES = (
    "const USAGE_REPORT_CARD = {",
    "const USAGE_REPORT_CARD_PAL = {",
    "const USAGE_REPORT_TIER_ACCENT = {",
    "function usageReportCardFont(size, weight, mono) {",
    "function usageReportTextWidth(text, font) {",
    "function usageReportWrapText(text, font, maxWidth) {",
    "function usageReportClipText(text, font, maxWidth) {",
    "function usageReportFigure(value, format) {",
    "function usageReportMachineName() {",
    "function usageReportCardTitle(model) {",
    "function usageReportCardModel(model, tier, mode, overrides) {",
    "function usageReportBuildCard(model, tier, mode) {",
    "function usageReportCardFilename(model, tier, mode) {",
)


def _i18n_report_copy(source: str, lang: str) -> dict[str, str]:
    """One language's report strings, so a harness measures real copy."""
    start = source.index("const I18N = {")
    literal = source[start : source.index("\n    };", start)]
    begin = literal.index(f"      {lang}: {{")
    later = sorted(
        mark
        for mark in (literal.index(f"      {name}: {{") for name in I18N_LANGS)
        if mark > begin
    )
    block = literal[begin : later[0] if later else len(literal)]
    copy: dict[str, str] = {}
    for line in block.splitlines():
        match = re.match(r"^        (usageReport[A-Za-z0-9]*): '(.*)',$", line)
        if not match:
            continue
        key, raw = match.groups()
        copy[key] = ast.literal_eval("'" + raw.replace(r"\'", "'") + "'")
    assert len(copy) > 50, f"the {lang} report copy did not parse"
    return copy


def _i18n_english_copy(source: str) -> dict[str, str]:
    """The layout harness measures the real English copy, not a stand-in."""
    return _i18n_report_copy(source, "en")


def _card_harness(source: str) -> str:
    """The layout half of the card painter, with the app's formatters stubbed.

    Only geometry is under test here, so the metric is a plausible stand-in:
    0.55em per glyph. The copy is real, which is what actually overflows a card.
    Stubs come first because the painter measures text at module scope.
    """
    prelude = """
const DEFAULT_HEAT_COLORS_LIGHT = ['#EEF2F7','#E0E7FF','#C7D2FE','#A5B4FC','#60A5FA','#3B82F6','#2563EB','#1E40AF'];
const DEFAULT_HEAT_COLORS_DARK = ['#172033','#1E293B','#1D4ED8','#2563EB','#3B82F6','#60A5FA','#93C5FD','#BFDBFE'];
const currentLang = 'en';
const usageReportState = { includeCost: false, nickname: 'wsl-box', version: '2.5.1' };
let serverUnderTest = { id: 'local', label: 'Local', baseUrl: '' };
const usageReportServer = () => serverUnderTest;
const COPY = __COPY__;
function t(key, vars) {
  let out = COPY[key] === undefined ? key : COPY[key];
  Object.keys(vars || {}).forEach((name) => { out = out.split('{' + name + '}').join(String(vars[name])); });
  return out;
}
const document = { createElement: () => ({ getContext: () => ({
  font: '400 10px sans-serif',
  measureText(text) {
    const size = parseFloat(String(this.font).split(' ')[1]) || 10;
    return { width: String(text).length * size * 0.55 };
  },
}) }) };
const parseDateKey = (value) => new Date(value + 'T00:00:00');
const formatTokenCount = (n) => (Number(n) >= 1e6 ? (Number(n) / 1e6).toFixed(1) + 'M' : String(n));
const formatNumber = (n) => String(n);
const formatCurrency = (n) => '$' + Number(n).toFixed(2);
const formatDuration = (ms) => Math.round(Number(ms) / 3600000) + 'h';
const formatShortDate = (value) => String(value);
const langLocale = () => 'en-US';
const usageReportToolLabel = (tool) => String(tool);
const usageReportShare = (f) => (Number(f) * 100).toFixed(1) + '%';
"""
    prelude = prelude.replace("__COPY__", json.dumps(_i18n_english_copy(source), ensure_ascii=False))
    constants = "\n".join(
        line + "\n"
        for line in source.splitlines()
        if line.strip().startswith(
            ('const USAGE_REPORT_SANS = ', 'const USAGE_REPORT_MONO = ', 'const usageReportMeasureCtx = ')
        )
    )
    return (
        prelude
        + _em_constant(source)
        + constants
        + "\n".join(_extract_js_function(source, signature) for signature in CARD_SIGNATURES)
        + "\n"
    )


CARD_FIXTURE = """
const busy = {
  period: 'month',
  range: { from: '2026-09-01', to: '2026-09-30' },
  elapsed: 30,
  tokens: 60123456789, messages: 41234, cost: 39412.55, sessions: 1875,
  activeMs: 4968000000, isEmpty: false,
  cells: Array.from({ length: 30 }, (_u, i) => ({
    date: '2026-09-' + String(i + 1).padStart(2, '0'),
    tokens: (i + 1) * 1e6, level: i % 8, future: false,
  })),
  streaks: { active_days: 28, current_streak: 12, longest_streak: 173 },
  firsts: { busiest_day: '2026-09-14', busiest_day_tokens: 4123000000 },
  tools: [{ tool: 'codex', tokens: 4e10 }, { tool: 'claude', tokens: 1.4e10 }, { tool: 'gemini_cli', tokens: 3e9 }],
  models: [{ model: 'gpt-5.2-pro-thinking max', tokens: 3e10 }, { model: 'claude-fable-5.1', tokens: 1.5e10 }],
  projectsAvailable: true,
  projectCount: 258,
  projectRows: Array.from({ length: 8 }, (_u, i) => ({
    project: 'tokdash-project-' + (i + 1) + '-with-a-very-long-repository-name',
    tokens: (9 - i) * 3e9,
  })),
  facetTotal: 58e9,
  unattrShare: 0.0056,
  gapShare: 0.097,
};
const sparse = {
  ...busy,
  period: 'year',
  range: { from: '2026-01-01', to: '2026-09-03' },
  elapsed: 246,
  cells: Array.from({ length: 371 }, (_u, i) => {
    const d = new Date(Date.UTC(2026, 0, 1 + Math.floor(i / 7) + (i % 7) * 0, 0));
    return { date: '2026-01-01', tokens: 0, level: 0, future: false };
  }),
  streaks: null,
  firsts: null,
  tools: null,
  models: null,
  projectsAvailable: false,
  projectRows: [],
  projectCount: 0,
  activeMs: 0,
  isEmpty: true,
  tokens: 0,
  messages: 0,
  cost: 0,
  // Active time is its own request and fails on its own: unknown, not zero.
  sessions: null,
};
"""


def _card_audit(tmp_path: Path, extra: str = "") -> dict:
    source = _source()
    body = _card_harness(source) + CARD_FIXTURE + extra
    return json.loads(_run_node(tmp_path, "usage-report-cards.js", body))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_card_fits_its_canvas_and_keeps_its_reconciliation_line(tmp_path: Path) -> None:
    """A card that runs under its own footer is the failure mode of any painter."""
    report = _card_audit(
        tmp_path,
        """
const report = { cards: {} };
for (const tier of ['green', 'amber']) {
  for (const mode of ['light', 'dark']) {
    const card = usageReportBuildCard(busy, tier, mode);
    report.cards[`${tier}/${mode}`] = {
      overflow: Number((card.used - card.budget).toFixed(3)),
      rows: card.opt.maxProjects,
      texts: card.ops.filter((op) => op.k === 'text').map((op) => op.text),
      rects: card.ops.filter((op) => op.k === 'rect').map((op) => [op.x, op.h]),
      bg: card.pal.bg,
      heat: card.pal.heat[7],
      file: usageReportCardFilename(busy, tier, mode),
    };
  }
}
process.stdout.write(JSON.stringify(report));
""",
    )
    cards = report["cards"]
    assert sorted(cards) == ["amber/dark", "amber/light", "green/dark", "green/light"]
    for name, card in cards.items():
        assert card["overflow"] <= 0, f"{name} runs {card['overflow']}px past its footer"
        # No left ribbon: nothing paints the full height of the card at its edge.
        assert not [x for x, h in card["rects"] if x == 0 and h > 400], f"{name} grew a spine"
    assert cards["green/light"]["bg"] != cards["green/dark"]["bg"]
    assert cards["green/light"]["heat"] != cards["green/dark"]["heat"]
    assert cards["green/light"]["rows"] == 0 and cards["amber/light"]["rows"] > 0
    assert cards["amber/light"]["file"] == "tokdash-report-2026-09-01_2026-09-30-amber-light.png"
    # Manifest lines wrap, so the privacy copy is read as one blob per card.
    green = " ".join(cards["green/light"]["texts"])
    amber = " ".join(cards["amber/light"]["texts"])
    assert "no project names" in green and "no prompt text" in green
    assert "no prompt text" in amber and "share deliberately" in amber
    assert "Grouped by repository" in amber, "the reconciliation line is the point of the tier"
    assert "tokdash-project-" not in green
    assert "tokdash-project-" in amber


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_year_card_shrinks_before_it_drops_rows(tmp_path: Path) -> None:
    """A 53-column heat map is the tight case; project rows must not pay for it."""
    report = _card_audit(
        tmp_path,
        """
const wide = { ...busy, period: 'year', range: { from: '2026-01-01', to: '2026-12-31' }, elapsed: 246,
  cells: Array.from({ length: 371 }, (_u, i) => ({ date: '2026-01-01', tokens: i * 1e5, level: i % 8, future: false })) };
const month = usageReportBuildCard(busy, 'amber', 'light');
const year = usageReportBuildCard(wide, 'amber', 'light');
process.stdout.write(JSON.stringify({
  monthRows: month.opt.maxProjects,
  yearRows: year.opt.maxProjects,
  yearOverflow: Number((year.used - year.budget).toFixed(3)),
  yearCell: year.ops.filter((op) => op.k === 'rect' && op.w === op.h).map((op) => op.w).sort((a, b) => b - a)[0],
}));
""",
    )
    assert report["yearOverflow"] <= 0
    assert report["yearCell"] >= 3, "heat cells collapsed below a legible size"
    assert report["yearRows"] >= 1, "the amber card lost its project rows to the heat map"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_card_survives_the_facets_an_older_server_omits(tmp_path: Path) -> None:
    """A pre-facet server still gets a card: missing figures print as em dashes."""
    report = _card_audit(
        tmp_path,
        """
const out = {};
for (const tier of ['green', 'amber']) {
  const card = usageReportBuildCard(sparse, tier, 'dark');
  out[tier] = {
    overflow: Number((card.used - card.budget).toFixed(3)),
    texts: card.ops.filter((op) => op.k === 'text').map((op) => op.text),
    icons: card.ops.filter((op) => op.k === 'icon').length,
  };
}
process.stdout.write(JSON.stringify(out));
""",
    )
    assert report["green"]["overflow"] <= 0 and report["amber"]["overflow"] <= 0
    assert report["green"]["icons"] == 0
    assert any("—" in text for text in report["green"]["texts"]), "a missing figure printed as a zero"
    # Named figures, not just "a dash somewhere": a PNG outlives the load that
    # made it, so a zero-filled streak block or session count is unfalsifiable.
    texts = report["green"]["texts"]
    assert "— of 246 days active" in texts, "a missing streaks facet printed 0 days active"
    assert "— sessions · 0 requests" in texts, "an absent session count printed as 0 sessions"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_card_names_the_machine_it_was_painted_on(tmp_path: Path) -> None:
    """Spec D1: one machine per report, and the card says which. A remote server
    prints its registry label; the local box prints the translated fallback, since
    "Local" is a UI label and not a name; an owned nickname wins over both."""
    body = _card_harness(_source()) + _extract_js_function(_source(), "function usageReportMachineName() {")
    body += """
const cases = [
  [{ id: 'local', label: 'Local' }, ''],
  [{ id: 'wsl', label: 'wsl-box' }, ''],
  [{ id: 'mac', label: '  ' }, ''],
  [{ id: 'mac', label: 'Mac Studio' }, 'desk'],
];
const out = [];
for (const [server, nickname] of cases) {
  serverUnderTest = server;
  usageReportState.nickname = nickname;
  out.push(usageReportMachineName());
}
process.stdout.write(JSON.stringify(out));
"""
    assert json.loads(_run_node(tmp_path, "usage-report-machine.js", body)) == [
        "this machine",
        "wsl-box",
        "this machine",
        "desk",
    ]
    assert "nickname: usageReportMachineName()" in _source(), "the card must stamp it, not the raw pref"
    assert "nicknameInput.placeholder = usageReportMachineName()" in _source()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_card_range_line_names_the_machine_too(tmp_path: Path) -> None:
    """The attribution used to be the only line on the card that got this right:
    the range line under the title said "all agent activity on this machine"
    whatever server the report had been read from."""
    report = _card_audit(
        tmp_path,
        """
serverUnderTest = { id: 'mac', label: 'mac-studio' };
usageReportState.nickname = '';
const card = usageReportBuildCard(busy, 'green', 'light');
process.stdout.write(JSON.stringify({
  texts: card.ops.filter((op) => op.k === 'text').map((op) => op.text),
}));
""",
    )
    joined = " ".join(report["texts"])
    assert "mac-studio" in joined
    assert "this machine" not in joined, "the card described the wrong machine"
    assert "{machine}" not in joined, "the placeholder reached the canvas unsubstituted"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_short_window_paints_a_strip_not_a_column_of_strays(tmp_path: Path) -> None:
    """Three days of a month is one column of the calendar grid; on a card with no
    weekday labels that reads as three stray squares over an unexplained hole."""
    report = _card_audit(
        tmp_path,
        """
const short = {
  ...busy,
  period: 'month',
  range: { from: '2026-09-01', to: '2026-09-03' },
  elapsed: 3,
  cells: Array.from({ length: 30 }, (_u, i) => ({
    date: '2026-09-' + String(i + 1).padStart(2, '0'),
    tokens: (i + 1) * 1e6, level: i % 8, future: i > 2,
  })),
};
const out = {};
for (const tier of ['green', 'amber']) {
  const card = usageReportBuildCard(short, tier, 'light');
  const cells = card.ops.filter((op) => op.k === 'rect' && op.w === op.h && op.w === 18);
  out[tier] = {
    overflow: Number((card.used - card.budget).toFixed(3)),
    squares: cells.length,
    rows: [...new Set(cells.map((op) => op.y))].length,
    xs: cells.map((op) => op.x),
  };
}
process.stdout.write(JSON.stringify(out));
""",
    )
    for tier, card in report.items():
        assert card["overflow"] <= 0
        assert card["squares"] == 3, f"{tier} card painted {card['squares']} heat squares for 3 days"
        assert card["rows"] == 1, f"{tier} card stacked the short window into a column"
        assert card["xs"] == sorted(set(card["xs"])), f"{tier} card did not run the strip left to right"


def test_share_panel_offers_two_tiers_and_three_export_paths() -> None:
    source = _source()
    panel = source[source.index('data-i18n="usageReportShareTitle"') : source.index("<!-- Quota Tab -->")]
    for marker in (
        'id="usageReportNicknameInput"',
        'id="usageReportCostToggle"',
        'id="usageReportGreenCards"',
        'id="usageReportAmberCards"',
        'data-export="green"',
        'data-export="amber"',
        'data-export="all"',
        'id="usageReportExportStatus" hidden',
    ):
        assert marker in panel, marker
    # A per-card save button would be a single-theme download path by another name.
    assert "data-save" not in panel


def test_every_export_writes_both_themes_for_the_tier_it_names() -> None:
    """Howard's rule: a download is never one file, and never the on-screen theme."""
    source = _source()
    export = _extract_js_function(source, "async function usageReportExportCards(tier) {")
    assert "for (const mode of ['light', 'dark'])" in export
    assert "tiers = tier === 'all' ? ['green', 'amber'] : [tier]" in export
    assert export.count("toBlob") == 1, "one paint-to-file path, so previews and files agree"
    assert "usageReportCardFilename(model, current, mode)" in export
    assert "t('usageReportExported', { n: wrote })" in export
    assert "t('usageReportExportNothing')" in export, "a download that wrote nothing must say so"
    assert "usageReportExportCards(button.dataset.export)" in source
    assert not re.search(r"anchor\.download\s*=\s*[^,)]*\(model,[^)]*'light'\)", export)


def test_a_download_never_names_a_window_it_did_not_paint() -> None:
    """A period switch mid-paint must not pair one window's filename with the
    previous window's pixels, and the version stamp is rarely in for the first
    paint, so the cards have to be repainted when it lands."""
    source = _source()
    share = _extract_js_function(source, "async function usageReportRenderShare(model) {")
    assert share.index("usageReportState.previews = {}") < share.index("await usageReportCardFonts()"), (
        "the preview set has to be retired before the paint goes async"
    )
    version = _extract_js_function(source, "function usageReportLoadVersion() {")
    assert "usageReportRenderShare(usageReportState.model)" in version


def test_a_landing_load_leaves_a_half_typed_nickname_alone() -> None:
    """The placeholder has to follow the server the report reads, and it is read on
    every render. The field's value is not: a load that lands while someone is
    still typing would otherwise wipe text that has not been committed yet.
    """
    block = _report_block(_source())
    render = _extract_js_function(block, "function usageReportRender() {")
    assert "usageReportShareHint()" in render
    assert "usageReportSyncShareInputs()" not in render
    hint = _extract_js_function(block, "function usageReportShareHint() {")
    assert ".value" not in hint, "only the placeholder may be written on a render"
    assert "placeholder = usageReportMachineName()" in hint
    wire = _extract_js_function(block, "function usageReportWireShare() {")
    assert "usageReportSyncShareInputs()" in wire, "stored prefs still reach the field once, at wire time"


def test_share_card_colours_are_literal_and_the_ramp_is_the_default_scale() -> None:
    """A PNG outlives the theme that painted it, so it must not read live tokens."""
    source = _source()
    palette = _extract_js_function(source, "const USAGE_REPORT_CARD_PAL = {")
    assert "var(--" not in palette
    assert "heat: DEFAULT_HEAT_COLORS_LIGHT" in palette
    assert "heat: DEFAULT_HEAT_COLORS_DARK" in palette


def test_the_prefs_key_follows_the_convention_the_page_already_uses() -> None:
    """Every other stored key is `tokdash-*`; a lone dotted key is invisible to
    anyone grepping localStorage for this app."""
    source = _source()
    key = re.search(r"const USAGE_REPORT_PREFS_KEY = '([^']+)'", source)
    assert key and key.group(1).startswith("tokdash-"), key and key.group(1)
    assert "." not in key.group(1)


def test_share_prefs_carry_the_nickname_and_the_cost_choice() -> None:
    source = _source()
    read = _extract_js_function(source, "function usageReportReadPrefs() {")
    write = _extract_js_function(source, "function usageReportWritePrefs() {")
    for fn in (read, write):
        assert "nickname" in fn and "includeCost" in fn
    assert "raw.includeCost === true" in read, "an absent pref must not print money by default"


def test_a_control_changed_mid_flight_is_applied_late_not_dropped() -> None:
    """The report on screen belongs to the period and server it was fetched for,
    so a click during a load cannot be shown yet. It used to vanish without a
    trace; now the control snaps back and the choice runs when the load lands.
    """
    block = _report_block(_source())
    loader = _extract_js_function(block, "async function loadUsageReport(options = {}) {")
    wire = _extract_js_function(block, "function usageReportWire() {")
    # One exit, not two. The load used to return early on a total failure and had to
    # remember to settle the queue on both paths; the try/finally collapsed them, so
    # "every exit settles the queue" is now structurally true rather than duplicated.
    assert loader.count("usageReportApplyPendingChoice()") == 1
    assert loader.count("return;") == 1, "only the re-entrancy guard at the top may return early"
    assert "usageReportState.pendingPeriod = button.dataset.period" in wire
    assert "usageReportState.pendingServerId = select.value" in wire
    assert "select.value = usageReportState.serverId" in wire, "the box names the server actually on screen"
    applied = _extract_js_function(block, "function usageReportApplyPendingChoice() {")
    # A plain load, not a forced one. The queued choice is a period or server the
    # reader has not seen yet, so it wants the cache like any other first load;
    # forcing a recompute made clicking during a load the slowest path in the tab.
    assert "loadUsageReport()" in applied
    assert "loadUsageReport({ refresh: true })" not in applied, (
        "a queued choice is a first read, not a refresh"
    )
    assert "usageReportWritePrefs()" in applied, "a queued choice that lands is the stored choice"


def test_the_share_panel_is_cleared_before_the_next_load() -> None:
    """Stale previews would export a period the header no longer names."""
    block = _report_block(_source())
    loading = _extract_js_function(block, "function usageReportRenderLoading() {")
    assert "usageReportClearShare()" in loading
    assert "usageReportState.previews = {}" in block
