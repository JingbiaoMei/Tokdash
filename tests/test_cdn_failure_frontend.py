"""Behaviour of the CDN-failure handling in the dashboard.

The page loads Tailwind, Chart.js, Three.js, Flatpickr and Google Fonts from
CDNs with no local copy, so "the CDN is blocked or down" is a normal operating
condition rather than a theoretical one. These tests pin the behaviour that
makes that condition survivable, because every one of them was a bug:

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


def test_flatpickr_stylesheet_failure_is_actually_detectable() -> None:
    """A broken stylesheet link cannot be found by inspecting document.styleSheets.

    A <link rel="stylesheet"> that fails to load still appears in
    document.styleSheets carrying its href, so an href sniff always passes and
    the notice never fires for the CSS-only failure. The link records a load
    flag instead, which is positive evidence the CSS arrived.
    """
    source = _source()

    link = re.search(r"<link[^>]*flatpickr\.min\.css[^>]*>", source)
    assert link, "the Flatpickr stylesheet link not found"
    assert "onload" in link.group(0), (
        "the stylesheet link must record a load flag, or a CSS-only CDN failure "
        "is undetectable"
    )

    banner = source[source.find("const CDN_CHECKS") : source.find("function addCdnFailure")]
    # Comments explain why href sniffing was dropped; only the code is asserted.
    banner_code = "\n".join(
        line for line in banner.splitlines() if not line.strip().startswith("//")
    )
    assert "__tokdashFlatpickrCssLoaded" in banner_code, (
        "the Flatpickr check must test the link's load flag"
    )
    assert "document.styleSheets" not in banner_code, (
        "href sniffing cannot detect a failed stylesheet and must not be used"
    )


# --------------------------------------------------------------------------
# The notice itself
# --------------------------------------------------------------------------


def test_one_failed_notice_cannot_silence_the_others() -> None:
    """Each notice is built in its own try/catch, and each lookup is isolated.

    t is a hoisted function declaration, so `typeof t === 'function'` is true
    even when the main script died before it reached `const I18N` - calling it
    then throws. Without a per-card catch the first throw stopped every card
    after it, so a dead CDN plus a dead main script produced no notices at all.
    """
    source = _source()
    reporter = source[
        source.find("function reportCdnFailures") : source.find("if (document.readyState")
    ]

    guarded = reporter.count("addCdnFailure(")
    assert guarded >= 2, "both the checks loop and the fonts branch need a catch"
    assert reporter.count("try {") >= 2, (
        "every addCdnFailure call must be wrapped, or one throw hides the rest"
    )

    translator = _extract_js_function(source, "function translate(key) {")
    assert translator.count("try {") >= 2, (
        "each lookup needs its own catch: t() and I18N can fail independently"
    )


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
