from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"
THEME_CONFIG_JS = Path(tokdash.__file__).parent / "static" / "theme-config.js"
THEMES_CSS = Path(tokdash.__file__).parent / "static" / "themes.css"


def _heat_palette_for(theme: str) -> tuple[list[str], list[str]]:
    source = THEME_CONFIG_JS.read_text(encoding="utf-8")
    heat_source = source[source.index("heatColorsMap:") : source.index("chartPaletteMap:")]
    match = re.search(
        rf"{re.escape(theme)}:\s*\{{\s*light:\s*(\[[^\]]+\]),\s*dark:\s*(\[[^\]]+\])",
        heat_source,
    )
    assert match, f"heat palette not found for {theme}"
    return json.loads(match.group(1)), json.loads(match.group(2))


def _extract_js_function(source: str, signature: str) -> str:
    start = source.find(signature)
    assert start != -1, f"{signature} not found in index.html"
    depth = 0
    body_start = (
        start + len(signature) - 1
        if signature.endswith("{")
        else source.find("{", start)
    )
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


PROFILE_FUNCTIONS = [
    "function parseDateKey(value) {",
    "function formatDateKey(date) {",
    "function startOfWeekMonday(date) {",
    "function createEmptyContribution(key) {",
    "function safeProfileNumber(value) {",
    "function getProfileActivityWindow(contributions, today, weekCount = 52) {",
    "function profileDayTokens(day) {",
    "function summarizeProfileActivity(days) {",
    "function groupProfileActivityWeeks(days) {",
    "function buildProfileActivitySeries(mode, weeks) {",
    "function getProfileDailyLevel(value, peak) {",
    "function getProfileAggregateHeight(value, peak) {",
    "function getProfileMilestoneTier(value) {",
    "function buildProfileActivityCells(mode, days, weeks) {",
    "function buildOverviewProfilePreview(contributions, today = new Date(), mode = 'daily') {",
]


