"""Behaviour of library-load failure handling in the dashboard.

The page loads Tailwind, Chart.js, Three.js and Flatpickr from local vendor
assets, while Google Fonts stays remote. A missing or unreadable asset is a
possible operating condition rather than a theoretical one. These tests pin
the behaviour that makes that condition survivable, because every one of them was a bug:

- a missing Chart.js used to throw inside the render and take the tables, the
  session modal, the Quota tab and the language switch down with it;
- a missing Flatpickr used to throw at the top level of the main script, which
  killed the initial data load, and then left the quick range buttons dead;
- a failed Flatpickr *stylesheet* was undetectable, because a broken
  <link rel="stylesheet"> still appears in document.styleSheets with its href;
- one translation lookup throwing silenced every remaining notice.

The JavaScript under test is the real text from index.html, run under node with
hand-written stubs, matching the other *_frontend.py suites.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _extract_js_function(source: str, signature: str) -> str:
    """Slice one complete function declaration out of index.html.

    The parameter list is skipped explicitly: a default such as
    `options = {}` puts braces inside parentheses, and counting those as the
    function body truncates the function at the default value.
    """
    start = source.find(signature)
    assert start != -1, f"{signature} not found in index.html"

    parens = 0
    body_start = -1
    for index in range(start, len(source)):
        char = source[index]
        if char == "(":
            parens += 1
        elif char == ")":
            parens -= 1
        elif char == "{" and parens == 0:
            body_start = index
            break
    assert body_start != -1, f"no body for {signature}"

    depth = 0
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


def _run_js(tmp_path: Path, name: str, script: str, arg: str | None = None) -> str:
    harness = tmp_path / f"{name}.js"
    harness.write_text(script, encoding="utf-8")
    command = ["node", str(harness)] + ([arg] if arg is not None else [])
    result = subprocess.run(
        command, check=True, capture_output=True, encoding="utf-8"
    )
    return result.stdout


def _banner_script(source: str) -> str:
    """The inline <script> that follows the #cdnFailureBanner region."""
    region = source.find('<div id="cdnFailureBanner"')
    assert region != -1, "the banner region not found"
    start = source.find("<script>", region)
    end = source.find("</script>", start)
    assert start != -1 and end != -1, "the banner script not found"
    return source[start + len("<script>") : end]


# Just enough DOM for the banner script: the region, the date-range trigger,
# <html>'s class list and element creation. `createFailures` makes the next N
# createElement calls throw, which is how a notice that cannot be built is
# simulated. Timers are stubbed so a warning's 8s fade does not hold node open.
_BANNER_DOM = r"""
const logs = [];
console.error = (...args) => logs.push(args.map(String).join(' '));
setTimeout = () => 0;
clearTimeout = () => {};
class El {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.style = { cssText: '' }; this.parent = null; this.textContent = '';
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  removeAttribute(name) { delete this.attrs[name]; }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((c) => c !== this);
    this.parent = null;
  }
  get isConnected() {
    for (let node = this; node; node = node.parent) if (node.root) return true;
    return false;
  }
  addEventListener() {}
  querySelector(selector) {
    const match = /data-cdn-key="([^"]+)"/.exec(selector);
    return match ? this.children.find((c) => c.dataset.cdnKey === match[1]) || null : null;
  }
}
const banner = new El('div'); banner.root = true;
const trigger = new El('button'); trigger.root = true; trigger.disabled = false;
trigger.setAttribute('aria-haspopup', 'dialog');
const htmlClasses = new Set();
const domListeners = {};
let createFailures = 0;
const document = {
  readyState: 'loading',
  hidden: false,
  documentElement: { classList: { add: (name) => htmlClasses.add(name) } },
  getElementById: (id) => ({ cdnFailureBanner: banner, dateRangeTrigger: trigger })[id] || null,
  createElement(tag) {
    if (createFailures > 0) { createFailures -= 1; throw new Error('createElement failed'); }
    return new El(tag);
  },
  addEventListener(type, fn) { (domListeners[type] = domListeners[type] || []).push(fn); },
};
const window = globalThis;
"""

