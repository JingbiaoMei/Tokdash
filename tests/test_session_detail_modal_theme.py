"""Theme and translation contract for the session detail modal.

Clicking a session row in the Sessions explorer raises a modal with an
Overview & Graphs / View Chat switch, three token charts, a speed chart and the
message transcript. That markup used to carry a fixed slate-and-indigo palette,
so on the light themes the tab labels, graph headings and chat chips read as
dark-on-dark text, and none of those strings were in the six dictionaries, so
every locale showed English. Neither failure raises an error at runtime, so
both are pinned here:

* the modal markup paints with theme tokens only (no fixed palette utilities),
  and its labels clear a readable contrast in both modes, measured in Chromium;
* the modal's own code, run under node on the stub DOM in
  ``tests/support/i18n_profile.py``: the view switch marks its state with
  aria-pressed, every chart takes its ink and series colours from the theme and
  its words from the current language, an open modal repaints on a language or
  theme switch, a new open clears the last session and translates a failure,
  and the transcript is written in each of the six languages.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

from support.frontend import extract_js_function, needs_node
from support.i18n_profile import APPLY_I18N_FUNCTIONS, APPLY_I18N_STATE, page_script, run_page

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

MODAL_START = '<div id="sessionModal" class='
MODAL_END = "<!-- Slide-Over Session Inspector Drawer -->"

# The modal scrims are the one deliberate exception: a dark overlay behind a
# dialog is right in both modes, and both are spelled out here so a new fixed
# colour anywhere else in the modal still fails the test.
ALLOWED_FIXED_PALETTE = (
    "absolute inset-0 bg-slate-950/70 backdrop-blur-sm",
    "hidden fixed inset-0 z-[60] bg-slate-950/80 backdrop-blur-md "
    "flex items-center justify-center p-4 sm:p-8",
)

FIXED_PALETTE = re.compile(
    r"(?:bg|text|border|from|to)-(?:slate|gray|zinc|neutral|indigo|violet|purple|white)\b"
)

@pytest.fixture(scope="module")
def source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def modal_region(source: str) -> str:
    start = source.index(MODAL_START)
    return source[start : source.index(MODAL_END, start)]


# --- no fixed palette in the modal ------------------------------------------


def test_modal_markup_has_no_fixed_palette_classes(modal_region: str) -> None:
    offenders = []
    for classes in re.findall(r'class="([^"]+)"', modal_region):
        if FIXED_PALETTE.search(classes) and classes.strip() not in ALLOWED_FIXED_PALETTE:
            offenders.append(classes.strip())
    assert not offenders, (
        "the session modal must take its colours from the theme tokens "
        "(--color-text/-muted/-border/-primary/-messages); these fixed palette "
        f"utilities left it unreadable on light themes: {offenders}"
    )


def test_view_switch_buttons_carry_the_themed_class_and_state(modal_region: str) -> None:
    """The stylesheet paints the switch from .session-view-btn[aria-pressed];
    test_the_view_switch_marks_its_state_with_aria_pressed runs the switcher."""
    for element_id in ("sessionModalAnalyticsBtn", "sessionModalChatToggleBtn"):
        button = re.search(r'<button id="' + element_id + r'"[^>]*>', modal_region)
        assert button, element_id + " is gone"
        tag = button.group(0)
        assert "session-view-btn" in tag, element_id + " lost its themed class"
        assert 'aria-pressed="' in tag, element_id + " lost its selected state"


def test_every_modal_class_has_a_stylesheet_rule(modal_region: str, source: str) -> None:
    """Every session-modal class used by the markup has a rule to match."""
    style = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    used = set()
    for classes in re.findall(r'class="([^"]+)"', modal_region):
        used |= {c for c in classes.split() if c.startswith("session-")}
    missing = sorted(c for c in used if "." + c not in style)
    assert not missing, f"no stylesheet rule for {missing}"


# --- chart axis titles ------------------------------------------------------


def test_turn_chart_axis_titles_come_from_the_dictionary(source: str) -> None:
    """Axis titles are drawn on the canvas, so a literal there stays English
    however the rest of the dashboard is translated."""
    sites = [
        match.group(1)
        for match in re.finditer(r"(?<!function )buildTurnChartOptions\(\s*([^,)\n]+)", source)
    ]
    assert len(sites) >= 4, f"turn chart lost its axis-title call sites: {sites}"
    literal = [site for site in sites if not site.startswith("t(")]
    assert not literal, f"axis titles bypass t(): {literal}"


# --- the modal's own code, run under node -----------------------------------
#
# The renderers run on the stub DOM in tests/support/i18n_profile.py with the
# real dictionaries. Chart.js is replaced by createChart recording the config it
# would have been given, which is where every colour and axis word lives.


@needs_node
def test_the_view_switch_marks_its_state_with_aria_pressed(tmp_path: Path) -> None:
    """aria-pressed carries the selected state, so each theme paints its own pill;
    the class list is never rewritten. Opening the chat stops the speed read."""
    script = page_script(
        ["function setSessionModalView(mode) {"],
        prelude="""
const sessionSpeedModalState = { token: 3, request: 0, loading: true, payload: null, requestedGroup: 'g', controller: null, stopJob: null };
let activeSessionDetail = null;
document.body.append(
  el('button', { id: 'sessionModalAnalyticsBtn', class: 'session-view-btn', 'aria-pressed': 'true' }),
  el('button', { id: 'sessionModalChatToggleBtn', class: 'session-view-btn', 'aria-pressed': 'false' }),
  el('div', { id: 'sessionModalAnalyticsView' }),
  el('div', { id: 'sessionModalChatView' }),
);
""",
        body="""
