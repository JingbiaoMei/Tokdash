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
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from support.frontend import extract_js_function, index_source, needs_node, run_node
from support.report import activate_dashboard_tab_program

I18N_LANGS = ("en", "zh", "ja", "ko", "es", "pt")

# Kana and Han, for the copy rules that only apply where words carry no spaces.
CJK = r"[぀-ヿ一-鿿]"

# The source line names the selected server whose tokdash counted the usage.
MACHINE_KEYS = (
    "usageReportFooterSrc",
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
    "function usageReportWindows(period, back = 0) {",
    "function usageReportPeriodLength(period, fromKey) {",
    "function usageReportDayKeys(fromKey, toKey) {",
    "function usageReportCalendar(period, fromKey, toKey) {",
)


def _source() -> str:
    return index_source()


def _report_block(source: str) -> str:
    """The Report tab's own JS, from its first constant to the tab activator."""
    start = source.index("const USAGE_REPORT_FACETS")
    end = source.index("    function activateDashboardTab(tab) {", start)
    return source[start:end]


def _run_node(tmp_path: Path, name: str, body: str, *argv: str) -> str:
    """Write a harness to ``tmp_path`` and return what node prints.

    run_node decodes stdout as UTF-8 whatever the platform, which this tab needs:
    the locale default turns every em dash and middot it prints into a
    replacement character on a Windows runner.
    """
    harness = tmp_path / name
    harness.write_text(body, encoding="utf-8")
    return run_node(harness, *argv)


# --- wiring -----------------------------------------------------------------


def test_report_tab_is_registered_between_stats_and_quota() -> None:
    source = _source()
    # Slice from the tab nav by its stable id rather than its class list, so
    # adding an attribute (the aria hook, or the id the desktop hide rule
    # keys on) does not break the lookup.
    nav = source[source.index('<nav id="dashboardSectionNav"') :]
    tabs = re.findall(r'<button class="tab-btn" data-tab="([^"]+)"', nav)
    assert "report" in tabs
    assert tabs.index("report") == tabs.index("stats") + 1
    assert 'data-i18n="usageReport"' in nav
    # The switch finds a tab's panel by a built id, which the page-wide id check
    # cannot see, so the panel the button opens is pinned here.
    assert '<div id="report-content" class="tab-content">' in source


@needs_node
def test_the_report_tab_loads_once_and_sets_its_gate_before_it_dispatches(tmp_path: Path) -> None:
    """The tab switch owns the Report page's `active` flag.

    The page loads the first time it is opened and not on every switch, like the
    pricing tab: the period switcher, the server picker and Retry are what ask
    the server again. The flag has to be set before that load, because the
    loader reads it to decide whether the two slow requests have anywhere to
    land. Leaving retires what was in flight, since its callbacks queue more work
    behind a view the reader left; coming back resumes what was put off, and a
    silent re-read that came due while the page was away runs then.
    """
    program = activate_dashboard_tab_program("""
const out = {};
out.first = visit('report');
out.away = visit('overview');
out.back = visit('report');
usageReportState.staleReread = { token: 1, period: 'month', back: 0, serverId: 'local' };
out.awayAgain = visit('stats');
out.backToStale = visit('report');
process.stdout.write(JSON.stringify(out));
""")
    out = json.loads(_run_node(tmp_path, "report-activate.js", program))

    first = out["first"]
    assert first["panels"] == ["report-content"] and first["selected"] == ["report"], first
    assert first["reportActive"] is True
    assert "loadReport:active=true" in first["log"], (
        f"the load dispatched before the page was marked active: {first['log']}"
    )
    loads = [entry for visit in out.values() for entry in visit["log"] if entry.startswith("loadReport")]
    assert loads == ["loadReport:active=true"], f"the report loaded on more than its first visit: {loads}"

    for leaving in ("away", "awayAgain"):
        assert "retireReport:active=true" in out[leaving]["log"], (leaving, out[leaving]["log"])
        assert out[leaving]["reportActive"] is False

    assert "resumeReport:active=true" in out["back"]["log"], out["back"]["log"]
    assert "fireStale:active=true" in out["backToStale"]["log"], out["backToStale"]["log"]
    assert not [entry for entry in out["backToStale"]["log"] if entry.startswith("resumeReport")]

    # Opening the report asks the speed page for nothing.
    for visit in (out["first"], out["back"], out["backToStale"]):
        assert not [entry for entry in visit["log"] if "Speed" in entry], visit["log"]


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


def test_report_renders_data_text_without_inner_html() -> None:
    """Tool, model and project names are data; they never travel as markup."""
    assert "innerHTML" not in _report_block(_source())


# --- window math, executed for real under node ------------------------------


