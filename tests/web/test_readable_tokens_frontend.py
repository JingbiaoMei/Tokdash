"""Tests for the readable tokens frontend.

Covers the human-readable token formatting in the dashboard, the switch that
turns it on and off, and the settings panel the switch lives in. The formatters,
the switch and the panel run under node; what the Overview's Tokens card prints
with the formatting on is checked by the render itself, in
test_overview_empty_states_frontend.py.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


from support.frontend import INDEX_HTML, extract_js_function, index_source, needs_node, run_node


def _run_readable_token_js(
    tmp_path: Path, expression: str, payload: object
) -> object:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        extract_js_function(source, signature)
        for signature in (
            "function normalizeTokenCount(value) {",
            "function normalizeOverviewTokenCount(value) {",
            "function formatCompactTokenCount(value) {",
            "function formatTokenCount(value, includeUnit = false) {",
            "function loadOverviewReadableTokensPreference(storage = null) {",
            "function saveOverviewReadableTokensPreference(enabled, storage = null) {",
        )
    )
    key_match = (
        "const OVERVIEW_READABLE_TOKENS_STORAGE_KEY = "
        "'tokdash-overview-readable-tokens';"
    )
    assert key_match in source
    harness = tmp_path / "readable-tokens.js"
    harness.write_text(
        "let currentLang = 'en';\n"
        "const LANG_LOCALES = { en: 'en-US', zh: 'zh-CN', ja: 'ja-JP', ko: 'ko-KR', es: 'es-ES', pt: 'pt-BR' };\n"
        "function langLocale(lang = currentLang) { return LANG_LOCALES[lang] || 'en-US'; }\n"
        "const LABELS = { tokensUnit: 'tokens' };\n"
        "function t(key) { return LABELS[key] || key; }\n"
        "function formatNumber(value) { return Number(value || 0).toLocaleString('en-US'); }\n"
        "let overviewReadableTokens = true;\n"
        + key_match
        + "\n"
        + functions
        + "\nconst payload = JSON.parse(process.argv[2]);\n"
        + f"const result = {expression};\n"
        + "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    return json.loads(run_node(harness, json.dumps(payload)))


@needs_node
def test_readable_token_formatter_boundaries(tmp_path: Path) -> None:
    # The Overview KPI shows the bare number: its card is already labelled
    # "Total Tokens", and the unit only costs width in a six-card row.
    cases = {
        0: "0",
        -1: "0",
        999: "999",
        1_000: "1K",
        842_315: "842.3K",
        999_999: "1M",
        1_000_000: "1M",
        1_049_999: "1M",
        1_050_000: "1.1M",
        482_563_219: "482.6M",
        999_999_999: "1B",
        1_000_000_000: "1B",
        1_249_000_000: "1.2B",
        1_000_000_000_000: "1T",
    }
    result = _run_readable_token_js(
        tmp_path,
        "Object.fromEntries(payload.map(value => "
        "[String(value), formatCompactTokenCount(value)]))",
        list(cases),
    )
    assert result == {str(key): value for key, value in cases.items()}


@needs_node
def test_global_token_formatter_respects_readable_preference(tmp_path: Path) -> None:
    expression = """
(() => {
  overviewReadableTokens = true;
  const readable = [
    formatTokenCount(12_450),
    formatTokenCount(12_450_000),
    formatTokenCount(12_450_000_000),
    formatTokenCount(12_450_000, true),
  ];
  overviewReadableTokens = false;
  const exact = [formatTokenCount(12_450), formatTokenCount(12_450_000, true)];
  return { readable, exact };
})()
"""
    assert _run_readable_token_js(tmp_path, expression, None) == {
        "readable": ["12.5K", "12.5M", "12.5B", "12.5M tokens"],
        "exact": ["12,450", "12,450,000 tokens"],
    }


@needs_node
def test_compact_formatter_reuses_current_locale_and_refreshes_on_language_change(tmp_path):
    expression = """