let aborted = 0;
let stopped = 0;
sessionSpeedModalState.controller = { abort() { aborted += 1; } };
sessionSpeedModalState.stopJob = () => { stopped += 1; };
const ids = ['sessionModalAnalyticsBtn', 'sessionModalChatToggleBtn'];
const snap = () => ({
  views: ['sessionModalAnalyticsView', 'sessionModalChatView'].map((id) => document.getElementById(id).style.display),
  pressed: ids.map((id) => document.getElementById(id).getAttribute('aria-pressed')),
  classes: ids.map((id) => document.getElementById(id).className),
});
setSessionModalView('chat');
const chat = { ...snap(), aborted, stopped, stopJob: sessionSpeedModalState.stopJob,
  request: sessionSpeedModalState.request, loading: sessionSpeedModalState.loading };
activeSessionDetail = { tool: 'codex' };
setSessionModalView('analytics');
print({ chat, analytics: { ...snap(), reloads: calls('loadSessionSpeedTimeline') } });
""",
    )
    report = run_page(tmp_path, "session-view-switch", script)
    assert report["chat"] == {
        "views": ["none", "block"],
        "pressed": ["false", "true"],
        "classes": ["session-view-btn", "session-view-btn"],
        "aborted": 1,
        "stopped": 1,
        "stopJob": None,
        "request": 1,
        "loading": False,
    }
    assert report["analytics"] == {
        "views": ["block", "none"],
        "pressed": ["true", "false"],
        "classes": ["session-view-btn", "session-view-btn"],
        "reloads": [[3, False, "g"]],
    }


@needs_node
def test_session_charts_take_their_ink_and_words_from_the_theme_and_language(tmp_path: Path) -> None:
    """Every session chart (the three turn charts, the speed chart and the three
    full-screen views) paints ticks, axis titles, grid and legend with the
    theme's own --color-muted / --color-text, uses the series colours of the
    current mode, and names its axes and titles in the current language.

    Chart.js's defaults (#666 ticks, black-tinted grid) all but vanish on a dark
    card, and a canvas label stays English whatever the page is translated to.
    """
    script = page_script(
        [
            "function isDark() {",
            "function getCssVar(name) {",
            "function sessionChartInk() {",
            "function turnSeriesColors() {",
            "function turnSeriesFill(hex) {",
            "function makeTurnDatasets(series) {",
            "function buildTurnChartOptions(xAxisTitle, options = {}) {",
            "function buildTurnSeries(turns) {",
            "function renderSessionCharts(turns) {",
            "function buildCumulativeTurnSeries(turns) {",
            "function buildCumulativeTimeSeries(turns) {",
            "function expandSessionChart(type) {",
            "function renderSessionSpeedTimeline(payload) {",
        ],
        prelude="""
let sessionPerTurnChart = null;
let sessionCumulativeChart = null;
let sessionTimeCumulativeChart = null;
let sessionOutputSpeedChart = null;
let graphExpandChartInstance = null;
let graphExpandChartType = null;
let activeSessionDetail = null;
const sessionSpeedModalState = { group: '', view: 'chart' };
const charts = [];
function createChart(context, config) {
  charts.push({ canvas: context.canvas.id, config });
  return { config, data: config.data, destroy() {} };
}
function buildTimeCumulativeSeries() {
  return { labels: ['10:00', '10:05'],
    cumulative: { input: [1, 6], cache: [2, 8], output: [3, 10], reasoning: [4, 12], total: [10, 36] } };
}
function buildSessionSpeedPlot() {
  return { useTime: false, streams: [
    { stream: 'gpt-5', points: [{ x: 1, y: 40 }] },
    { stream: 'gpt-5-mini', points: [{ x: 2, y: 30 }] },
  ] };
}
function getChartPalette() { return ['#palette0', '#palette1']; }
function formatNumber(value) { return String(value); }
function formatPercent(value) { return `${Math.round(Number(value) * 100)}%`; }
function formatTokenCount(value) { return String(value); }
function createTableCell(tag, className, content) { return el(tag, className ? { class: className } : {}, String(content ?? '')); }
document.body.append(
  ...['sessionPerTurnChart', 'sessionCumulativeChart', 'sessionTimeCumulativeChart'].map((id) => el('canvas', { id })),
  el('div', { id: 'graphExpandModal', class: 'hidden' },
    el('h3', { id: 'graphExpandTitle' }),
    el('p', { id: 'graphExpandSubtitle' }),
    el('canvas', { id: 'graphExpandCanvas' })),
  el('section', { id: 'sessionSpeedPanel' },
    el('p', { id: 'sessionSpeedCoverage' }),
    el('canvas', { id: 'sessionOutputSpeedChart' }),
    el('table', {}, el('tbody', { id: 'sessionSpeedBody' }))),
);
""",
        i18n=True,
        body="""
const root = document.documentElement;
const turns = [
  { turn_index: 1, tokens: 10, tokens_in: 1, tokens_cache: 2, tokens_out: 3, tokens_reasoning: 4, timestamp: '2026-10-01T10:00:00Z' },
  { turn_index: 2, tokens: 26, tokens_in: 5, tokens_cache: 6, tokens_out: 7, tokens_reasoning: 8, timestamp: '2026-10-01T10:05:00Z' },
];
activeSessionDetail = { tool: 'codex', data: { turns } };
const speed = { groups: [{ model: 'gpt-5', speed_calls: 3 }], summary: {}, cache: { state: 'ready' },
  series: [{ model: 'gpt-5', measurement_status: 'measured', output_tok_per_s: 40, sequence: 1 }] };