_BANNER_REPORT = r"""
let threw = null;
try { (domListeners.DOMContentLoaded || []).forEach((fn) => fn()); }
catch (err) { threw = String(err); }
process.stdout.write(JSON.stringify({
  threw,
  logs,
  cards: banner.children.map((card) => ({
    key: card.dataset.cdnKey,
    text: card.children[0].textContent,
    closeLabel: card.children[1].children[1].textContent,
    css: card.style.cssText,
  })),
  trigger: {
    disabled: trigger.disabled,
    haspopup: trigger.getAttribute('aria-haspopup'),
    title: trigger.getAttribute('title'),
  },
  htmlClasses: [...htmlClasses],
}));
"""

# Every library present and the Flatpickr stylesheet's load flag set: the
# baseline each banner test removes one thing from.
_ALL_LOADED = (
    "window.tailwind = {};\n"
    "globalThis.Chart = function Chart() {};\n"
    "globalThis.THREE = {};\n"
    "globalThis.flatpickr = function flatpickr() {};\n"
    "window.__tokdashFlatpickrCssLoaded = true;\n"
)


def _run_banner(tmp_path: Path, name: str, setup: str) -> dict:
    """Run the real banner script against the stub DOM, then fire DOMContentLoaded."""
    script = _BANNER_DOM + setup + "\n" + _banner_script(_source()) + "\n" + _BANNER_REPORT
    return json.loads(_run_js(tmp_path, name, script))


def _flatpickr_css_onload(source: str) -> str:
    """The code the Flatpickr stylesheet link runs when it loads."""
    link = re.search(r"<link[^>]*flatpickr-4\.6\.13\.min\.css[^>]*>", source)
    assert link, "the Flatpickr stylesheet link not found"
    onload = re.search(r'onload="([^"]+)"', link.group(0))
    assert onload, (
        "the stylesheet link must record a load flag, or a CSS-only CDN failure "
        "is undetectable"
    )
    return onload.group(1)


# --------------------------------------------------------------------------
# Chart.js: one guarded factory instead of a guard per call site
# --------------------------------------------------------------------------


def test_every_chart_is_built_through_the_guarded_factory() -> None:
    """No call site may reach for the global directly.

    A bare `new Chart` anywhere but the factory is a call site that throws when
    the CDN is blocked, which is the failure mode this exists to prevent.
    """
    source = _source()
    factory = _extract_js_function(source, "function createChart(context, config) {")

    assert source.count("new Chart(") == 1, (
        "new Chart must appear exactly once, inside createChart()"
    )
    assert "new Chart(" in factory, "createChart() must be the guarded constructor"

    # No ad-hoc guard should reappear at a call site either: the factory owns it.
    outside = source.replace(factory, "")
    guards = [line for line in outside.splitlines() if "typeof Chart" in line]
    assert all(
        "cdnChartFailed" in line for line in guards
    ), f"a Chart.js guard escaped the factory: {guards}"


@needs_node
def test_chart_factory_returns_null_when_the_library_is_missing(tmp_path: Path) -> None:
    """Without Chart.js the factory yields null instead of throwing.

    Two scripts, because the only honest simulation of an absent global is a
    scope where the binding was never declared - a local `var Chart` would not
    be visible to a factory defined outside it.
    """
    source = _source()
    factory = _extract_js_function(source, "function createChart(context, config) {")

    without = json.loads(
        _run_js(
            tmp_path,
            "chart_factory_absent",
            factory
            + "\nprocess.stdout.write(JSON.stringify({ isNull:"
            " createChart({}, {}) === null }));\n",
        )
    )
    with_lib = json.loads(
        _run_js(
            tmp_path,
            "chart_factory_present",
            "class Chart { constructor() { this.made = true; } }\n"
            + factory
            + "\nprocess.stdout.write(JSON.stringify({"
            " made: createChart({}, {}) instanceof Chart }));\n",
        )
    )

    assert without["isNull"], "createChart must return null without Chart.js"
    assert with_lib["made"], "createChart must still build a chart when it is there"