@needs_node
@pytest.mark.parametrize(
    ("today", "period", "expected", "calendar"),
    [
        (
            "2028-02-29",
            "day",
            {"from": "2028-02-29", "to": "2028-02-29", "length": 1, "elapsed": 1},
            {"first": "2028-02-29", "last": "2028-02-29"},
        ),
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
    body = "\n".join(extract_js_function(source, signature) for signature in HELPERS)
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


@needs_node
def test_an_elapsed_window_never_exceeds_the_period_it_claims(tmp_path: Path) -> None:
    """The in-progress note prints `{d} of {n} days`; both have to be true."""
    source = _source()
    body = "\n".join(extract_js_function(source, signature) for signature in HELPERS)
    body += """
usageReportToday = () => new Date(process.argv[2] + 'T12:00:00');
const out = [];
for (const period of ['day', 'week', 'month', 'year']) {
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
        "day": {"period": "day", "elapsed": 1, "length": 1},
        "week": {"period": "week", "elapsed": 4, "length": 7},
        "month": {"period": "month", "elapsed": 3, "length": 30},
        "year": {"period": "year", "elapsed": 246, "length": 365},
    }


@needs_node
def test_back_steps_whole_periods(tmp_path: Path) -> None:
    """The top-bar chips walk whole periods back: a past week is the full
    Monday-to-Sunday, a past month its first to its last, a past year Jan-Dec.
    Only the current window stops at today."""
    source = _source()
    body = "\n".join(extract_js_function(source, signature) for signature in HELPERS)
    body += """
usageReportToday = () => new Date('2026-09-07T12:00:00');
const out = [];
for (const period of ['day', 'week', 'month', 'year']) {
  for (const back of [0, 1, 2]) {
    const w = usageReportWindows(period, back);
    out.push({ period, back, from: w.date_from, to: w.date_to,
      length: usageReportPeriodLength(period, w.date_from),
      elapsed: usageReportDayKeys(w.date_from, w.date_to).length });
  }
}
process.stdout.write(JSON.stringify(out));
"""
    rows = json.loads(_run_node(tmp_path, "usage-report-back.js", body))
    by = {(row["period"], row["back"]): row for row in rows}
    for back, day in enumerate(("2026-09-07", "2026-09-06", "2026-09-05")):
        assert by[("day", back)] == {"period": "day", "back": back, "from": day, "to": day, "length": 1, "elapsed": 1}
    assert by[("week", 0)]["from"] == "2026-09-07" and by[("week", 0)]["to"] == "2026-09-07"
    assert (by[("week", 1)]["from"], by[("week", 1)]["to"]) == ("2026-08-31", "2026-09-06")
    assert (by[("week", 2)]["from"], by[("week", 2)]["to"]) == ("2026-08-24", "2026-08-30")
    assert (by[("month", 1)]["from"], by[("month", 1)]["to"]) == ("2026-08-01", "2026-08-31")
    assert (by[("year", 1)]["from"], by[("year", 1)]["to"]) == ("2025-01-01", "2025-12-31")
    for row in rows:
        if row["back"] > 0:
            assert row["elapsed"] == row["length"], f"{row}: a past window must be the whole period"


@needs_node
@pytest.mark.parametrize(
    ("today", "back", "expected"),
    [
        ("2026-01-01", 1, "2025-12-31"),
        ("2028-03-01", 1, "2028-02-29"),
        ("2026-10-26", 1, "2026-10-25"),
        ("2026-03-30", 1, "2026-03-29"),
    ],
)
def test_day_navigation_crosses_calendar_and_clock_boundaries(
    tmp_path: Path, today: str, back: int, expected: str
) -> None:
    body = "process.env.TZ = 'Europe/London';\n"
    body += "\n".join(extract_js_function(_source(), signature) for signature in HELPERS)
    body += f"""
usageReportToday = () => new Date('{today}T12:00:00');
process.stdout.write(JSON.stringify(usageReportWindows('day', {back})));
"""
    assert json.loads(_run_node(tmp_path, "report-day-boundary.js", body)) == {
        "date_from": expected,
        "date_to": expected,
    }


@needs_node
def test_duration_units_are_localized_across_languages_and_language_switches(tmp_path: Path) -> None:
    source = _source()
    locales = re.search(r"const LANG_LOCALES = \{[^\n]+\};", source)
    assert locales
    body = locales.group(0) + "\nlet currentLang = 'en'; const durationUnitFormatters = new Map();\n"
    for signature in (
        "function langLocale(lang = currentLang) {",
        "function formatDurationUnit(unit, value) {",
        "function formatDurationUnits(ms) {",
    ):
        body += extract_js_function(source, signature) + "\n"
    body += """
const durations = [0, 45000, 5 * 60000, (7 * 60 + 39) * 60000, 24 * 3600000, 65 * 3600000, 8 * 86400000, 34 * 86400000];
const out = {};
for (const lang of Object.keys(LANG_LOCALES)) {
  currentLang = lang;
  out[lang] = durations.map(formatDurationUnits);
}
// A cached formatter must still follow the language after switching back.
currentLang = 'en';
out.enAgain = durations.map(formatDurationUnits);
process.stdout.write(JSON.stringify(out));
"""
    report = json.loads(_run_node(tmp_path, "report-localized-time.js", body))
    assert report.pop("enAgain") == report["en"]
    assert report == {
        "en": ["—", "45 seconds", "5 minutes", "7 hours 39 minutes", "1 day", "2 days 17 hours", "1 week 1 day", "1 month 4 days"],
        "zh": ["—", "45秒钟", "5分钟", "7小时39分钟", "1天", "2天17小时", "1周1天", "1个月4天"],
        "ja": ["—", "45秒", "5分", "7時間39分", "1日", "2日17時間", "1週間1日", "1か月4日"],
        "ko": ["—", "45초", "5분", "7시간 39분", "1일", "2일 17시간", "1주 1일", "1개월 4일"],
        "es": ["—", "45 segundos", "5 minutos", "7 horas 39 minutos", "1 día", "2 días 17 horas", "1 semana 1 día", "1 mes 4 días"],
        "pt": ["—", "45 segundos", "5 minutos", "7 horas 39 minutos", "1 dia", "2 dias 17 horas", "1 semana 1 dia", "1 mês 4 dias"],
    }


@needs_node
def test_the_report_tab_swaps_quick_ranges_for_period_chips(tmp_path: Path) -> None:
    """On the Report tab the trailing-window presets step aside for chips that
    walk the report's period back, and the chips are drawn as the tab opens."""
    program = activate_dashboard_tab_program("""
process.stdout.write(JSON.stringify({
  report: visit('report'), overview: visit('overview'), stats: visit('stats'),
}));
""")
    out = json.loads(_run_node(tmp_path, "report-top-bar.js", program))
    assert (out["report"]["rangesHidden"], out["report"]["chipsHidden"]) == (True, False)
    assert "renderPeriodChips" in out["report"]["log"]
    for tab in ("overview", "stats"):
        assert (out[tab]["rangesHidden"], out[tab]["chipsHidden"]) == (False, True), (tab, out[tab])
        assert "renderPeriodChips" not in out[tab]["log"]


def test_a_past_window_names_itself() -> None:
    """'This month' over an August report is a lie: past windows take the
    card-style title, and a completed year prints its number, not 'year to date'."""
    source = _source()
    title = extract_js_function(source, "function usageReportPeriodTitle(model) {")
    assert "usageReportCardTitle(model)" in title
    card = extract_js_function(source, "function usageReportCardTitle(model) {")
    assert "-12-31" in card


def test_the_card_filename_names_the_theme() -> None:
    """The card wears the active style theme, so the export names it -- the
    same window saved under two themes must not overwrite itself."""
    fn = extract_js_function(_source(), "function usageReportCardFilename(model, tier, mode) {")
    assert "dataset.uiTheme" in fn


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
  // The served intensity contradicts the token counts on purpose: the busier day
  // is served as the faintest. The day map shades by the Stats tab's rule.
  daily: [
    { date: '2026-09-01', tokens: 300, messages: 30, intensity: 0 },
    { date: '2026-09-02', tokens: 10, messages: 1, intensity: 7 },
  ],
  streaks: { active_days: 2, current_streak: 2, longest_streak: 9 },
  firsts: { busiest_day: '2026-09-01', busiest_day_tokens: 300 },
  tools: { ranked: [{ tool: 'codex', tokens: 600, cost: 6 }, { tool: 'claude', tokens: 300, cost: 3 }] },
  models: { ranked: [
    { model: 'alpha', tokens: 600, messages: 60, cost: 6 },
    { model: 'beta', tokens: 300, messages: 30, cost: 0 },
    { model: 'idle', tokens: 0, messages: 0, cost: 0 },
  ] },
  projects: {
    projects: [
      { project: 'one', tokens: 500, messages: 50, cost: 5 },
      { project: 'two', tokens: 300, messages: 30, cost: 3 },
    ],
    unattributed: { tokens: 90 },
    attributed_project_count: 2,
  },
};
// codex has a per-app row with an output column; claude only has a by-tool row,
// which carries no output figure.
const USAGE = {
  total_tokens: 1000, total_messages: 100, total_cost: 10,
  by_tool: { claude: { tokens_in: 40, tokens_cache: 50 } },
  apps: { codex: { tokens_in: 10, tokens_out: 20, tokens_cache: 30 } },
};
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
  agents: model.agentRows.map((row) => row.tool),
  agentSessions: model.agentRows.map((row) => row.sessions),
  modelNames: model.modelRows.map((row) => row.model),
  modelShares: model.modelRows.map((row) => row.share),
  projectNames: model.projectRows.map((row) => row.project),
  projectShares: model.projectRows.map((row) => row.share),
  hasStreaks: !!model.streaks,
  notices: model.notices,
  levels: Object.fromEntries(model.cells.filter((cell) => cell.inWindow).map((cell) => [cell.date, cell.level])),
  agentSplit: model.agentRows.map((row) => [row.tool, row.tokens, row.tokensIn, row.tokensOut, row.tokensCache]),
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


@needs_node
def test_a_failed_active_time_leaves_no_count_rather_than_a_zero(tmp_path: Path) -> None:
    """`/api/active-time` is one of three requests and can fail on its own.

    Summing an absent `by_tool` map yields 0, which the report would then print
    as "you drove 0 sessions" for a month somebody worked. It also used to empty
    the agent table: every row lost its session count at once, and a session-less
    row was a row nobody could see.
    """
    source = _source()
    body = "\n".join(extract_js_function(source, sig) for sig in MODEL_SIGNATURES) + MODEL_FIXTURE
    report = json.loads(_run_node(tmp_path, "usage-report-model.js", body))

    whole = report["whole"]
    assert whole["sessions"] == 4
    assert whole["agents"] == ["codex", "claude"], "the table prints every ranked tool"
    assert whole["agentSessions"] == [4, None], "an untracked session count is absent, not zero"

    down = report["noActiveTime"]
    assert down["sessions"] is None, "an absent source has no session count, not a count of zero"
    assert down["activeMs"] == 0
    assert down["notices"]["activeTimeMissing"] is True
    assert down["agents"] == ["codex", "claude"], "the ranking still stands on its token columns"
    assert down["agentSessions"] == [None, None]

    # The other half of the same class: no streaks facet is no active-day count.
    assert report["noInsights"]["hasStreaks"] is False
    assert report["noInsights"]["sessions"] == 4


@needs_node
def test_the_first_paint_builds_a_model_over_two_absent_sources(tmp_path: Path) -> None:
    """The hero paints off /api/usage before the other two have been issued.

    Every read of `insights` and `activeTime` in the model is guarded by its
    Missing flag, so the first paint can pass literal nulls. Running it is the
    point: an unguarded read is a TypeError that leaves the tab on its spinner
    forever, which no amount of reading the source proves absent.
    """
    source = _source()
    body = "\n".join(extract_js_function(source, sig) for sig in MODEL_SIGNATURES) + MODEL_FIXTURE
    first = json.loads(_run_node(tmp_path, "usage-report-model.js", body))["firstPaint"]

    assert first["notices"]["pending"] is True, "the banner needs silence told from refusal"
    assert first["sessions"] is None, "no active time yet is not a count of zero"
    assert first["hasStreaks"] is False
    assert first["activeMs"] == 0


@needs_node
def test_the_day_map_shades_by_the_dashboard_quantiles_not_the_served_intensity(tmp_path: Path) -> None:
    """Spec D3: one shading rule for the product, and it is the Stats tab's.

    The fixture serves an `intensity` that ranks the two days backwards. The
    report's levels come from the quantile thresholds over the calendar's token
    counts, so the 300-token day is the darkest, the 10-token day the faintest
    shaded one, and a day with no row is unshaded.
    """
    source = _source()
    body = "\n".join(extract_js_function(source, sig) for sig in MODEL_SIGNATURES) + MODEL_FIXTURE
    whole = json.loads(_run_node(tmp_path, "usage-report-levels.js", body))["whole"]
    assert whole["levels"] == {"2026-09-01": 7, "2026-09-02": 1, "2026-09-03": 0}, whole["levels"]


@needs_node
def test_the_agent_table_never_invents_a_token_split(tmp_path: Path) -> None:
    """Tokens and cost come from the facet scan, the split from /api/usage.

    The two are different scans, and the page must not blur them into one figure.
    Only /api/usage's per-app rows carry output tokens, so a tool with no such row
    (OpenClaw, or here claude) prints a dash for output rather than a borrowed
    or invented one.
    """
    source = _source()
    body = "\n".join(extract_js_function(source, sig) for sig in MODEL_SIGNATURES) + MODEL_FIXTURE
    whole = json.loads(_run_node(tmp_path, "usage-report-split.js", body))["whole"]
    assert whole["agentSplit"] == [
        ["codex", 600, 10, 20, 30],
        ["claude", 300, 40, None, 50],
    ], whole["agentSplit"]


AGENTS_TABLE_SIGNATURES = (
    "function usageReportSortedRows(rows, sort) {",
    "function usageReportAgentRow(row) {",
    "function usageReportRenderAgents(model) {",
)

# The sort state and the set of name columns are page-level constants the
# renderers read. The harness ships the real declarations rather than copies, so
# a changed default is a changed test result and not a silent disagreement with
# the fixture.
def _table_sort_state(source: str) -> str:
    start = source.index("const usageReportTableSort = {")
    end = source.index("};", start) + 2
    names = next(
        line
        for line in source.splitlines()
        if line.strip().startswith("const USAGE_REPORT_NAME_FIELDS")
    )
    return f"{source[start:end]}\n{names.strip()}\n"

# A tbody is a tbody: the harness counts emitted rows rather than reading for a
# literal `slice(0, 10)`, which a cap renamed to `slice(0, 12)` would walk past.
AGENTS_TABLE_FIXTURE = """
const USAGE_REPORT_EM = '\\u2014';
const nodes = [];
function makeNode(tag) {
  const node = {
    tag, children: [], className: '', textContent: '', colSpan: null,
    append(...kids) { kids.forEach((kid) => this.children.push(kid)); },
    appendChild(kid) { this.children.push(kid); return kid; },
    replaceChildren(...kids) { this.children = kids; },
  };
  nodes.push(node);
  return node;
}
const body = makeNode('tbody');
const note = makeNode('p');
const document = {
  createElement: makeNode,
  createDocumentFragment: () => makeNode('fragment'),
  getElementById: (id) => (id === 'usageReportAgentsBody' ? body : note),
};
const t = (key) => key;
const formatNumber = (v) => `n${v}`;
const formatTokenCount = (v) => `t${v}`;
const formatCurrency = (v) => `$${v}`;
const formatDurationUnits = (v) => `d${v}`;
function usageReportFigure(value, format) {
  return value === null || value === undefined ? USAGE_REPORT_EM : format(value);
}
// The real one fetches a brand asset; the row's tool name is all this test needs.
function createToolIdentity(tool) { return makeNode(`identity:${tool}`); }
function usageReportFillNote() {}
function usageReportShowFacetCoverage() { return false; }
function usageReportShare(value) { return String(value); }
const rows = Array.from({ length: 14 }, (_, index) => ({
  tool: `tool-${index}`,
  tokens: 1000 - index,
  cost: index,
  tokensIn: index,
  tokensOut: index,
  tokensCache: index,
  sessions: index === 13 ? null : index,
  activeMs: index === 13 ? null : index * 1000,
  messages: index === 13 ? null : index * 2,
}));
const REPORT = {
  tools: rows, agentRows: rows, tokens: 1000, facetTotal: 1000, projectsAvailable: true,
};
usageReportRenderAgents(REPORT);
const emitted = body.children[0].children;
// Sorted by the name column this time, and ascending: the column a reader
// chose is the order they get, whatever the facet's own ranking was.
usageReportTableSort.agents = { field: 'tool', direction: 'asc' };
usageReportRenderAgents(REPORT);
const byName = body.children[0].children.map(
  (tr) => tr.children[0].children[0].tag,
);
usageReportTableSort.agents = { field: 'cost', direction: 'desc' };
usageReportRenderAgents(REPORT);
const costOrder = body.children[0].children.map(
  (tr) => tr.children[0].children[0].tag,
);
// The no-session tool is looked up by name, not by index. If a cap comes back
// the row-count assertion gets to report it, rather than this harness crashing
// on a fourteenth row that was never printed.
const noSessionRow = emitted.find(
  (tr) => tr.children[0].children[0].tag === 'identity:tool-13',
);
process.stdout.write(JSON.stringify({
  rowCount: emitted.length,
  tags: emitted.map((tr) => tr.children[0].children[0].tag),
  buttonRows: emitted.filter((tr) => tr.children.some((td) => td.tag === 'button')).length,
  emptyCells: noSessionRow
    ? noSessionRow.children.slice(1).map((td) => td.textContent)
    : null,
  // Column order, printed: the overview's token split, then its total, then the
  // activity the report alone serves, then cost and messages.
  topCells: emitted[0].children.slice(1).map((td) => td.textContent),
  rowCells: emitted[1].children.slice(1).map((td) => td.textContent),
  byName,
  costOrder,
}));
"""


@needs_node
def test_the_agent_table_prints_every_tool_it_was_given(tmp_path: Path) -> None:
    """The table used to headline ten tools and park the rest behind an "Other
    tools on this machine" disclosure. On a machine with a long tail that read as
    a table of the same ten names, so the fold is gone.

    Executed, because the fold came back the moment anybody capped the loop: the
    report renders fourteen tools and demands fourteen rows, in rank order, with
    no fold row anywhere in the emitted tbody.
    """
    source = _source()
    block = _report_block(source)
    body = "\n".join(extract_js_function(block, sig) for sig in AGENTS_TABLE_SIGNATURES)
    body += _table_sort_state(source)
    body += AGENTS_TABLE_FIXTURE
    table = json.loads(_run_node(tmp_path, "usage-report-agents.js", body))

    assert table["rowCount"] == 14, "one row per ranked tool, with nothing folded away"
    assert len(table["topCells"]) == 8, "nine columns, one cell each after the name"
    assert table["tags"] == [f"identity:tool-{index}" for index in range(14)], "rank order holds"
    # The default sort is the facet's own: biggest token count first.
    assert table["costOrder"][0] == "identity:tool-13", "cost descending ranks the dear tool first"
    assert table["costOrder"][-1] == "identity:tool-0"
    # Plain code-unit order, not a locale collation with numeric awareness: the
    # page compares names with localeCompare's default options, and so does this.
    expected_names = sorted(f"identity:tool-{index}" for index in range(14))
    assert table["byName"] == expected_names, (
        "clicking the name column re-ranks the same fourteen rows by name"
    )
    assert table["buttonRows"] == 0, "the disclosure button is gone, not merely closed"
    # The last tool has no recorded session, so the two columns that come from
    # activity go blank -- which is not the same fact as a zero, and is exactly
    # why the fold used to hide such rows. Its tokens and cost still print.
    assert table["topCells"] == ["t0", "t0", "t0", "t1000", "n0", "d0", "\u2014", "n0"], table["topCells"]
    assert table["rowCells"] == ["t1", "t1", "t1", "t999", "n1", "d1000", "$1", "n2"], table["rowCells"]
    # The last tool recorded no session, so its activity and message columns are
    # blank rather than zero -- and nothing was priced for it, so cost is blank too.
    assert table["emptyCells"] == [
        "t13", "t13", "t13", "t987", "\u2014", "\u2014", "$13", "\u2014",
    ], table["emptyCells"]


# --- the three ranking tables, sorted by whatever column the reader picks ----


def _report_table_headers(source: str, table_id: str) -> list[str]:
    """The ``<th>`` cells of one report table, markup and all."""
    start = source.index(f'id="{table_id}"')
    thead = source[source.index("<thead>", start) : source.index("</thead>", start)]
    return re.findall(r"<th\b.*?</th>", thead, re.S)


def test_every_column_of_every_ranking_table_is_a_sort_control() -> None:
    """Three ranking tables, and every column of each one is clickable.

    A header without a ``data-report-sort`` is a column a reader can click and
    get nothing from, and a header that carries two labels is the old "cost, and
    by the way this is list price" column again. Both look fine until the page is
    up, so the markup is pinned here: one sortable cell, one sort field, one
    translatable label, per column.
    """
    source = _source()
    tables = re.findall(r'data-report-table="(\w+)"', source)
    assert tables == ["agents", "models", "projects"], tables

    state_start = source.index("const usageReportTableSort = {")
    state = source[state_start : source.index("};", state_start)]
    for key, table_id in zip(tables, (
        "usageReportAgentsTable",
        "usageReportModelsTable",
        "usageReportProjectsTable",
    )):
        assert f"{key}: {{ field:" in state, f"{key} has no sort slot to remember a click"
        headers = _report_table_headers(source, table_id)
        assert headers, table_id
        fields = []
        for th in headers:
            assert 'class="is-sortable"' in th, (table_id, th)
            assert 'aria-sort="none"' in th, (table_id, th)
            match = re.search(r'data-report-sort="([^"]+)"', th)
            assert match, (table_id, th)
            fields.append(match.group(1))
            assert th.count("data-i18n=") == 1, (table_id, th)
        assert len(fields) == len(set(fields)), (table_id, fields)

    # The click path: wired once, and a click repaints all three tables so the
    # arrow on the header a reader just used is the arrow on the rows below it.
    block = _report_block(source)
    wire = extract_js_function(block, "function usageReportWireTableSort() {")
    assert "th[data-report-sort]" in wire
    assert "usageReportRenderTables(usageReportState.model)" in wire
    assert "usageReportWireTableSort();" in extract_js_function(block, "function usageReportWire() {")


def test_the_report_tables_speak_the_overview_vocabulary() -> None:
    """Two pages, one set of column names, in that order.

    The report used to say "In" where the overview said "Input" and "Tokens" where
    it said "Total", and qualified cost with an API-list-price footnote. Every
    header now carries the key the overview tables carry, in the order the overview
    prints them, so the same word cannot mean two things depending on the tab a
    reader is on. The active-time column is an estimate, and its header tooltip is
    where the report admits it.
    """
    source = _source()
    expected = {
        "usageReportAgentsTable": (
            "tool", "input", "output", "cache", "total", "sessions", "active", "cost", "msgs",
        ),
        "usageReportModelsTable": ("model", "total", "share", "cost", "msgs"),
        "usageReportProjectsTable": ("project", "total", "share", "cost", "msgs"),
    }
    for table_id, labels in expected.items():
        found = []
        for th in _report_table_headers(source, table_id):
            match = re.search(r'data-i18n="([^"]+)"', th)
            assert match, (table_id, th)
            found.append(match.group(1))
        assert tuple(found) == labels, (table_id, found)

    active = next(
        th for th in _report_table_headers(source, "usageReportAgentsTable")
        if 'data-report-sort="activeMs"' in th
    )
    assert 'data-i18n-title="activeTimeHint"' in active, "the active column stopped carrying the estimate caveat"


@needs_node
def test_the_share_column_ranks_because_the_row_carries_the_share_it_prints(
    tmp_path: Path,
) -> None:
    """A sortable column needs a field, not a figure invented at paint time.

    The by-model and by-project tables show a share the facet does not serve, so it
    is folded onto the row where the sort can read it. Executed because the failure
    is silent: without the field every row ties, and the column "works" by handing
    back the name order.
    """
    source = _source()
    body = "\n".join(extract_js_function(source, sig) for sig in MODEL_SIGNATURES) + MODEL_FIXTURE
    model = json.loads(_run_node(tmp_path, "usage-report-share-field.js", body))["whole"]

    assert model["modelNames"] == ["alpha", "beta"], "a ranked model with no tokens gets no row"
    assert model["modelShares"] == [pytest.approx(600 / 900), pytest.approx(300 / 900)]
    assert model["projectNames"] == ["one", "two"]
    assert model["projectShares"] == [pytest.approx(500 / 900), pytest.approx(300 / 900)]
    # A facet that did not answer has no total to divide by, and no rows to sort.
    down = json.loads(_run_node(tmp_path, "usage-report-share-field.js", body))["noInsights"]
    assert down["modelNames"] == [] and down["modelShares"] == []
    assert down["projectNames"] == [] and down["projectShares"] == []


FACET_TABLE_SIGNATURES = (
    "function usageReportSortedRows(rows, sort) {",
    "function usageReportNoticeRow(colSpan, message) {",
    "function usageReportFacetRow(name, row) {",
    "function usageReportRenderModels(model) {",
    "function usageReportRenderProjects(model) {",
)

FACET_TABLE_FIXTURE = """
const USAGE_REPORT_EM = '\\u2014';
function makeNode(tag, id) {
  const node = {
    id, tag, children: [], className: '', textContent: '', colSpan: null,
    append(...kids) { kids.forEach((kid) => this.children.push(kid)); },
    appendChild(kid) { this.children.push(kid); return kid; },
    replaceChildren(...kids) { this.children = kids; },
  };
  return node;
}
const bodies = {
  usageReportModelsBody: makeNode('tbody', 'usageReportModelsBody'),
  usageReportProjectsBody: makeNode('tbody', 'usageReportProjectsBody'),
};
const notes = {
  usageReportModelsNote: makeNode('p', 'usageReportModelsNote'),
  usageReportProjectsNote: makeNode('p', 'usageReportProjectsNote'),
};
const filled = {};
const document = {
  createElement: (tag) => makeNode(tag),
  createDocumentFragment: () => makeNode('fragment'),
  getElementById: (id) => bodies[id] || notes[id],
};
const t = (key, params) => (params ? `${key}:${JSON.stringify(params)}` : key);
const formatNumber = (v) => `n${v}`;
const formatTokenCount = (v) => `t${v}`;
const formatCurrency = (v) => `$${v}`;
function usageReportFigure(value, format) {
  return value === null || value === undefined ? USAGE_REPORT_EM : format(value);
}
function usageReportShare(fraction) {
  return fraction === null || fraction === undefined ? USAGE_REPORT_EM : `${Math.round(fraction * 1000) / 10}%`;
}
function usageReportFillNote(el, lines) { filled[el.id] = lines.map((line) => line.text); }

const rows = [
  { model: 'alpha', project: 'alpha', tokens: 100, messages: 10, cost: 1, share: 0.1 },
  { model: 'beta', project: 'beta', tokens: 900, messages: 5, cost: 40, share: 0.9 },
  { model: 'gamma', project: 'gamma', tokens: 400, messages: 90, cost: 0, share: 0.4 },
];
const REPORT = {
  models: rows, modelRows: rows,
  projectsAvailable: true, projectRows: rows,
  facetTotal: 1000, unattrShare: 0.011,
};
// A cell's text may sit on a child label, the way the real row nests it.
const textOf = (node) => node.textContent || (node.children || []).map(textOf).join('');
const cells = (tbody) => tbody.children[0].children.map((tr) => tr.children.map((td) => textOf(td)));
const names = (tbody) => cells(tbody).map((row) => row[0]);

usageReportRenderModels(REPORT);
usageReportRenderProjects(REPORT);
const out = {
  modelsDefault: cells(bodies.usageReportModelsBody),
  projectsDefault: names(bodies.usageReportProjectsBody),
  notes: { ...filled },
};

// The reader clicked the cost column, then the share column ascending: the
// column they chose is the order they get, not the facet's ranking of tokens.
usageReportTableSort.models = { field: 'cost', direction: 'desc' };
usageReportRenderModels(REPORT);
out.byCost = names(bodies.usageReportModelsBody);
usageReportTableSort.models = { field: 'share', direction: 'asc' };
usageReportRenderModels(REPORT);
out.byShare = names(bodies.usageReportModelsBody);
usageReportTableSort.models = { field: 'messages', direction: 'asc' };
usageReportRenderModels(REPORT);
out.byMessages = names(bodies.usageReportModelsBody);

// A facet an older server never served: one notice spanning the table, not a
// header over an empty grid.
usageReportTableSort.models = { field: 'tokens', direction: 'desc' };
usageReportRenderModels({ models: null, modelRows: [], facetTotal: 0 });
usageReportRenderProjects({ projectsAvailable: false, projectRows: [], facetTotal: 0 });
const noticeRow = bodies.usageReportModelsBody.children[0];
const notice = noticeRow.children[0];
out.notice = { cells: noticeRow.children.length, colSpan: notice.colSpan, text: notice.textContent };
out.projectsNotice = bodies.usageReportProjectsBody.children[0].children[0].textContent;

// A facet that answered with nothing for this window: one notice row, not a
// header over an empty grid, and not the "never served" notice either.
usageReportRenderModels({ models: [], modelRows: [], facetTotal: 0 });
const emptyRow = bodies.usageReportModelsBody.children[0].children[0];
out.emptyModels = { cells: emptyRow.children.length, colSpan: emptyRow.children[0].colSpan, text: emptyRow.children[0].textContent };

process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_the_model_and_project_tables_rank_on_the_column_the_header_names(
    tmp_path: Path,
) -> None:
    """Two new tables, run rather than read, because both halves fail quietly.

    The rows are the facet's four figures with the share the reader clicked kept
    in step with them, and a facet an older server does not serve has to say so in
    the table it is missing from instead of printing an empty one.
    """
    source = _source()
    block = _report_block(source)
    body = "\n".join(extract_js_function(block, sig) for sig in FACET_TABLE_SIGNATURES)
    body += _table_sort_state(source) + FACET_TABLE_FIXTURE
    out = json.loads(_run_node(tmp_path, "usage-report-facet-tables.js", body))

    assert [row[0] for row in out["modelsDefault"]] == ["beta", "gamma", "alpha"], "tokens desc is the default"
    # The dear model leads a cost ranking even though it is not the biggest one,
    # and a model nothing was priced for prints a dash rather than $0.00.
    assert out["byCost"] == ["beta", "alpha", "gamma"], out["byCost"]
    assert out["byShare"] == ["alpha", "gamma", "beta"], "the share column ranks by the share it shows"
    assert out["byMessages"] == ["beta", "alpha", "gamma"], "message count is its own ranking"
    assert out["modelsDefault"][0] == ["beta", "t900", "90%", "$40", "n5"], out["modelsDefault"][0]
    assert out["modelsDefault"][1] == ["gamma", "t400", "40%", "\u2014", "n90"]
    assert out["modelsDefault"][2] == ["alpha", "t100", "10%", "$1", "n10"]
    assert out["projectsDefault"] == ["beta", "gamma", "alpha"], "the project table sorts the same way"
    # The difference between the table and the hero is the period's own usage, and
    # the note under the project table is where it is admitted.
    assert out["notes"]["usageReportProjectsNote"] == ['usageReportUnattributed:{"pct":"1.1%"}']
    assert out["notes"]["usageReportModelsNote"] == []
    assert out["notice"] == {
        "cells": 1,
        "colSpan": 5,
        "text": 'usageReportFacetsMissing:{"facets":"models"}',
    }
    assert out["projectsNotice"] == 'usageReportFacetsMissing:{"facets":"projects"}'
    assert out["emptyModels"] == {"cells": 1, "colSpan": 5, "text": "usageReportEmptyPeriod"}


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


def _facets_line(block: str) -> str:
    """The tab's facet constant as the page declares it."""
    return next(
        line.strip() for line in block.splitlines()
        if line.strip().startswith("const USAGE_REPORT_FACETS = ")
    )


def _loader_program(block: str) -> str:
    """The loader as the page defines it, with the one helper it now asks first.

    `usageReportSummaryWanted` is not stubbed: whether the two slow requests are
    owed is exactly what these tests are about, so a stub would be the assertion.
    The facet constant is the page's own, so the URLs it builds are the real ones.
    """
    return (LOADER_HARNESS
            .replace("__FACETS__", _facets_line(block))
            .replace("__SUMMARY_WANTED__", extract_js_function(
                block, "function usageReportSummaryWanted() {"))
            .replace("__LOADER__", extract_js_function(
                block, "async function loadUsageReport(options = {}) {")))


LOADER_HARNESS = """
const usageReportState = {
  loading: false, loaded: false, error: null, model: null,
  period: 'month', back: 0, serverId: 'local', staleReread: false,
  active: true, summaryKey: '', summaryLoading: false, usagePayload: null,
  loadToken: 0, summaryToken: 0, staleTimer: null,
};
const calls = [];
const urls = [];
const scheduled = [];
const cleared = [];
clearTimeout = (id) => { cleared.push(id); };
let responder = () => null;
// The Overview tab's review-session toggle, switched off. The report pins the
// filter open, so it must never be what the request carries.
let includeCodexReviewSessions = false;
function usageReportWire() {}
function usageReportRenderControls() {}
function usageReportRenderLoading() { calls.push('renderLoading'); }
function usageReportServer() { return { id: 'local', label: 'local' }; }
function usageReportLoadVersion() {}
function usageReportWindows() { return { date_from: '2026-09-01', date_to: '2026-09-05' }; }
function usageReportSummaryKey() { return 'local|2026-09-01|2026-09-05'; }
function usageReportBuildModel(args) { return { notices: {}, args }; }
function usageReportRenderPartial() { calls.push('partial'); }
function usageReportRender() { calls.push('render'); }
function usageReportScheduleStaleReread(payloads) {
  calls.push('scheduleStale');
  scheduled.push(payloads.map((payload) => (payload && payload.path) || null));
}
// The speed view rides the same window, so the loader invalidates it. These stubs
// keep this harness about the loader's own control flow; the speed table is tested in
// tests/test_output_speed_frontend.py.
const document = { hidden: false };
const usageReportSpeedState = { subtab: 'summary' };
function usageReportSpeedInvalidate() { calls.push('speedInvalidate'); }
// Recorded unconditionally: the report's own load never dispatches the speed
// page, whatever that page's gate would have done with the call.
function usageReportLoadSpeed() { calls.push('loadSpeed'); }
function usageReportEnsureSummary() { calls.push('ensureSummary'); }
function usageReportApplyPendingChoice() { calls.push('applyPending'); }
function fetchJsonWithRetry(server, url) {
  calls.push('fetch:' + String(url).split('?')[0]);
  urls.push(String(url));
  return Promise.resolve(responder(url));
}
__FACETS__
__SUMMARY_WANTED__
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

  // On the summary subtab the slow pair is asked for, and speed is not.
  out.summaryCalls = calls.slice();

  // On the speed subtab, neither: the pair is the panel they are not looking at,
  // and speed waits for the request that synchronises the logs.
  calls.length = 0;
  usageReportState.loaded = false;
  usageReportState.active = false;
  await loadUsageReport({ refresh: true });
  out.speedCalls = calls.slice();
  out.summaryKeyWhileDeferred = usageReportState.summaryKey;

  // A load already in flight when the reader left: same deferral, same reason. The
  // page cannot show the answer, and `usageReportResumePending` asks on the way back.
  calls.length = 0;
  usageReportState.active = true;
  usageReportState.active = false;
  await loadUsageReport();
  out.leftMidFlightCalls = calls.slice();

  // One whole load with the page on screen, then the same window refreshed:
  // every URL it sends, the payloads it hands the stale check, and the re-read
  // timer it retires on the way in.
  calls.length = 0;
  urls.length = 0;
  usageReportState.active = true;
  usageReportState.staleTimer = 41;
  const tokenBefore = usageReportState.loadToken;
  responder = (url) => ({ path: String(url).split('?')[0] });
  await loadUsageReport();
  out.whole = {
    urls: urls.slice(), calls: calls.slice(), scheduled: scheduled.slice(),
    cleared: cleared.slice(), staleTimer: usageReportState.staleTimer,
    tokenStep: usageReportState.loadToken - tokenBefore,
  };
  urls.length = 0;
  await loadUsageReport({ refresh: true });
  out.refreshedUrls = urls.slice();

  // A load that throws part-way still settles a queued choice, and only once.
  calls.length = 0;
  responder = () => { throw new Error('a model bug'); };
  await loadUsageReport();
  out.thrown = {
    calls: calls.slice(), loading: usageReportState.loading, error: !!usageReportState.error,
  };
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


@needs_node
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
        extract_js_function(_source(), sig)
        for sig in (
            "function formatDateKey(date) {",
            "function startOfWeekMonday(date) {",
            "function usageReportWindows(period, back = 0) {",
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
const usageReportState = {
  staleTimer: null, staleReread: null, loadToken: 3, back: 0, active: true,
};
const document = { hidden: false };
let nextId = 1;
const armed = new Map();
const cleared = [];
setTimeout = (fn, ms) => { const id = nextId++; armed.set(id, { fn, ms }); return id; };
clearTimeout = (id) => { cleared.push(id); armed.delete(id); };
const reread = [];
function usageReportSilentReread(token, period, back, serverId) { reread.push([token, period, back, serverId]); }
const USAGE_REPORT_STALE_REREAD_MS = 12000;
__SERVED__
__SCHEDULE__
__ARM__
__FIRE__
const fresh = { response_cache: { status: 'hit' } };
const stale = { response_cache: { status: 'stale' } };
const bare = {};                       // a route that serves no cache metadata
const server = { id: 'local' };
const out = {};

// Nothing stale: nothing armed, and nothing left waiting to be armed later.
usageReportScheduleStaleReread([fresh, fresh, fresh], 'month', 0, server);
out.allFresh = armed.size;
out.intentWhenFresh = usageReportState.staleReread;

// A route with no metadata must not be guessed at.
usageReportScheduleStaleReread([bare, bare, bare], 'month', 0, server);
out.noMetadata = armed.size;

// Only the SLOW pair is stale -- usage is already fresh. This is the case that
// keying the decision on /api/usage alone used to miss entirely.
usageReportScheduleStaleReread([fresh, stale, fresh], 'month', 0, server);
out.slowPairStale = armed.size;
const firstId = usageReportState.staleTimer;

// A second schedule must retire the first timer, not stack on it.
usageReportScheduleStaleReread([fresh, fresh, stale], 'month', 0, server);
out.afterSecond = { armed: armed.size, clearedFirst: cleared.includes(firstId) };
out.delay = armed.get(usageReportState.staleTimer).ms;

// The timer goes off with the page out of sight: nothing is read, and the reason
// for reading it is not thrown away.
document.hidden = true;
armed.get(usageReportState.staleTimer).fn();
out.whileHidden = { reread: reread.length, waiting: !!usageReportState.staleReread };

// The reader comes back, and the re-read they left behind runs.
document.hidden = false;
usageReportFireStaleReread();
out.fired = reread;
out.intentAfterFire = usageReportState.staleReread;
out.handleCleared = usageReportState.staleTimer === null;
process.stdout.write(JSON.stringify(out));
"""


@needs_node
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
        .replace("__SERVED__", extract_js_function(
            block, "function usageReportServedStale(payload) {"))
        .replace("__SCHEDULE__", extract_js_function(
            block, "function usageReportScheduleStaleReread(payloads, period, back, server) {"))
        .replace("__ARM__", extract_js_function(block, "function usageReportArmStaleReread() {"))
        .replace("__FIRE__", extract_js_function(block, "function usageReportFireStaleReread() {"))
    )
    out = json.loads(_run_node(tmp_path, "usage-report-schedule.js", body))

    assert out["allFresh"] == 0, "nothing stale, nothing to re-read"
    assert out["intentWhenFresh"] is None, "a fresh report left a re-read waiting"
    assert out["noMetadata"] == 0, "a route that says nothing is treated as fresh"
    assert out["slowPairStale"] == 1, (
        "a stale insights/active-time must arm the re-read even when usage is fresh"
    )
    assert out["afterSecond"] == {"armed": 1, "clearedFirst": True}, "timers stacked"
    assert out["delay"] == 12000
    assert out["whileHidden"] == {"reread": 0, "waiting": True}, (
        "a re-read fired at a hidden page, or its intent was discarded with it"
    )
    assert out["fired"] == [[3, "month", 0, "local"]], "the token is captured at schedule time"
    assert out["intentAfterFire"] is None, "a re-read that ran stays queued"
    assert out["handleCleared"] is True