function inkOf(config) {
  const { x, y } = config.options.scales;
  return {
    x: [x.title.color, x.ticks.color, x.grid.color],
    y: [y.title ? y.title.color : null, y.ticks.color, y.grid.color],
    legend: config.options.plugins.legend.labels.color,
    xTitle: x.title.text,
    yTitle: y.title ? y.title.text : null,
  };
}
const out = {};
for (const [mode, lang, tokens] of [
  ['light', 'en', { '--color-muted': '#muted-light', '--color-text': '#text-light' }],
  ['dark', 'ja', { '--color-muted': '#muted-dark', '--color-text': '#text-dark' }],
]) {
  root.classList.toggle('dark', mode === 'dark');
  root.__computed = tokens;
  currentLang = lang;
  charts.length = 0;
  renderSessionCharts(turns);
  renderSessionSpeedTimeline(speed);
  const expanded = [];
  for (const type of ['perTurn', 'cumulative', 'timeline']) {
    expandSessionChart(type);
    expanded.push([document.getElementById('graphExpandTitle').textContent,
      document.getElementById('graphExpandSubtitle').textContent]);
  }
  out[mode] = {
    ink: sessionChartInk(),
    colors: turnSeriesColors(),
    charts: charts.map(({ canvas, config }) => ({
      canvas,
      type: config.type,
      ink: inkOf(config),
      series: config.data.datasets.map((dataset) => dataset.borderColor || dataset.backgroundColor),
      labels: config.data.labels || null,
    })),
    expanded,
    words: {
      expanded: [
        [t('graphExpandTitle', { chart: t('turnTrend') }), t('graphExpandSubPerTurn')],
        [t('graphExpandTitle', { chart: t('cumulativeTurnTrend') }), t('graphExpandSubCumulative')],
        [t('graphExpandTitle', { chart: t('cumulativeTrendByTime') }), t('graphExpandSubTimeline')],
      ],
      turnAxis: t('sessionSpeedColTurn'),
      timeAxis: t('time'),
      turnLabels: [t('turnNumber', { n: 1 }), t('turnNumber', { n: 2 })],
      speedAxes: [t('sessionSpeedResponseOrder'), t('sessionSpeedColRate')],
    },
  };
}
// Both take placeholders, so the parity test cannot see them.
out.absent = Object.keys(I18N).filter((lang) => ['graphExpandTitle', 'turnNumber']
  .some((key) => !Object.prototype.hasOwnProperty.call(I18N[lang], key)));
print(out);
""",
    )
    report = run_page(tmp_path, "session-chart-ink", script)
    assert report.pop("absent") == [], "a language falls back to English for the chart titles"
    assert report["light"]["ink"] == {
        "tick": "#muted-light",
        "legend": "#text-light",
        "grid": "rgba(100,116,139,0.24)",
    }
    assert report["dark"]["ink"] == {
        "tick": "#muted-dark",
        "legend": "#text-dark",
        "grid": "rgba(148,163,184,0.2)",
    }
    light_colors = set(report["light"]["colors"].values())
    assert light_colors.isdisjoint(report["dark"]["colors"].values()), "one series palette serves both modes"

    for mode, row in report.items():
        ink, colors, words = row["ink"], row["colors"], row["words"]
        assert [chart["canvas"] for chart in row["charts"]] == [
            "sessionPerTurnChart",
            "sessionCumulativeChart",
            "sessionTimeCumulativeChart",
            "sessionOutputSpeedChart",
            "graphExpandCanvas",
            "graphExpandCanvas",
            "graphExpandCanvas",
        ], mode
        for chart in row["charts"]:
            where = f"{mode} {chart['canvas']}"
            assert chart["ink"]["x"] == [ink["tick"], ink["tick"], ink["grid"]], where
            assert chart["ink"]["y"][1:] == [ink["tick"], ink["grid"]], where
            assert chart["ink"]["y"][0] in (None, ink["tick"]), where
            assert chart["ink"]["legend"] == ink["legend"], where

        line = [colors[key] for key in ("input", "cache", "output", "reasoning", "total")]
        turn_charts = [chart for chart in row["charts"] if chart["canvas"] != "sessionOutputSpeedChart"]
        for chart in turn_charts:
            expected = line[:4] if chart["type"] == "bar" else line
            assert chart["series"] == expected, f"{mode} {chart['canvas']} ignores turnSeriesColors"

        assert row["expanded"] == words["expanded"], mode
        assert [chart["ink"]["xTitle"] for chart in turn_charts] == [
            words["turnAxis"], words["turnAxis"], words["timeAxis"],
            words["turnAxis"], words["turnAxis"], words["timeAxis"],
        ], mode
        speed = row["charts"][3]
        assert [speed["ink"]["xTitle"], speed["ink"]["yTitle"]] == words["speedAxes"], mode
        assert row["charts"][4]["labels"] == words["turnLabels"], mode
    assert report["dark"]["words"]["expanded"] != report["light"]["words"]["expanded"]


MODAL_REPAINTS = (
    "renderSessionModalMeta",
    "setShareBar",
    "renderSessionCharts",
    "renderSessionSpeedTimeline",
    "renderSessionChat",
    "expandSessionChart",
)


@needs_node
def test_an_open_modal_repaints_on_a_language_or_theme_switch(tmp_path: Path) -> None:
    """A theme switch (rerenderPresentation) and a language switch (applyI18n,
    through rerenderTokenPresentation) both repaint an open modal; a closed one
    is left alone.

    The speed chart repaints before the full-screen view is rebuilt, because the
    expanded copy clones the small chart's options and would keep the old ink.
    """
    script = page_script(
        ["function rerenderPresentation() {", "function rerenderTokenPresentation() {", *APPLY_I18N_FUNCTIONS],
        prelude=APPLY_I18N_STATE
        + """