@needs_node
def test_tool_chart_legend_survives_a_missing_chart_library(tmp_path: Path) -> None:
    """The legend is plain HTML, so losing the pie chart must not lose it.

    The legend is the only thing left that names the tools once the doughnut
    cannot be drawn, and it used to sit behind the same guard as `new Chart`.
    """
    source = _source()
    script = (
        "let legendCalls = 0, legendEntries = null;\n"
        "function renderToolChartLegend(entries, colors) {"
        " legendCalls += 1; legendEntries = entries.map(([name]) => name); }\n"
        "function getChartPalette() { return ['#000', '#111']; }\n"
        "function formatToolName(name) { return name; }\n"
        "function formatTokenCount(value) { return String(value); }\n"
        "function t(key) { return key; }\n"
        "let toolChart = null;\n"
        "const document = { getElementById: () => ({ getContext: () => ({}) }) };\n"
        + _extract_js_function(source, "function createChart(context, config) {")
        + "\n"
        + _extract_js_function(source, "function updateToolChart(byTool) {")
        + "\n"
        + "updateToolChart({ claude: { tokens: 10, cost: 1 }, codex: { tokens: 4, cost: 2 } });\n"
        + "process.stdout.write(JSON.stringify({"
        + " legendCalls, legendEntries, toolChartIsNull: toolChart === null, }));\n"
    )
    out = json.loads(_run_js(tmp_path, "tool_legend", script))

    assert out["toolChartIsNull"], "no chart should have been built without Chart.js"
    assert out["legendCalls"] == 1, "the legend must still render"
    assert out["legendEntries"] == ["claude", "codex"], (
        "the legend must still name the tools"
    )


@needs_node
def test_quota_charts_survive_a_missing_chart_library(tmp_path: Path) -> None:
    """The Quota tab repaints on every language switch and every server block.

    A throw in here used to report a data-fetch failure that had not happened,
    and to stop the rest of applyI18n - including the rerender that follows it.
    """
    source = _source()
    script = (
        "const quotaUtilizationCharts = new Map();\n"
        "const quotaConsumptionCharts = new Map();\n"
        "let quotaUtilizationChart = null, quotaConsumptionChart = null;\n"
        "function getChartPalette() { return ['#000', '#111']; }\n"
        "function t(key) { return key; }\n"
        "function formatQuotaAxisLabel(value) { return String(value); }\n"
        "function currentQuotaRange() { return '7d'; }\n"
        "function collapseAntigravitySeriesForCharts(series) { return series; }\n"
        "function quotaProviderVisible() { return true; }\n"
        "function quotaVisibilityHostKey(key) { return key; }\n"
        "function quotaSeriesLabel(series) { return series.provider; }\n"
        "const canvas = { getContext: () => ({}) };\n"
        "const document = { getElementById: () => null };\n"
        + _extract_js_function(source, "function createChart(context, config) {")
        + "\n"
        + _extract_js_function(source, "function renderQuotaCharts(history, targets = {}) {")
        + "\n"
        + "const history = { granularity: 'day', series:"
        " [{ provider: 'anthropic', label: 'Anthropic', points: [] }] };\n"
        + "renderQuotaCharts(history, { utilizationCanvas: canvas, consumptionCanvas: canvas });\n"
        + "renderQuotaCharts(history, { utilizationCanvas: canvas, consumptionCanvas: canvas });\n"
        + "process.stdout.write(JSON.stringify({ stored: quotaUtilizationCharts.size }));\n"
    )
    out = json.loads(_run_js(tmp_path, "quota_charts", script))

    assert out["stored"] == 1, "two repaints must leave one entry, not two"