REREAD_HARNESS = """
const usageReportState = {
  loading: false, loaded: true, error: null, model: { tag: 'original' },
  period: 'month', back: 0, serverId: 'local', staleTimer: null, loadToken: 7,
};
let mode = 'ok';
let onRequest = null;
const renders = [];
const asked = [];
function usageReportServer() { return { id: 'local' }; }
function usageReportWindows() { return { date_from: '2026-09-01', date_to: '2026-09-05' }; }
function usageReportBuildModel() { return { tag: 'reread' }; }
function usageReportRender() { renders.push(usageReportState.model.tag); }
__FACETS__
function fetchJsonWithRetry(server, url) {
  asked.push(String(url));
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
  await usageReportSilentReread(7, 'month', 0, 'local');
  out.afterFailure = snap();

  mode = 'null';
  await usageReportSilentReread(7, 'month', 0, 'local');
  out.afterNull = snap();

  // The reader switches period while the round trip is in flight.
  mode = 'ok';
  onRequest = () => { usageReportState.period = 'year'; };
  await usageReportSilentReread(7, 'month', 0, 'local');
  out.afterPeriodSwitch = snap();
  onRequest = null;
  usageReportState.period = 'month';

  // An explicit load ran and bumped the token.
  await usageReportSilentReread(6, 'month', 0, 'local');
  out.afterStaleToken = snap();

  // Nothing changed: it commits.
  await usageReportSilentReread(7, 'month', 0, 'local');
  out.afterSuccess = snap();
  out.asked = asked;
  process.stdout.write(JSON.stringify(out));
})();
"""