let toolChart = null;
let modelChart = null;
let lastUsageResponse = null;
let statsLoaded = false;
const calendarState = { contributions: [], view: 'month' };
const usageReportState = { model: null, loading: false };
let activeDayDetail = null;
const turns = [{ turn_index: 1 }, { turn_index: 2 }];
const session = { tokens: 10, tokens_in: 4, tokens_cache: 3, tokens_out: 2, tokens_reasoning: 1 };
let activeSessionDetail = { tool: 'codex', data: { session, turns } };
const speedPayload = { series: ['measured'] };
const chatPayload = { messages: ['hello'], toolCalls: ['exec'] };
const sessionSpeedModalState = { payload: speedPayload };
let sessionChatPayload = chatPayload;
let graphExpandChartType = 'speed';
document.body.append(
  el('section', { id: 'stats-content', class: 'tab-content' }),
  el('div', { id: 'sessionModal', class: 'fixed inset-0 z-50' },
    ...['Input', 'Cache', 'Output', 'Total'].map((name) => el('span', { id: `sessionModal${name}` }))),
);
""",
        i18n=True,
        body=f"const REPAINTS = {json.dumps(MODAL_REPAINTS)};\n"
        + """
function repaints(run) {
  __calls.length = 0;
  run();
  return __calls.filter((call) => REPAINTS.includes(call.name)).map((call) => [call.name, ...call.args]);
}
const both = () => ({
  theme: repaints(() => rerenderPresentation()),
  language: repaints(() => { currentLang = currentLang === 'ja' ? 'ko' : 'ja'; applyI18n(); }),
});
const open = both();
graphExpandChartType = null;
sessionSpeedModalState.payload = null;
sessionChatPayload = null;
const nothingExtra = both();
graphExpandChartType = 'speed';
sessionSpeedModalState.payload = speedPayload;
sessionChatPayload = chatPayload;
document.getElementById('sessionModal').classList.add('hidden');
const closed = both();
print({ open, nothingExtra, closed });
""",
    )
    report = run_page(tmp_path, "session-modal-repaints", script)
    turns = [{"turn_index": 1}, {"turn_index": 2}]
    session = {"tokens": 10, "tokens_in": 4, "tokens_cache": 3, "tokens_out": 2, "tokens_reasoning": 1}
    speed = ["renderSessionSpeedTimeline", {"series": ["measured"]}]
    shares = [
        ["setShareBar", "sessionShareInput", 4, 10],
        ["setShareBar", "sessionShareCache", 3, 10],
        ["setShareBar", "sessionShareOutput", 2, 10],
        ["setShareBar", "sessionShareReasoning", 1, 10],
    ]
    assert report["open"]["theme"] == [
        ["renderSessionCharts", turns],
        speed,
        ["expandSessionChart", "speed"],
    ]
    assert report["open"]["language"] == [
        ["renderSessionModalMeta", "codex", session],
        *shares,
        ["renderSessionCharts", turns],
        speed,
        ["renderSessionChat", ["hello"], ["exec"]],
        ["expandSessionChart", "speed"],
    ]
    for calls in report["open"].values():
        names = [call[0] for call in calls]
        assert names.index("renderSessionSpeedTimeline") < names.index("expandSessionChart")
    assert report["nothingExtra"] == {
        "theme": [["renderSessionCharts", turns]],
        "language": [["renderSessionModalMeta", "codex", session], *shares, ["renderSessionCharts", turns]],
    }
    assert report["closed"] == {"theme": [], "language": []}


@needs_node
def test_opening_a_session_clears_the_last_one_and_translates_a_failure(tmp_path: Path) -> None:
    """A new open blanks the previous session's figures, bars and charts before
    it reads; a failed read says so in the current language and follows a
    language switch through its data-i18n hook; the next open takes that hook
    off before reading, so a later switch cannot paint the failure over a
    loaded session, whose meta line is rewritten in the new language instead.
    """
    script = page_script(
        [
            "async function openSessionModal(tool, sessionId, ownerServer = null) {",
            "function renderSessionModalMeta(tool, session) {",
            "function renderLoadingPlaceholder(element, label = t('loading')) {",
            "function rerenderTokenPresentation() {",
            *APPLY_I18N_FUNCTIONS,
        ],
        prelude=APPLY_I18N_STATE
        + """