def _run_profile_js(tmp_path: Path, expression: str, payload: dict) -> object:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, signature) for signature in PROFILE_FUNCTIONS
    )
    harness = tmp_path / "profile-stats.js"
    harness.write_text(
        functions
        + "\nconst payload = JSON.parse(process.argv[2]);\n"
        + f"const result = {expression};\n"
        + "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(payload)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


TOOLTIP_JS_SETUP = """
const LABELS = {
  cumulative: 'Cumulative', total: 'Total', tokensUnit: 'tokens',
  input: 'Input', output: 'Output', cacheRead: 'Cache read',
  cacheWrite: 'Cache write', reasoning: 'Reasoning',
  estimatedCost: 'Estimated cost', milestone: 'Milestone',
};
function t(key) { return LABELS[key] || key; }
function formatNumber(value) { return new Intl.NumberFormat('en-US').format(Number(value) || 0); }
function formatCurrency(value) { return `$${(Number(value) || 0).toFixed(2)}`; }
function formatShortDate(value) { return value || '—'; }
"""


def _run_profile_tooltip_js(tmp_path: Path, expression: str, payload: dict) -> object:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        [
            _extract_js_function(source, "function safeProfileNumber(value) {"),
            _extract_js_function(source, "function getProfileMilestoneTier(value) {"),
            _extract_js_function(
                source,
                "function formatProfileAggregateTooltip(aggregate, cumulative = false, milestonesEnabled = false) {",
            ),
        ]
    )
    harness = tmp_path / "profile-tooltip.js"
    harness.write_text(
        TOOLTIP_JS_SETUP
        + "\n"
        + functions
        + "\nconst payload = JSON.parse(process.argv[2]);\n"
        + f"const result = {expression};\n"
        + "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(payload)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def _run_profile_tooltip_position_js(
    tmp_path: Path, expression: str, payload: dict
) -> object:
    source = INDEX_HTML.read_text(encoding="utf-8")
    function = _extract_js_function(
        source,
        "function calculateProfileActivityTooltipPosition(targetRect, tooltipRect, viewport, gap = 8) {",
    )
    harness = tmp_path / "profile-tooltip-position.js"
    harness.write_text(
        function
        + "\nconst payload = JSON.parse(process.argv[2]);\n"
        + f"const result = {expression};\n"
        + "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(payload)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def _day(date: str, tokens: int, *, cost: float = 0.0) -> dict:
    return {
        "date": date,
        "totals": {"tokens": tokens, "cost": cost, "messages": 0},
        "tokenBreakdown": {
            "input": tokens // 2,
            "output": tokens - tokens // 2,
            "cacheRead": 0,
            "cacheWrite": 0,
            "reasoning": 0,
        },
        "sources": [],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_profile_window_is_two_monday_aligned_weeks_and_marks_future(tmp_path: Path):
    payload = {"today": "2026-01-07", "days": [_day("2026-01-06", 20)]}
    result = _run_profile_js(
        tmp_path,
        "getProfileActivityWindow(payload.days, new Date(`${payload.today}T12:00:00`), 2)",
        payload,
    )
    assert len(result) == 14
    assert result[0]["date"] == "2025-12-29"
    assert result[-1]["date"] == "2026-01-11"
    assert sum(bool(day["isFuture"]) for day in result) == 4
    assert next(day for day in result if day["date"] == "2026-01-06")["totals"][
        "tokens"
    ] == 20


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_profile_summary_excludes_future_and_calculates_streaks(tmp_path: Path):
    payload = {
        "today": "2026-01-07",
        "days": [
            _day("2025-12-29", 4),
            _day("2025-12-30", 4),
            _day("2025-12-31", 4),
            _day("2026-01-01", 4),
            _day("2026-01-02", 4),
            _day("2026-01-05", 10),
            _day("2026-01-06", 20),
            _day("2026-01-07", 30),
            _day("2026-01-08", 999),
        ],
    }
    result = _run_profile_js(
        tmp_path,
        "summarizeProfileActivity(getProfileActivityWindow(payload.days, new Date(`${payload.today}T12:00:00`), 2))",
        payload,
    )
    assert result["recordedTokens"] == 80
    assert result["peakDay"]["date"] == "2026-01-07"
    assert result["activeDays"] == 8
    assert result["currentStreak"] == 3
    assert result["longestStreak"] == 5


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_profile_week_and_cumulative_cells_follow_seven_level_scale(tmp_path: Path):
    payload = {
        "today": "2026-01-11",
        "days": [_day("2025-12-29", 10), _day("2026-01-05", 30)],
    }
    expression = """
    (() => {
      const days = getProfileActivityWindow(payload.days, new Date(`${payload.today}T12:00:00`), 2);
      const weeks = groupProfileActivityWeeks(days);
      return {
        weeks,
        weekly: buildProfileActivityCells('weekly', days, weeks),
        cumulative: buildProfileActivityCells('cumulative', days, weeks),
        levels: [0, 1, 25, 50, 75, 100].map((value) => getProfileDailyLevel(value, 100)),
      };
    })()
    """
    result = _run_profile_js(tmp_path, expression, payload)
    assert [week["totals"]["tokens"] for week in result["weeks"]] == [10, 30]
    weekly_heights = [
        sum(
            cell["active"]
            for cell in result["weekly"]["cells"][offset : offset + 7]
        )
        for offset in (0, 7)
    ]
    cumulative_heights = [
        sum(
            cell["active"]
            for cell in result["cumulative"]["cells"][offset : offset + 7]
        )
        for offset in (0, 7)
    ]
    assert weekly_heights == [3, 7]
    assert cumulative_heights == [2, 7]
    assert cumulative_heights == sorted(cumulative_heights)
    assert result["cumulative"]["series"][1]["startDate"] == "2025-12-29"
    assert result["cumulative"]["series"][1]["endDate"] == "2026-01-11"
    assert result["levels"] == [0, 1, 1, 2, 3, 4]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_profile_milestones_are_inclusive_and_enrich_each_mode(tmp_path: Path):
    payload = {
        "today": "2026-01-11",
        "values": [
            -1,
            0,
            99_999_999,
            100_000_000,
            299_999_999,
            300_000_000,
            499_999_999,
            500_000_000,
            999_999_999,
            1_000_000_000,
        ],
        "days": [
            _day("2025-12-29", 100_000_000),
            _day("2026-01-05", 250_000_000),
        ],
    }
    expression = """
    (() => {
      const today = new Date(`${payload.today}T12:00:00`);
      const days = getProfileActivityWindow(payload.days, today, 2);
      const weeks = groupProfileActivityWeeks(days);
      const daily = buildProfileActivityCells('daily', days, weeks);
      const weekly = buildProfileActivityCells('weekly', days, weeks);
      const cumulative = buildProfileActivityCells('cumulative', days, weeks);
      const compactCaps = (result) => result.cells
        .filter((cell) => cell.isCap)
        .map((cell) => ({
          value: cell.value,
          milestone: cell.milestone?.level || null,
        }));
      return {
        tiers: payload.values.map((value) => getProfileMilestoneTier(value)?.level || null),
        labels: payload.values.map((value) => getProfileMilestoneTier(value)?.label || null),
        dailyMilestones: daily.cells
          .filter((cell) => cell.milestone)
          .map((cell) => ({date: cell.day.date, level: cell.milestone.level})),
        weeklyCaps: compactCaps(weekly),
        cumulativeCaps: compactCaps(cumulative),
        weeklyNonCapMilestones: weekly.cells.filter(
          (cell) => !cell.isCap && cell.milestone,
        ).length,
        previewMode: buildOverviewProfilePreview(payload.days, today, 'weekly').mode,
      };
    })()
    """
    result = _run_profile_js(tmp_path, expression, payload)

    assert result["tiers"] == [None, None, None, 1, 1, 2, 2, 3, 3, 4]
    assert result["labels"][3::2] == ["100M+", "300M+", "500M+", "1B+"]
    assert result["dailyMilestones"] == [
        {"date": "2025-12-29", "level": 1},
        {"date": "2026-01-05", "level": 1},
    ]
    assert result["weeklyCaps"] == [
        {"value": 100_000_000, "milestone": 1},
        {"value": 250_000_000, "milestone": 1},
    ]
    assert result["cumulativeCaps"] == [
        {"value": 100_000_000, "milestone": 1},
        {"value": 350_000_000, "milestone": 2},
    ]
    assert result["weeklyNonCapMilestones"] == 0
    assert result["previewMode"] == "weekly"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_profile_default_window_is_52_weeks_and_empty_or_zero_data_is_safe(
    tmp_path: Path,
):
    expression = """
    (() => {
      const days = getProfileActivityWindow(payload.days, new Date(`${payload.today}T12:00:00`));
      const weeks = groupProfileActivityWeeks(days);
      return {
        dayCount: days.length,
        weekCount: weeks.length,
        summary: summarizeProfileActivity(days),
        daily: buildProfileActivityCells('daily', days, weeks),
        weekly: buildProfileActivityCells('weekly', days, weeks),
        cumulative: buildProfileActivityCells('cumulative', days, weeks),
      };
    })()
    """
    for source_days in ([], [_day("2026-01-07", 0)]):
        payload = {"today": "2026-01-07", "days": source_days}
        result = _run_profile_js(tmp_path, expression, payload)
        assert result["dayCount"] == 364
        assert result["weekCount"] == 52
        assert len(result["daily"]["cells"]) == 364
        assert len(result["weekly"]["cells"]) == 364
        assert len(result["cumulative"]["cells"]) == 364
        assert result["summary"] == {
            "recordedTokens": 0,
            "peakDay": None,
            "activeDays": 0,
            "currentStreak": 0,
            "longestStreak": 0,
        }
        assert not any(cell["active"] for cell in result["daily"]["cells"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_overview_profile_preview_uses_daily_52_week_model(tmp_path: Path):
    payload = {
        "today": "2026-07-27",
        "days": [_day("2026-07-26", 25), _day("2026-07-27", 75)],
    }
    result = _run_profile_js(
        tmp_path,
        "buildOverviewProfilePreview(payload.days, new Date(`${payload.today}T12:00:00`))",
        payload,
    )
    assert len(result["days"]) == 364
    assert len(result["cells"]) == 364
    assert result["summary"]["recordedTokens"] == 100
    assert result["summary"]["activeDays"] == 2
    assert result["summary"]["currentStreak"] == 2
    assert result["summary"]["longestStreak"] == 2
    today_cell = next(
        cell for cell in result["cells"] if cell["day"]["date"] == payload["today"]
    )
    assert today_cell["level"] == 4
    assert result["cells"][-1]["day"]["isFuture"] is True
    assert result["cells"][-1]["level"] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_profile_week_aggregates_breakdown_and_cost_across_year_boundary(
    tmp_path: Path,
):
    first = _day("2025-12-29", 100, cost=1.25)
    first["tokenBreakdown"].update(cacheRead=7, cacheWrite=3, reasoning=2)
    second = _day("2026-01-02", 50, cost=0.75)
    second["tokenBreakdown"].update(cacheRead=5, cacheWrite=1, reasoning=4)
    payload = {"today": "2026-01-04", "days": [first, second]}
    result = _run_profile_js(
        tmp_path,
        "groupProfileActivityWeeks(getProfileActivityWindow(payload.days, new Date(`${payload.today}T12:00:00`), 1))[0]",
        payload,
    )
    assert result["startDate"] == "2025-12-29"
    assert result["endDate"] == "2026-01-04"
    assert result["totals"]["tokens"] == 150
    assert result["totals"]["cost"] == 2
    assert result["tokenBreakdown"] == {
        "input": 75,
        "output": 75,
        "cacheRead": 12,
        "cacheWrite": 4,
        "reasoning": 6,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_profile_tooltip_model_has_semantic_rows_and_accessible_text(tmp_path: Path):
    payload = {
        "aggregate": {
            "startDate": "2026-07-20",
            "endDate": "2026-07-26",
            "totals": {"tokens": 1234, "cost": 1.25},
            "tokenBreakdown": {
                "input": 500,
                "output": 300,
                "cacheRead": 250,
                "cacheWrite": 100,
                "reasoning": 84,
            },
        }
    }
    result = _run_profile_tooltip_js(
        tmp_path,
        "formatProfileAggregateTooltip(payload.aggregate)",
        payload,
    )
    assert result["title"] == "2026-07-20 → 2026-07-26"
    assert [row["key"] for row in result["rows"]] == [
        "total",
        "input",
        "output",
        "cacheRead",
        "cacheWrite",
        "reasoning",
        "estimatedCost",
    ]
    assert [row["tone"] for row in result["rows"]] == [
        "primary",
        "primary",
        "secondary",
        "cost",
        "cta",
        "reasoning",
        "cost",
    ]
    assert result["rows"][0] == {
        "key": "total",
        "label": "Total",
        "value": "1,234 tokens",
        "tone": "primary",
        "prominent": True,
    }
    assert result["accessibleText"] == (
        "2026-07-20 → 2026-07-26. Total: 1,234 tokens. Input: 500. "
        "Output: 300. Cache read: 250. Cache write: 100. Reasoning: 84. "
        "Estimated cost: $1.25"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_profile_tooltip_keeps_zero_rows_and_omits_zero_reasoning(tmp_path: Path):
    payload = {
        "aggregate": {
            "startDate": "2026-07-20",
            "endDate": "2026-07-26",
            "totals": {"tokens": 0, "cost": 0},
            "tokenBreakdown": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "reasoning": 0,
            },
        }
    }
    result = _run_profile_tooltip_js(
        tmp_path,
        "formatProfileAggregateTooltip(payload.aggregate, true)",
        payload,
    )
    assert result["title"] == "Cumulative · 2026-07-20 → 2026-07-26"
    assert [row["key"] for row in result["rows"]] == [
        "total",
        "input",
        "output",
        "cacheRead",
        "cacheWrite",
        "estimatedCost",
    ]
    assert [row["value"] for row in result["rows"]] == [
        "0 tokens",
        "0",
        "0",
        "0",
        "0",
        "$0.00",
    ]
    assert "Reasoning" not in result["accessibleText"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_profile_tooltip_exposes_milestone_badge_tone_and_accessible_text(
    tmp_path: Path,
):
    payload = {
        "high": {
            "startDate": "2026-07-20",
            "endDate": "2026-07-26",
            "totals": {"tokens": 1_000_000_000, "cost": 12.5},
            "tokenBreakdown": {
                "input": 400_000_000,
                "output": 300_000_000,
                "cacheRead": 200_000_000,
                "cacheWrite": 100_000_000,
                "reasoning": 0,
            },
        },
        "normal": {
            "startDate": "2026-07-27",
            "endDate": "2026-07-27",
            "totals": {"tokens": 99_999_999, "cost": 1.0},
            "tokenBreakdown": {},
        },
    }
    result = _run_profile_tooltip_js(
        tmp_path,
        "({ high: formatProfileAggregateTooltip(payload.high, false, true), hidden: formatProfileAggregateTooltip(payload.high), normal: formatProfileAggregateTooltip(payload.normal, false, true) })",
        payload,
    )

    assert result["high"]["milestone"] == {
        "level": 4,
        "minTokens": 1_000_000_000,
        "label": "1B+",
        "tone": "milestone-4",
    }
    assert result["high"]["rows"][0]["tone"] == "milestone-4"
    assert "Milestone: 1B+" in result["high"]["accessibleText"]
    assert result["normal"]["milestone"] is None
    assert result["normal"]["rows"][0]["tone"] == "primary"
    assert "Milestone:" not in result["normal"]["accessibleText"]
    assert result["hidden"]["milestone"] is None
    assert result["hidden"]["rows"][0]["tone"] == "primary"
    assert "Milestone:" not in result["hidden"]["accessibleText"]


def test_profile_milestone_tooltip_and_tier_aura_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)
    formatter = _extract_js_function(
        source,
        "function formatProfileAggregateTooltip(aggregate, cumulative = false, milestonesEnabled = false) {",
    )
    renderer = _extract_js_function(
        source,
        "function renderProfileActivityTooltipContent(tooltip, model) {",
    )
    profile_renderer = _extract_js_function(source, "function renderProfileView() {")

    assert "milestone: 'Milestone'" in source
    assert "milestone: '里程碑'" in source
    assert "getProfileMilestoneTier(tokenTotal)" in formatter
    assert "profile-activity-tooltip-badge" in renderer
    assert "badge.textContent = model.milestone.label;" in renderer
    assert "tooltip.dataset.milestone" in renderer
    assert ".innerHTML" not in renderer
    assert "applyProfileMilestone(cell, definition.milestone);" in profile_renderer
    assert "applyProfileMilestone(hit, milestone);" in profile_renderer
    for level in range(1, 5):
        assert f'--profile-milestone-{level}:' in compact
        assert f'[data-milestone="{level}"]' in source
        assert f'[data-tone="milestone-{level}"]' in source
    assert "prefers-reduced-motion:reduce" in compact


def test_profile_milestones_follow_active_heat_palette_and_keep_paper_keyline():
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)
    themes = THEMES_CSS.read_text(encoding="utf-8")

    light_rule = """
html[data-ui-theme="paper"] .profile-activity-shell,
html[data-ui-theme="paper"] .overview-profile-band {
  --profile-milestone-keyline: var(--color-bg);
}
""".strip()
    dark_rule = """
html.dark[data-ui-theme="paper"] .profile-activity-shell,
html.dark[data-ui-theme="paper"] .overview-profile-band {
  --profile-milestone-keyline: var(--color-bg);
}
""".strip()

    assert light_rule in themes
    assert dark_rule in themes
    assert "--profile-milestone-keyline:transparent;" in compact
    assert compact.count(
        "inset0001pxvar(--profile-milestone-keyline)"
    ) >= 8

    palette_sync = _extract_js_function(
        source,
        "function syncProfileMilestonePalette() {",
    )
    assert "for (let level = 1; level <= 4; level += 1)" in palette_sync
    assert "getProfileActivityColor(level)" in palette_sync
    assert "shell.style.setProperty(`--profile-milestone-${level}`" in palette_sync

    style_setter = _extract_js_function(source, "function applyStyleTheme(theme) {")
    color_mode_setter = _extract_js_function(source, "function applyTheme(theme) {")
    assert "syncProfileMilestonePalette();" in style_setter
    assert "syncProfileMilestonePalette();" in color_mode_setter


def test_profile_uses_github_geometry_stronger_auras_and_edge_gutter():
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)

    assert (
        ".profile-activity-grid-wrap{position:relative;min-width:595px;}"
        in compact
    )
    assert (
        ".profile-activity-grid{display:grid;grid-auto-flow:column;"
        "grid-template-columns:repeat(52,minmax(8px,1fr));"
        "grid-template-rows:repeat(7,minmax(8px,1fr));"
        "gap:3.5px;width:100%;}"
        in compact
    )
    assert (
        ".profile-activity-week-hits{position:absolute;inset:0;display:grid;"
        "grid-template-columns:repeat(52,minmax(8px,1fr));"
        "gap:3.5px;pointer-events:none;}"
        in compact
    )
    assert (
        ".profile-activity-months{display:flex;justify-content:space-between;"
        "min-width:595px;"
        in compact
    )
    assert (
        ".profile-activity-legend{display:flex;align-items:center;"
        "justify-content:flex-end;gap:8px;min-width:595px;"
        in compact
    )
    assert ".profile-activity-cell{" in compact
    assert "border-radius:2px" in compact

    assert (
        ".profile-activity-scroll{--profile-interactive-aura-reserve:26px;"
        "--profile-edge-safety-gutter:8px;overflow-x:auto;"
        "padding-block:8px12px;"
        "padding-inline:0calc(var(--profile-interactive-aura-reserve)"
        "+var(--profile-edge-safety-gutter));}"
    ) in compact
    assert (
        ".overview-profile-scroll{--overview-profile-interactive-aura-reserve:16px;"
        "--profile-edge-safety-gutter:8px;overflow-x:auto;"
        "padding-block:2px4px;"
        "padding-inline:0calc(var(--overview-profile-interactive-aura-reserve)"
        "+var(--profile-edge-safety-gutter));}"
    ) in compact

    for fragment in [
        "0002pxcolor-mix(insrgb,var(--profile-milestone-tone)86%,transparent)",
        "0011pxcolor-mix(insrgb,var(--profile-milestone-tone)72%,transparent)",
        "0014pxcolor-mix(insrgb,var(--profile-milestone-tone)80%,transparent)",
        "0003pxcolor-mix(insrgb,var(--profile-milestone-tone)72%,transparent)",
        "0017pxcolor-mix(insrgb,var(--profile-milestone-tone)88%,transparent)",
        "0004pxcolor-mix(insrgb,var(--profile-milestone-tone)78%,transparent)",
        "0022pxcolor-mix(insrgb,var(--profile-milestone-tone)96%,transparent)",
    ]:
        assert fragment in compact

    overview_base_rules = [
        '.overview-profile-cell[data-milestone="1"]{box-shadow:inset 0 0 0 1px '
        "var(--profile-milestone-keyline),0 0 0 1px color-mix(in srgb,"
        "var(--profile-milestone-tone) 72%,transparent),0 0 6px color-mix("
        "in srgb,var(--profile-milestone-tone) 42%,transparent);}",
        '.overview-profile-cell[data-milestone="2"]{box-shadow:inset 0 0 0 1px '
        "var(--profile-milestone-keyline),0 0 0 1px color-mix(in srgb,"
        "var(--profile-milestone-tone) 78%,transparent),0 0 8px color-mix("
        "in srgb,var(--profile-milestone-tone) 52%,transparent);}",
        '.overview-profile-cell[data-milestone="3"]{box-shadow:inset 0 0 0 1px '
        "var(--profile-milestone-keyline),0 0 0 1px color-mix(in srgb,"
        "var(--color-text) 74%,transparent),0 0 0 2px color-mix(in srgb,"
        "var(--profile-milestone-tone) 48%,transparent),0 0 10px color-mix("
        "in srgb,var(--profile-milestone-tone) 62%,transparent);}",
        '.overview-profile-cell[data-milestone="4"]{box-shadow:inset 0 0 0 1px '
        "var(--profile-milestone-keyline),0 0 0 1px color-mix(in srgb,"
        "var(--color-text) 82%,transparent),0 0 0 3px color-mix(in srgb,"
        "var(--profile-milestone-tone) 58%,transparent),0 0 13px color-mix("
        "in srgb,var(--profile-milestone-tone) 72%,transparent);}",
    ]
    for rule in overview_base_rules:
        assert rule in source

    overview_interaction_rule = (
        ".overview-profile-grid button.overview-profile-cell[data-milestone]"
        ":not(:disabled):is(:hover,:focus-visible),.overview-profile-grid "
        ".overview-profile-cell[data-milestone].is-column-hover{box-shadow:"
        "inset 0 0 0 1px var(--profile-milestone-keyline),0 0 0 1px "
        "color-mix(in srgb,var(--color-text) 88%,transparent),0 0 0 3px "
        "color-mix(in srgb,var(--profile-milestone-tone) 68%,transparent),"
        "0 0 16px color-mix(in srgb,var(--profile-milestone-tone) 82%,transparent);}"
    )
    assert overview_interaction_rule in source


def test_profile_view_html_and_localization_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    required_ids = [
        "statsViewTitle",
        "viewProfile",
        "statsMetricControls",
        "calendarProfileView",
        "profileStatRecordedTokens",
        "profileStatPeakDay",
        "profileStatActiveDays",
        "profileStatCurrentStreak",
        "profileStatLongestStreak",
        "profileActivityGrid",
        "profileActivityWeekHits",
        "profileActivityMonths",
        "profileActivityLegend",
        "profileActivityScroller",
        "profileActivityTooltip",
        "profileModeDaily",
        "profileModeWeekly",
        "profileModeCumulative",
    ]
    for element_id in required_ids:
        assert f'id="{element_id}"' in source
    assert source.count("profileView: '") == 2
    assert source.count("recordedTokens: '") == 2
    assert source.count("tokenActivity: '") == 2
    assert ".profile-activity-cell" in source
    assert "border-radius:2px" in source.replace(" ", "")
    assert "function renderProfileView() {" in source
    assert "function setProfileActivityMode(mode) {" in source
    assert "function showProfileActivityTooltip(target, model) {" in source
    assert "setView('profile')" in source
    assert "renderProfileView();" in source
    assert "'calendarProfileView'" in source


def test_profile_visual_polish_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)
    assert re.findall(
        r'class="profile-activity-stat" data-tone="([^"]+)"', source
    ) == ["primary", "secondary", "cost", "cta", "primary"]
    assert '.profile-activity-head h3::before' in source
    assert '.profile-activity-mode[aria-pressed="true"]' in source
    assert '.profile-activity-tooltip-header' in source
    assert '.profile-activity-tooltip-row[data-tone="reasoning"]' in source
    assert '.profile-activity-tooltip-row[data-key="estimatedCost"]' in source
    assert "button.profile-activity-cell:not(:disabled):is(:hover,:focus-visible)" in source
    assert ".profile-activity-cell{transform:none!important;}" in compact

    renderer = _extract_js_function(
        source,
        "function renderProfileActivityTooltipContent(tooltip, model) {",
    )
    assert "document.createElement(" in renderer
    assert ".textContent =" in renderer
    assert "tooltip.replaceChildren(header, body);" in renderer
    assert ".innerHTML" not in renderer

    binder = _extract_js_function(
        source,
        "function bindProfileActivityTooltip(target, model) {",
    )
    assert "model.accessibleText" in binder
    assert "content.replaceAll" not in binder

    resolver = _extract_js_function(
        source, "function resolveProfileActivityTooltip(target) {"
    )
    assert "dataset.profileTooltipId" in resolver
    assert "profileActivityTooltip" in resolver


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_profile_tooltip_position_handles_zoomed_viewport_and_edges(tmp_path: Path):
    payload = {
        "cases": [
            {
                "target": {
                    "left": 1500,
                    "top": 900,
                    "bottom": 920,
                    "width": 20,
                },
                "tooltip": {"width": 260, "height": 180},
                "viewport": {"width": 3072, "height": 1728},
            },
            {
                "target": {
                    "left": 3060,
                    "top": 500,
                    "bottom": 512,
                    "width": 12,
                },
                "tooltip": {"width": 260, "height": 180},
                "viewport": {"width": 3072, "height": 1728},
            },
            {
                "target": {"left": 10, "top": 3, "bottom": 15, "width": 12},
                "tooltip": {"width": 260, "height": 180},
                "viewport": {"width": 3072, "height": 1728},
            },
            {
                "target": {
                    "left": 500,
                    "top": 1700,
                    "bottom": 1712,
                    "width": 20,
                },
                "tooltip": {"width": 260, "height": 180},
                "viewport": {"width": 3072, "height": 1728},
            },
            {
                "target": {"left": 90, "top": 50, "bottom": 60, "width": 10},
                "tooltip": {"width": 260, "height": 180},
                "viewport": {"width": 200, "height": 120},
            },
        ]
    }
    result = _run_profile_tooltip_position_js(
        tmp_path,
        "payload.cases.map((item) => calculateProfileActivityTooltipPosition(item.target, item.tooltip, item.viewport))",
        payload,
    )

    assert result == [
        {"left": 1380, "top": 712},
        {"left": 2804, "top": 312},
        {"left": 8, "top": 23},
        {"left": 380, "top": 1512},
        {"left": 8, "top": 8},
    ]


def test_profile_tooltips_use_viewport_portal_and_reflow_on_zoom():
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)
    portal_markup = """
  <!-- Viewport-level tooltips must stay outside filtered/transformed cards. -->
  <div id="overviewProfileTooltip" class="profile-activity-tooltip overview-profile-tooltip" role="tooltip" hidden></div>
  <div id="profileActivityTooltip" class="profile-activity-tooltip" role="tooltip" hidden></div>
""".strip()

    assert portal_markup in source
    assert source.count('id="overviewProfileTooltip"') == 1
    assert source.count('id="profileActivityTooltip"') == 1
    assert "max-height:calc(100dvh-16px)" in compact
    assert "contain:layoutpaintstyle" in compact

    viewport = _extract_js_function(
        source,
        "function getProfileActivityViewport() {",
    )
    positioner = _extract_js_function(
        source,
        "function positionActiveProfileActivityTooltip() {",
    )
    shower = _extract_js_function(
        source,
        "function showProfileActivityTooltip(target, model) {",
    )
    assert "window.visualViewport" in viewport
    assert "target.getBoundingClientRect()" in positioner
    assert "tooltip.getBoundingClientRect()" in positioner
    assert "tooltip.style.maxWidth" in positioner
    assert "tooltip.style.maxHeight" in positioner
    assert "snapProfileActivityTooltipCoordinate" in positioner
    assert "offsetWidth" not in shower
    assert "offsetHeight" not in shower
    assert "window.innerWidth" not in shower
    assert "window.innerHeight" not in shower
    assert "window.addEventListener('resize', scheduleProfileActivityTooltipPosition" in source
    assert "window.addEventListener('scroll', scheduleProfileActivityTooltipPosition" in source
    assert "window.visualViewport?.addEventListener(" in source


def test_profile_activity_legend_is_safe_localized_and_mode_aware():
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)

    assert 'id="profileActivityLegend"' in source
    assert source.count("activityLegend: '") == 2
    assert ".profile-activity-legend{" in compact
    assert ".profile-activity-legend-swatches{" in compact
    assert ".profile-activity-legend-bars{" in compact
    assert ".profile-activity-legend-badge{" in compact

    renderer = _extract_js_function(
        source,
        "function renderProfileActivityLegend(targetId, mode) {",
    )
    assert "document.createElement(" in renderer
    assert "legend.replaceChildren(scale, divider, milestones, toggle);" in renderer
    assert "mode === 'daily'" in renderer
    assert "getProfileActivityColor(level)" in renderer
    assert "profile-activity-legend-bars" in renderer
    assert "profile-activity-legend-badge" in renderer
    assert "profile-milestone-toggle" in renderer
    assert "glyph.setAttribute('aria-hidden', 'true');" in renderer
    assert ".innerHTML" not in renderer

    profile_renderer = _extract_js_function(
        source,
        "function renderProfileView() {",
    )
    assert (
        "renderProfileActivityLegend('profileActivityLegend', profileActivityMode);"
        in profile_renderer
    )