# --------------------------------------------------------------------------
# Flatpickr: the quick ranges are the control that still works without it
# --------------------------------------------------------------------------


@needs_node
def test_quick_ranges_commit_without_flatpickr(tmp_path: Path) -> None:
    """"Last 7 Days" must still fetch data when the picker failed to load.

    The handler used to wrap commitDateSelection in `if (flatpickrInstance)`,
    so with the CDN blocked every quick range was a dead button: no request, and
    the label stuck on whatever it said before. commitDateSelection already
    no-ops its two picker-only steps, so the wrapper bought nothing.
    """
    source = _source()
    handler_start = source.find("// Quick range buttons")
    handler_end = source.find("function formatLocalDateYmd", handler_start)
    assert handler_start != -1 and handler_end != -1, "quick range handler not found"
    handler = source[handler_start:handler_end]

    script = (
        "const clicks = [];\n"
        "const buttons = ['today', 'yesterday', 'last7days'].map((range) => ({\n"
        "  range,\n"
        "  handlers: {},\n"
        "  addEventListener(type, fn) { this.handlers[type] = fn; },\n"
        "  getAttribute(name) { return name === 'data-range' ? this.range : null; },\n"
        "  closest() { return null; },\n"
        "}));\n"
        "const document = { querySelectorAll: () => buttons };\n"
        "let flatpickrInstance = null;\n"
        "let currentStartDate = null, currentEndDate = null;\n"
        "let pendingStartDate = null, pendingEndDate = null, activeQuickRange = null;\n"
        "let isSyncingDatePicker = false, isCommittingDatePicker = false;\n"
        "function cloneDate(date) { return new Date(date.getTime()); }\n"
        "function ymd(date) { return date.toISOString().slice(0, 10); }\n"
        "function syncDatePickerFooter() {}\n"
        "function syncDateRangeControl() {}\n"
        "function setQuickRangeMoreOpen() {}\n"
        "function updateDashboardByDateRange(start, end) {"
        " clicks.push([ymd(start), ymd(end)]); }\n"
        + _extract_js_function(source, "function commitDateSelection(startDate, endDate, options = {}) {")
        + "\n"
        + _extract_js_function(source, "function getQuickRangeDates(range, today = new Date()) {")
        + "\n"
        + handler
        + "\n"
        + "for (const button of buttons) button.handlers.click.call(button);\n"
        + "process.stdout.write(JSON.stringify({ clicks, activeQuickRange }));\n"
    )
    out = json.loads(_run_js(tmp_path, "quick_ranges", script))

    assert len(out["clicks"]) == 3, (
        "every quick range must request data without flatpickr, got "
        f"{out['clicks']}"
    )
    assert out["clicks"][0][0] == out["clicks"][0][1], "today must be a single day"
    assert out["clicks"][1][0] == out["clicks"][1][1], "yesterday must be a single day"
    assert out["clicks"][2][0] < out["clicks"][2][1], "last7days must be a range"
    assert out["activeQuickRange"] == "last7days", "the active range must be tracked"


@needs_node
def test_flatpickr_stylesheet_failure_is_actually_detectable(tmp_path: Path) -> None:
    """The link's own onload code is what tells the check the CSS arrived.

    A <link rel="stylesheet"> that fails to load still appears in
    document.styleSheets carrying its href, so an href sniff always passes and
    the notice never fires for the CSS-only failure. The link records a load
    flag instead. Running the link's real onload code, then the real check,
    ties the two together: a renamed flag on either side shows the warning on
    every normal load, and a check that cannot fire misses the failure.
    """
    onload = _flatpickr_css_onload(_source())
    loaded_setup = _ALL_LOADED.replace("window.__tokdashFlatpickrCssLoaded = true;\n", "")

    loaded = _run_banner(tmp_path, "flatpickr_css_loaded", loaded_setup + onload + ";\n")
    failed = _run_banner(tmp_path, "flatpickr_css_failed", loaded_setup)

    assert loaded["threw"] is None and failed["threw"] is None
    assert loaded["cards"] == [], (
        "a stylesheet that ran its onload must not be reported as failed, got "
        f"{[card['key'] for card in loaded['cards']]}"
    )
    assert [card["key"] for card in failed["cards"]] == ["cdnFlatpickrFailed"], (
        "a stylesheet that never loaded must be reported"
    )