@needs_node
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
        .replace("__FACETS__", _facets_line(block))
        .replace("__REREAD__", extract_js_function(
            block, "async function usageReportSilentReread(token, period, back, serverId) {"))
        .replace("__WANTS__", extract_js_function(
            block, "function usageReportStillWants(token, period, back, serverId) {"))
    )
    out = json.loads(_run_node(tmp_path, "usage-report-reread.js", body))

    for case in ("afterFailure", "afterNull", "afterPeriodSwitch", "afterStaleToken"):
        assert out[case]["model"] == "original", f"{case} overwrote a good report"
        assert out[case]["renders"] == 0, f"{case} repainted the page"

    assert out["afterSuccess"]["model"] == "reread", "a clean re-read must commit"
    assert out["afterSuccess"]["renders"] == 1

    # It must never take the loading flag: that is what swallowed period clicks and
    # made Retry a no-op during a refresh the reader could not see.
    asked = out.pop("asked")
    for case in out.values():
        assert case["loading"] is False

    # The server is already recomputing a stale entry, so the re-read reads it
    # again rather than forcing a second recompute with refresh=1. And it asks for
    # the whole facet set: the facets are folded out of one scan on the server.
    facets = _facets_line(block).split("'")[1]
    assert asked, "the re-read never asked for anything"
    for url in asked:
        query = parse_qs(urlsplit(url).query)
        assert "refresh" not in query and "period" not in query, url
        assert query["date_from"] == ["2026-09-01"] and query["date_to"] == ["2026-09-05"], url
        if url.startswith("/api/insights"):
            assert query["facets"] == [facets], url


@needs_node
def test_an_empty_200_does_not_wedge_the_report_tab(tmp_path: Path) -> None:
    """`fetchJson` resolves to null when an OK response carries no JSON body.

    Sequencing moved the first dereference of that value inside the window where
    `loading` is true, so the TypeError left the flag set and the guard at the top
    of `loadUsageReport` then refused every later load -- period buttons, server
    switch and Retry alike -- until a page reload. Executed rather than asserted
    against the source, because the bug is control flow, not a string.
    """
    block = _report_block(_source())
    out = json.loads(_run_node(tmp_path, "usage-report-loader.js", _loader_program(block)))

    assert out["afterNull"]["loading"] is False, "an empty 200 left the tab wedged"
    assert out["afterNull"]["loaded"] is True
    assert out["recovered"] is True, "the tab refused every load after the first failure"
    assert out["loadingAtRest"] is False


@needs_node
def test_a_load_asks_three_endpoints_for_its_own_calendar_window(tmp_path: Path) -> None:
    """What one load sends, read off the requests it makes.

    Each of the three endpoints is asked for the window by explicit dates, never
    by `period=`: the server's `period=week` is a trailing seven days, and the
    report labels its window with calendar words (spec 3). The active-time
    request pins the Codex review-session filter open rather than inheriting the
    Overview tab's toggle, since the scope line claims every agent session. The
    insights request names the whole facet set, once: the server folds every
    facet out of one scan, so a second request for a subset buys that scan again.

    /api/usage alone fills the hero, so it is awaited and painted before the slow
    two are issued: fired together they contend for the server's GIL and a 2 s
    request became a 13 s one. (That the slow two still overlap each other is the
    summary-race test's control.) The stale check then sees all three payloads,
    because the slow pair is the one most often served stale.
    """
    block = _report_block(_source())
    out = json.loads(_run_node(tmp_path, "usage-report-requests.js", _loader_program(block)))
    whole = out["whole"]
    facets = _facets_line(block).split("'")[1]

    paths = [urlsplit(url).path for url in whole["urls"]]
    assert paths == ["/api/usage", "/api/insights", "/api/active-time"], whole["urls"]
    for url in whole["urls"]:
        query = parse_qs(urlsplit(url).query)
        assert query["date_from"] == ["2026-09-01"] and query["date_to"] == ["2026-09-05"], url
        assert "period" not in query, f"a trailing-window alias reached the server: {url}"
        assert "refresh" not in query, url
    insights = parse_qs(urlsplit(whole["urls"][1]).query)
    assert insights["facets"] == [facets], whole["urls"][1]
    assert {"hourly", "weekday", "tools", "models", "projects"} <= set(facets.split(",")), (
        "the sections below the day map are facet-fed; a missing facet is a hole"
    )
    active = parse_qs(urlsplit(whole["urls"][2]).query)
    assert active["include_review_sessions"] == ["true"], whole["urls"][2]

    calls = whole["calls"]
    assert calls.index("fetch:/api/usage") < calls.index("partial") < calls.index("fetch:/api/insights"), calls
    assert calls.index("partial") < calls.index("fetch:/api/active-time"), calls
    assert whole["scheduled"][-1] == ["/api/usage", "/api/insights", "/api/active-time"], (
        "the stale check must see the slow pair, not just the fastest request"
    )
    # An explicit load retires a silent re-read still queued: its timer is
    # cleared, and the token a late answer is checked against moves on.
    assert whole["cleared"][-1] == 41 and whole["staleTimer"] is None, whole
    assert whole["tokenStep"] == 1
    assert calls.count("applyPending") == 1

    refreshed = [parse_qs(urlsplit(url).query) for url in out["refreshedUrls"]]
    assert len(refreshed) == 3 and all(query["refresh"] == ["1"] for query in refreshed), out["refreshedUrls"]

    # Every exit settles a choice queued mid-flight, the throwing one included.
    thrown = out["thrown"]
    assert thrown["loading"] is False and thrown["error"] is True, thrown
    assert thrown["calls"].count("applyPending") == 1, thrown["calls"]


@needs_node
def test_leaving_report_defers_summary_and_never_dispatches_speed(tmp_path: Path) -> None:
    block = _report_block(_source())
    out = json.loads(_run_node(tmp_path, "usage-report-defer.js", _loader_program(block)))
    summary, deferred = out["summaryCalls"], out["speedCalls"]
    assert "fetch:/api/usage" in summary and "fetch:/api/insights" in summary
    assert "fetch:/api/active-time" in summary
    assert "loadSpeed" not in summary
    assert "fetch:/api/usage" in deferred
    assert "fetch:/api/insights" not in deferred and "fetch:/api/active-time" not in deferred
    assert "loadSpeed" not in deferred
    assert out["summaryKeyWhileDeferred"] == "", (
        "a deferred summary must not record itself as answered"
    )

    left = out["leftMidFlightCalls"]
    assert "fetch:/api/usage" in left, "the load the reader started is not abandoned"
    assert "fetch:/api/insights" not in left and "fetch:/api/active-time" not in left, (
        f"a load continued into the expensive half after the page was left: {left}"
    )
    for calls in (summary, deferred, left):
        assert not [call for call in calls if call.startswith("fetch:/api/output-speed")], calls
        assert "speedInvalidate" not in calls, "the report's load charged the speed page"


SUMMARY_RACE_HARNESS = """
const usageReportState = {
  loading: false, loaded: false, error: null, model: null,
  period: 'month', back: 0, serverId: 'local', staleTimer: null, staleReread: null,
  loadToken: 0, summaryToken: 0, summaryKey: '', summaryLoading: false,
  usagePayload: null, active: true,
};
const document = { hidden: false };
const usageReportSpeedState = { subtab: 'summary' };
// One cursor is the whole window. Moving it is what a period chip does, and the
// pair's loader reads the key off it when it publishes rather than when it asks.
let CURRENT = '2026-09-01';
const RANGES = {
  '2026-09-01': { date_from: '2026-09-01', date_to: '2026-09-05' },
  '2026-09-08': { date_from: '2026-09-08', date_to: '2026-09-12' },
  '2026-10-01': { date_from: '2026-10-01', date_to: '2026-10-05' },
};
function usageReportWindows() { return RANGES[CURRENT]; }
function usageReportServer() { return { id: 'local' }; }
function usageReportBuildModel(args) {
  // The hero's marker is what tells a pair that answered for the current window
  // out of one that answered for a payload that window has since replaced.
  return {
    tag: `${args.dateFrom}..${args.dateTo}`,
    hero: args.usage?.marker ?? null,
    scan: args.insights?.facets || '',
  };
}
function usageReportWire() {}
function usageReportRenderControls() {}
function usageReportRenderLoading() {}
function usageReportRenderPartial() {}
function usageReportRender() {}
function usageReportScheduleStaleReread() {}
function usageReportApplyPendingChoice() {}
function usageReportLoadVersion() {}
function usageReportSpeedInvalidate() {}
function usageReportLoadSpeed() {}
const USAGE_REPORT_FACETS = 'daily';
// The two slow requests are parked until the test releases the window they were
// asked for, which is the only way to reproduce a round trip that outlives it.
// The release keys off the dates in the URL, so a request that failed to carry
// its window would strand here rather than pass by accident.
const parked = [];
let usageSerial = 0;
function fetchJsonWithRetry(server, url) {
  const path = String(url).split('?')[0];
  if (path === '/api/usage') return Promise.resolve({ ok: true, marker: 'hero-' + (usageSerial += 1) });
  const from = (/date_from=([^&]*)/.exec(String(url)) || [])[1] || '';
  return new Promise((resolve) => {
    parked.push({ from, resolve: () => resolve({ facets: 'facet-scan@' + from }) });
  });
}
const pump = () => new Promise((resolve) => setTimeout(resolve, 0));
// `first` and `last` pick which of two same-window pairs answers: a load and a
// pair already in flight ask for the same dates, and only the order tells them
// apart.
async function settle(from, take) {
  const indices = [];
  for (let index = 0; index < parked.length; index += 1) {
    if (parked[index].from === from) indices.push(index);
  }
  let chosen = indices;
  if (take === 'first') chosen = indices.slice(0, 2);
  if (take === 'last') chosen = indices.slice(-2);
  const mine = chosen.map((index) => parked[index]);
  for (let index = chosen.length - 1; index >= 0; index -= 1) parked.splice(chosen[index], 1);
  mine.forEach((entry) => entry.resolve());
  for (let index = 0; index < 16; index += 1) await pump();
  return mine.length;
}
__SUMMARY_WANTED__
__SUMMARY_KEY__
__ENSURE_SUMMARY__
__LOADER__
(async () => {
  const out = {};
  const tag = () => usageReportState.model?.tag ?? null;

  // Control: a pair that lands with its window still up must publish, or the
  // two guards below could both be satisfied by never publishing at all.
  let load = loadUsageReport();
  for (let index = 0; index < 8; index += 1) await pump();
  out.controlReleased = await settle('2026-09-01');
  await load;
  out.control = tag();

  // A pair asked for window A while the reader was on the speed subtab; they
  // move to window B; only then does A's answer arrive.
  usageReportState.active = false;
  CURRENT = '2026-09-01';
  usageReportState.period = 'month';
  load = loadUsageReport();
  for (let index = 0; index < 8; index += 1) await pump();
  await load;
  out.deferredKey = usageReportState.summaryKey;
  usageReportState.active = true;
  usageReportEnsureSummary();
  for (let index = 0; index < 8; index += 1) await pump();
  out.inFlightForA = parked.length;
  out.loadingA = usageReportState.summaryLoading;

  CURRENT = '2026-09-08';
  usageReportState.period = 'week';
  load = loadUsageReport();
  for (let index = 0; index < 8; index += 1) await pump();
  out.windowBReleased = await settle('2026-09-08');
  await load;
  out.windowB = tag();

  out.lateAReleased = await settle('2026-09-01');
  out.afterLateAnswer = { tag: tag(), summaryLoading: usageReportState.summaryLoading };

  // The window moving by a path that never starts a load: nothing bumped the
  // pair's token, so only the window the answer is for can catch it.
  usageReportState.summaryKey = '';
  usageReportState.model = { tag: 'KEEP-ME' };
  CURRENT = '2026-09-08';
  usageReportState.period = 'week';
  usageReportEnsureSummary();
  for (let index = 0; index < 8; index += 1) await pump();
  out.inFlightForKeyCase = parked.length;
  CURRENT = '2026-10-01';
  usageReportState.period = 'year';
  out.movedReleased = await settle('2026-09-08');
  out.afterWindowMove = {
    tag: tag(), summaryLoading: usageReportState.summaryLoading,
    summaryKey: usageReportState.summaryKey,
  };

  // The window the reader is actually on is still owed its figures.
  usageReportEnsureSummary();
  for (let index = 0; index < 8; index += 1) await pump();
  out.reaskedFor = parked.map((entry) => entry.from);

  // A Refresh re-reads the window already on screen, so the pair in flight was
  // asked against the hero this load replaces. Neither the dates nor the server
  // have moved, so only the pair's own token can retire it.
  parked.length = 0;
  CURRENT = '2026-10-01';
  usageReportState.period = 'year';
  usageReportState.summaryKey = '';
  usageReportState.summaryLoading = false;
  usageReportState.model = { tag: '2026-10-01..2026-10-05', hero: 'before', scan: '' };
  usageReportState.usagePayload = { ok: true, marker: 'hero-stale' };
  usageReportEnsureSummary();
  for (let index = 0; index < 8; index += 1) await pump();
  out.inFlightForRefresh = parked.length;
  load = loadUsageReport({ refresh: true });
  for (let index = 0; index < 8; index += 1) await pump();
  out.refreshReleased = await settle('2026-10-01', 'last');
  await load;
  out.afterRefresh = { tag: tag(), hero: usageReportState.model?.hero ?? null };
  out.staleReleased = await settle('2026-10-01', 'first');
  out.afterStalePair = {
    tag: tag(), hero: usageReportState.model?.hero ?? null,
    summaryLoading: usageReportState.summaryLoading,
  };
  process.stdout.write(JSON.stringify(out));
})();
"""