def test_profile_milestone_toggle_defaults_off_persists_and_syncs_both_views():
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)

    assert "const PROFILE_MILESTONES_STORAGE_KEY = 'tokdash-profile-milestones';" in source
    assert (
        "let profileMilestonesEnabled = "
        "localStorage.getItem(PROFILE_MILESTONES_STORAGE_KEY) === 'on';"
        in source
    )
    assert source.count("milestones: '") == 2
    assert source.count("showMilestones: '") == 2
    assert source.count("hideMilestones: '") == 2
    assert ".profile-milestone-toggle{" in compact
    assert '[data-milestones-enabled="false"]' in source

    setter = _extract_js_function(
        source,
        "function setProfileMilestonesEnabled(enabled) {",
    )
    assert "profileMilestonesEnabled = Boolean(enabled);" in setter
    assert "localStorage.setItem(" in setter
    assert "profileMilestonesEnabled ? 'on' : 'off'" in setter
    assert "renderProfileView();" in setter
    assert "renderOverviewProfilePreview();" in setter

    syncer = _extract_js_function(
        source,
        "function syncProfileMilestoneControls() {",
    )
    assert "document.querySelectorAll('.profile-milestone-toggle')" in syncer
    assert "'aria-pressed'" in syncer
    assert "'data-milestones-enabled'" in syncer

    applier = _extract_js_function(
        source,
        "function applyProfileMilestone(target, milestone) {",
    )
    assert "delete target.dataset.milestone;" in applier
    assert "if (!profileMilestonesEnabled || !milestone) return;" in applier