let toolChart = null;
let lastUsageResponse = null;
let statsLoaded = false;
const calendarState = { view: 'month' };
let activeDayDetail = null;
let activeSessionDetail = null;
let sessionChatPayload = null;
let graphExpandChartType = null;
const sessionSpeedModalState = { token: 1, payload: null };
function sessionModalCurrent() { return true; }
function formatToolName(tool) { return tool === 'codex' ? 'Codex' : tool; }
function formatNumber(value) { return String(value); }
function formatDuration(ms) { return `${Math.round(Number(ms || 0) / 1000)}s`; }
console.error = () => {};
const SHARES = ['sessionShareInput', 'sessionShareCache', 'sessionShareOutput', 'sessionShareReasoning'];
document.body.append(
  el('section', { id: 'stats-content', class: 'tab-content' }),
  el('div', { id: 'sessionModal', class: 'hidden fixed inset-0 z-50' },
    el('h2', { id: 'sessionModalTitle' }),
    el('p', { id: 'sessionModalMeta' }),
    ...['Input', 'InputPct', 'Cache', 'CachePct', 'Output', 'OutputPct', 'Total', 'ReasoningPct']
      .map((name) => el('span', { id: `sessionModal${name}` })),
    ...SHARES.flatMap((prefix) => [el('span', { id: `${prefix}Label` }), el('div', { id: `${prefix}Bar` })]),
    el('section', { id: 'sessionSpeedPanel' })),
);
function paintLastSession() {
  SHARES.forEach((prefix) => {
    document.getElementById(`${prefix}Label`).textContent = '60% · 600';
    document.getElementById(`${prefix}Bar`).style.width = '60%';
  });
}
// What the modal shows at the moment it starts reading the next session.
const reads = [];
let answer = null;
function fetchFromHost(server, url) {
  reads.push({
    url,
    metaHook: document.getElementById('sessionModalMeta').getAttribute('data-i18n'),
    labels: SHARES.map((prefix) => document.getElementById(`${prefix}Label`).textContent),
    bars: SHARES.map((prefix) => document.getElementById(`${prefix}Bar`).style.width),
    chartsCleared: calls('renderSessionCharts').some((args) => Array.isArray(args[0]) && args[0].length === 0),
  });
  return answer();
}
""",
        i18n=True,
        body="""
const meta = document.getElementById('sessionModalMeta');
const line = () => ({ text: meta.textContent, hook: meta.getAttribute('data-i18n') });
paintLastSession();
__calls.length = 0;
answer = () => Promise.reject(new Error('session gone'));
await openSessionModal('codex', 'abc123');
const failed = line();
currentLang = 'ja';
applyI18n();
const failedJa = line();
renderSessionModalMeta('codex', { project: 'p', model: 'm', token_events: 1, active_ms: 1000, span_ms: 1000 });
const untouched = line();
paintLastSession();
__calls.length = 0;
answer = () => Promise.resolve({
  session: { project: 'tokdash', model: 'gpt-5', token_events: 7, active_ms: 5000, span_ms: 9000, tokens: 10, tokens_in: 4 },
  turns: [], messages: [], tool_calls: [],
});
await openSessionModal('codex', 'def456');
const loaded = line();
currentLang = 'ko';
applyI18n();
const loadedKo = line();
const words = (lang) => ({ failed: I18N[lang].sessionDetailLoadFailed, events: I18N[lang].events, activeTime: I18N[lang].activeTime });
print({ reads, failed, failedJa, untouched, loaded, loadedKo, en: words('en'), ja: words('ja'), ko: words('ko') });
""",
    )
    report = run_page(tmp_path, "session-modal-open", script)
    blank = {"metaHook": None, "labels": ["-"] * 4, "bars": ["0%"] * 4, "chartsCleared": True}
    assert report["reads"] == [
        {"url": "/api/session?tool=codex&session_id=abc123", **blank},
        {"url": "/api/session?tool=codex&session_id=def456", **blank},
    ]
    en, ja, ko = report["en"], report["ja"], report["ko"]
    assert ja["failed"] != en["failed"]
    assert report["failed"] == {"text": en["failed"], "hook": "sessionDetailLoadFailed"}
    assert report["failedJa"] == {"text": ja["failed"], "hook": "sessionDetailLoadFailed"}
    assert report["untouched"] == report["failedJa"]
    assert report["loaded"] == {
        "text": f"Codex · tokdash · gpt-5 · 7 {ja['events']} · {ja['activeTime']} 5s",
        "hook": None,
    }
    assert report["loadedKo"] == {
        "text": f"Codex · tokdash · gpt-5 · 7 {ko['events']} · {ko['activeTime']} 5s",
        "hook": None,
    }


@needs_node
def test_the_transcript_is_written_in_the_current_language(tmp_path: Path) -> None:
    """Every label renderSessionChat writes comes from the dictionary of the
    current language, and every language defines the ones that reach t() with
    placeholders or through a variable (the three role names), which the i18n
    parity test cannot follow. No raw key ever shows."""
    script = page_script(
        ["function renderSessionChat(messages, toolCalls) {", "function escapeHtml(str) {"],
        prelude="""
let sessionChatPayload = null;
document.body.append(
  el('span', { id: 'sessionChatMsgCount' }),
  el('span', { id: 'sessionChatToolCount' }),
  el('div', { id: 'sessionChatContainer' }),
);
""",
        i18n=True,
        body="""
const messages = [
  { role: 'user', content_chars: 1840, timestamp: '2026-10-11 09:14:22' },
  { role: 'assistant', has_reasoning: true, token_count: 512, has_tool_calls: true, model: 'gpt-5' },
  { role: 'system' },
];
const KEYS = Object.keys(I18N.en).filter((key) => key.startsWith('sessionChat'));
// t() falls back to English, so a key missing from one language would still
// render; these reach t() with placeholders or through a variable, where the
// parity test cannot see them.
const RUNTIME_KEYS = ['sessionChatMessages', 'sessionChatToolCalls', 'sessionChatMessageIndex', 'sessionChatChars',
  'sessionChatTokens', 'sessionChatRoleUser', 'sessionChatRoleAssistant', 'sessionChatRoleSystem'];