@needs_node
def test_a_failed_date_picker_is_disabled_rather_than_left_dead(tmp_path: Path) -> None:
    """Script or stylesheet missing: hide the calendar and disable its trigger.

    Without the stylesheet the calendar is an unstyled block left static in the
    body's flex row (a 528x6451px column at 1400x900, the whole viewport on a
    phone); without the script the trigger opens nothing. Either way the page
    marks the picker unavailable, and the trigger says why instead of inviting
    a click. With both loaded, nothing about the trigger changes.
    """
    setup = "function t(key) { return 'T:' + key; }\n"
    no_css = _run_banner(
        tmp_path,
        "picker_no_css",
        setup + _ALL_LOADED.replace("window.__tokdashFlatpickrCssLoaded = true;\n", ""),
    )
    no_script = _run_banner(
        tmp_path,
        "picker_no_script",
        setup + _ALL_LOADED.replace("globalThis.flatpickr = function flatpickr() {};\n", ""),
    )
    healthy = _run_banner(tmp_path, "picker_healthy", setup + _ALL_LOADED)

    for name, out in (("stylesheet", no_css), ("script", no_script)):
        assert out["threw"] is None, f"missing {name}: the banner threw {out['threw']}"
        assert "tokdash-datepicker-unavailable" in out["htmlClasses"], (
            f"missing {name}: the calendar must be hidden"
        )
        assert out["trigger"] == {
            "disabled": True,
            "haspopup": None,
            "title": "T:cdnFlatpickrFailed",
        }, f"missing {name}: the trigger must be disabled and say why, got {out['trigger']}"

    assert healthy["htmlClasses"] == []
    assert healthy["trigger"] == {"disabled": False, "haspopup": "dialog", "title": None}

    rule = re.search(
        r"\.tokdash-datepicker-unavailable\s+\.flatpickr-calendar\s*\{([^}]*)\}", _source()
    )
    assert rule and re.search(r"display:\s*none", rule.group(1)), (
        "the unavailable class must actually hide the calendar"
    )


@needs_node
def test_the_disabled_date_trigger_keeps_its_reason(tmp_path: Path) -> None:
    """syncDateRangeControl rewrites the trigger's title on every range change
    and language switch, so it must not put back "select a range" on a trigger
    that cannot open."""
    source = _source()
    script = (
        "function t(key) { return 'T:' + key; }\n"
        "const els = {\n"
        "  dateRangeTrigger: { disabled: false, attrs: {},"
        " setAttribute(name, value) { this.attrs[name] = value; } },\n"
        "  dateRangePresetLabel: { textContent: '' },\n"
        "  dateRangeExactLabel: { textContent: '' },\n"
        "};\n"
        "const document = { getElementById: (id) => els[id] || null, querySelectorAll: () => [] };\n"
        "let activeQuickRange = 'today', currentStartDate = null, currentEndDate = null;\n"
        "function formatDateRangeTriggerText() { return ''; }\n"
        + _extract_js_function(source, "function syncDateRangeControl() {")
        + "\n"
        + "syncDateRangeControl();\n"
        + "const enabledTitle = els.dateRangeTrigger.attrs.title;\n"
        + "els.dateRangeTrigger.disabled = true;\n"
        + "syncDateRangeControl();\n"
        + "process.stdout.write(JSON.stringify({ enabledTitle,"
        " disabledTitle: els.dateRangeTrigger.attrs.title }));\n"
    )
    out = json.loads(_run_js(tmp_path, "trigger_title", script))

    assert out["enabledTitle"] == "T:selectRange"
    assert out["disabledTitle"] == "T:cdnFlatpickrFailed"


