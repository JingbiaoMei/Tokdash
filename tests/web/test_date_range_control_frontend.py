"""Tests for the date range control in the frontend.

Covers the date-range trigger: label, markup and ARIA, the quick-range overflow
menu, and the picker's open/close and commit behaviour, including that a state
sync never fetches.
"""
from __future__ import annotations

import json
from pathlib import Path

from support.frontend import extract_js_function, index_source, needs_node, run_node


@needs_node
def test_date_range_trigger_text_is_localized_and_deterministic(
    tmp_path: Path,
) -> None:
    source = index_source()
    functions = "\n".join(
        extract_js_function(source, signature)
        for signature in (
            "function sameLocalDate(left, right) {",
            "function formatDateRangeDate(date, lang = currentLang) {",
            "function formatDateRangeTriggerText(startDate, endDate, lang = currentLang) {",
        )
    )
    harness = tmp_path / "date-range-control.js"
    harness.write_text(
        "const LANG_LOCALES = { en: 'en-US', zh: 'zh-CN', ja: 'ja-JP', ko: 'ko-KR', es: 'es-ES', pt: 'pt-BR' };\n"
        "function langLocale(lang) { return LANG_LOCALES[lang] || 'en-US'; }\n"
        + functions
        + "\nconst cases = JSON.parse(process.argv[2]);\n"
        + "const result = cases.map(({ start, end, lang }) => "
        + "formatDateRangeTriggerText(new Date(`${start}T12:00:00`), new Date(`${end}T12:00:00`), lang));\n"
        + "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    cases = [
        {"start": "2026-08-10", "end": "2026-08-10", "lang": "en"},
        {"start": "2026-08-03", "end": "2026-08-09", "lang": "en"},
        {"start": "2026-08-10", "end": "2026-08-10", "lang": "zh"},
        {"start": "2026-08-03", "end": "2026-08-09", "lang": "zh"},
        {"start": "2026-08-10", "end": "2026-08-10", "lang": "ja"},
        {"start": "2026-08-03", "end": "2026-08-09", "lang": "ko"},
        {"start": "2026-08-10", "end": "2026-08-10", "lang": "es"},
        {"start": "2026-08-10", "end": "2026-08-10", "lang": "pt"},
    ]
    assert json.loads(run_node(harness, json.dumps(cases))) == [
        "Aug 10, 2026",
        "Aug 3, 2026 – Aug 9, 2026",
        "2026年8月10日",
        "2026年8月3日 – 2026年8月9日",
        "2026年8月10日",
        "2026년 8월 3일 – 2026년 8월 9일",
        "10 ago 2026",
        "10 de ago. de 2026",
    ]


def test_date_range_trigger_markup_and_localization_contract() -> None:
    source = index_source()
    assert 'class="date-range-calendar-icon"' in source
    assert 'class="date-range-chevron"' in source
    assert 'aria-haspopup="dialog"' in source
    # The trigger's title is only reached through a ternary, which the i18n
    # parity test cannot see as a literal t('...') call.
    assert source.count("selectRange: '") == 6


def test_quick_ranges_use_progressive_disclosure_without_horizontal_scroll() -> None:
    source = index_source()
    rail_start = source.index('<div class="topbar-control-rail">')
    quick_start = source.index("<!-- Quick Range Buttons -->")
    actions_start = source.index('<div class="topbar-actions">')
    tabs_start = source.index("<!-- Tabs -->")
    quick_markup = source[quick_start:actions_start]
    quick_css_start = source.index("    .quick-range-panel {")
    quick_css_end = source.index("    .topbar-actions {")
    quick_css = source[quick_css_start:quick_css_end]

    assert rail_start < quick_start < actions_start < tabs_start
    assert quick_markup.count('class="btn btn-ghost quick-range-btn"') == 10
    assert quick_markup.index('data-range="lastMonth"') < quick_markup.index('id="quickRangeMoreToggle"')
    assert quick_markup.index('id="quickRangeMoreToggle"') < quick_markup.index('data-range="last14days"')
    assert 'id="quickRangeMoreMenu"' in quick_markup
    assert 'data-i18n="moreRanges"' in quick_markup
    assert '"quick quick"' in source
    assert "flex-wrap: nowrap;" in quick_css
    assert "overflow: visible;" in quick_css
    assert "overflow-x: auto;" not in quick_css
    assert quick_markup.count("data-quick-primary") == 6
    assert "function syncQuickRangeLayout()" in source
    assert "quickRangeMoreMenu.prepend(quickRangePrimaryButtons[index]);" in source
    assert "new ResizeObserver(scheduleQuickRangeLayout).observe(quickRangePanel);" in source


def test_a_language_switch_relabels_the_date_range_trigger() -> None:
    # Held as a call-site check until applyI18n has a harness of its own: the
    # relabelling itself is run by test_the_picker_opens_from_its_trigger_and_commits_without_fetching.
    i18n = extract_js_function(index_source(), "function applyI18n() {")
    assert "syncDateRangeControl();" in i18n