const container = document.getElementById('sessionChatContainer');
const out = {};
for (const lang of Object.keys(I18N)) {
  currentLang = lang;
  renderSessionChat(messages, [{ name: 'exec' }]);
  const html = container.innerHTML;
  const counts = [document.getElementById('sessionChatMsgCount').textContent, document.getElementById('sessionChatToolCount').textContent];
  const want = [
    t('sessionChatMessageIndex', { role: t('sessionChatRoleUser'), index: 1 }),
    t('sessionChatMessageIndex', { role: t('sessionChatRoleAssistant'), index: 2 }),
    t('sessionChatMessageIndex', { role: t('sessionChatRoleSystem'), index: 3 }),
    t('sessionChatChars', { count: Number(1840).toLocaleString() }),
    t('sessionChatTokens', { count: Number(512).toLocaleString() }),
    t('sessionChatToolCallChip'),
    t('sessionChatReasoning'),
    t('sessionChatNoContent'),
  ];
  renderSessionChat([], []);
  const empty = container.innerHTML;
  const emptyWant = [t('sessionChatEmpty'), t('sessionChatEmptyHint')];
  out[lang] = {
    missing: want.filter((label) => !html.includes(escapeHtml(label))),
    emptyMissing: emptyWant.filter((label) => !empty.includes(escapeHtml(label))),
    rawKeys: KEYS.filter((key) => html.includes(key) || empty.includes(key)),
    counts,
    countsWant: [t('sessionChatMessages', { count: 3 }), t('sessionChatToolCalls', { count: 1 })],
    absent: RUNTIME_KEYS.filter((key) => !Object.prototype.hasOwnProperty.call(I18N[lang], key)),
    user: t('sessionChatRoleUser'),
  };
}
print(out);
""",
    )
    report = run_page(tmp_path, "session-chat-language", script)
    assert len(report) == 6
    for lang, row in report.items():
        assert row["missing"] == [], lang
        assert row["emptyMissing"] == [], lang
        assert row["rawKeys"] == [], lang
        assert row["counts"] == row["countsWant"], lang
        assert row["absent"] == [], f"{lang} falls back to English for {row['absent']}"
    assert report["zh"]["user"] != report["en"]["user"]


# --- the labels are actually readable in both modes -------------------------

# renderSessionChat() builds the transcript cards; the markup below reproduces
# them statically so the stylesheet can be measured without booting the
# dashboard. test_chat_fixture_classes_match_the_renderer keeps it honest.
CHAT_FIXTURE = """
<div class="session-chat-summary flex items-center justify-between gap-2 p-3.5 rounded-xl">
  <span id="sessionChatMsgCount" class="session-chat-chip text-xs font-mono font-bold px-2 py-0.5 rounded">3 messages</span>
</div>
<div class="session-chat-card session-chat-card--user p-4 rounded-2xl shadow-md">
  <div class="session-chat-card-head flex items-center justify-between mb-3 pb-2">
    <span class="session-chat-role px-2.5 py-0.5 rounded-full text-[11px] font-mono font-bold uppercase tracking-wider border" data-i18n-dynamic>User #1</span>
    <span class="session-chat-time text-[11px] font-mono">2026-10-11 09:14:22</span>
  </div>
  <div class="session-chat-meta flex items-center gap-2 text-xs">
    <span class="session-chat-meta-chip text-[10px] px-2 py-0.5 rounded font-mono" data-i18n-dynamic>1,840 chars</span>
  </div>
</div>
<div class="session-chat-card session-chat-card--assistant p-4 rounded-2xl shadow-md">
  <div class="session-chat-reasoning rounded-xl px-3.5 py-2.5 text-xs font-semibold flex items-center gap-2">Reasoning recorded</div>
</div>
<div class="session-chat-empty p-8 text-center rounded-2xl">No conversational transcript stored for this session.</div>
"""

CONTRAST_SELECTORS = {
    "idle tab label": "#sessionModalChatToggleBtn span",
    "selected tab label": "#sessionModalAnalyticsBtn span",
    "modal title": "#sessionModalTitle",
    "message count chip": "#sessionChatMsgCount",
    "chat view note": "#sessionModalChatView .session-chat-note",
    "role badge": ".session-chat-role",
    "meta chip": ".session-chat-meta-chip",
    "message timestamp": ".session-chat-time",
    "empty transcript note": ".session-chat-empty",
    "chart heading": ".session-graph-title",
    "chart subtitle": ".session-modal-sub",
}

# Foreground colour plus the background composited over every translucent
# ancestor, so the caller can take a WCAG ratio.
PROBE_JS = """
(selector) => {
  const scratch = document.createElement('canvas').getContext('2d');
  const parse = (value, depth) => {
    let m = value.match(/rgba?\\(([^)]+)\\)/);
    if (m) {
      const p = m[1].split(/[\\s,/]+/).filter(Boolean).map(Number);
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    }
    // A computed color-mix() arrives as color(srgb r g b / a), channels on 0..1.
    m = value.match(/^color\\(\\s*\\S+\\s+([^)]+)\\)/);
    if (m) {
      const p = m[1].split(/[\\s,/]+/).filter(Boolean).map(Number);
      return { r: p[0] * 255, g: p[1] * 255, b: p[2] * 255, a: p.length > 3 ? p[3] : 1 };
    }
    m = value.match(/^#([0-9a-f]{6})$/i);
    if (m) {
      const n = parseInt(m[1], 16);
      return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 };
    }
    if (depth) return null;
    // Wide-gamut forms: let the 2D canvas resolve them down to sRGB.
    scratch.fillStyle = '#000';
    scratch.fillStyle = value;
    const flat = String(scratch.fillStyle);
    return flat === value ? null : parse(flat, 1);
  };
  const over = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a),
    a: 1,
  });
  const el = document.querySelector(selector);
  if (!el) return null;
  const layers = [];
  for (let node = el; node; node = node.parentElement) {
    const c = parse(getComputedStyle(node).backgroundColor);
    if (c && c.a > 0) {
      layers.push(c);
      if (c.a >= 1) break;
    }
    if (!node.parentElement) break;
  }
  const root = parse(getComputedStyle(document.documentElement).backgroundColor);
  let bg = root && root.a >= 1 ? root : { r: 255, g: 255, b: 255, a: 1 };
  for (const layer of layers.reverse()) bg = over(layer, bg);
  return { fg: parse(getComputedStyle(el).color), bg };
}
"""


def _channel(value: float) -> float:
    scaled = value / 255
    return scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4


def _contrast(fg: dict, bg: dict) -> float:
    def luminance(color):
        return (
            0.2126 * _channel(color["r"])
            + 0.7152 * _channel(color["g"])
            + 0.0722 * _channel(color["b"])
        )

    light = max(luminance(fg), luminance(bg))
    dark = min(luminance(fg), luminance(bg))
    return (light + 0.05) / (dark + 0.05)


@pytest.fixture(scope="module")
def chromium():
    """One headless Chromium for the module's measured pages.

    Playwright and Chromium stay optional developer tools, so the default suite
    runs without adding a browser dependency to the package. The fixture is
    module-scoped because the suite redirects HOME per test, and the browser
    lives under ~/.cache; it is shared because a second sync Playwright cannot
    start while the first is running.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=os.environ.get("TOKDASH_TEST_CHROMIUM"),
                args=["--disable-gpu", "--disable-features=Vulkan,VizDisplayCompositor"],
            )
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Playwright Chromium is not installed")
            raise
        try:
            yield browser
        finally:
            browser.close()