(() => {
  const original = Intl.NumberFormat;
  let constructions = 0;
  Intl.NumberFormat = function(...args) { constructions++; return new original(...args); };
  const english = Array.from({length:100}, () => formatCompactTokenCount(1249000));
  const afterEnglish = constructions;
  currentLang = 'pt';
  const portuguese = Array.from({length:100}, () => formatCompactTokenCount(1249000));
  const afterPortuguese = constructions;
  currentLang = 'en';
  const switchedBack = formatCompactTokenCount(842315);
  return {english,portuguese,switchedBack,afterEnglish,afterPortuguese,constructions};
})()
"""
    assert _run_readable_token_js(tmp_path, expression, None) == {
        "english": ["1.2M"] * 100, "portuguese": ["1,2M"] * 100,
        "switchedBack": "842.3K", "afterEnglish": 1, "afterPortuguese": 2, "constructions": 3,
    }


@needs_node
def test_readable_token_preference_defaults_and_fails_soft(tmp_path: Path) -> None:
    expression = """
(() => {
  const writes = [];
  const missing = {
    getItem: () => null,
    setItem: (key, value) => writes.push([key, value]),
  };
  const disabled = {
    getItem: () => '0',
    setItem: (key, value) => writes.push([key, value]),
  };
  const broken = {
    getItem: () => { throw new Error('blocked'); },
    setItem: () => { throw new Error('blocked'); },
  };
  saveOverviewReadableTokensPreference(false, missing);
  saveOverviewReadableTokensPreference(true, missing);
  saveOverviewReadableTokensPreference(true, broken);
  return {
    missing: loadOverviewReadableTokensPreference(missing),
    disabled: loadOverviewReadableTokensPreference(disabled),
    broken: loadOverviewReadableTokensPreference(broken),
    writes,
  };
})()
"""
    assert _run_readable_token_js(tmp_path, expression, None) == {
        "missing": True,
        "disabled": False,
        "broken": True,
        "writes": [
            ["tokdash-overview-readable-tokens", "0"],
            ["tokdash-overview-readable-tokens", "1"],
        ],
    }


def test_readable_token_switch_markup_and_tooltip_contract() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = "".join(source.split())
    panel_start = source.index('id="settingsPanel"')
    panel_end = source.index("theme-config.js")  # panel markup ends right before the app scripts
    assert panel_start < source.index('id="readableTokensToggle"') < panel_end
    assert "#totalTokens:hover+.overview-token-exact-tooltip" not in compact
    assert ".overview-token-value-wrap:hover.overview-token-exact-tooltip" in compact
    assert ".overview-token-value-wrap:has(:focus-visible).overview-token-exact-tooltip" in compact


# --- the switch, run for real under node --------------------------------------

TOGGLE_HARNESS = """
const nodes = {};
function node(id) {
  if (!nodes[id]) {
    const el = {
      id, textContent: '', style: {}, attrs: {}, classes: new Set(),
      classList: {
        add: (name) => el.classes.add(name),
        remove: (name) => el.classes.delete(name),
        contains: (name) => el.classes.has(name),
      },
      setAttribute(name, value) { el.attrs[name] = String(value); },
      getAttribute(name) { return name in el.attrs ? el.attrs[name] : null; },
    };
    nodes[id] = el;
  }
  return nodes[id];
}
const document = { getElementById: node };
const stored = {};
const window = {
  localStorage: {
    getItem: (key) => (key in stored ? stored[key] : null),
    setItem: (key, value) => { stored[key] = String(value); },
  },
};
function t(key) { return key; }
const formatTokenCount = (value) => `t${value}`;

// Every view the switch repaints, recorded in the order it asks...
const repaints = [];
const repaint = (name) => (...args) => { repaints.push([name, ...args]); };
const renderOverviewTab = repaint('renderOverviewTab');
const renderSessionsTab = repaint('renderSessionsTab');
const renderServersTab = repaint('renderServersTab');
const renderProfileView = repaint('renderProfileView');
const renderMonthHeatmap = repaint('renderMonthHeatmap');
const renderYearHeatmap = repaint('renderYearHeatmap');
const updateTopStatsForYear = repaint('updateTopStatsForYear');
const render3DCalendar = repaint('render3DCalendar');
const updateTopStatsForAllTime = repaint('updateTopStatsForAllTime');
const renderSessionModalMeta = repaint('renderSessionModalMeta');
const setShareBar = repaint('setShareBar');
const renderSessionCharts = repaint('renderSessionCharts');
const renderSessionSpeedTimeline = repaint('renderSessionSpeedTimeline');
const renderSessionChat = repaint('renderSessionChat');
const expandSessionChart = repaint('expandSessionChart');
const showDayDetails = repaint('showDayDetails');
const getVisibleCalendarData = (view) => `visible:${view}`;