# The picker's real initialiser, state and sync code against a stub DOM and a
# stub flatpickr that captures the options it is built with. Two quick-range
# buttons stand for the rail: "today" on it, "last14days" inside the More menu.
DATE_PICKER_HARNESS = r"""
function t(key) { return 'T:' + key; }
console.error = () => {};
class El {
  constructor(id) { this.id = id; this.attrs = {}; this.handlers = {}; this.textContent = ''; this.disabled = false; this.dataset = {}; this.inMore = false; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  addEventListener(type, fn) { this.handlers[type] = fn; }
  closest(selector) { return selector === '#quickRangeMoreMenu' && this.inMore ? {} : null; }
}
const els = {};
for (const id of ['dateRangePicker', 'dateRangeTrigger', 'dateRangePresetLabel',
                  'dateRangeExactLabel', 'quickRangeMoreToggle', 'quickRangeMoreLabel']) {
  els[id] = new El(id);
}
const buttons = ['today', 'last14days'].map((range) => {
  const button = new El(range);
  button.dataset.range = range;
  button.inMore = range === 'last14days';
  return button;
});
const document = {
  getElementById: (id) => els[id] || null,
  querySelectorAll: (selector) => (selector === '.quick-range-btn' ? buttons : []),
};

// Anything that would load data is a spy.
const network = { fetches: 0, loads: 0 };
globalThis.fetch = () => { network.fetches += 1; return Promise.reject(new Error('offline')); };
function updateDashboardByDateRange() { network.loads += 1; }
function updateDashboard() { network.loads += 1; }

function syncDatePickerFooter() {}
function attachDatePickerFooter() {}
function ymd(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}
function formatDateRangeTriggerText(start, end) { return start && end ? `${ymd(start)}..${ymd(end)}` : ''; }

let picker = null;
function flatpickr(input, options) {
  picker = {
    input, options, opened: 0,
    open() { this.opened += 1; },
    close() {},
    setDate() {},
  };
  return picker;
}

__STATE__

__FUNCTIONS__

const trigger = els.dateRangeTrigger;
const labels = () => ({
  preset: els.dateRangePresetLabel.textContent,
  pressed: buttons.map((button) => [button.dataset.range, button.getAttribute('aria-pressed')]),
  morePressed: els.quickRangeMoreToggle.getAttribute('aria-pressed'),
  moreLabel: els.quickRangeMoreLabel.textContent,
});
const out = {};

initDateRangePicker();
out.boundToInput = picker.input === els.dateRangePicker;
out.positionedOnTrigger = picker.options.positionElement === trigger;
if (trigger.handlers.click) trigger.handlers.click();
out.openedByClick = picker.opened;

picker.options.onReady([], '', picker);
out.ready = { ...labels(), title: trigger.getAttribute('title') };

picker.options.onOpen([]);
out.expandedWhileOpen = trigger.getAttribute('aria-expanded');
picker.options.onClose([]);
out.expandedAfterClose = trigger.getAttribute('aria-expanded');

const before = { ...network };
syncDateRangeControl();
out.syncRequests = network.fetches - before.fetches + network.loads - before.loads;

commitDateSelection(new Date(2026, 6, 16, 12), new Date(2026, 6, 29, 12), { rangeKey: 'last14days', triggerUpdate: false });
out.secondary = { ...labels(), active: activeQuickRange, exact: els.dateRangeExactLabel.textContent };

commitDateSelection(new Date(2026, 6, 1, 12), new Date(2026, 6, 3, 12), { triggerUpdate: false });
out.custom = { ...labels(), active: activeQuickRange };

process.stdout.write(JSON.stringify(out));
"""


def _date_state_block(source: str) -> str:
    """The page's date-range state bindings, initial values included."""
    start = source.index("    let flatpickrInstance = null;")
    end = source.index("    function cloneDate(date) {", start)
    return source[start:end]


@needs_node
def test_the_picker_opens_from_its_trigger_and_commits_without_fetching(tmp_path: Path) -> None:
    source = index_source()
    functions = "\n".join(
        extract_js_function(source, signature)
        for signature in (
            "function cloneDate(date) {",
            "function getPendingRangeFromSelection(",
            "function syncDateRangeControl() {",
            "function commitDateSelection(startDate, endDate, options = {}) {",
            "function initDateRangePicker() {",
        )
    )
    harness = tmp_path / "date-picker.js"
    harness.write_text(
        DATE_PICKER_HARNESS.replace("__STATE__", _date_state_block(source)).replace("__FUNCTIONS__", functions),
        encoding="utf-8",
    )
    out = json.loads(run_node(harness))

    # The calendar hangs off the visible trigger, not the hidden input it writes,
    # and a click on the trigger is what opens it.
    assert out["boundToInput"] is True
    assert out["positionedOnTrigger"] is True
    assert out["openedByClick"] == 1
    # The trigger reports the popup's state to assistive tech as it opens and closes.
    assert out["expandedWhileOpen"] == "true"
    assert out["expandedAfterClose"] == "false"

    # First paint: the page starts on Today, pressed on the rail.
    assert out["ready"] == {
        "preset": "T:today",
        "pressed": [["today", "true"], ["last14days", "false"]],
        "morePressed": "false",
        "moreLabel": "T:moreRanges",
        "title": "T:selectRange",
    }
    # Repainting the control is bookkeeping: it never loads data.
    assert out["syncRequests"] == 0

    # A preset inside the More menu names itself on the trigger and on the menu button.
    assert out["secondary"] == {
        "preset": "T:last14days",
        "pressed": [["today", "false"], ["last14days", "true"]],
        "morePressed": "true",
        "moreLabel": "T:last14days",
        "active": "last14days",
        "exact": "2026-07-16..2026-07-29",
    }
    # A range picked on the calendar is a custom range: no preset stays pressed.
    assert out["custom"] == {
        "preset": "T:customRange",
        "pressed": [["today", "false"], ["last14days", "false"]],
        "morePressed": "false",
        "moreLabel": "T:moreRanges",
        "active": None,
    }