def _measured_markup(modal_region: str) -> str:
    return (
        modal_region.replace(MODAL_END, "")
        # Show the modal and both views: the measurement is closer to the
        # rendered page when nothing sits at display:none.
        .replace('class="hidden fixed inset-0 z-50"', 'class="fixed inset-0 z-50"')
        .replace('style="display:none;"', "")
    )


@pytest.fixture(scope="module")
def modal_pages(source: str, modal_region: str, chromium):
    """One page per mode, holding the real stylesheet and the real modal markup.

    Each page is built already in its mode. Toggling the class on a page that
    has been measured once answers with the previous mode's colours, because a
    style change is only re-resolved for elements that get laid out.
    """
    style = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    markup = _measured_markup(modal_region)
    pages = {}
    for mode in ("light", "dark"):
        page = chromium.new_page(viewport={"width": 1600, "height": 1000})
        page.set_content(
            '<!doctype html><html class="' + ("dark" if mode == "dark" else "") + '">'
            "<head><style>" + style + "</style></head><body>"
            + markup + CHAT_FIXTURE + "</body></html>"
        )
        page.wait_for_timeout(100)
        pages[mode] = page
    yield pages
    for page in pages.values():
        page.close()


def test_chat_fixture_classes_match_the_renderer(source: str, modal_region: str) -> None:
    """The measured markup cannot drift away from the modal and its renderer."""
    owned = extract_js_function(source, "function renderSessionChat(") + modal_region
    used = {
        name
        for group in re.findall(r'class="([^"]+)"', CHAT_FIXTURE)
        for name in group.split()
        if name.startswith("session-chat")
    }
    missing = sorted(name for name in used if name not in owned)
    assert not missing, f"neither the markup nor renderSessionChat emits {missing}"


@pytest.mark.parametrize("mode", ("light", "dark"))
def test_modal_labels_clear_aa_contrast(modal_pages, mode: str) -> None:
    """Both modes keep every modal label above the WCAG AA small-text ratio."""
    page = modal_pages[mode]
    thin = []
    for label, selector in CONTRAST_SELECTORS.items():
        colors = page.evaluate(PROBE_JS, selector)
        assert colors and colors["fg"], f"{label} not found in the modal markup"
        ratio = _contrast(colors["fg"], colors["bg"])
        if ratio < 4.5:
            thin.append(f"{label} = {ratio:.2f}:1")
    assert not thin, f"{mode} mode leaves these modal labels under the 4.5:1 AA ratio: {thin}"


# --- the #217 follow-ups -----------------------------------------------------


def test_script_written_modal_labels_are_marked_for_the_cjk_rules(source: str, modal_region: str) -> None:
    """#215's CJK rules key off data-i18n, which these labels cannot carry."""
    for element_id in ("graphExpandTitle", "sessionChatMsgCount", "sessionChatToolCount", "sessionSpeedCoverage"):
        tag = re.search(r'<[a-z0-9]+ [^>]*id="' + element_id + r'"[^>]*>', modal_region)
        assert tag and "data-i18n-dynamic" in tag.group(0), element_id + " misses the CJK rules"
    chat = extract_js_function(source, "function renderSessionChat(")
    role = re.search(r'<span class="session-chat-role[^"]*"[^>]*>', chat).group(0)
    assert "data-i18n-dynamic" in role, "the role badge misses the CJK rules"
    assert '"session-chat-meta-chip text-[10px] px-2 py-0.5 rounded font-mono" data-i18n-dynamic>${escapeHtml(c)}' in chat
    # The model chip shows a model id, not a translation: it keeps the mono face.
    assert "font-mono\">${escapeHtml(m.model)}" in chat
    for lang in ("zh-CN", "ja", "ko"):
        assert f"html:lang({lang}) [data-i18n-dynamic]:is(.mono, .font-mono)" in source, lang
        assert f"html:lang({lang}) [data-i18n-dynamic]," in source or f"html:lang({lang}) [data-i18n-dynamic] {{" in source, lang