def test_paper_uses_warm_terracotta_heat_palette():
    light, dark = _heat_palette_for("paper")
    assert light == [
        "#F4EDE0", "#ECDDC8", "#DFC5A4", "#CEA475",
        "#B97C4A", "#9D5E35", "#7D432C", "#5C2E23",
    ]
    assert dark == [
        "#1F1914", "#2B2018", "#3A291D", "#513523",
        "#70442B", "#985D38", "#C47E50", "#E8B080",
    ]

    profile_indices = [round((level / 4) * (len(light) - 1)) for level in range(5)]
    assert profile_indices == [0, 2, 4, 5, 7]
    assert [light[index] for index in profile_indices] == [
        "#F4EDE0", "#DFC5A4", "#B97C4A", "#9D5E35", "#5C2E23",
    ]
    assert [dark[index] for index in profile_indices] == [
        "#1F1914", "#3A291D", "#70442B", "#985D38", "#E8B080",
    ]

    classic_light, classic_dark = _heat_palette_for("classic")
    assert classic_light == [
        "#EEF2F7", "#E0E7FF", "#C7D2FE", "#A5B4FC",
        "#60A5FA", "#3B82F6", "#2563EB", "#1E40AF",
    ]
    assert classic_dark == [
        "#172033", "#1E293B", "#1D4ED8", "#2563EB",
        "#3B82F6", "#60A5FA", "#93C5FD", "#BFDBFE",
    ]

    activity_color = _extract_js_function(
        INDEX_HTML.read_text(encoding="utf-8"),
        "function getProfileActivityColor(level) {",
    )
    assert "Math.round((safeLevel / 4) * Math.max(0, HEAT_COLORS.length - 1))" in activity_color


