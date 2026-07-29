from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"


def _extract_js_function(source: str, signature: str) -> str:
    start = source.find(signature)
    assert start != -1, f"{signature} not found in index.html"
    depth = 0
    for index in range(source.find("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


def _run_quick_range_js(
    tmp_path: Path, cases: list[dict[str, str]]
) -> list[dict[str, object]]:
    source = INDEX_HTML.read_text(encoding="utf-8")
    function = _extract_js_function(
        source, "function getQuickRangeDates(range, today = new Date()) {"
    )
    harness = tmp_path / "quick-ranges.js"
    harness.write_text(
        function
        + "\nfunction ymd(date) {"
        + " return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;"
        + " }\n"
        + "const cases = JSON.parse(process.argv[2]);\n"
        + "const result = cases.map((item) => {\n"
        + "  const today = new Date(`${item.today}T12:00:00`);\n"
        + "  const before = today.getTime();\n"
        + "  const range = getQuickRangeDates(item.range, today);\n"
        + "  return { start: ymd(range.startDate), end: ymd(range.endDate), inputUnchanged: today.getTime() === before };\n"
        + "});\n"
        + "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(cases)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_last_week_button_and_localizations_are_present():
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert 'data-range="lastWeek"' in source
    assert "lastWeek: 'Last Week'" in source
    assert "lastWeek: '上周'" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_last_week_is_previous_monday_through_sunday_without_mutation(
    tmp_path: Path,
):
    cases = [
        {"range": "lastWeek", "today": "2026-07-27"},
        {"range": "lastWeek", "today": "2026-08-02"},
        {"range": "lastWeek", "today": "2026-01-01"},
    ]
    assert _run_quick_range_js(tmp_path, cases) == [
        {"start": "2026-07-20", "end": "2026-07-26", "inputUnchanged": True},
        {"start": "2026-07-20", "end": "2026-07-26", "inputUnchanged": True},
        {"start": "2025-12-22", "end": "2025-12-28", "inputUnchanged": True},
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_existing_quick_ranges_keep_their_current_dates(tmp_path: Path):
    cases = [
        {"range": "today", "today": "2026-07-29"},
        {"range": "yesterday", "today": "2026-07-29"},
        {"range": "last7days", "today": "2026-07-29"},
        {"range": "last14days", "today": "2026-07-29"},
        {"range": "last4weeks", "today": "2026-07-29"},
        {"range": "thisMonth", "today": "2026-07-29"},
        {"range": "lastMonth", "today": "2026-07-29"},
        {"range": "thisYear", "today": "2026-07-29"},
        {"range": "lastYear", "today": "2026-07-29"},
    ]
    assert _run_quick_range_js(tmp_path, cases) == [
        {"start": "2026-07-29", "end": "2026-07-29", "inputUnchanged": True},
        {"start": "2026-07-28", "end": "2026-07-28", "inputUnchanged": True},
        {"start": "2026-07-23", "end": "2026-07-29", "inputUnchanged": True},
        {"start": "2026-07-16", "end": "2026-07-29", "inputUnchanged": True},
        {"start": "2026-07-02", "end": "2026-07-29", "inputUnchanged": True},
        {"start": "2026-07-01", "end": "2026-07-29", "inputUnchanged": True},
        {"start": "2026-06-01", "end": "2026-06-30", "inputUnchanged": True},
        {"start": "2026-01-01", "end": "2026-07-29", "inputUnchanged": True},
        {"start": "2025-01-01", "end": "2025-12-31", "inputUnchanged": True},
    ]