def test_share_bars_use_the_series_colours_of_their_mode(source: str, modal_region: str) -> None:
    """Inline light hexes kept the bars light on dark themes while the charts
    beside them switched to the brighter set."""
    for name in ("Input", "Cache", "Output", "Reasoning"):
        bar = re.search(r'<div id="sessionShare' + name + r'Bar"[^>]*>', modal_region).group(0)
        assert "background" not in bar, name + " bar hardcodes its colour inline"
        assert "session-share-fill--" + name.lower() in bar
    # turnSeriesColors() returns `isDark() ? { dark set } : { light set }`.
    dark, light = re.findall(r"\{ (input: [^}]+)\}", extract_js_function(source, "function turnSeriesColors("))
    style = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    for mode, palette, prefix in (("light", light, "    ."), ("dark", dark, "    html.dark .")):
        for series in ("input", "cache", "output", "reasoning"):
            hex_color = re.search(series + r': "(#[0-9A-F]{6})"', palette).group(1)
            rule = prefix + "session-share-fill--" + series + " { background: " + hex_color + "; }"
            assert rule in style, f"{mode} {series} bar does not match turnSeriesColors ({hex_color})"


# Tailwind's play build injects its utilities after this stylesheet, so at equal
# specificity a utility wins. These stand-ins load in the same order, carrying
# the values the real utilities set.
TAILWIND_AFTER = (
    ".text-xs{font-size:.75rem;line-height:1rem}"
    ".font-mono{font-family:ui-monospace,SFMono-Regular,monospace}"
    ".font-bold{font-weight:700}.font-extrabold{font-weight:800}"
    ".tracking-wider{letter-spacing:.05em}.uppercase{text-transform:uppercase}"
)

FOLLOWUP_PROBE_JS = """
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const style = getComputedStyle(el);
  return { family: style.fontFamily, weight: style.fontWeight, spacing: style.letterSpacing,
           lineHeight: style.lineHeight, fontSize: style.fontSize, background: style.backgroundColor };
}
"""


@pytest.fixture(scope="module")
def followup_pages(source: str, modal_region: str, chromium):
    """Pages per language and mode with the real stylesheet, the modal markup,
    the transcript fixture, and Tailwind-order utilities after them."""
    style = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    markup = _measured_markup(modal_region)
    pages = {}
    for lang, mode in (("en", "light"), ("zh-CN", "light"), ("ja", "light"), ("ko", "light"), ("en", "dark")):
        page = chromium.new_page(viewport={"width": 1600, "height": 1000})
        page.set_content(
            f'<!doctype html><html lang="{lang}" class="{"dark" if mode == "dark" else ""}">'
            "<head><style>" + style + "</style><style>" + TAILWIND_AFTER + "</style></head><body>"
            + markup + CHAT_FIXTURE + "</body></html>"
        )
        page.wait_for_timeout(100)
        pages[(lang, mode)] = page
    yield pages
    for page in pages.values():
        page.close()


def _probe(page, selector: str) -> dict:
    found = page.evaluate(FOLLOWUP_PROBE_JS, selector)
    assert found, selector + " not found"
    return found


@pytest.mark.parametrize("lang", ("zh-CN", "ja", "ko"))
def test_script_written_labels_take_the_cjk_rules(followup_pages, lang: str) -> None:
    page = followup_pages[(lang, "light")]
    title = _probe(page, "#graphExpandTitle")
    assert title["family"].startswith('"Fira Sans"'), title["family"]
    assert title["weight"] == "700", "an 800 heading is past the heaviest CJK weight"
    role = _probe(page, ".session-chat-role")
    assert role["spacing"] == "normal", "tracking splits Han characters apart"
    assert role["family"].startswith('"Fira Sans"'), role["family"]
    chip = _probe(page, ".session-chat-meta-chip")
    assert chip["weight"] == "400", "a regular chip must not turn bold"
    count = _probe(page, "#sessionChatMsgCount")
    assert count["family"].startswith('"Fira Sans"'), count["family"]


def test_latin_labels_keep_their_mono_face_and_tracking(followup_pages) -> None:
    page = followup_pages[("en", "light")]
    title = _probe(page, "#graphExpandTitle")
    assert title["family"].startswith('"Fira Code"'), title["family"]
    assert title["weight"] == "800"
    role = _probe(page, ".session-chat-role")
    assert role["spacing"] != "normal", "English keeps the badge's tracking"


def test_share_bars_switch_to_the_bright_set_in_dark_mode(followup_pages) -> None:
    light = _probe(followup_pages[("en", "light")], "#sessionShareInputBar")["background"]
    dark = _probe(followup_pages[("en", "dark")], "#sessionShareInputBar")["background"]
    assert light == "rgb(30, 64, 175)", light
    assert dark == "rgb(96, 165, 250)", dark


def test_empty_transcript_note_gets_its_own_leading(followup_pages, source: str) -> None:
    empty = re.search(r'<div class="(session-chat-empty[^"]*)"', extract_js_function(source, "function renderSessionChat(")).group(1)
    assert "text-xs" not in empty.split(), "a text-xs utility brings back its 1rem leading"
    note = _probe(followup_pages[("en", "light")], ".session-chat-empty")
    assert note["fontSize"] == "12px"
    assert note["lineHeight"] == "20.4px", "a utility's 1rem leading overrides the note's 1.7"