// ...and what it must never do: go back to a server for figures it already has.
const requests = [];
function fetch(url) { requests.push(['fetch', String(url)]); return new Promise(() => {}); }
function fetchSelectedServers(url) { requests.push(['fetchSelectedServers', String(url)]); return new Promise(() => {}); }
function updateDashboard() { requests.push(['updateDashboard']); }
function loadStats() { requests.push(['loadStats']); }

// The state of a page with the Overview, Sessions, Servers and Stats views all
// holding data, the Stats calendar on its month view.
let overviewReadableTokens = true;
let toolChart = { destroy: () => repaints.push(['toolChart.destroy']) };
let lastUsageResponse = { range: 'cached' };
let statsLoaded = true;
const calendarState = { view: 'month' };
let activeSessionDetail = null;
let activeDayDetail = null;
const sessionSpeedModalState = { payload: null };
let sessionChatPayload = null;
let graphExpandChartType = null;
function isOverviewActive() { return true; }
function isSessionsActive() { return true; }
function isServersActive() { return true; }
node('stats-content').classes.add('active');

__FUNCTIONS__

const names = () => repaints.splice(0).map((call) => call[0]);
const out = {};
setOverviewReadableTokens(false);
out.flip = {
  readable: overviewReadableTokens,
  stored: stored['tokdash-overview-readable-tokens'] ?? null,
  ariaChecked: node('readableTokensToggle').getAttribute('aria-checked'),
  overviewRepaintedWith: (repaints.find((call) => call[0] === 'renderOverviewTab') || [])[1] ?? null,
  repaints: names(),
};
// Each of the Stats calendar's other views repaints itself.
for (const view of ['profile', 'year', '3d']) {
  calendarState.view = view;
  rerenderTokenPresentation();
  out[view] = { calls: repaints.map((call) => call[0]), threeD: repaints.find((call) => call[0] === 'render3DCalendar')?.[1] ?? null };
  repaints.length = 0;
}
// An open session modal and an open day panel are repainted in place.
activeSessionDetail = {
  tool: 'codex',
  data: { session: { tokens: 1200, tokens_in: 1000, tokens_cache: 0, tokens_out: 200 }, turns: [] },
};
activeDayDetail = { date: '2026-01-05' };
rerenderTokenPresentation();
out.openPanels = {
  calls: repaints.map((call) => call[0]),
  dayDetailsArgs: (repaints.find((call) => call[0] === 'showDayDetails') || []).slice(1),
  modalTotal: node('sessionModalTotal').textContent,
};
out.requests = requests;
process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_the_switch_saves_the_choice_and_repaints_every_cached_view_without_a_request(
    tmp_path: Path,
) -> None:
    """Flipping readable tokens is presentation only.

    Every view already holding token figures repaints from what it has, the
    choice persists, and the switch reports its new state. Nothing goes back to a
    server: a toggle that refetched would make every flip as slow as a range
    change and could land a fresher range under the old label.
    """
    source = index_source()
    functions = "\n".join(
        [
            "const OVERVIEW_READABLE_TOKENS_STORAGE_KEY = 'tokdash-overview-readable-tokens';",
            *(
                extract_js_function(source, signature)
                for signature in (
                    "function saveOverviewReadableTokensPreference(enabled, storage = null) {",
                    "function syncOverviewReadableTokensToggle() {",
                    "function setOverviewReadableTokens(enabled) {",
                    "function rerenderTokenPresentation() {",
                )
            ),
        ]
    )
    # The key is the stored preference's name, which existing users already have.
    assert "const OVERVIEW_READABLE_TOKENS_STORAGE_KEY = 'tokdash-overview-readable-tokens';" in source
    harness = tmp_path / "readable-tokens-toggle.js"
    harness.write_text(TOGGLE_HARNESS.replace("__FUNCTIONS__", functions), encoding="utf-8")
    out = json.loads(run_node(harness))

    flip = out["flip"]
    assert flip["readable"] is False
    assert flip["stored"] == "0", "the choice did not persist"
    assert flip["ariaChecked"] == "false", "the switch still reads as on"
    assert flip["overviewRepaintedWith"] == {"range": "cached"}, "the Overview must repaint what it has"
    assert {"renderOverviewTab", "renderSessionsTab", "renderServersTab", "renderMonthHeatmap"} <= set(
        flip["repaints"]
    ), flip["repaints"]

    assert "renderProfileView" in out["profile"]["calls"]
    assert {"renderYearHeatmap", "updateTopStatsForYear"} <= set(out["year"]["calls"])
    assert {"render3DCalendar", "updateTopStatsForAllTime"} <= set(out["3d"]["calls"])
    assert out["3d"]["threeD"] == "visible:3d"

    panels = out["openPanels"]
    assert {"renderSessionModalMeta", "renderSessionCharts", "showDayDetails"} <= set(panels["calls"])
    assert panels["modalTotal"] == "t1200"
    assert panels["dayDetailsArgs"] == [{"date": "2026-01-05"}, {"scroll": False}], (
        "the open day panel must repaint in place, not scroll the page to it"
    )

    assert out["requests"] == [], f"flipping the switch went back to a server: {out['requests']}"


