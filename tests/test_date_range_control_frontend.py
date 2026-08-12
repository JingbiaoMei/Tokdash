from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_date_range_trigger_text_is_localized_and_deterministic(
    tmp_path: Path,
) -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, signature)
        for signature in (
            "function sameLocalDate(left, right) {",
            "function formatDateRangeDate(date, lang = currentLang) {",
            "function formatDateRangeTriggerText(startDate, endDate, lang = currentLang) {",
        )
    )
    harness = tmp_path / "date-range-control.js"
    harness.write_text(
        functions
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
    ]
    result = subprocess.run(
        ["node", str(harness), json.dumps(cases)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == [
        "Aug 10, 2026",
        "Aug 3, 2026 – Aug 9, 2026",
        "2026年8月10日",
        "2026年8月3日 – 2026年8月9日",
    ]


def test_date_range_trigger_markup_and_localization_contract() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="dateRangeTrigger"' in source
    assert 'id="dateRangePresetLabel"' in source
    assert 'id="dateRangeExactLabel"' in source
    assert 'class="date-range-calendar-icon"' in source
    assert 'class="date-range-chevron"' in source
    assert 'aria-haspopup="dialog"' in source
    assert source.count("customRange: '") == 2
    assert source.count("selectRange: '") == 2


def test_quick_ranges_extend_right_without_stretching_buttons() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    rail_start = source.index('<div class="topbar-control-rail">')
    quick_start = source.index("<!-- Quick Range Buttons -->")
    actions_start = source.index('<div class="topbar-actions">')
    tabs_start = source.index("<!-- Tabs -->")

    assert rail_start < quick_start < actions_start < tabs_start
    assert source[quick_start:actions_start].count('class="btn btn-ghost quick-range-btn"') == 10
    assert '"quick quick"' in source
    assert "display: flex;" in source
    assert "flex-wrap: nowrap;" in source
    assert "flex: 0 0 auto;" in source
    assert "padding: 4px 10px;" in source
    assert "overflow-x: auto;" in source
    assert "white-space: nowrap;" in source


def test_date_range_state_sync_does_not_fetch() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    sync = _extract_js_function(source, "function syncDateRangeControl() {")
    commit = _extract_js_function(
        source,
        "function commitDateSelection(startDate, endDate, options = {}) {",
    )
    i18n = _extract_js_function(source, "function applyI18n() {")

    assert "activeQuickRange || 'customRange'" in sync
    assert "formatDateRangeTriggerText(currentStartDate, currentEndDate)" in sync
    assert "button.setAttribute('aria-pressed', String(isActive));" in sync
    assert "trigger.setAttribute('title', t('selectRange'));" in sync
    assert "fetch(" not in sync
    assert "updateDashboard" not in sync
    assert "rangeKey = null" in commit
    assert "activeQuickRange = rangeKey;" in commit
    assert "syncDateRangeControl();" in commit
    assert "syncDateRangeControl();" in i18n


def test_date_range_open_and_commit_contract() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert "positionElement: dateTrigger" in source
    assert (
        "dateTrigger.addEventListener('click', () => flatpickrInstance?.open());"
        in source
    )
    assert "dateTrigger.setAttribute('aria-expanded', 'true');" in source
    assert "dateTrigger.setAttribute('aria-expanded', 'false');" in source
    assert "rangeKey: range" in source
    assert "let activeQuickRange = 'today';" in source