@needs_node
def test_a_deferred_summary_cannot_answer_for_a_window_that_moved(tmp_path: Path) -> None:
    """The Summary pair is the half of a load that can outlive it.

    Its callback builds a whole model rather than merging one figure in, so an
    answer for the window being replaced finishes by overwriting the new window's
    model with the old window's facet scan: last month's rankings under this
    month's title, with nothing else about the page visibly wrong.

    Two guards cover two different paths. An explicit load retires the pair with
    its token; an answer whose window moved by a path that never started a load
    is caught only by the window it was asked for. Both run here rather than
    being asserted as text, because the bad publish looks exactly like a good one.
    """
    block = _report_block(_source())
    body = (
        SUMMARY_RACE_HARNESS
        .replace("__SUMMARY_WANTED__", extract_js_function(
            block, "function usageReportSummaryWanted() {"))
        .replace("__SUMMARY_KEY__", extract_js_function(
            block, "function usageReportSummaryKey() {"))
        .replace("__ENSURE_SUMMARY__", extract_js_function(
            block, "function usageReportEnsureSummary() {"))
        .replace("__LOADER__", extract_js_function(
            block, "async function loadUsageReport(options = {}) {"))
    )
    out = json.loads(_run_node(tmp_path, "usage-report-summary-race.js", body))

    assert out["control"] == "2026-09-01..2026-09-05", out
    assert out["controlReleased"] == 2, "the control pair did not actually go out"

    assert out["deferredKey"] == "", "the summary was recorded as answered while deferred"
    assert out["inFlightForA"] == 2 and out["loadingA"] is True, out
    assert out["windowB"] == "2026-09-08..2026-09-12", out
    assert out["lateAReleased"] == 2, "window A's pair never went out"
    assert out["afterLateAnswer"]["tag"] == "2026-09-08..2026-09-12", (
        "a summary answer for the replaced window overwrote the new one"
    )
    assert out["afterLateAnswer"]["summaryLoading"] is False, (
        "the discarded pair kept its in-flight guard: that subtab would never ask again"
    )

    assert out["inFlightForKeyCase"] == 2, out
    assert out["movedReleased"] == 2, out
    assert out["afterWindowMove"]["tag"] == "KEEP-ME", (
        "the pair published for a window the page is no longer on"
    )
    assert out["afterWindowMove"]["summaryLoading"] is False, out
    assert out["afterWindowMove"]["summaryKey"] == "", (
        "a window that was never answered recorded itself as answered"
    )
    assert out["reaskedFor"] == ["2026-10-01", "2026-10-01"], (
        "the window the reader is actually on is not owed its figures"
    )

    # Same window, older hero: the refresh must keep the figures it just read.
    assert out["inFlightForRefresh"] == 2, out
    assert out["afterRefresh"]["tag"] == "2026-10-01..2026-10-05", out
    assert out["afterRefresh"]["hero"] not in ("hero-stale", "before"), out
    assert out["staleReleased"] == 2, "the pair in flight for that window never went out"
    assert out["afterStalePair"]["hero"] == out["afterRefresh"]["hero"], (
        "a pair asked against the previous hero overwrote the refresh's figures"
    )
    assert out["afterStalePair"]["summaryLoading"] is False, out


def test_the_version_stamp_waits_for_the_full_paint() -> None:
    """/api/version is fired first and answers instantly, so it can land between
    the two paints. Stamping the partial model would render the footer and the
    share cards off a half-empty report -- the one thing the partial paint is
    careful not to do."""
    block = _report_block(_source())
    version = extract_js_function(block, "function usageReportLoadVersion() {")
    # The whole statement, not the condition and the ordering: asserting those two
    # separately passes for `if (...notices.pending) { }`, which guards nothing.
    assert "if (usageReportState.model.notices.pending) return;" in version
    assert version.index("notices.pending") < version.index("usageReportRenderShare")