def test_a_language_switch_reformats_the_token_figures() -> None:
    """Compact figures are locale-formatted ("1.2M" vs "1,2M"), so applyI18n repaints
    them through the same path the switch uses. Nothing runs applyI18n under test
    yet, so this one call is checked in its source."""
    i18n = extract_js_function(index_source(), "function applyI18n() {")

    assert "rerenderTokenPresentation();" in i18n


# --- the settings panel the switch lives in ------------------------------------

SETTINGS_PANEL_HARNESS = """
const focused = [];
const nodes = {};
function makeNode(id, parent = null) {
  const el = {
    id, parent, hidden: true, checked: false, attrs: {}, classes: new Set(), listeners: {},
    isConnected: true,
    classList: {
      add: (name) => el.classes.add(name),
      remove: (name) => el.classes.delete(name),
      contains: (name) => el.classes.has(name),
    },
    setAttribute(name, value) { el.attrs[name] = String(value); },
    getAttribute(name) { return name in el.attrs ? el.attrs[name] : null; },
    addEventListener(type, fn) { (el.listeners[type] ||= []).push(fn); },
    contains(other) {
      for (let at = other; at; at = at.parent) if (at === el) return true;
      return false;
    },
    closest() { return null; },
    focus() { focused.push(el.id); },
  };
  nodes[id] = el;
  return el;
}
const documentListeners = {};
const document = {
  getElementById: (id) => nodes[id] || null,
  addEventListener(type, fn) { (documentListeners[type] ||= []).push(fn); },
};
// The shipped structure: the panel sits inside its full-page backdrop, and the
// trigger that opens it lives in the sidebar.
const trigger = makeNode('sidebarSettingsBtn');
const backdrop = makeNode('settingsBackdrop');
const panel = makeNode('settingsPanel', backdrop);
const control = makeNode('readableTokensToggle', panel);
const timing = makeNode('speedIncludeRequestWindows', panel);
makeNode('refreshReport');
makeNode('appSidebar');

// A click runs the target's listeners, then each ancestor's, then the document's.
function click(target) {
  const event = { type: 'click', target };
  for (let at = target; at; at = at.parent) (at.listeners.click || []).forEach((fn) => fn(event));
  (documentListeners.click || []).forEach((fn) => fn(event));
}
function press(key) {
  const event = { type: 'keydown', key };
  (documentListeners.keydown || []).forEach((fn) => fn(event));
}

const usageReportSpeedState = { includeRequestWindows: true };
function usageReportSpeedReadPrefs() {}
function requestAnimationFrame(fn) { fn(); }
function hideUsageRefreshReport() {}
function setSidebarDrawerOpen() {}

__FUNCTIONS__

const state = () => ({
  panelHidden: panel.hidden,
  backdropHidden: backdrop.hidden,
  expanded: trigger.getAttribute('aria-expanded'),
  backdropOpen: backdrop.classes.has('open'),
  focused: focused.splice(0),
});
const out = {};
setSettingsPanelOpen(true);
out.opened = { ...state(), timingChecked: timing.checked };
click(control);
out.afterClickInside = state();
press('Escape');
out.afterEscape = state();
setSettingsPanelOpen(true);
state();
click(backdrop);
out.afterBackdropClick = state();
process.stdout.write(JSON.stringify(out));
"""