def test_overview_profile_renderer_reuses_stats_warm_response():
    source = INDEX_HTML.read_text(encoding="utf-8")
    warm = _extract_js_function(
        source, "function scheduleStatsWarm(force = false) {"
    )
    renderer = _extract_js_function(
        source, "function renderOverviewProfilePreview(contributions = statsCache.default) {"
    )
    resolver = _extract_js_function(
        source, "function resolveProfileActivityTooltip(target) {"
    )

    assert warm.count("fetchJsonWithRetry(appPath('/api/stats')") == 1
    assert "fillMissingDays(data.contributions || [])" in warm
    assert "statsCache.default = filledContributions;" in warm
    assert "renderOverviewProfilePreview(filledContributions);" in warm
    assert "setOverviewProfileState('error');" in warm
    assert "fetch(" not in renderer
    assert "buildOverviewProfilePreview(contributions" in renderer
    assert "overviewProfileTooltip" in renderer
    assert "bindProfileActivityTooltip(cell, tooltip);" in renderer
    assert "shell?.dataset.state !== 'error'" in renderer
    assert "target?.dataset.profileTooltipId || 'profileActivityTooltip'" in resolver
    assert "renderOverviewProfilePreview();" in _extract_js_function(
        source, "function renderOverviewTab(data) {"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_overview_profile_warm_cache_refreshes_only_when_forced(tmp_path: Path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    scheduler = _extract_js_function(
        source, "function scheduleStatsWarm(force = false) {"
    )
    harness = tmp_path / "profile-warm-cache.js"
    harness.write_text(
        """
let statsWarmScheduled = false;
let statsLoaded = false;
const statsCache = { default: null };
let fetchCount = 0;
let renderCount = 0;
function setTimeout(callback) { callback(); }
function scheduleIdle(callback) { callback(); }
function appPath(value) { return value; }
function fetchJsonWithRetry() {
  fetchCount += 1;
  return Promise.resolve({ contributions: [] });
}
function fillMissingDays(contributions) { return contributions; }
function isOverviewActive() { return true; }
function renderOverviewProfilePreview() { renderCount += 1; }
function setOverviewProfileState() {}
"""
        + scheduler
        + """
async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}
(async () => {
  scheduleStatsWarm();
  await flushPromises();
  const initial = { fetchCount, renderCount, pending: statsWarmScheduled };

  scheduleStatsWarm();
  await flushPromises();
  const cached = { fetchCount, renderCount, pending: statsWarmScheduled };

  scheduleStatsWarm(true);
  await flushPromises();
  const forced = { fetchCount, renderCount, pending: statsWarmScheduled };

  process.stdout.write(JSON.stringify({ initial, cached, forced }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == {
        "initial": {"fetchCount": 1, "renderCount": 1, "pending": False},
        "cached": {"fetchCount": 1, "renderCount": 1, "pending": False},
        "forced": {"fetchCount": 2, "renderCount": 2, "pending": False},
    }
    assert "scheduleStatsWarm(forceRefresh);" in source


def test_overview_profile_summary_markup_and_style_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)
    required_ids = [
        "overviewProfileSummary",
        "overviewProfileTitle",
        "overviewProfileOpen",
        "overviewProfileLoading",
        "overviewProfileUnavailable",
        "overviewProfileReady",
        "overviewProfileActiveDays",
        "overviewProfileCurrentStreak",
        "overviewProfileLongestStreak",
        "overviewProfileScroller",
        "overviewProfileModeDaily",
        "overviewProfileModeWeekly",
        "overviewProfileModeCumulative",
        "overviewProfileActivityLabel",
        "overviewProfileGridWrap",
        "overviewProfileGrid",
        "overviewProfileWeekHits",
        "overviewProfileLegend",
        "overviewProfileTooltip",
    ]
    for element_id in required_ids:
        assert f'id="{element_id}"' in source

    assert source.index('id="modelChart"') < source.index('id="overviewProfileSummary"')
    assert source.index('id="overviewProfileSummary"') < source.index('id="toolsTable"')
    for key in [
        "overviewProfileEyebrow",
        "overviewProfileTitle",
        "overviewProfileRange",
        "viewFullProfile",
        "overviewProfileUnavailable",
    ]:
        assert source.count(f"{key}: '") == 2

    assert ".overview-profile-band{" in compact
    assert "grid-template-columns:minmax(180px,220px)minmax(0,1fr)" in compact
    assert (
        ".overview-profile-grid-wrap{position:relative;min-width:595px;}"
        in compact
    )
    assert (
        ".overview-profile-grid{display:grid;grid-auto-flow:column;"
        "grid-template-columns:repeat(52,minmax(8px,1fr));"
        "grid-template-rows:repeat(7,minmax(8px,1fr));"
        "gap:3.5px;width:100%;}"
        in compact
    )
    assert (
        ".overview-profile-week-hits{"
        "grid-template-columns:repeat(52,minmax(8px,1fr));"
        "gap:3.5px;}"
        in compact
    )
    assert ".overview-profile-cell{border-radius:2px" in compact
    assert "overviewProfileRange: 'Past 52 weeks'" in source
    assert "overviewProfileRange: '过去 52 周'" in source
    assert source.count('class="profile-activity-mode overview-profile-mode"') == 3
    assert 'aria-labelledby="overviewProfileActivityLabel"' in source
    assert 'aria-label="Activity aggregation"' not in source
    assert ".overview-profile-mode{" in compact
    assert 'id="overviewProfileWeekHits"' in source
    assert "@media(max-width:800px)" in compact
    assert ".overview-profile-cell{transform:none!important;}" in compact


def test_overview_profile_modes_share_state_and_render_aggregate_hits_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    overview_renderer = _extract_js_function(
        source,
        "function renderOverviewProfilePreview(contributions = statsCache.default) {",
    )
    profile_renderer = _extract_js_function(source, "function renderProfileView() {")
    setter = _extract_js_function(source, "function setProfileActivityMode(mode) {")
    syncer = _extract_js_function(
        source,
        "function syncProfileActivityModeControls() {",
    )

    assert "buildOverviewProfilePreview(contributions, new Date(), profileActivityMode)" in overview_renderer
    assert "profileActivityMode === 'daily' ? 'button' : 'span'" in overview_renderer
    assert "overviewProfileWeekHits" in overview_renderer
    assert "formatProfileAggregateTooltip(" in overview_renderer
    assert "profileMilestonesEnabled" in overview_renderer
    assert "setOverviewProfileColumnHover(weekIndex, true)" in overview_renderer
    assert "applyProfileMilestone(hit, milestone);" in overview_renderer
    assert "fetch(" not in overview_renderer
    assert "syncProfileActivityModeControls();" in overview_renderer
    assert "syncProfileActivityModeControls();" in profile_renderer
    assert "syncProfileActivityModeControls();" in setter
    assert "if (isOverviewActive()) renderOverviewProfilePreview();" in setter
    assert "fetch(" not in setter
    for element_id in [
        "profileModeDaily",
        "profileModeWeekly",
        "profileModeCumulative",
        "overviewProfileModeDaily",
        "overviewProfileModeWeekly",
        "overviewProfileModeCumulative",
    ]:
        assert element_id in syncer
    for element_id in [
        "overviewProfileModeDaily",
        "overviewProfileModeWeekly",
        "overviewProfileModeCumulative",
    ]:
        assert f"document.getElementById('{element_id}').addEventListener" in source


def test_overview_profile_legend_reuses_mode_and_scroller_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)

    assert 'id="overviewProfileLegend"' in source
    assert source.index('id="overviewProfileGridWrap"') < source.index(
        'id="overviewProfileLegend"'
    ) < source.index('id="overviewProfileTooltip"')
    assert ".overview-profile-legend{" in compact

    renderer = _extract_js_function(
        source,
        "function renderOverviewProfilePreview(contributions = statsCache.default) {",
    )
    assert (
        "renderProfileActivityLegend('overviewProfileLegend', profileActivityMode);"
        in renderer
    )
    assert "fetch(" not in renderer

    i18n = _extract_js_function(source, "function applyI18n() {")
    assert "renderProfileActivityLegend('profileActivityLegend', profileActivityMode);" in i18n
    assert "renderProfileActivityLegend('overviewProfileLegend', profileActivityMode);" in i18n


def test_overview_profile_navigation_preserves_profile_state_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    activator = _extract_js_function(source, "function activateDashboardTab(tab) {")
    opener = _extract_js_function(source, "function openOverviewProfile() {")

    assert "content.classList.add('active');" in activator
    assert "const preserveNavigation = preserveStatsNavigationOnNextActivation;" in activator
    assert "preserveStatsNavigationOnNextActivation = false;" in activator
    assert "loadStats({ preserveNavigation });" in activator
    assert "preserveStatsNavigationOnNextActivation = true;" in opener
    assert "activateDashboardTab('stats');" in opener
    assert "setView('profile');" in opener
    assert "profileActivityMode =" not in opener
    assert "monthCursor =" not in opener
    assert "yearCursor =" not in opener
    assert "addEventListener('click', openOverviewProfile)" in source


def test_overview_profile_resize_rearms_initial_scroll_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    helper = _extract_js_function(
        source, "function ensureOverviewProfileInitialScroll() {"
    )

    no_overflow = "if (scroller.scrollWidth <= scroller.clientWidth) {"
    assert no_overflow in helper
    assert "overviewProfileDidInitialScroll = false;" in helper
    assert helper.index(no_overflow) < helper.index(
        "if (overviewProfileDidInitialScroll) return;"
    )
    assert (
        "window.addEventListener('resize', ensureOverviewProfileInitialScroll, "
        "{ passive: true });"
    ) in source


def test_activity_insights_profile_and_overview_markup_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    for element_id in (
        "profileActivityInsights",
        "profileActivityInsightsKpis",
        "profileActivityCoverage",
        "profileActivityToolRanking",
        "overviewActivityInsights",
        "overviewActivityInsightsValues",
        "overviewActivityInsightsState",
    ):
        assert f'id="{element_id}"' in source
    assert source.index('id="profileActivityLegend"') < source.index(
        'id="profileActivityInsights"'
    )
    assert source.index('id="overviewProfileLegend"') < source.index(
        'id="overviewActivityInsights"'
    )


def test_activity_insights_use_one_shared_fetch_and_limit_profile_to_five_tools():
    source = INDEX_HTML.read_text(encoding="utf-8")
    loader = _extract_js_function(
        source, "async function loadActivityInsights(options = {}) {"
    )
    profile = _extract_js_function(
        source, "function renderProfileActivityInsights() {"
    )
    overview = _extract_js_function(
        source, "function renderOverviewActivityInsights() {"
    )

    assert source.count("appPath('/api/activity-insights')") == 1
    assert "force ? '?refresh=1' : ''" in loader
    assert "fetchJsonWithRetry(url)" in loader
    assert "activityInsightsState.promise" in loader
    assert ".slice(0, 5)" in profile
    assert "fetch(" not in profile
    assert "fetch(" not in overview
    assert "innerHTML" not in profile
    assert "innerHTML" not in overview
    assert "textContent" in profile
    assert "textContent" in overview


def test_activity_insights_localization_states_and_responsive_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)
    for key in (
        "activityInsightsTitle",
        "activityRecordedChats",
        "activityReasoning",
        "activityToolCalls",
        "activityTopTool",
        "activityCoverage",
        "activityNoData",
        "activityUnavailable",
        "activityLocalScope",
        "effortXhigh",
    ):
        assert source.count(f"{key}: '") == 2
    assert "@media(max-width:768px)" in compact
    assert (
        ".profile-activity-insights-kpis{display:grid;"
        "grid-template-columns:repeat(4,minmax(0,1fr))" in compact
    )
    assert (
        ".profile-activity-insights-kpis,.overview-activity-insights-values"
        "{grid-template-columns:repeat(2,minmax(0,1fr))" in compact
    )
    assert ".overview-activity-insights{background:transparent" in compact
    assert ".overview-profile-grid-wrap{position:relative;min-width:595px;" in compact


def _run_activity_insights_js(
    tmp_path: Path, expression: str, payload: dict, labels: dict | None = None
) -> object:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, signature)
        for signature in (
            "function safeActivityNumber(value) {",
            "function formatActivityShare(value) {",
            "function formatActivityEffort(effort) {",
            "function buildActivityInsightValues(data) {",
        )
    )
    harness = tmp_path / "activity-insights.js"
    harness.write_text(
        "const LABELS = JSON.parse(process.argv[3]);\n"
        "function t(key) { return LABELS[key] || key; }\n"
        "function formatNumber(value) { return new Intl.NumberFormat('en-US').format(Number(value) || 0); }\n"
        + functions
        + "\nconst payload = JSON.parse(process.argv[2]);\n"
        + f"const result = {expression};\n"
        + "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "node",
            str(harness),
            json.dumps(payload),
            json.dumps(
                labels
                or {
                    "effortXhigh": "Extra High",
                    "effortHigh": "High",
                    "effortMedium": "Medium",
                    "effortLow": "Low",
                }
            ),
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_activity_insights_display_values_cover_full_partial_and_unknown_effort(
    tmp_path: Path,
):
    full = {
        "recorded_chats": {"value": 12},
        "reasoning": {
            "most_used": {"effort": "xhigh", "count": 48, "share": 0.48}
        },
        "tools": {
            "total_calls": 892,
            "most_used": {"name": "exec", "count": 330, "share": 0.37},
        },
    }
    result = _run_activity_insights_js(
        tmp_path, "buildActivityInsightValues(payload)", full
    )
    assert result == {
        "empty": False,
        "chats": "12",
        "reasoning": "Extra High · 48%",
        "toolCalls": "892",
        "topTool": "exec · 37%",
    }

    partial = {
        "recorded_chats": {"value": 1},
        "reasoning": {"most_used": {"effort": "turbo", "share": 0.5}},
        "tools": {"total_calls": 0, "most_used": None},
    }
    result = _run_activity_insights_js(
        tmp_path, "buildActivityInsightValues(payload)", partial
    )
    assert result["empty"] is False
    assert result["reasoning"] == "turbo · 50%"
    assert result["topTool"] == "—"

    empty = _run_activity_insights_js(
        tmp_path, "buildActivityInsightValues(payload)", {}
    )
    assert empty["empty"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_main_inline_script_parses_with_node(tmp_path: Path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    inline_scripts = re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>", source, flags=re.DOTALL
    )
    main_script = max(inline_scripts, key=len)
    harness = tmp_path / "tokdash-main.js"
    harness.write_text(main_script, encoding="utf-8")
    subprocess.run(
        ["node", "--check", str(harness)],
        check=True,
        capture_output=True,
        text=True,
    )