def test_the_first_paint_renders_only_what_usage_answers() -> None:
    """Anything else would print an em dash it has to take back a moment later.

    The sections the other two requests feed keep the placeholder
    usageReportRenderLoading() left on them, so the page fills in instead.
    """
    block = _report_block(_source())
    partial = extract_js_function(block, "function usageReportRenderPartial(model) {")
    full = extract_js_function(block, "function usageReportRender() {")
    hero = extract_js_function(block, "function usageReportRenderHero(model) {")
    assert "usageReportRenderHeroTotals(model)" in partial
    # The activity half is reached through the whole-hero wrapper, which only the
    # full paint calls.
    assert "usageReportRenderHeroActivity(model)" not in partial
    assert "usageReportRenderHeroActivity(model)" in hero
    assert "usageReportRenderHero(model)" in full
    for absent in (
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


# --- delta honesty ----------------------------------------------------------


@needs_node
def test_a_delta_outside_the_data_is_never_printed(tmp_path: Path) -> None:
    """Year to date compares against an equal window that predates the records.

    The server cannot know the history stops, so the report decides: no delta on
    the year view, and a multiple-of-nothing delta is not a percentage to print.
    """
    body = extract_js_function(_source(), "function usageReportShowDelta(model) {")
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


# --- the page itself, painted onto a stub DOM -------------------------------


REPORT_DOM_PRELUDE = r"""
// `t` prints a key with its values, or the real copy for keys a test hands in.
const COPY = __COPY__;
function t(key, vars) {
  if (Object.prototype.hasOwnProperty.call(COPY, key)) {
    let text = COPY[key];
    Object.entries(vars || {}).forEach(([name, value]) => { text = text.split(`{${name}}`).join(String(value)); });
    return text;
  }
  return vars ? `${key}${JSON.stringify(vars)}` : key;
}
function textNode(value) { return { tag: '#text', textContent: String(value) }; }
function makeNode(tag, id) {
  const classes = new Set();
  return {
    tag, id, children: [], hidden: false, title: '', dataset: {}, attrs: {}, style: {}, listeners: {},
    value: '', placeholder: '', checked: false, options: [], colSpan: null, _text: '',
    classList: {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      toggle: (name, on) => {
        const want = on === undefined ? !classes.has(name) : !!on;
        if (want) classes.add(name); else classes.delete(name);
        return want;
      },
    },
    get className() { return [...classes].join(' '); },
    set className(value) {
      classes.clear();
      String(value).split(/\s+/).filter(Boolean).forEach((name) => classes.add(name));
    },
    get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); },
    set textContent(value) { this._text = String(value); this.children = []; },
    append(...kids) { kids.forEach((kid) => this.children.push(typeof kid === 'string' ? textNode(kid) : kid)); },
    appendChild(kid) { this.children.push(kid); return kid; },
    replaceChildren(...kids) { this._text = ''; this.children = []; this.append(...kids); },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    fire(type, event = {}) { (this.listeners[type] || []).forEach((fn) => fn(event)); },
    click() { this.fire('click'); },
    remove() {},
    querySelector() { return null; },
  };
}
// Any id resolves, so a section a renderer writes to is always there to read.
const elements = {};
const document = {
  hidden: false,
  body: makeNode('body'),
  documentElement: makeNode('html'),
  getElementById: (id) => elements[id] || (elements[id] = makeNode('div', id)),
  createElement: (tag) => makeNode(tag),
  createDocumentFragment: () => makeNode('#fragment'),
  createTextNode: textNode,
  querySelectorAll: () => [],
  addEventListener() {},
};
const window = { addEventListener() {} };
const HEAT_COLORS = ['#h0', '#h1', '#h2', '#h3', '#h4', '#h5', '#h6', '#h7'];
function heatColor(level) { return HEAT_COLORS[Math.max(0, Math.min(HEAT_COLORS.length - 1, level))]; }
function formatNumber(value) { return String(value); }
function formatTokenCount(value) { return `${value}t`; }
function formatCurrency(value) { return `$${value}`; }
function formatDurationUnits(ms) { return `${ms}ms`; }
function formatShortDate(value, includeYear) {
  return `${value instanceof Date ? formatDateKey(value) : value}${includeYear ? '+y' : ''}`;
}
function formatToolName(tool) { return `tool:${tool}`; }
function getDayLabels() { return ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']; }
function createToolIdentity(tool) { const node = makeNode('span'); node.textContent = `identity:${tool}`; return node; }
let TODAY = new Date(2026, 8, 9);
function usageReportToday() { return new Date(TODAY.getTime()); }
let SERVERS = [{ id: 'local', label: 'Local' }];
function usageReportServers() { return SERVERS; }
"""

# Small pieces nearly every renderer reaches for, shipped real rather than stubbed.
REPORT_DOM_HELPERS = (
    "function formatDateKey(date) {",
    "function parseDateKey(value) {",
    "function startOfWeekMonday(date) {",
    "function getMonthLabels() {",
    "function renderLoadingPlaceholder(element, label = t('loading')) {",
    "function usageReportWindows(period, back = 0) {",
    "function usageReportServer() {",
    "function usageReportToolLabel(tool) {",
    "function usageReportFigure(value, format) {",
    "function usageReportShare(fraction) {",
    "function usageReportHours(ms) {",
    "function usageReportHourLabel(hour) {",
    "function usageReportPeakHour(hour) {",
    "function usageReportHourWindow(hours) {",
    "function usageReportMachineName() {",
    "function usageReportText(el, value) {",
    "function usageReportFillNote(el, entries) {",
    "function usageReportStatLine(label, value) {",
)


def _const_line(source: str, name: str) -> str:
    """One single-line page constant, as declared."""
    return next(
        line.strip() for line in source.splitlines() if line.strip().startswith(f"const {name} = ")
    )


def _report_dom(scenario: str, *signatures: str, copy: dict[str, str] | None = None) -> str:
    """The real report renderers named by ``signatures`` over the stub DOM.

    The page's own state literal and constants come along, so a default that
    changes is a changed result. ``copy`` maps keys to real dictionary text for
    the tests that read a sentence rather than a key.
    """
    source = _source()
    return "\n".join([
        REPORT_DOM_PRELUDE.replace("__COPY__", json.dumps(copy or {}, ensure_ascii=False)),
        _em_constant(source),
        *(_const_line(source, name) for name in (
            "USAGE_REPORT_HEAT_FACETS", "USAGE_REPORT_PERIODS", "USAGE_REPORT_PREFS_KEY",
        )),
        extract_js_function(source, "const usageReportState = {"),
        *(extract_js_function(source, signature) for signature in REPORT_DOM_HELPERS + signatures),
        scenario,
    ])


BANNER_SCENARIO = """
const FLAGS = __FLAGS__;
const banner = document.getElementById('usageReportBanner');
const paint = (notices, extra = {}) => {
  usageReportRenderBanner({ notices, sourceErrors: [], unavailableTools: [], ...extra });
  return { hidden: banner.hidden, text: banner.textContent };
};
const none = Object.fromEntries(FLAGS.map((flag) => [flag, false]));
const out = { clean: paint(none), each: {}, pending: {} };
FLAGS.forEach((flag) => {
  out.each[flag] = paint({ ...none, [flag]: true });
  // The first paint: the two slow requests have not been asked for yet.
  out.pending[flag] = paint({ ...none, [flag]: true, pending: true });
});
out.partial = paint(none, { sourceErrors: ['codex', 'kimi'] });
out.unavailable = paint(none, { unavailableTools: ['cline'] });
out.everything = paint(Object.fromEntries(FLAGS.map((flag) => [flag, true])),
  { sourceErrors: ['codex'], unavailableTools: ['cline'] });

// Neither totals source answered: the state panel names the server instead.
const statePanel = () => ({
  panel: document.getElementById('usageReportStatePanel').hidden,
  stack: document.getElementById('usageReportStack').hidden,
  retry: document.getElementById('usageReportRetry').hidden,
  body: document.getElementById('usageReportStateBody').textContent,
});
document.getElementById('usageReportRetry').hidden = true;
usageReportState.error = { server: { id: 'mac', label: 'Studio' }, detail: 'connection refused' };
usageReportRenderState();
out.unreachable = statePanel();
usageReportState.error = null;
usageReportRenderState();
out.recovered = statePanel();
process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_report_names_every_degradation_state(tmp_path: Path) -> None:
    """Each of the three requests can fail on its own, and the banner is the only
    surface that says which one did.

    The flags are read off a model the real builder made, so a notice the model
    starts recording and the banner never reads is a failure here rather than a
    silent zero. On the first paint the slow two are absent without having failed:
    `pending` keeps them out of the banner, which would otherwise flash
    "unavailable" a moment before the data lands. Usage is the request the first
    paint is made of, so its notice is never held back.
    """
    source = _source()
    body = "\n".join(extract_js_function(source, sig) for sig in MODEL_SIGNATURES) + MODEL_FIXTURE
    notices = json.loads(_run_node(tmp_path, "usage-report-notices.js", body))["whole"]["notices"]
    flags = sorted(set(notices) - {"pending"})
    assert {"insightsMissing", "usageMissing", "activeTimeMissing"} <= set(flags), flags

    program = _report_dom(
        BANNER_SCENARIO.replace("__FLAGS__", json.dumps(flags)),
        "function usageReportRenderBanner(model) {",
        "function usageReportRenderState() {",
    )
    out = json.loads(_run_node(tmp_path, "usage-report-banner.js", program))

    assert out["clean"] == {"hidden": True, "text": ""}
    for flag in flags:
        assert out["each"][flag]["hidden"] is False and out["each"][flag]["text"], (
            f"the model records {flag} and the banner never says so"
        )
    assert out["each"]["insightsMissing"]["text"] == "usageReportInsightsMissing"
    assert out["each"]["usageMissing"]["text"] == "usageReportTotalsFallback"
    assert out["each"]["activeTimeMissing"]["text"] == "usageReportActiveMissing"
    assert out["pending"]["insightsMissing"] == {"hidden": True, "text": ""}
    assert out["pending"]["activeTimeMissing"] == {"hidden": True, "text": ""}
    assert out["pending"]["usageMissing"]["text"] == "usageReportTotalsFallback"
    assert out["partial"]["text"] == 'usageReportPartial{"tools":"tool:codex, tool:kimi"}'
    assert out["unavailable"]["text"] == 'usageReportActiveUnavailable{"tools":"tool:cline"}'
    assert out["everything"]["text"] == " ".join([
        "usageReportInsightsMissing",
        "usageReportTotalsFallback",
        "usageReportActiveMissing",
        'usageReportPartial{"tools":"tool:codex"}',
        'usageReportActiveUnavailable{"tools":"tool:cline"}',
    ])
    assert out["unreachable"] == {
        "panel": False, "stack": True, "retry": False,
        "body": 'usageReportUnreachable{"server":"Studio","error":"connection refused"}',
    }
    assert out["recovered"]["panel"] is True and out["recovered"]["stack"] is False


@needs_node
def test_every_line_that_names_a_machine_names_the_selected_one(tmp_path: Path) -> None:
    """The report is single-server, and the footer's source line used to be a
    fixed string about "this machine": read from a remote server, it described
    the wrong box. It names the selected server, the local box by the translated
    fallback, and an owned nickname over both; the version is the one that
    server reported, or a dash.
    """
    english = _i18n_english_copy(_source())
    copy = {key: english[key] for key in ("usageReportFooterSrc", "usageReportCardNicknameFallback")}
    program = _report_dom("""
const footer = document.getElementById('usageReportFooter');
const model = { coverage: { stored_sources: ['codex', 'claude'], live_sources: ['kimi'] } };
const lines = () => footer.children.map((row) => row.textContent);
const out = {};
SERVERS = [{ id: 'local', label: 'Local' }, { id: 'mac', label: 'mac-studio' }];
usageReportState.version = '2.6.0';
usageReportState.serverId = 'mac';
usageReportRenderFooter(model);
out.remote = lines();
usageReportState.serverId = 'local';
usageReportRenderFooter(model);
out.local = lines();
usageReportState.nickname = 'desk';
usageReportRenderFooter(model);
out.nickname = lines();
usageReportState.version = null;
usageReportRenderFooter(model);
out.noVersion = lines();
usageReportRenderFooter({ coverage: null });
out.noCoverage = lines();
process.stdout.write(JSON.stringify(out));
""", "function usageReportRenderFooter(model) {", copy=copy)
    out = json.loads(_run_node(tmp_path, "usage-report-footer.js", program))

    def line(machine: str, version: str) -> list[str]:
        text = copy["usageReportFooterSrc"]
        for name, value in (("n", "3"), ("machine", machine), ("version", version)):
            text = text.replace("{" + name + "}", value)
        return [text]

    assert "mac-studio" in out["remote"][0], out["remote"]
    assert out["remote"] == line("mac-studio", "2.6.0")
    assert out["local"] == line(copy["usageReportCardNicknameFallback"], "2.6.0")
    assert out["nickname"] == line("desk", "2.6.0")
    assert out["noVersion"] == line("desk", "\u2014")
    assert out["noCoverage"] == []


WHEN_SCENARIO = """
const host = document.getElementById('usageReportWhen');
const when = (over) => ({
  period: 'week', range: { from: '2026-09-07', to: '2026-09-09' }, timezone: 'Europe/London',
  hourly: {
    buckets: Array.from({ length: 24 }, (_, hour) => ({ hour, tokens: hour === 3 ? 50 : 10 })),
    peak_hour: 3, night_share: 0.25, night_hours: [1, 2],
  },
  weekday: {
    buckets: Array.from({ length: 7 }, (_, weekday) => ({ weekday, tokens: (weekday + 1) * 100 })),
    peak_weekday: 2,
  },
  // Monday to Wednesday: the window is three days old.
  elapsedWeekdays: new Set([0, 1, 2]),
  ...over,
});
const hoursWith = (bars, name) => bars
  .map((bar, hour) => (bar.className.split(' ').includes(name) ? hour : null))
  .filter((hour) => hour !== null);
const out = {};
usageReportRenderWhen(when({}));
const [hours, side] = host.children;
const bars = hours.children[0].children;
out.week = {
  night: hoursWith(bars, 'is-night'),
  peak: hoursWith(bars, 'is-peak'),
  notes: side.children.filter((node) => node.className === 'usage-report-notice').map((node) => node.textContent),
  rows: side.children
    .filter((node) => node.className.split(' ').includes('usage-report-wk-row'))
    .map((row) => [row.children[0].textContent, row.children[1].children[0].style.width, row.children[2].textContent]),
  tz: document.getElementById('usageReportTz').textContent,
};
usageReportRenderWhen(when({ hourly: null }));
out.noHourly = { children: host.children.length, text: host.textContent };
usageReportRenderWhen(when({ weekday: null }));
out.noWeekday = { children: host.children.length, text: host.textContent };
process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_weekday_rows_that_have_not_happened_print_no_figure(tmp_path: Path) -> None:
    """A three-day window has not met Thursday; that is not a zero-usage Thursday.

    The same paint marks the night hours the server served (spec 5.7: the window
    is never spelled out in the page), and with the rhythm facets absent only
    this section collapses, naming the two facets it is missing rather than the
    whole request list.
    """
    program = _report_dom(WHEN_SCENARIO, "function usageReportRenderWhen(model) {")
    out = json.loads(_run_node(tmp_path, "usage-report-when.js", program))

    rows = out["week"]["rows"]
    assert [row[0] for row in rows] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert [row[2] for row in rows] == ["100t", "200t", "300t"] + ["\u2014"] * 4, rows
    assert all(row[1] == "0%" for row in rows[3:]), "an unreached weekday drew a bar"
    assert all(row[1] != "0%" for row in rows[:3]), rows
    assert out["week"]["night"] == [1, 2], "the night bars must follow the served hours"
    assert out["week"]["peak"] == [3]
    assert out["week"]["notes"] == [
        'usageReportNightNote{"window":"01:00\u201303:00"}',
        "usageReportPeakDay: Wed",
    ]
    assert out["week"]["tz"] == 'usageReportWhenTz{"tz":"Europe/London"}'
    missing = 'usageReportFacetsMissing{"facets":"hourly,weekday"}'
    assert out["noHourly"] == {"children": 1, "text": missing}
    assert out["noWeekday"] == {"children": 1, "text": missing}


HEAT_SCENARIO = """
const host = document.getElementById('usageReportHeat');
const legend = document.getElementById('usageReportLegend');
const streak = document.getElementById('usageReportStreak');
const month = (over) => ({
  period: 'month', dailyAvailable: true, streaks: null, firsts: null, elapsed: 9,
  cells: Array.from({ length: 30 }, (_, i) => ({
    date: `2026-09-${String(i + 1).padStart(2, '0')}`, tokens: i, messages: 1,
    level: i % 8, future: i > 8, inWindow: i <= 8,
  })),
  ...over,
});
// Whatever the previous window left behind.
const prefill = () => {
  legend.replaceChildren(makeNode('i'));
  streak.textContent = 'a stale streak';
  host.replaceChildren(makeNode('div'));
};
const read = () => ({
  host: host.children.length, text: host.textContent,
  legend: legend.children.length, streak: streak.textContent,
});
const out = {};
for (const period of ['day', 'week']) {
  prefill();
  usageReportRenderHeat(month({ period }));
  out[period] = read();
}
prefill();
usageReportRenderHeat(month({ dailyAvailable: false }));
out.missing = read();
prefill();
usageReportRenderHeat(month({}));
const grid = host.children.find((node) => node.className === 'usage-report-month');
out.month = {
  ...read(),
  cells: grid ? grid.children.length : null,
  blanks: grid ? grid.children.filter((cell) => cell.className.split(' ').includes('is-blank')).length : null,
};
process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_the_day_map_steps_aside_for_short_windows_and_names_its_own_facets(tmp_path: Path) -> None:
    """A day or a week has no daily card: the map, its legend and its streak line
    are cleared rather than left over from the previous window. A month draws a
    grid that opens on its first weekday. With the daily facets absent, the notice
    names the three facets the map is made of, not the whole request list.
    """
    program = _report_dom(
        HEAT_SCENARIO,
        "function usageReportCell(day) {",
        "function usageReportRenderLegend() {",
        "function usageReportRenderStreak(model) {",
        "function usageReportFitYearCells(wrap, cols) {",
        "function usageReportRenderHeat(model) {",
    )
    out = json.loads(_run_node(tmp_path, "usage-report-heat.js", program))

    for period in ("day", "week"):
        assert out[period] == {"host": 0, "text": "", "legend": 0, "streak": ""}, (period, out[period])
    heat_facets = _const_line(_source(), "USAGE_REPORT_HEAT_FACETS").split("'")[1]
    assert set(heat_facets.split(",")) == {"daily", "streaks", "firsts"}
    assert out["missing"]["text"] == f'usageReportFacetsMissing{{"facets":"{heat_facets}"}}'
    assert out["missing"]["streak"] == ""
    lead = date(2026, 9, 1).weekday()
    assert out["month"]["cells"] == lead + 30, out["month"]
    assert out["month"]["blanks"] == lead
    assert out["month"]["legend"] == len(["less"]) + 8 + len(["more"])


CONTROLS_SCENARIO = """
TODAY = new Date(2026, 1, 10);
const chipHost = document.getElementById('reportPeriodChips');
const out = { daysHidden: {}, chips: {} };
for (const period of ['day', 'week', 'month', 'year']) {
  usageReportState.period = period;
  usageReportRenderControls();
  out.daysHidden[period] = document.getElementById('usageReportDaysPanel').hidden;
  out.chips[period] = chipHost.children.map((chip) => [chip.textContent, chip.attrs['aria-pressed']]);
}
out.serverPicker = document.getElementById('usageReportServerWrap').hidden;

// A chip clicked while a load is in flight waits for it, rather than vanishing.
const loads = [];
function loadUsageReport(options) { loads.push(options === undefined ? 'plain' : options); }
usageReportState.period = 'month';
usageReportRenderControls();
usageReportState.loading = true;
chipHost.children[2].fire('click');
out.midLoad = { back: usageReportState.back, pendingBack: usageReportState.pendingBack, loads: loads.length };
usageReportState.loading = false;
chipHost.children[2].fire('click');
out.atRest = { back: usageReportState.back, pendingBack: usageReportState.pendingBack, loads: loads.slice() };
chipHost.children[2].fire('click');
out.sameWindowLoads = loads.length;
process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_the_period_chips_are_named_from_real_dates_and_queue_mid_load(tmp_path: Path) -> None:
    """The chips walk the report's period back, named from the calendar rather
    than "last month, whatever that was", so a month across a year turn names its
    year. A chip clicked during a load describes data not on screen yet, so it is
    held and applied when the load lands. The day and week windows have no
    daily card, so the controls hide its panel.
    """
    program = _report_dom(
        CONTROLS_SCENARIO,
        "function usageReportChipDefs() {",
        "function usageReportRenderPeriodChips() {",
        "function usageReportRenderControls() {",
    )
    out = json.loads(_run_node(tmp_path, "usage-report-controls.js", program))

    assert out["daysHidden"] == {"day": True, "week": True, "month": False, "year": False}
    labels = {period: [label for label, _ in chips] for period, chips in out["chips"].items()}
    assert labels["month"] == ["feb", "jan", "dec 2025", "nov 2025", "oct 2025", "sep 2025"]
    assert labels["week"] == [
        "usageReportChipThisWeek", "usageReportChipLastWeek",
        'usageReportChipWeeksAgo{"n":2}', 'usageReportChipWeeksAgo{"n":3}',
    ]
    assert labels["year"] == ["2026", "2025", "2024", "2023"]
    assert labels["day"] == [
        "usageReportTitleDay", "usageReportChipYesterday",
        "2026-02-08", "2026-02-07", "2026-02-06", "2026-02-05", "2026-02-04",
    ]
    for chips in out["chips"].values():
        assert [pressed for _, pressed in chips] == ["true"] + ["false"] * (len(chips) - 1)
    assert out["serverPicker"] is True, "one server offers no picker"

    assert out["midLoad"] == {"back": 0, "pendingBack": 2, "loads": 0}, "a mid-load chip click was dropped"
    assert out["atRest"] == {"back": 2, "pendingBack": None, "loads": ["plain"]}
    assert out["sameWindowLoads"] == 1, "the window already on screen was asked for again"


LOADING_SCENARIO = """
const SECTIONS = __SECTIONS__;
const KPIS = ['usageReportKTokens', 'usageReportKCost', 'usageReportKMessages',
  'usageReportKSessions', 'usageReportKActive', 'usageReportKDays'];
[...SECTIONS, ...KPIS].forEach((id) => {
  const stale = makeNode('span');
  stale.textContent = `the last window's ${id}`;
  document.getElementById(id).replaceChildren(stale);
});
['usageReportGreenCards', 'usageReportAmberCards'].forEach((id) => {
  document.getElementById(id).replaceChildren(makeNode('canvas'));
});
const status = document.getElementById('usageReportExportStatus');
status.hidden = false;
status.textContent = 'Exported 4 files';
usageReportState.previews = { 'green:light': {}, 'green:dark': {} };
usageReportState.period = 'week';
usageReportRenderLoading();
const node = (id) => document.getElementById(id);
process.stdout.write(JSON.stringify({
  sections: Object.fromEntries(SECTIONS.map((id) => [id, node(id).textContent])),
  kpis: Object.fromEntries(KPIS.map((id) => [id, [node(id).textContent, node(id).classList.contains('tokdash-loading-placeholder')]])),
  title: node('usageReportTitle').textContent,
  banner: node('usageReportBanner').hidden,
  statePanel: node('usageReportStatePanel').hidden,
  cards: ['usageReportGreenCards', 'usageReportAmberCards'].map((id) => node(id).children.length),
  status: [status.hidden, status.textContent],
  previews: Object.keys(usageReportState.previews),
}));
"""


@needs_node
def test_the_share_panel_is_cleared_before_the_next_load(tmp_path: Path) -> None:
    """A stale podium under a fresh hero is a report that lies about the period,
    and a stale preview would export a period the header no longer names.

    Every section the next load fills is blanked as it starts, the hero tiles
    turn back into loading placeholders, and the share cards, their previews and
    the export status line are cleared with them.
    """
    sections = [
        "usageReportKTokensSub", "usageReportKDaysSub", "usageReportHeat", "usageReportStreak",
        "usageReportLegend", "usageReportRange", "usageReportProgress", "usageReportPodium",
        "usageReportGroupNote", "usageReportWhen", "usageReportTz", "usageReportAgentsBody",
        "usageReportAgentsNote", "usageReportModelsBody", "usageReportModelsNote",
        "usageReportProjectsBody", "usageReportProjectsNote", "usageReportFooter",
    ]
    program = _report_dom(
        LOADING_SCENARIO.replace("__SECTIONS__", json.dumps(sections)),
        "function usageReportClearShare() {",
        "function usageReportRenderLoading() {",
    )
    out = json.loads(_run_node(tmp_path, "usage-report-loading.js", program))

    stale = {section: text for section, text in out["sections"].items() if text}
    assert not stale, f"sections kept the last window's content: {sorted(stale)}"
    for kpi, (text, placeholder) in out["kpis"].items():
        assert placeholder and text == "loading", (kpi, text)
    assert out["title"] == "usageReportTitleWeek"
    assert out["banner"] is True and out["statePanel"] is True
    assert out["cards"] == [0, 0], "the previous window's cards are still up"
    assert out["status"] == [True, ""]
    assert out["previews"] == [], "the export would still write the previous window's previews"


@needs_node
def test_an_unknown_count_prints_a_dash_where_the_hero_reads_it(tmp_path: Path) -> None:
    """`model.sessions` and `model.streaks` are absent when their request failed.

    A single formatNumber(null) puts "0 sessions" back on the page, and a
    zero-filled streak block prints "0/30", a month nobody worked. (The share
    card reads the same two figures; a card from an older server is checked by
    the card harness below.)
    """
    program = _report_dom("""
const read = () => Object.fromEntries(['usageReportKSessions', 'usageReportKActive', 'usageReportKDays', 'usageReportKDaysSub']
  .map((id) => [id, document.getElementById(id).textContent]));
const base = { isEmpty: false, elapsed: 30, period: 'month' };
const out = {};
usageReportRenderHeroActivity({ ...base, sessions: null, streaks: null, agentMs: 0 });
out.unknown = read();
usageReportRenderHeroActivity({ ...base, sessions: 4, streaks: { active_days: 12 }, agentMs: 7200000 });
out.known = read();
usageReportRenderHeroActivity({ ...base, isEmpty: true, sessions: 0, streaks: { active_days: 0 }, agentMs: 5 });
out.empty = read();
process.stdout.write(JSON.stringify(out));
""", "function usageReportRenderHeroActivity(model) {")
    out = json.loads(_run_node(tmp_path, "usage-report-hero-activity.js", program))

    sub = 'usageReportDaysSub{"n":30}'
    assert out["unknown"] == {
        "usageReportKSessions": "\u2014", "usageReportKActive": "\u2014",
        "usageReportKDays": "\u2014", "usageReportKDaysSub": sub,
    }
    # One figure for active time, and it is the agent total.
    assert out["known"] == {
        "usageReportKSessions": "4", "usageReportKActive": "7200000ms",
        "usageReportKDays": "12/30", "usageReportKDaysSub": sub,
    }
    assert out["empty"] == {
        "usageReportKSessions": "0", "usageReportKActive": "\u2014",
        "usageReportKDays": "\u2014", "usageReportKDaysSub": "",
    }


@needs_node
def test_the_podium_keeps_its_missing_data_notices(tmp_path: Path) -> None:
    """With no project facet the podium says so under the tiles, and a facet scan
    that covers visibly less than the period total says how much it covers. The
    ranking keeps those notices; it has no decorative footnotes to print.
    """
    program = _report_dom("""
const host = document.getElementById('usageReportPodium');
const note = document.getElementById('usageReportGroupNote');
const read = () => ({
  tiles: host.children.map((tile) => tile.textContent),
  note: note.textContent, hidden: note.hidden,
  faint: note.children.flatMap((fragment) => fragment.children || [])
    .filter((span) => span.className === 'usage-report-note-faint').length,
});
const bare = { tools: null, models: null, projectRows: [], projectsAvailable: false, facetTotal: 0, tokens: 100, atByTool: {} };
const out = {};
usageReportRenderPodium(bare);
out.bare = read();
usageReportRenderPodium({
  ...bare, projectsAvailable: true, facetTotal: 50,
  tools: [{ tool: 'codex', tokens: 50 }], models: [{ model: 'm', tokens: 50, cost: 2 }],
  projectRows: [{ project: 'p', tokens: 40, cost: 1 }],
  atByTool: { codex: { session_count: 3, active_ms_sum: 3600000 } },
});
out.narrow = read();
process.stdout.write(JSON.stringify(out));
""", "function usageReportPlainName(label) {",
        "function usageReportPodiumTile(kindLabel, nameNode, valueText, shareText, subText) {",
        "function usageReportShowFacetCoverage(model) {",
        "function usageReportRenderPodium(model) {")
    out = json.loads(_run_node(tmp_path, "usage-report-podium.js", program))

    assert out["bare"]["note"] == "usageReportMissingProjects" and out["bare"]["hidden"] is False
    assert len(out["bare"]["tiles"]) == 3
    assert all("\u2014" in tile for tile in out["bare"]["tiles"]), out["bare"]["tiles"]
    assert out["narrow"]["note"] == 'usageReportFacetCovers{"pct":"50%"}', out["narrow"]
    assert out["narrow"]["faint"] == 1 and out["narrow"]["hidden"] is False
    assert "identity:codex" in out["narrow"]["tiles"][0]


# --- copy deck --------------------------------------------------------------


def test_report_translations_keep_the_same_placeholders() -> None:
    source = _source()
    english = _i18n_report_copy(source, "en")
    for language in I18N_LANGS:
        copy = _i18n_report_copy(source, language)
        for key, value in english.items():
            expected = set(re.findall(r"\{(\w+)\}", value))
            actual = set(re.findall(r"\{(\w+)\}", copy[key]))
            assert actual == expected, f"{language}.{key}: {actual} != {expected}"


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
    a remote report stamped from it printed the local build in its footer line.
    The stamp no longer rides the exported PNG (cosyncing review item 8), but
    the footer still names the build the report was read from."""
    version = extract_js_function(_source(), "function usageReportLoadVersion() {")
    assert "getRuntimeVersion" not in version
    assert "fetchJsonWithRetry(server, '/api/version')" in version
    assert "usageReportState.versionServerId" in version, "the answer is keyed to its server"
    assert "usageReportRenderShare(usageReportState.model)" in version


# --- PR-B: podium, rhythm, agent table, caveats ------------------------------


def test_the_new_sections_render_after_the_day_map() -> None:
    block = _report_block(_source())
    render = extract_js_function(block, "function usageReportRender() {")
    order = [
        "usageReportRenderHeat(model)",
        "usageReportRenderPodium(model)",
        "usageReportRenderWhen(model)",
        "usageReportRenderAgents(model)",
        "usageReportRenderModels(model)",
        "usageReportRenderProjects(model)",
        "usageReportRenderFooter(model)",
    ]
    positions = [render.index(call) for call in order]
    assert positions == sorted(positions), "the sections fell out of the spec's order"


@needs_node
def test_reconciliation_separates_the_unattributed_share_from_the_gap(
    tmp_path: Path,
) -> None:
    """The one real defect the design review found, pinned as arithmetic.

    A facet that sees 90% of the period's tokens has a 10% gap, and an
    unattributed bucket of 5% is not that gap: printing only the bucket
    understates it by half.
    """
    body = extract_js_function(
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


@needs_node
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
    body = _em_constant(source) + extract_js_function(
        source, "function usageReportHourLabel(hour) {"
    ) + extract_js_function(source, "function usageReportHourWindow(hours) {")
    body += f"""
process.stdout.write(usageReportHourWindow({json.dumps(hours)}));
"""
    assert _run_node(tmp_path, "usage-report-night.js", body) == expected


@needs_node
def test_a_small_share_keeps_digits_that_survive_the_eye(tmp_path: Path) -> None:
    """0.4% rounded to 0% reads as a missing slice, which is a different fact."""
    source = _source()
    body = _em_constant(source) + extract_js_function(source, "function usageReportShare(fraction) {")
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
    "const USAGE_REPORT_CARD_FALLBACK = {",
    "function usageReportThemeTokens(theme, dark) {",
    "function usageReportCardPalette(mode) {",
    "const USAGE_REPORT_TIER_ACCENT = {",
    "function usageReportFontFamily(mono = false) {",
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
let fontTokensUnderTest = __FONT_TOKENS__;
const usageReportState = { includeCost: false, nickname: 'wsl-box', version: '2.5.1' };
let serverUnderTest = { id: 'local', label: 'Local', baseUrl: '' };
const usageReportServer = () => serverUnderTest;
const COPY = __COPY__;
function t(key, vars) {
  let out = COPY[key] === undefined ? key : COPY[key];
  Object.keys(vars || {}).forEach((name) => { out = out.split('{' + name + '}').join(String(vars[name])); });
  return out;
}
const document = { documentElement: { dataset: {} }, createElement: () => ({ getContext: () => ({
  font: '400 10px sans-serif',
  measureText(text) {
    const size = parseFloat(String(this.font).split(' ')[1]) || 10;
    return { width: String(text).length * size * 0.55 };
  },
}) }) };
function getComputedStyle(element) {
  if (element !== document.documentElement) throw new Error('expected root font tokens');
  return { getPropertyValue: (name) => fontTokensUnderTest[name] || '' };
}
const parseDateKey = (value) => new Date(value + 'T00:00:00');
const formatTokenCount = (n) => (Number(n) >= 1e6 ? (Number(n) / 1e6).toFixed(1) + 'M' : String(n));
const formatNumber = (n) => String(n);
const formatCurrency = (n) => '$' + Number(n).toFixed(2);
const formatDuration = (ms) => Math.round(Number(ms) / 3600000) + 'h';
const formatDurationUnits = (ms) => Math.round(Number(ms) / 3600000) + 'h';
const formatShortDate = (value) => String(value);
const langLocale = () => 'en-US';
const usageReportToolLabel = (tool) => String(tool);
const usageReportShare = (f) => (Number(f) * 100).toFixed(1) + '%';
"""
    prelude = prelude.replace("__COPY__", json.dumps(_i18n_english_copy(source), ensure_ascii=False))

    def resolve_font(name: str) -> str:
        value = re.search(rf"^\s*{name}:([^;]+);", source, re.MULTILINE).group(1).strip()
        return re.sub(r"var\((--font[\w-]+)\)", lambda match: resolve_font(match.group(1)), value)

    prelude = prelude.replace("__FONT_TOKENS__", json.dumps({
        name: resolve_font(name) for name in ("--font-sans", "--font-mono")
    }))
    constants = "\n".join(
        line + "\n"
        for line in source.splitlines()
        if line.strip().startswith('const usageReportMeasureCtx = ')
    )
    return (
        prelude
        + _em_constant(source)
        + constants
        + "\n".join(extract_js_function(source, signature) for signature in CARD_SIGNATURES)
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
  tools: [
    { tool: 'codex', tokens: 4e10 }, { tool: 'claude', tokens: 1.4e10 }, { tool: 'gemini_cli', tokens: 3e9 },
    { tool: 'opencode', tokens: 2e9 }, { tool: 'qoder_cli', tokens: 1e9 },
  ],
  models: [
    { model: 'gpt-5.2-pro-thinking max', tokens: 3e10 }, { model: 'claude-fable-5.1', tokens: 1.5e10 },
    { model: 'gemini-3-pro', tokens: 1e10 }, { model: 'deepseek-v4', tokens: 5e9 }, { model: 'llama-5-405b', tokens: 2e9 },
  ],
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


@needs_node
def test_share_card_fonts_follow_language_style_changes(tmp_path: Path) -> None:
    report = _card_audit(tmp_path, """
const out = [];
for (const region of ['SC', 'JP', 'KR', 'SC']) {
  fontTokensUnderTest = {
    '--font-sans': '"Test Sans ' + region + '"',
    '--font-mono': '"Test Mono ' + region + '"',
  };
  const card = usageReportBuildCard(busy, 'green', 'light');
  out.push({
    sans: usageReportCardFont(16, 700, false),
    mono: usageReportCardFont(16, 500, true),
    fonts: card.ops.filter(op => op.k === 'text').map(op => op.font),
  });
}
process.stdout.write(JSON.stringify(out));
""")
    for region, snapshot in zip(("SC", "JP", "KR", "SC"), report):
        assert snapshot["sans"] == f'700 16px "Test Sans {region}"'
        assert snapshot["mono"] == f'500 16px "Test Mono {region}"'
        assert snapshot["fonts"]
        assert all(f' {region}"' in font for font in snapshot["fonts"])


@needs_node
def test_share_cards_wait_for_current_font_loads_after_switching_language(tmp_path: Path) -> None:
    body = "const document = { fonts: { ready: Promise.resolve('initial') } };\n"
    body += extract_js_function(_source(), "function usageReportCardFonts() {")
    body += """
const first = usageReportCardFonts();
document.fonts.ready = Promise.resolve('new language');
const second = usageReportCardFonts();
process.stdout.write(JSON.stringify({ fresh: first !== second, current: second === document.fonts.ready }));
"""
    report = json.loads(_run_node(tmp_path, "share-font-ready.js", body))
    assert report == {"fresh": True, "current": True}


@needs_node
def test_a_card_fits_its_canvas_and_never_runs_under_its_footer(tmp_path: Path) -> None:
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
      harnessIcons: card.ops.filter((op) => op.k === 'icon').length,
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
    assert cards["green/light"]["rows"] == 0 and 0 < cards["amber/light"]["rows"] <= 5
    # The busy month fits three project rows once the heat cells shrink, so
    # the ladder must spend the cells before it gives up the third row.
    assert cards["amber/light"]["rows"] >= 3, "a project row was dropped while a smaller heat cell could have paid for it"
    assert cards["amber/light"]["file"] == "tokdash-report-2026-09-01_2026-09-30-amber-default-light.png"
    # Neither tier carries a footer manifest or a reconciliation line any more;
    # the amber tier is a top-5 project ranking and nothing else.
    green = " ".join(cards["green/light"]["texts"])
    amber = " ".join(cards["amber/light"]["texts"])
    for blob in (green, amber):
        assert "no project names" not in blob and "no prompt text" not in blob
        assert "Grouped by repository" not in blob
    assert "TOP PROJECTS" in amber and "PROJECTS ·" not in amber, "the project count went with the label"
    assert "tokdash-project-" not in green
    assert "tokdash-project-" in amber
    # Both rankings run to five: the fixture carries five of each and every
    # one must reach the canvas, not a silent top-3.
    assert cards["green/light"]["harnessIcons"] == 5, "the harness ranking stopped short of five"
    for harness in ("codex", "claude", "gemini_cli", "opencode", "qoder_cli"):
        assert harness in green, f"{harness} never reached the card"
    for model in ("gpt-5.2-pro-thinking max", "claude-fable-5.1", "gemini-3-pro", "deepseek-v4", "llama-5-405b"):
        assert model in green, f"{model} never reached the card"


@needs_node
def test_the_hero_shows_tokens_and_cost_in_the_cost_colour(tmp_path: Path) -> None:
    """Cosyncing review item 6: the two figures a shared card is read for are
    tokens and cost, so they share the hero line -- the cost in the palette's
    cost colour, and only when the toggle allows money on the card."""
    report = _card_audit(
        tmp_path,
        """
usageReportState.includeCost = true;
const card = usageReportBuildCard(busy, 'green', 'light');
const hero = card.ops.filter((op) => op.k === 'text' && (op.text === '60123.5M' || op.text.startsWith(' · $')));
process.stdout.write(JSON.stringify({
  hero: hero.map((op) => ({ text: op.text, fill: op.fill })),
  cost: card.pal.cost,
  primary: card.pal.primary,
}));
""",
    )
    assert report["hero"][0]["text"] == "60123.5M" and report["hero"][0]["fill"] == report["primary"]
    assert report["hero"][1]["text"] == " · $39412.55" and report["hero"][1]["fill"] == report["cost"]
    assert report["cost"] != report["primary"], "the cost must not read as more tokens"


@needs_node
def test_day_share_cards_use_one_date_and_omit_the_daily_map(tmp_path: Path) -> None:
    report = _card_audit(
        tmp_path,
        """
const day = { ...busy, period: 'day', range: { from: '2026-09-03', to: '2026-09-03' }, elapsed: 1,
  cells: [{ date: '2026-09-03', tokens: 1e6, level: 7, future: false }] };
const out = {};
for (const tier of ['green', 'amber']) {
  const card = usageReportBuildCard(day, tier, 'light');
  out[tier] = {
    overflow: card.used - card.budget,
    texts: card.ops.filter((op) => op.k === 'text').map((op) => op.text),
    heat: card.ops.filter((op) => op.k === 'rect' && op.w === op.h && op.w >= 8).length,
  };
}
process.stdout.write(JSON.stringify(out));
""",
    )
    for card in report.values():
        assert card["overflow"] <= 0
        assert card["heat"] == 0
        assert card["texts"].count("2026-09-03") == 1
        assert not any("2026-09-03 – 2026-09-03" in text for text in card["texts"])


@needs_node
def test_the_card_range_line_is_just_the_window(tmp_path: Path) -> None:
    """Cosyncing review item 7: "Sep 1 – Sep 30 · all agent activity on
    mac-studio" restates what the page footer already says, so the card's
    range line is the window and nothing else."""
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
    assert "2026-09-01 – 2026-09-30" in joined
    assert "mac-studio" not in joined, "the card restated the machine the page footer names"
    assert "this machine" not in joined
    assert "{machine}" not in joined, "the placeholder reached the canvas unsubstituted"


@needs_node
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


@needs_node
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
    # The hero sub wraps, so read it as one blob.
    joined = " ".join(report["green"]["texts"])
    assert "— of 246 days active" in joined, "a missing streaks facet printed 0 days active"
    assert "— sessions · 0 requests" in joined, "an absent session count printed as 0 sessions"
    assert "— agent time" in joined, "a missing active-time answer printed as 0"


@needs_node
def test_the_machine_name_still_resolves_for_the_page_footer(tmp_path: Path) -> None:
    """The card no longer names the machine (cosyncing review item 8), but the
    page footer and the nickname hint still do. A remote server prints its
    registry label; the local box prints the translated fallback, since "Local"
    is a UI label and not a name; an owned nickname wins over both."""
    body = _card_harness(_source()) + extract_js_function(_source(), "function usageReportMachineName() {")
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
    assert "nicknameInput.placeholder = usageReportMachineName()" in _source()


@needs_node
def test_the_card_heat_drops_the_week_and_squares_the_month(tmp_path: Path) -> None:
    """The week card drops the heat block -- one row of stretched cells reads
    as a loading artifact. The month card takes the page's presentation
    (squares) but pinned to the left edge, not centred; the year keeps the
    full-width map. Unelapsed days paint as the ramp's empty colour."""
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
    tokens: (i + 1) * 1e6, level: (i % 7) + 1, future: i > 2,
  })),
};
const week = {
  ...busy,
  period: 'week',
  range: { from: '2026-08-31', to: '2026-09-06' },
  elapsed: 7,
  cells: Array.from({ length: 7 }, (_u, i) => ({
    date: '2026-08-' + String(25 + i),
    tokens: (i + 1) * 1e6, level: (i % 7) + 1, future: false,
  })),
};
const squares = (card, lo, hi) => card.ops.filter((op) => op.k === 'rect' && op.w === op.h && op.w >= lo && op.w < hi);
const audit = (card, lo, hi) => {
  const cells = squares(card, lo, hi);
  return {
    overflow: Number((card.used - card.budget).toFixed(3)),
    cells: cells.length,
    left: cells.length ? Math.min(...cells.map((op) => op.x)) : null,
    rightEdge: cells.length ? Math.max(...cells.map((op) => op.x + op.w)) : null,
    empty: cells.filter((op) => op.fill === '#EEF2F7').length,
  };
};
const out = {};
for (const tier of ['green', 'amber']) {
  out[tier] = audit(usageReportBuildCard(short, tier, 'light'), 9, 30);
}
out.week = audit(usageReportBuildCard(week, 'green', 'light'), 6, 60);
out.year = audit(usageReportBuildCard(sparse, 'green', 'light'), 3, 9);
process.stdout.write(JSON.stringify(out));
""",
    )
    for name in ("green", "amber"):
        card = report[name]
        assert card["overflow"] <= 0, f"{name} card overflows"
        assert card["cells"] == 30, f"{name} card dropped days from the heat block"
        assert card["left"] == 24, f"{name} month grid is not pinned to the content edge"
    assert report["green"]["empty"] == 27, "the unelapsed month must paint as empty cells, not holes"
    assert report["week"]["cells"] == 0, "the week card still paints its stretched cell row"
    assert report["week"]["overflow"] <= 0
    assert report["year"]["cells"] == 371, "the year card dropped days from the heat block"
    assert report["year"]["rightEdge"] == pytest.approx(336, abs=0.01), "the year map no longer reaches the content edge"


def test_the_month_grid_rule_draws_fixed_squares_at_their_own_width() -> None:
    """The month keeps the square grid: seven fixed columns at their own width,
    not columns stretched across the panel. Layout only shows in a browser, so
    the rule itself is what is read here; the grid it styles is built by the day
    map test above."""
    source = _source()
    month_rule = next(line for line in source.splitlines() if line.strip().startswith(".usage-report-month {"))
    assert "repeat(7, 24px)" in month_rule
    assert "width: max-content" in month_rule and "margin: 0 auto" in month_rule
    assert "minmax" not in month_rule, "the month grid still stretches"


def test_share_panel_offers_two_tiers_and_three_export_paths() -> None:
    """The buttons name the tier they export, and the exporter is handed that
    name as written: three paths, and each must be one the exporter knows."""
    source = _source()
    panel = source[source.index('data-i18n="usageReportShareTitle"') : source.index("<!-- Quota Tab -->")]
    assert re.findall(r'data-export="([^"]+)"', panel) == ["green", "amber", "all"]
    assert 'id="usageReportExportStatus" hidden' in panel, "the status line starts hidden"
    # A per-card save button would be a single-theme download path by another name.
    assert "data-save" not in panel


def test_every_export_writes_both_themes_for_the_tier_it_names() -> None:
    """Howard's rule: a download is never one file, and never the on-screen theme."""
    source = _source()
    export = extract_js_function(source, "async function usageReportExportCards(tier) {")
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
    share = extract_js_function(source, "async function usageReportRenderShare(model) {")
    assert share.index("usageReportState.previews = {}") < share.index("await usageReportCardFonts()"), (
        "the preview set has to be retired before the paint goes async"
    )
    version = extract_js_function(source, "function usageReportLoadVersion() {")
    assert "usageReportRenderShare(usageReportState.model)" in version