def _settings_panel_wiring(source: str) -> str:
    """The page's own listeners for the panel: backdrop click, outside click, keys.

    They are top-level statements, so the block is sliced from where it starts up
    to the end of its keydown listener and run as it is.
    """
    start = source.index("    const settingsPanel = document.getElementById('settingsPanel');")
    keydown = source.index("document.addEventListener('keydown', (event) => {", start)
    handler = extract_js_function(source[keydown:], "(event) => {")
    return source[start:keydown] + "document.addEventListener('keydown', " + handler + ");"


@needs_node
def test_the_settings_panel_opens_closes_and_hands_focus_back(tmp_path: Path) -> None:
    """Escape and a click on the backdrop close the panel; a click inside does not.

    The panel is a child of its backdrop, so a click on any control bubbles
    through the backdrop's listener: that listener must act only on clicks that
    land on the backdrop itself. Closing from the keyboard or the backdrop puts
    focus back on the trigger, and the trigger's aria-expanded follows the panel.
    """
    source = index_source()
    functions = (
        extract_js_function(source, "function setSettingsPanelOpen(open, returnFocus = false) {")
        + "\n"
        + _settings_panel_wiring(source)
    )
    harness = tmp_path / "settings-panel.js"
    harness.write_text(SETTINGS_PANEL_HARNESS.replace("__FUNCTIONS__", functions), encoding="utf-8")
    out = json.loads(run_node(harness))

    assert out["opened"] == {
        "panelHidden": False, "backdropHidden": False, "expanded": "true",
        "backdropOpen": True, "focused": [], "timingChecked": True,
    }
    assert out["afterClickInside"]["panelHidden"] is False, "a click on a control closed the panel"
    closed = {
        "panelHidden": True, "backdropHidden": True, "expanded": "false",
        "backdropOpen": False, "focused": ["sidebarSettingsBtn"],
    }
    assert out["afterEscape"] == closed
    assert out["afterBackdropClick"] == closed


def test_settings_panel_groups_display_and_install_controls() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    panel_start = source.index('id="settingsPanel"')
    panel_end = source.index("theme-config.js")  # panel markup ends right before the app scripts
    overview_start = source.index('id="overview-content"')

    # PR #110 turned the header-anchored panel into a centered modal opened from the
    # sidebar, with a phone-width mirror (#sidebar is display:none below 768px).
    assert source.index('id="sidebarSettingsBtn"') < panel_start
    assert 'id="mobileSettingsBtn"' in source
    assert 'aria-controls="settingsPanel"' in source
    assert 'aria-labelledby="settingsPanelTitle"' in source
    for control_id in (
        "langToggle",
        "themeToggle",
        "styleThemeSelect",
        "readableTokensToggle",
        "installBtn",
    ):
        assert panel_start < source.index(f'id="{control_id}"') < panel_end

    # The backdrop is position:fixed, so its markup must live at body level: the
    # style themes set backdrop-filter on .surface, and a filtered ancestor becomes
    # the containing block for fixed elements — trapped in the topbar, the "full
    # page" overlay covered 1118x144px and pushed the panel above the viewport.
    assert overview_start < source.index('id="settingsBackdrop"') < panel_start

    assert re.search(r"\.settings-panel\s*\{[^}]*position:\s*static", source), "panel must be modal-flow, not header-anchored"