@needs_node
def test_date_picker_init_survives_a_missing_flatpickr(tmp_path: Path) -> None:
    """initDateRangePicker is a top-level call in the main script; a throw here
    used to take the initial data load down with it."""
    source = _source()
    script = (
        "console.error = () => {};\n"
        "const handlers = {};\n"
        "const document = { getElementById: (id) => ({ id,"
        " addEventListener(type, fn) { handlers[id + ':' + type] = fn; } }) };\n"
        "let flatpickrInstance = null;\n"
        + _extract_js_function(source, "function initDateRangePicker() {")
        + "\n"
        + "let threw = null;\n"
        + "try { initDateRangePicker(); handlers['dateRangeTrigger:click'](); }"
        " catch (err) { threw = String(err); }\n"
        + "process.stdout.write(JSON.stringify({ threw,"
        " instanceIsNull: flatpickrInstance === null }));\n"
    )
    out = json.loads(_run_js(tmp_path, "picker_init", script))

    assert out["threw"] is None, f"initDateRangePicker threw without flatpickr: {out['threw']}"
    assert out["instanceIsNull"]


# --------------------------------------------------------------------------
# The notice itself
# --------------------------------------------------------------------------


@needs_node
def test_one_failed_notice_cannot_silence_the_others(tmp_path: Path) -> None:
    """A notice that cannot be built must not cost the user the rest.

    Tailwind, Chart.js and Three.js all missing, and the first card's
    createElement throws: the Tailwind notice is lost, but the Chart.js and
    Three.js notices after it must still appear. Without a per-card catch the
    first throw ended the whole report.
    """
    setup = (
        "function t(key) { return 'T:' + key; }\n"
        "globalThis.flatpickr = function flatpickr() {};\n"
        "window.__tokdashFlatpickrCssLoaded = true;\n"
        "createFailures = 1;\n"
    )
    out = _run_banner(tmp_path, "isolated_notices", setup)

    assert out["threw"] is None, f"one failed notice stopped the report: {out['threw']}"
    assert [card["key"] for card in out["cards"]] == ["cdnChartFailed", "cdnThreeFailed"]
    assert any("cdnTailwindFailed" in line for line in out["logs"]), (
        "the notice that could not be built must still be logged"
    )


@needs_node
def test_notices_fall_back_to_english_when_the_main_script_died(tmp_path: Path) -> None:
    """t is a hoisted function declaration, so `typeof t === 'function'` is true
    even when the main script died before it reached `const I18N`, and calling
    it then throws. The notice must fall back to the English strings, and to the
    bare key only when there is no dictionary at all."""
    dead_t = (
        "function t() { throw new ReferenceError("
        "\"Cannot access 'I18N' before initialization\"); }\n"
    )
    only_chart_missing = _ALL_LOADED.replace("globalThis.Chart = function Chart() {};\n", "")

    english = _run_banner(
        tmp_path,
        "fallback_english",
        dead_t
        + "globalThis.I18N = { en: { cdnChartFailed: 'Chart.js (en)', close: 'Close (en)' } };\n"
        + only_chart_missing,
    )
    bare = _run_banner(tmp_path, "fallback_bare", dead_t + only_chart_missing)

    assert english["threw"] is None and bare["threw"] is None
    assert [(c["text"], c["closeLabel"]) for c in english["cards"]] == [
        ("Chart.js (en)", "Close (en)")
    ], "a dead t() must fall back to the English dictionary"
    assert [(c["text"], c["closeLabel"]) for c in bare["cards"]] == [
        ("cdnChartFailed", "close")
    ], "with no dictionary at all the notice still renders, keyed"


