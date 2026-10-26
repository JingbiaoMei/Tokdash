"""Tests for the quick ranges frontend.

Covers the dates each quick range button selects, run through the real
getQuickRangeDates.
"""
from __future__ import annotations

import json
from pathlib import Path

from support.frontend import extract_js_function, index_source, needs_node, run_node


def _run_quick_range_js(
    tmp_path: Path, cases: list[dict[str, str]]
) -> list[dict[str, object]]:
    source = index_source()
    function = extract_js_function(
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
    return json.loads(run_node(harness, json.dumps(cases)))


@needs_node
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


@needs_node
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