def test_a_landing_load_leaves_a_half_typed_nickname_alone() -> None:
    """The placeholder has to follow the server the report reads, and it is read on
    every render. The field's value is not: a load that lands while someone is
    still typing would otherwise wipe text that has not been committed yet.
    """
    block = _report_block(_source())
    render = extract_js_function(block, "function usageReportRender() {")
    assert "usageReportShareHint()" in render
    assert "usageReportSyncShareInputs()" not in render
    hint = extract_js_function(block, "function usageReportShareHint() {")
    assert ".value" not in hint, "only the placeholder may be written on a render"
    assert "placeholder = usageReportMachineName()" in hint
    wire = extract_js_function(block, "function usageReportWireShare() {")
    assert "usageReportSyncShareInputs()" in wire, "stored prefs still reach the field once, at wire time"


def test_share_card_colours_follow_the_theme_with_a_literal_fallback() -> None:
    """The card wears the active theme in both exported modes; the literal slate
    palette survives only as the no-stylesheet fallback (and the node tests)."""
    source = _source()
    palette = extract_js_function(source, "function usageReportCardPalette(mode) {")
    assert "usageReportThemeTokens(theme, dark)" in palette
    assert "getHeatColors(theme, dark)" in palette, "the ramp is the theme's own, not the default scale"
    assert "USAGE_REPORT_CARD_FALLBACK[mode]" in palette
    tokens = extract_js_function(source, "function usageReportThemeTokens(theme, dark) {")
    assert 'html.dark[data-ui-theme="' in tokens, "the dark export must read the theme's dark tokens"
    fallback = extract_js_function(source, "const USAGE_REPORT_CARD_FALLBACK = {")
    assert "var(--" not in fallback
    assert "heat: DEFAULT_HEAT_COLORS_LIGHT" in fallback
    assert "heat: DEFAULT_HEAT_COLORS_DARK" in fallback