@needs_node
def test_the_notice_region_lets_clicks_through_between_cards(tmp_path: Path) -> None:
    """The region's box spans its widest card, so it must not take clicks
    itself; each card opts back in, and is capped so one long message cannot
    spread across the content beneath it."""
    region = re.search(r"<div id=\"cdnFailureBanner\"[^>]*>", _source())
    assert region, "the banner region not found"
    assert re.search(r"pointer-events:\s*none", region.group(0)), (
        "the region must not swallow clicks in the gaps between cards"
    )

    out = _run_banner(
        tmp_path,
        "card_style",
        "function t(key) { return 'T:' + key; }\n"
        + _ALL_LOADED.replace("globalThis.Chart = function Chart() {};\n", ""),
    )
    assert len(out["cards"]) == 1
    css = out["cards"][0]["css"]
    assert re.search(r"pointer-events:\s*auto", css), "a card must stay clickable"
    assert re.search(r"max-width:\s*400px", css), "a card needs its own width cap"


def test_the_notice_does_not_cover_the_header_controls() -> None:
    """Bottom-right, and a gutter on narrow screens.

    Top-right holds Refresh, the theme toggle and the date-range trigger -
    exactly the controls a broken CDN leaves unusable. A fixed 400px width also
    runs off the left edge at 360px.
    """
    source = _source()
    region = re.search(r"<div id=\"cdnFailureBanner\"[^>]*>", source)
    assert region, "the banner region not found"
    style = region.group(0)

    assert "bottom:" in style, "the banner must sit below the header"
    assert "top:" not in style, "top-right overlaps the header controls"
    assert "calc(100vw - 32px)" in style, (
        "the banner needs a viewport-relative cap or it runs off a 360px screen"
    )
    assert "hidden" not in style, (
        "a live region that is display:none when its cards arrive is not "
        "announced; keep it rendered and let it be empty instead"
    )


def test_every_locale_carries_the_cdn_keys() -> None:
    """The notice is translated, not English-only.

    Covered structurally here because the parity test in
    test_i18n_languages.py already checks the key sets match across languages;
    this guards against the keys being dropped from the object entirely.
    """
    source = _source()
    for key in (
        "cdnTailwindFailed",
        "cdnChartFailed",
        "cdnThreeFailed",
        "cdnFlatpickrFailed",
        "cdnFontsFailed",
    ):
        assert source.count(f"{key}:") == 6, f"{key} must exist in all six locales"


# --------------------------------------------------------------------------
# Three.js: the 3D view says what happened instead of throwing
# --------------------------------------------------------------------------


@needs_node
def test_3d_view_survives_a_missing_three(tmp_path: Path) -> None:
    """Without Three.js the 3D view shows the failure in place, with dashes
    rather than a $0 / 0 overlay that would read as a real reading."""
    source = _source()
    script = (
        "function t(key) { return 'T:' + key; }\n"
        "const els = {};\n"
        "const document = { getElementById: (id) =>"
        " (els[id] = els[id] || { innerHTML: '', textContent: '' }) };\n"
        + _extract_js_function(source, "function render3DCalendar(contributions) {")
        + "\n"
        + "let threw = null;\n"
        + "try { render3DCalendar([{ date: '2026-09-30', tokens: 5, cost: 1 }]); }"
        " catch (err) { threw = String(err); }\n"
        + "process.stdout.write(JSON.stringify({ threw,"
        " view: els.calendar3D.innerHTML, cost: els.stats3DTotalCost.textContent,"
        " tokens: els.stats3DTotalTokens.textContent }));\n"
    )
    out = json.loads(_run_js(tmp_path, "three_missing", script))

    assert out["threw"] is None, f"render3DCalendar threw without Three.js: {out['threw']}"
    assert "T:cdnThreeFailed" in out["view"], "the view must say Three.js failed"
    assert (out["cost"], out["tokens"]) == ("—", "—")