def test_the_prefs_key_follows_the_convention_the_page_already_uses() -> None:
    """Every other stored key is `tokdash-*`; a lone dotted key is invisible to
    anyone grepping localStorage for this app."""
    source = _source()
    key = re.search(r"const USAGE_REPORT_PREFS_KEY = '([^']+)'", source)
    assert key and key.group(1).startswith("tokdash-"), key and key.group(1)
    assert "." not in key.group(1)


def test_share_prefs_carry_the_nickname_and_the_cost_choice() -> None:
    source = _source()
    read = extract_js_function(source, "function usageReportReadPrefs() {")
    write = extract_js_function(source, "function usageReportWritePrefs() {")
    for fn in (read, write):
        assert "nickname" in fn and "includeCost" in fn
    # Cost prints by default (the header is tokens + cost); the toggle is the
    # opt-out. Only a prefsV-stamped blob may carry that opt-out -- period
    # clicks write prefs too, so an unstamped includeCost:false is incidental.
    assert "prefsV === 2" in read, "only a stamped pref may turn cost off"
    assert "prefsV: 2" in write, "new writes must stamp the blob"


def test_a_control_changed_mid_flight_is_applied_late_not_dropped() -> None:
    """The report on screen belongs to the period and server it was fetched for,
    so a click during a load cannot be shown yet. It used to vanish without a
    trace; now the control snaps back and the choice runs when the load lands.
    """
    block = _report_block(_source())
    loader = extract_js_function(block, "async function loadUsageReport(options = {}) {")
    wire = extract_js_function(block, "function usageReportWire() {")
    # One exit, not two. The load used to return early on a total failure and had to
    # remember to settle the queue on both paths; the try/finally collapsed them, so
    # "every exit settles the queue" is now structurally true rather than duplicated.
    assert loader.count("usageReportApplyPendingChoice()") == 1
    assert loader.count("return;") == 1, "only the re-entrancy guard at the top may return early"
    assert "usageReportState.pendingPeriod = button.dataset.period" in wire
    assert "usageReportState.pendingServerId = select.value" in wire
    assert "select.value = usageReportState.serverId" in wire, "the box names the server actually on screen"
    applied = extract_js_function(block, "function usageReportApplyPendingChoice() {")
    # A plain load, not a forced one. The queued choice is a period or server the
    # reader has not seen yet, so it wants the cache like any other first load;
    # forcing a recompute made clicking during a load the slowest path in the tab.
    assert "loadUsageReport()" in applied
    assert "loadUsageReport({ refresh: true })" not in applied, (
        "a queued choice is a first read, not a refresh"
    )
    assert "usageReportWritePrefs()" in applied, "a queued choice that lands is the stored choice"


