"""Tests for the profile stats and activity heatmap frontend.

Covers activity heatmap rendering, daily/weekly/cumulative aggregation, intensity
levels, theme palettes, milestone tiers, and contribution reconciliation. The
model functions run under node on their own; the Stats profile, the Overview
band, their legends, tooltips, mode buttons and milestone switch, and the two
activity-insight panels run on the stub DOM in tests/support/i18n_profile.py.
"""
from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

import tokdash

from support.frontend import extract_js_function, index_source, needs_node, run_node
from support.i18n_profile import (
    APPLY_I18N_FUNCTIONS,
    APPLY_I18N_STATE,
    page_script,
    run_page,
    slice_line,
    slice_statement,
)


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


PROFILE_FUNCTIONS = [
    "function parseDateKey(value) {",
    "function formatDateKey(date) {",
    "function startOfWeekMonday(date) {",
    "function createEmptyContribution(key) {",
    "function safeProfileNumber(value) {",
    "function buildUsageProfileContribution(dateKey, usageData) {",
    "function reconcileTodayProfileContribution(contributions, usageData, startDate, endDate, today = new Date()) {",
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
        extract_js_function(source, signature) for signature in PROFILE_FUNCTIONS
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
let currentLang = 'en';
const LANG_LOCALES = { en: 'en-US', zh: 'zh-CN', ja: 'ja-JP', ko: 'ko-KR', es: 'es-ES', pt: 'pt-BR' };
function langLocale(lang = currentLang) { return LANG_LOCALES[lang] || 'en-US'; }
let overviewReadableTokens = true;
"""


def _run_profile_tooltip_js(tmp_path: Path, expression: str, payload: dict) -> object:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        [
            extract_js_function(source, "function normalizeTokenCount(value) {"),
            extract_js_function(source, "function formatCompactTokenCount(value) {"),
            extract_js_function(
                source,
                "function formatTokenCount(value, includeUnit = false) {",
            ),
            extract_js_function(source, "function safeProfileNumber(value) {"),
            extract_js_function(source, "function getProfileMilestoneTier(value) {"),
            extract_js_function(
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
    function = extract_js_function(
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


@needs_node
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


@needs_node
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


@needs_node
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


@needs_node
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


@needs_node
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


@needs_node
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


@needs_node
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


@needs_node
@pytest.mark.parametrize("end,cumulative,title,date_calls", [
    ("2026-07-20", False, "2026-07-20", 1),
    ("2026-07-20", True, "Cumulative · 2026-07-20 → 2026-07-20", 2),
    ("2026-07-26", False, "2026-07-20 → 2026-07-26", 2),
    ("2026-07-26", True, "Cumulative · 2026-07-20 → 2026-07-26", 2),
])
def test_profile_tooltip_formats_only_dates_used_by_its_title(tmp_path, end, cumulative, title, date_calls):
    result = _run_profile_tooltip_js(
        tmp_path,
        """(() => {
          const original = formatShortDate;
          let calls = 0;
          formatShortDate = (...args) => { calls++; return original(...args); };
          const models = Array.from({length:365}, () =>
            formatProfileAggregateTooltip(payload.aggregate, payload.cumulative));
          return {calls, titles:models.map(model=>model.title),
            accessible:models.map(model=>model.accessibleText)};
        })()""",
        {"aggregate": {"startDate": "2026-07-20", "endDate": end}, "cumulative": cumulative},
    )
    assert result["calls"] == 365 * date_calls
    assert result["titles"] == [title] * 365
    assert all(value.startswith(title + ". Total:") for value in result["accessible"])


@needs_node
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
        "value": "1.2K tokens",
        "tone": "primary",
        "prominent": True,
    }
    assert result["accessibleText"] == (
        "2026-07-20 → 2026-07-26. Total: 1.2K tokens. Input: 500. "
        "Output: 300. Cache read: 250. Cache write: 100. Reasoning: 84. "
        "Estimated cost: $1.25"
    )


@needs_node
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


@needs_node
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


def test_paper_keeps_a_keyline_around_milestone_cells():
    """Paper's heat ramp starts at its own background colour, so a milestone
    aura needs a keyline in that colour to read as a ring around the cell."""
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


@needs_node
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


@needs_node
def test_overview_profile_warm_cache_refreshes_only_when_forced(tmp_path: Path):
    """The Overview band is painted from one background /api/stats read.

    Its gap-filled, today-reconciled contributions become the shared stats
    cache and are handed to the band as they are; a second warm reuses them, a
    forced one reads again, and a failed read marks the band as failed while
    keeping what is cached.
    """
    source = INDEX_HTML.read_text(encoding="utf-8")
    scheduler = extract_js_function(
        source, "function scheduleStatsWarm(force = false) {"
    )
    harness = tmp_path / "profile-warm-cache.js"
    harness.write_text(
        """
let statsWarmScheduled = false;
let statsLoaded = false;
const statsCache = { default: null };
const lastUsageResponse = { today: 'usage' };
const currentStartDate = 'range-start';
const currentEndDate = 'range-end';
let fetchCount = 0;
let failNext = false;
const urls = [];
const rendered = [];
const states = [];
const reconciled = [];
function setTimeout(callback) { callback(); }
function scheduleIdle(callback) { callback(); }
function t(key) { return key; }
function fetchSelectedServers(url) {
  fetchCount += 1;
  urls.push(url);
  if (failNext) return Promise.reject(new Error('stats down'));
  return Promise.resolve([{ payload: { contributions: [{ date: '2026-01-05' }] } }]);
}
function combineStatsPayloads(payloads) { return payloads[0]; }
function fillMissingDays(contributions) { return [...contributions, { date: 'filled' }]; }
function reconcileTodayProfileContribution(contributions, usage, start, end) {
  reconciled.push({ dates: contributions.map((day) => day.date), usage: usage === lastUsageResponse, range: [start, end] });
  return [...contributions, { date: 'today' }];
}
function isOverviewActive() { return true; }
function renderOverviewProfilePreview(contributions) { rendered.push(contributions); }
function setOverviewProfileState(state) { states.push(state); }
"""
        + scheduler
        + """
async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}
const cached = () => (statsCache.default || []).map((day) => day.date);
(async () => {
  scheduleStatsWarm();
  await flushPromises();
  const initial = { fetchCount, renders: rendered.length, pending: statsWarmScheduled,
    cache: cached(), handedOver: rendered[0] === statsCache.default };

  scheduleStatsWarm();
  await flushPromises();
  const reused = { fetchCount, renders: rendered.length, pending: statsWarmScheduled };

  failNext = true;
  scheduleStatsWarm(true);
  await flushPromises();
  const failed = { fetchCount, renders: rendered.length, pending: statsWarmScheduled, states: [...states], cache: cached() };

  failNext = false;
  scheduleStatsWarm(true);
  await flushPromises();
  const forced = { fetchCount, renders: rendered.length, pending: statsWarmScheduled };

  process.stdout.write(JSON.stringify({ initial, reused, failed, forced, urls, reconciled: reconciled[0] }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
""",
        encoding="utf-8",
    )
    report = json.loads(run_node(harness))
    contributions = ["2026-01-05", "filled", "today"]
    assert report["initial"] == {
        "fetchCount": 1,
        "renders": 1,
        "pending": False,
        "cache": contributions,
        "handedOver": True,
    }
    assert report["reused"] == {"fetchCount": 1, "renders": 1, "pending": False}
    assert report["failed"] == {
        "fetchCount": 2,
        "renders": 1,
        "pending": False,
        "states": ["error"],
        "cache": contributions,
    }
    assert report["forced"] == {"fetchCount": 3, "renders": 2, "pending": False}
    assert report["urls"] == ["/api/stats"] * 3
    assert report["reconciled"] == {
        "dates": ["2026-01-05", "filled"],
        "usage": True,
        "range": ["range-start", "range-end"],
    }
    # The callers that start and repaint it, which no harness here runs.
    assert "scheduleStatsWarm(forceRefresh);" in source
    assert "renderOverviewProfilePreview();" in extract_js_function(
        source, "function renderOverviewTab(data) {"
    )


@needs_node
def test_today_usage_reconciles_the_profile_day_without_mutating_history(tmp_path: Path):
    payload = {
        "contributions": [
            {
                "date": "2026-08-11",
                "totals": {"tokens": 80, "cost": 0.8, "messages": 1},
                "tokenBreakdown": {"input": 20, "output": 10, "cacheRead": 50, "cacheWrite": 0, "reasoning": 0},
                "sources": [],
            },
            {
                "date": "2026-08-12",
                "totals": {"tokens": 10, "cost": 0.1, "messages": 1},
                "tokenBreakdown": {"input": 2, "output": 1, "cacheRead": 7, "cacheWrite": 0, "reasoning": 0},
                "sources": [],
            },
        ],
        "usage": {
            "total_tokens": 300,
            "total_cost": 2.5,
            "total_messages": 4,
            "coding_models": [
                {
                    "source": "codex",
                    "name": "gpt-test",
                    "tokens": 300,
                    "tokens_in": 40,
                    "tokens_out": 20,
                    "tokens_cache": 100,
                    "cost": 2.5,
                    "messages": 4,
                }
            ],
            "openclaw_models": [],
        },
        "startDate": "2026-08-12",
        "endDate": "2026-08-12",
        "today": "2026-08-12",
    }
    result = _run_profile_js(
        tmp_path,
        "reconcileTodayProfileContribution(payload.contributions, payload.usage, parseDateKey(payload.startDate), parseDateKey(payload.endDate), parseDateKey(payload.today))",
        payload,
    )

    assert result[0]["date"] == "2026-08-11"
    assert result[0]["totals"]["tokens"] == 80
    assert result[1]["totals"] == {"tokens": 300, "cost": 2.5, "messages": 4}
    assert result[1]["tokenBreakdown"] == {
        "input": 40,
        "output": 20,
        "cacheRead": 100,
        "cacheWrite": 0,
        "reasoning": 140,
    }
    assert result[1]["sources"][0]["source"] == "codex"
    assert result[1]["sources"][0]["modelId"] == "gpt-test"

    source = INDEX_HTML.read_text(encoding="utf-8")
    renderer = extract_js_function(source, "function renderOverviewTab(data) {")
    loader = extract_js_function(source, "async function loadStats(options = {}) {")
    # The warm path is run above; these two callers are not run anywhere.
    assert "reconcileTodayProfileContribution(" in renderer
    assert "reconcileTodayProfileContribution(" in loader


# --- the two heatmaps on a stub DOM ------------------------------------------
#
# The Stats profile and the Overview band are drawn by renderProfileView and
# renderOverviewProfilePreview into a page shaped like index.html's: six mode
# buttons, two grids with their week-hit rows, two legends and the two tooltips
# that live directly under <body>. Everything here is the page's own code; the
# number formatters and fetches are stand-ins, and any fetch is recorded.

PROFILE_VIEW_FUNCTIONS = [
    *PROFILE_FUNCTIONS,
    "function formatProfileMetricNumber(value) {",
    "function formatProfileAggregateTooltip(aggregate, cumulative = false, milestonesEnabled = false) {",
    "function formatProfileDayTooltip(day, milestonesEnabled = false) {",
    "function getProfileActivityColor(level) {",
    "function syncProfileMilestonePalette() {",
    "function syncProfileMilestoneControls() {",
    "function renderProfileActivityLegend(targetId, mode) {",
    "function renderProfileActivityTooltipContent(tooltip, model) {",
    "function resolveProfileActivityTooltip(target) {",
    "function showProfileActivityTooltip(target, model) {",
    "function hideProfileActivityTooltip(target) {",
    "function bindProfileActivityTooltip(target, model) {",
    "function renderProfileSummary(summary) {",
    "function renderProfileMonths(today = new Date()) {",
    "function setProfileColumnHover(weekIndex, active) {",
    "function applyProfileMilestone(target, milestone) {",
    "function renderProfileView() {",
    "function setOverviewProfileState(state) {",
    "function setOverviewProfileColumnHover(weekIndex, active) {",
    "function syncProfileActivityModeControls() {",
    "function renderOverviewProfilePreview(contributions = statsCache.default) {",
    "function setProfileActivityMode(mode) {",
    "function setProfileMilestonesEnabled(enabled) {",
    "function isOverviewActive() {",
    # Queued on the next frame by both renderers; the harness never runs the frame.
    "function ensureProfileActivityInitialScroll() {",
    "function ensureOverviewProfileInitialScroll() {",
]


def _profile_view_snippets(source: str) -> list[str]:
    """The profile's module state, read as the page declares it (the milestone
    switch starts from localStorage), and the six mode buttons' click wiring."""
    return [
        slice_statement(
            source,
            "const PROFILE_MILESTONES_STORAGE_KEY",
            "let profileActivityDidInitialScroll = false;",
        ),
        slice_statement(
            source,
            "let activeProfileActivityTooltipTarget = null;",
            "let profileActivityTooltipPositionFrame = 0;",
        ),
        "let overviewProfileDidInitialScroll = false;",
        slice_statement(
            source,
            "document.getElementById('profileModeDaily').addEventListener(",
            "document.getElementById('overviewProfileModeCumulative')"
            ".addEventListener('click', () => setProfileActivityMode('cumulative'));",
        ),
    ]


PROFILE_VIEW_PRELUDE = r"""
let HEAT_COLORS = ['#h0', '#h1', '#h2', '#h3', '#h4', '#h5', '#h6', '#h7'];
const statsCache = { default: null };
const calendarState = { view: 'profile', contributions: [] };
let statsLoaded = true;
function formatTokenCount(value, includeUnit = false) { return `${Number(value) || 0}${includeUnit ? ' tokens' : ''}`; }
function formatNumber(value) { return String(Number(value) || 0); }
function formatCurrency(value) { return `$${(Number(value) || 0).toFixed(2)}`; }
function formatShortDate(value) { return String(value || '—'); }
function pluralDays(count) { return `${count} d`; }
const fetched = [];
function fetch(url) { fetched.push(String(url)); return new Promise(() => {}); }
function fetchSelectedServers(url) { fetched.push(String(url)); return new Promise(() => {}); }
function fetchJsonWithRetry(url) { fetched.push(String(url)); return new Promise(() => {}); }

const PROFILE_TEST_MODES = ['Daily', 'Weekly', 'Cumulative'];
document.body.append(
  el('section', { id: 'overview-content', class: 'tab-content active' },
    el('section', { id: 'overviewProfileSummary', class: 'surface overview-profile-band', 'data-state': 'loading' },
      el('dd', { id: 'overviewProfileActiveDays' }),
      el('dd', { id: 'overviewProfileCurrentStreak' }),
      el('dd', { id: 'overviewProfileLongestStreak' }),
      ...PROFILE_TEST_MODES.map((mode) => el('button', { id: `overviewProfileMode${mode}`, class: 'profile-activity-mode overview-profile-mode', 'aria-pressed': 'false' })),
      el('div', { id: 'overviewProfileScroller' },
        el('div', { id: 'overviewProfileGridWrap' },
          el('div', { id: 'overviewProfileGrid' }),
          el('div', { id: 'overviewProfileWeekHits' }))),
      el('div', { id: 'overviewProfileLegend', class: 'profile-activity-legend overview-profile-legend' }))),
  el('section', { id: 'stats-content', class: 'tab-content' },
    el('div', { id: 'calendarProfileView' },
      el('div', { class: 'profile-activity-shell' },
        ...['RecordedTokens', 'PeakDay', 'ActiveDays', 'CurrentStreak', 'LongestStreak'].map((name) => el('dd', { id: `profileStat${name}` })),
        ...PROFILE_TEST_MODES.map((mode) => el('button', { id: `profileMode${mode}`, class: 'profile-activity-mode', 'aria-pressed': 'false' })),
        el('div', { id: 'profileActivityMonths' }),
        el('div', { id: 'profileActivityScroller' },
          el('div', { id: 'profileActivityGrid' }),
          el('div', { id: 'profileActivityWeekHits' })),
        el('div', { id: 'profileActivityLegend', class: 'profile-activity-legend' })))),
  el('div', { id: 'overviewProfileTooltip', class: 'profile-activity-tooltip overview-profile-tooltip', role: 'tooltip' }),
  el('div', { id: 'profileActivityTooltip', class: 'profile-activity-tooltip', role: 'tooltip' }),
);
document.getElementById('overviewProfileTooltip').hidden = true;
document.getElementById('profileActivityTooltip').hidden = true;

// A contribution `offset` days before today, all of it input tokens.
function contribution(offset, tokens) {
  const now = new Date();
  return {
    date: formatDateKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - offset)),
    totals: { tokens, cost: 1, messages: 1 },
    tokenBreakdown: { input: tokens, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0 },
    sources: [],
  };
}
function marked(id) {
  return document.getElementById(id).children.filter((node) => node.hasAttribute('data-milestone')).length;
}
function milestoneState() {
  return {
    enabled: profileMilestonesEnabled,
    stored: localStorage.getItem('tokdash-profile-milestones'),
    toggles: document.querySelectorAll('.profile-milestone-toggle').map((toggle) => ({
      legend: toggle.closest('.profile-activity-legend').id,
      pressed: toggle.getAttribute('aria-pressed'),
      label: toggle.getAttribute('aria-label'),
      title: toggle.title,
      legendFlag: toggle.closest('.profile-activity-legend').getAttribute('data-milestones-enabled'),
    })),
    marked: { profile: marked('profileActivityGrid'), overview: marked('overviewProfileGrid') },
  };
}
function describeLegend(id) {
  const legend = document.getElementById(id);
  const [scale, divider, milestones, toggle] = legend.children;
  const glyph = scale.children[1];
  return {
    mode: legend.dataset.mode,
    label: legend.getAttribute('aria-label'),
    parts: legend.children.map((child) => child.className),
    less: scale.children[0].textContent,
    more: scale.children[2].textContent,
    glyph: glyph.className,
    glyphHidden: glyph.getAttribute('aria-hidden'),
    dividerHidden: divider.getAttribute('aria-hidden'),
    items: glyph.children.map((item) => [item.className, item.style.background || null]),
    badges: milestones.children.map((badge) => [badge.dataset.milestone, badge.textContent]),
    toggle: toggle.children[0].textContent,
    toggleLabel: toggle.getAttribute('aria-label'),
  };
}
"""


def _profile_view_script(body: str, *, prelude: str = "", functions: tuple[str, ...] = ()) -> str:
    source = index_source()
    return page_script(
        [*PROFILE_VIEW_FUNCTIONS, *functions],
        snippets=_profile_view_snippets(source),
        prelude=prelude + PROFILE_VIEW_PRELUDE,
        body=body,
        i18n=True,
        source=source,
    )


@needs_node
def test_one_mode_switch_redraws_both_heatmaps_and_presses_six_buttons(tmp_path: Path):
    """The Stats profile and the Overview band share one mode.

    A click on any of the six buttons redraws whichever views are on screen
    from the stats already in hand (no fetch), presses the matching button in
    both groups, and switches both legends; an unknown mode changes nothing.
    """
    body = """
statsCache.default = [contribution(0, 1200000000), contribution(3, 5000)];
const BUTTONS = ['profileModeDaily', 'profileModeWeekly', 'profileModeCumulative',
  'overviewProfileModeDaily', 'overviewProfileModeWeekly', 'overviewProfileModeCumulative'];
function view(prefix) {
  const grid = document.getElementById(`${prefix}Grid`);
  const hits = document.getElementById(`${prefix}WeekHits`);
  return {
    cells: grid.children.length,
    cellTags: [...new Set(grid.children.map((cell) => cell.tagName))],
    hits: hits.children.length,
    hitsHidden: hits.hidden,
    hitTooltips: [...new Set(hits.children.map((hit) => hit.dataset.profileTooltipId || null))],
    legend: document.getElementById(`${prefix}Legend`).dataset.mode,
  };
}
function snapshot() {
  return {
    mode: profileActivityMode,
    pressed: BUTTONS.filter((id) => document.getElementById(id).getAttribute('aria-pressed') === 'true'),
    profile: view('profileActivity'),
    overview: view('overviewProfile'),
  };
}
document.getElementById('overviewProfileModeWeekly').click();
const weekly = snapshot();
setProfileActivityMode('sideways');
const ignored = snapshot();
document.getElementById('profileModeDaily').click();
const daily = snapshot();
// Neither view on screen: the buttons follow, the grids wait for the next draw.
document.getElementById('overview-content').classList.remove('active');
calendarState.view = 'month';
document.getElementById('profileModeCumulative').click();
const offscreen = snapshot();
// With no stats in hand the band says it is loading, unless its last load failed.
const band = document.getElementById('overviewProfileSummary');
band.dataset.state = 'error';
renderOverviewProfilePreview(null);
const afterError = band.dataset.state;
band.dataset.state = 'ready';
renderOverviewProfilePreview(null);
const pending = { state: band.dataset.state, busy: band.getAttribute('aria-busy') };
print({ weekly, ignored, daily, offscreen, afterError, pending, fetched });
"""
    report = run_page(tmp_path, "profile-modes", _profile_view_script(body))

    aggregate = {"cells": 364, "cellTags": ["SPAN"], "hits": 52, "hitsHidden": False}
    assert report["weekly"]["mode"] == "weekly"
    assert report["weekly"]["pressed"] == ["profileModeWeekly", "overviewProfileModeWeekly"]
    assert report["weekly"]["profile"] == {**aggregate, "hitTooltips": [None], "legend": "weekly"}
    assert report["weekly"]["overview"] == {
        **aggregate,
        "hitTooltips": ["overviewProfileTooltip"],
        "legend": "weekly",
    }
    assert report["ignored"] == report["weekly"]

    daily = {"cells": 364, "cellTags": ["BUTTON"], "hits": 0, "hitsHidden": True, "hitTooltips": [], "legend": "daily"}
    assert report["daily"]["pressed"] == ["profileModeDaily", "overviewProfileModeDaily"]
    assert report["daily"]["profile"] == daily
    assert report["daily"]["overview"] == daily

    assert report["offscreen"]["mode"] == "cumulative"
    assert report["offscreen"]["pressed"] == ["profileModeCumulative", "overviewProfileModeCumulative"]
    assert report["offscreen"]["profile"] == daily
    assert report["offscreen"]["overview"] == daily

    assert report["afterError"] == "error"
    assert report["pending"] == {"state": "loading", "busy": "true"}
    assert report["fetched"] == []


@needs_node
def test_the_milestone_switch_starts_off_persists_and_redraws_both_views(tmp_path: Path):
    """Milestones are opt-in: off until the reader turns them on, remembered in
    localStorage, and either legend's switch redraws both heatmaps, so a 1B-token
    day gains its tier in the Stats profile and the Overview band together."""
    body = """
currentLang = 'ja';
statsCache.default = [contribution(0, 1200000000)];
setProfileActivityMode('daily');
const before = milestoneState();
document.querySelector('#profileActivityLegend .profile-milestone-toggle').click();
const on = milestoneState();
document.querySelector('#overviewProfileLegend .profile-milestone-toggle').click();
const off = milestoneState();
print({ before, on, off, show: I18N.ja.showMilestones, hide: I18N.ja.hideMilestones });
"""
    report = run_page(tmp_path, "profile-milestones", _profile_view_script(body))
    show, hide = report["show"], report["hide"]
    assert show != "showMilestones" and hide != "hideMilestones"

    def toggles(pressed: str, label: str) -> list[dict]:
        return [
            {"legend": legend, "pressed": pressed, "label": label, "title": label, "legendFlag": pressed}
            for legend in ("overviewProfileLegend", "profileActivityLegend")
        ]

    assert report["before"] == {
        "enabled": False,
        "stored": None,
        "toggles": toggles("false", show),
        "marked": {"profile": 0, "overview": 0},
    }
    assert report["on"] == {
        "enabled": True,
        "stored": "on",
        "toggles": toggles("true", hide),
        "marked": {"profile": 1, "overview": 1},
    }
    assert report["off"] == {
        "enabled": False,
        "stored": "off",
        "toggles": toggles("false", show),
        "marked": {"profile": 0, "overview": 0},
    }

    remembered = run_page(
        tmp_path,
        "profile-milestones-remembered",
        _profile_view_script(
            """
statsCache.default = [contribution(0, 1200000000)];
setProfileActivityMode('daily');
print(milestoneState());
""",
            prelude="localStorage.setItem('tokdash-profile-milestones', 'on');\n",
        ),
    )
    assert remembered["enabled"] is True
    assert {toggle["pressed"] for toggle in remembered["toggles"]} == {"true"}
    assert remembered["marked"] == {"profile": 1, "overview": 1}


@needs_node
def test_the_legend_draws_swatches_or_bars_for_its_mode(tmp_path: Path):
    """Daily reads as five heat swatches, the aggregate modes as five bars; the
    milestone badges and the switch follow; every label is the current
    language's and is set as text."""
    body = """
currentLang = 'ja';
renderProfileActivityLegend('profileActivityLegend', 'daily');
const daily = describeLegend('profileActivityLegend');
renderProfileActivityLegend('profileActivityLegend', 'weekly');
const weekly = describeLegend('profileActivityLegend');
renderProfileActivityLegend('profileActivityLegend', 'sideways');
const fallback = describeLegend('profileActivityLegend');
renderProfileActivityLegend('noSuchLegend', 'daily');
print({
  daily, weekly, fallback,
  ja: { less: I18N.ja.less, more: I18N.ja.more, legend: I18N.ja.activityLegend,
        milestones: I18N.ja.milestones, show: I18N.ja.showMilestones },
  innerHTML: __innerHTMLWrites.length,
});
"""
    report = run_page(tmp_path, "profile-legend", _profile_view_script(body))
    ja = report["ja"]
    shared = {
        "label": ja["legend"],
        "parts": [
            "profile-activity-legend-scale",
            "profile-activity-legend-divider",
            "profile-activity-legend-milestones",
            "profile-milestone-toggle",
        ],
        "less": ja["less"],
        "more": ja["more"],
        "glyphHidden": "true",
        "dividerHidden": "true",
        "badges": [["1", "100M"], ["2", "300M"], ["3", "500M"], ["4", "1B"]],
        "toggle": ja["milestones"],
        "toggleLabel": ja["show"],
    }
    # Levels 0-4 spread over the eight-step heat ramp.
    swatches = [["profile-activity-legend-swatch", color] for color in ("#h0", "#h2", "#h4", "#h5", "#h7")]
    assert report["daily"] == {
        **shared,
        "mode": "daily",
        "glyph": "profile-activity-legend-glyph profile-activity-legend-swatches",
        "items": swatches,
    }
    assert report["weekly"] == {
        **shared,
        "mode": "weekly",
        "glyph": "profile-activity-legend-glyph profile-activity-legend-bars",
        "items": [["profile-activity-legend-bar", None]] * 5,
    }
    assert report["fallback"] == report["daily"]
    assert report["innerHTML"] == 0


@needs_node
def test_a_language_switch_redraws_both_legends_in_the_current_mode(tmp_path: Path):
    """applyI18n redraws the two legends itself: their accessible name and the
    switch label are written by the renderer, not by a data-i18n hook, and the
    mode is whatever the reader last picked."""
    body = """
renderProfileActivityLegend('profileActivityLegend', 'daily');
renderProfileActivityLegend('overviewProfileLegend', 'daily');
profileActivityMode = 'cumulative';
currentLang = 'ko';
applyI18n();
print({
  legends: ['profileActivityLegend', 'overviewProfileLegend'].map(describeLegend),
  ko: { legend: I18N.ko.activityLegend, milestones: I18N.ko.milestones, show: I18N.ko.showMilestones },
});
"""
    script = _profile_view_script(body, prelude=APPLY_I18N_STATE, functions=APPLY_I18N_FUNCTIONS)
    report = run_page(tmp_path, "profile-legend-i18n", script)
    ko = report["ko"]
    for legend in report["legends"]:
        assert legend["mode"] == "cumulative"
        assert legend["glyph"].endswith("profile-activity-legend-bars")
        assert legend["label"] == ko["legend"]
        assert legend["toggle"] == ko["milestones"]
        assert legend["toggleLabel"] == ko["show"]


@needs_node
def test_a_tooltip_shows_its_tier_badge_and_keeps_every_label_as_text(tmp_path: Path):
    """The tooltip is built from elements: the tier badge and data-milestone come
    and go with the model, a label that looks like markup stays text, and a cell
    shows the tooltip it names (the Overview's) or the Stats one by default."""
    body = """
const hostile = '<img src=x onerror=alert(1)>';
I18N.en.total = hostile;
const tooltip = document.getElementById('overviewProfileTooltip');
const breakdown = { input: 1, output: 1, cacheRead: 1, cacheWrite: 1, reasoning: 1 };
const high = formatProfileAggregateTooltip(
  { startDate: '2026-07-20', endDate: '2026-07-26', totals: { tokens: 1000000000, cost: 1 }, tokenBreakdown: breakdown }, false, true);
const plain = formatProfileAggregateTooltip(
  { startDate: '2026-07-27', endDate: '2026-07-27', totals: { tokens: 5, cost: 0 }, tokenBreakdown: breakdown }, false, true);
function describe(node) {
  const [header, body] = node.children;
  return {
    milestone: node.dataset.milestone ?? null,
    header: header.children.map((part) => [part.className, part.dataset.milestone ?? null, part.textContent]),
    rows: body.children.map((row) => ({
      key: row.dataset.key, tone: row.dataset.tone, prominent: row.dataset.prominent ?? null,
      label: row.children[1].textContent, labelNodes: row.children[1].children.length,
      value: row.children[2].textContent,
    })),
  };
}
renderProfileActivityTooltipContent(tooltip, high);
const withBadge = describe(tooltip);
renderProfileActivityTooltipContent(tooltip, plain);
const withoutBadge = describe(tooltip);

const overviewCell = el('button', { 'data-profile-tooltip-id': 'overviewProfileTooltip' });
const statsCell = el('button');
document.body.append(overviewCell, statsCell);
bindProfileActivityTooltip(overviewCell, high);
bindProfileActivityTooltip(statsCell, plain);
const visible = () => ({
  overview: !document.getElementById('overviewProfileTooltip').hidden,
  stats: !document.getElementById('profileActivityTooltip').hidden,
});
fire(overviewCell, 'mouseenter');
const hovered = { ...visible(), describedBy: overviewCell.getAttribute('aria-describedby'),
  label: overviewCell.getAttribute('aria-label') };
fire(overviewCell, 'mouseleave');
const left = { ...visible(), describedBy: overviewCell.getAttribute('aria-describedby') };
fire(statsCell, 'focus');
const focused = { ...visible(), describedBy: statsCell.getAttribute('aria-describedby'),
  title: document.getElementById('profileActivityTooltip').children[0].textContent };
print({ hostile, withBadge, withoutBadge, hovered, left, focused,
  accessible: high.accessibleText, innerHTML: __innerHTMLWrites.length });
"""
    report = run_page(tmp_path, "profile-tooltip-content", _profile_view_script(body))

    badge = report["withBadge"]
    assert badge["milestone"] == "4"
    assert badge["header"] == [
        ["profile-activity-tooltip-title", None, "2026-07-20 → 2026-07-26"],
        ["profile-activity-tooltip-badge", "4", "1B+"],
    ]
    assert [row["key"] for row in badge["rows"]] == [
        "total", "input", "output", "cacheRead", "cacheWrite", "reasoning", "estimatedCost",
    ]
    total = badge["rows"][0]
    assert total["tone"] == "milestone-4" and total["prominent"] == "true"
    assert total["label"] == report["hostile"] and total["labelNodes"] == 0

    plain = report["withoutBadge"]
    assert plain["milestone"] is None
    assert [part[0] for part in plain["header"]] == ["profile-activity-tooltip-title"]
    assert plain["rows"][0]["tone"] == "primary"

    assert report["hovered"] == {
        "overview": True,
        "stats": False,
        "describedBy": "overviewProfileTooltip",
        "label": report["accessible"],
    }
    assert report["left"] == {"overview": False, "stats": False, "describedBy": None}
    assert report["focused"] == {
        "overview": False,
        "stats": True,
        "describedBy": "profileActivityTooltip",
        "title": "2026-07-27",
    }
    assert report["innerHTML"] == 0


def test_every_milestone_tier_has_its_styles():
    """Tiers 1-4 each have a palette slot, a cell aura and a tooltip tone.

    The tier a cell carries is computed, so a missing rule only shows as one tier
    quietly looking like no tier at all.
    """
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)
    for level in range(1, 5):
        assert f"--profile-milestone-{level}:" in compact
        assert f'[data-milestone="{level}"]' in source
        assert f'[data-tone="milestone-{level}"]' in source


@needs_node
def test_milestone_auras_follow_the_active_heat_palette(tmp_path: Path):
    """A style or light/dark switch repaints the four tier colours on every shell
    from the new heat ramp, picked the way the heatmap picks its levels."""
    source = index_source()
    script = page_script(
        [
            "function isDark() {",
            "function getHeatColors(uiTheme, dark) {",
            "function getProfileActivityColor(level) {",
            "function syncProfileMilestonePalette() {",
            "function applyStyleTheme(theme) {",
            "function applyTheme(theme) {",
        ],
        snippets=[
            THEME_CONFIG_JS.read_text(encoding="utf-8"),
            slice_statement(
                source,
                "const THEME_CONFIG = window.TOKDASH_THEME_CONFIG || {};",
                "let HEAT_COLORS = [...HEAT_COLORS_LIGHT];",
            ),
            slice_line(source, "const VALID_STYLE_THEMES = ["),
        ],
        prelude="""
document.body.append(
  el('div', { class: 'profile-activity-shell' }),
  el('section', { class: 'overview-profile-band' }),
  el('div', { class: 'profile-activity-tooltip' }),
);
""",
        body="""
const shells = document.querySelectorAll('.profile-activity-shell, .overview-profile-band, .profile-activity-tooltip');
const read = () => shells.map((shell) => [1, 2, 3, 4].map((level) => shell.style.getPropertyValue(`--profile-milestone-${level}`)));
applyStyleTheme('paper');
const paperLight = read();
applyTheme('dark');
const paperDark = read();
applyStyleTheme('classic');
const classicDark = read();
print({ paperLight, paperDark, classicDark });
""",
        source=source,
    )
    report = run_page(tmp_path, "profile-milestone-palette", script)
    paper_light, paper_dark = _heat_palette_for("paper")
    _, classic_dark = _heat_palette_for("classic")
    tiers = (2, 4, 5, 7)  # levels 1-4 of 0-4 over an eight-step ramp
    for name, palette in (
        ("paperLight", paper_light),
        ("paperDark", paper_dark),
        ("classicDark", classic_dark),
    ):
        assert report[name] == [[palette[index] for index in tiers]] * 3, name


@needs_node
def test_the_overview_profile_link_opens_the_profile_without_resetting_it(tmp_path: Path):
    """"View full profile" lands on the Stats profile with its navigation and
    mode kept; any other way into Stats starts from a fresh navigation."""
    source = index_source()
    script = page_script(
        ["function openOverviewProfile() {", "function activateDashboardTab(tab) {"],
        snippets=[slice_line(source, "getElementById('overviewProfileOpen')")],
        prelude="""
document.body.append(
  el('button', { class: 'tab-btn', 'data-tab': 'overview', 'aria-selected': 'true' }),
  el('button', { class: 'tab-btn', 'data-tab': 'stats', 'aria-selected': 'false' }),
  el('a', { class: 'sidebar-nav-item active', 'data-tab-target': 'overview' }),
  el('a', { class: 'sidebar-nav-item', 'data-tab-target': 'stats' }),
  el('section', { id: 'overview-content', class: 'tab-content active' }, el('button', { id: 'overviewProfileOpen' })),
  el('section', { id: 'stats-content', class: 'tab-content' }),
  el('div', { id: 'overviewProfileTooltip' }),
);
let preserveStatsNavigationOnNextActivation = false;
let currentDashboardTab = 'overview';
const usageReportState = { active: false, loaded: true, staleReread: false };
const usageReportSpeedState = { active: false };
let quotaLoaded = true;
let pricingDbLoaded = true;
let profileActivityMode = 'weekly';
let monthCursor = 'month-cursor';
let yearCursor = 'year-cursor';
""",
        body="""
document.getElementById('overviewProfileOpen').click();
const opened = {
  order: callNames().filter((name) => name === 'loadStats' || name === 'setView'),
  loadStats: calls('loadStats'),
  setView: calls('setView'),
  active: document.querySelectorAll('.tab-content.active').map((node) => node.id),
  selected: document.querySelector('.tab-btn[data-tab="stats"]').getAttribute('aria-selected'),
  tooltipHidden: document.getElementById('overviewProfileTooltip').hidden,
  kept: [profileActivityMode, monthCursor, yearCursor],
  armed: preserveStatsNavigationOnNextActivation,
};
activateDashboardTab('overview');
activateDashboardTab('stats');
print({ opened, later: calls('loadStats') });
""",
        source=source,
    )
    report = run_page(tmp_path, "overview-profile-open", script)
    assert report["opened"] == {
        "order": ["loadStats", "setView"],
        "loadStats": [[{"preserveNavigation": True}]],
        "setView": [["profile"]],
        "active": ["stats-content"],
        "selected": "true",
        "tooltipHidden": True,
        "kept": ["weekly", "month-cursor", "year-cursor"],
        "armed": False,
    }
    assert report["later"] == [[{"preserveNavigation": True}], [{"preserveNavigation": False}]]


@needs_node
def test_the_overview_band_scrolls_to_today_once_per_overflow(tmp_path: Path):
    """The band opens scrolled to today, then leaves the reader's scroll alone
    on every resize, until a window wide enough to fit it re-arms the jump."""
    source = index_source()
    script = page_script(
        ["function ensureOverviewProfileInitialScroll() {"],
        snippets=[
            "let overviewProfileDidInitialScroll = false;",
            slice_line(source, "window.addEventListener('resize', ensureOverviewProfileInitialScroll"),
        ],
        prelude="""
const scroller = el('div', { id: 'overviewProfileScroller' });
document.body.append(scroller);
""",
        body="""
const steps = [];
const step = () => steps.push([scroller.scrollLeft, overviewProfileDidInitialScroll]);
scroller.scrollWidth = 1200;
scroller.clientWidth = 600;
ensureOverviewProfileInitialScroll();
step();
scroller.scrollLeft = 100;
window.dispatchWindowEvent('resize');
step();
scroller.clientWidth = 1400;
window.dispatchWindowEvent('resize');
step();
scroller.clientWidth = 600;
window.dispatchWindowEvent('resize');
step();
print(steps);
""",
        source=source,
    )
    assert run_page(tmp_path, "overview-profile-scroll", script) == [
        [1200, True],   # first draw: scrolled to today
        [100, True],    # a resize keeps where the reader scrolled
        [100, False],   # wide enough to fit: re-armed, nothing moved
        [1200, True],   # narrow again: back to today
    ]


class _ParentScanner(HTMLParser):
    """Record the parent tag of every element that carries an id."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.parents: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):  # type: ignore[no-untyped-def]
        element_id = dict(attrs).get("id")
        if element_id:
            self.parents[element_id] = self.stack[-1] if self.stack else ""
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):  # type: ignore[no-untyped-def]
        if tag in self.stack:
            while self.stack.pop() != tag:
                pass


def test_profile_and_overview_markup_order_and_portals():
    """Where the profile pieces sit in the page, which no render can move.

    The band sits between the model chart and the tools table, each legend under
    its grid with the activity insights after it, the overview mode buttons are
    named by their visible label, and both tooltips are children of <body>: a
    tooltip inside a filtered or transformed card is clipped by it and positioned
    against it instead of the viewport.
    """
    source = INDEX_HTML.read_text(encoding="utf-8")
    order = [
        ('id="modelChart"', 'id="overviewProfileSummary"'),
        ('id="overviewProfileSummary"', 'id="toolsTable"'),
        ('id="overviewProfileGridWrap"', 'id="overviewProfileLegend"'),
        ('id="profileActivityLegend"', 'id="profileActivityInsights"'),
        ('id="overviewProfileLegend"', 'id="overviewActivityInsights"'),
    ]
    for before, after in order:
        assert source.index(before) < source.index(after), f"{before} must come before {after}"
    assert 'aria-labelledby="overviewProfileActivityLabel"' in source

    # The markup ends where the main <script> (the one holding applyI18n) begins.
    scanner = _ParentScanner()
    scanner.feed(source[: source.rfind("<script>", 0, source.index("function applyI18n()"))])
    for tooltip in ("overviewProfileTooltip", "profileActivityTooltip"):
        assert scanner.parents.get(tooltip) == "body", (
            f"#{tooltip} sits in <{scanner.parents.get(tooltip)}>, not directly in <body>"
        )


@needs_node
def test_activity_insights_share_one_request_and_label_themselves(tmp_path: Path):
    """Both insight panels render from one /api/activity-insights request.

    A second load while one is in flight reuses it, a loaded answer is reused
    until a forced refresh, and the profile ranks at most five tools, each name
    set as text. The ranking's and the Overview panel's accessible names come
    from this render pass in the current language (the markup carries no
    data-i18n hook for them), and a failed load says so in both panels.
    """
    source = index_source()
    script = page_script(
        [
            "function safeActivityNumber(value) {",
            "function formatActivityShare(value) {",
            "function formatActivityEffort(effort) {",
            "function buildActivityInsightValues(data) {",
            "function createActivityInsightKpi(label, value, tone, compact = false) {",
            "function formatActivityCoverage(data) {",
            "function renderProfileActivityInsights() {",
            "function renderOverviewActivityInsights() {",
            "function renderActivityInsights() {",
            "async function loadActivityInsights(options = {}) {",
            "function renderLoadingPlaceholder(element, label = t('loading')) {",
        ],
        prelude="""
function formatNumber(value) { return String(Number(value) || 0); }
const activityInsightsState = { status: 'idle', data: null, promise: null };
const fetched = [];
let pendingFetch = null;
function fetchSelectedServers(url) {
  fetched.push(url);
  return new Promise((resolve, reject) => { pendingFetch = { resolve, reject }; });
}
function fetch(url) { fetched.push(`fetch:${url}`); return new Promise(() => {}); }
function combineInsightsPayloads(payloads) { return payloads[0]; }
document.body.append(
  el('section', { id: 'profileActivityInsights' },
    el('dl', { id: 'profileActivityInsightsKpis' }),
    el('p', { id: 'profileActivityCoverage' }),
    el('ol', { id: 'profileActivityToolRanking' })),
  el('section', { id: 'overviewActivityInsights' },
    el('dl', { id: 'overviewActivityInsightsValues' }),
    el('p', { id: 'overviewActivityInsightsState' })),
);
function kpis(id) {
  return document.getElementById(id).children.map((kpi) => [text(kpi.querySelector('dt')), text(kpi.querySelector('dd'))]);
}
function snapshot() {
  const ranking = document.getElementById('profileActivityToolRanking');
  const overview = document.getElementById('overviewActivityInsights');
  return {
    states: [document.getElementById('profileActivityInsights').dataset.state, overview.dataset.state],
    busy: overview.getAttribute('aria-busy'),
    labels: [ranking.getAttribute('aria-label'), overview.getAttribute('aria-label')],
    profileText: text(document.getElementById('profileActivityInsightsKpis')),
    note: text(document.getElementById('overviewActivityInsightsState')),
    tools: ranking.children.map((row) => [row.children[0].textContent, row.children[0].children.length]),
  };
}
""",
        i18n=True,
        body="""
currentLang = 'ja';
const hostile = '<img src=x onerror=alert(1)>';
const payload = {
  recorded_chats: { value: 12 },
  reasoning: { most_used: { effort: 'xhigh', share: 0.5 } },
  tools: {
    total_calls: 70,
    most_used: { name: 'exec', share: 0.3 },
    distribution: [hostile, 'exec', 'read', 'write', 'grep', 'glob', 'edit']
      .map((name) => ({ name, count: 10, share: 0.1 })),
  },
};
const first = loadActivityInsights();
const second = loadActivityInsights();
const loading = { ...snapshot(), fetched: [...fetched] };
pendingFetch.resolve([{ payload }]);
await Promise.all([first, second]);
const ready = { ...snapshot(), profile: kpis('profileActivityInsightsKpis'), overview: kpis('overviewActivityInsightsValues') };
await loadActivityInsights();
const reused = [...fetched];
const forced = loadActivityInsights({ force: true });
pendingFetch.resolve([{ payload }]);
await forced;
activityInsightsState.data = null;
const failing = loadActivityInsights({ force: true });
pendingFetch.reject(new Error('insights down'));
await failing.catch(() => {});
const failed = snapshot();
currentLang = 'ko';
renderActivityInsights();
const korean = snapshot().labels;
const efforts = {};
for (const lang of Object.keys(I18N)) {
  currentLang = lang;
  efforts[lang] = {
    got: ['xhigh', 'high', 'medium', 'low', 'turbo'].map(formatActivityEffort),
    want: [I18N[lang].effortXhigh, I18N[lang].effortHigh, I18N[lang].effortMedium, I18N[lang].effortLow, 'turbo'],
  };
}
const words = (lang) => Object.fromEntries(['activityTopTools', 'activityInsightsTitle', 'activityRecordedChats',
  'activityReasoning', 'activityToolCalls', 'activityTopTool', 'activityUnavailable', 'activityLocalScope',
  'effortXhigh', 'loading'].map((key) => [key, I18N[lang][key]]));
print({ hostile, loading, ready, reused, fetched, failed, korean, efforts, ja: words('ja'), ko: words('ko'),
  innerHTML: __innerHTMLWrites.length });
""",
        source=source,
    )
    report = run_page(tmp_path, "activity-insights-panels", script)
    ja, ko = report["ja"], report["ko"]

    loading = report["loading"]
    assert loading["states"] == ["loading", "loading"] and loading["busy"] == "true"
    assert loading["note"] == ja["loading"]
    assert loading["fetched"] == ["/api/activity-insights"]

    ready = report["ready"]
    assert ready["states"] == ["ready", "ready"] and ready["busy"] == "false"
    assert ready["labels"] == [ja["activityTopTools"], ja["activityInsightsTitle"]]
    figures = [
        [ja["activityRecordedChats"], "12"],
        [ja["activityReasoning"], f"{ja['effortXhigh']} · 50%"],
        [ja["activityToolCalls"], "70"],
        [ja["activityTopTool"], "exec · 30%"],
    ]
    assert ready["profile"] == figures
    assert ready["overview"] == figures
    assert ready["note"] == ja["activityLocalScope"]
    assert ready["tools"] == [
        [report["hostile"], 0], ["exec", 0], ["read", 0], ["write", 0], ["grep", 0],
    ]

    assert report["reused"] == ["/api/activity-insights"]
    assert report["fetched"] == [
        "/api/activity-insights",
        "/api/activity-insights?refresh=1",
        "/api/activity-insights?refresh=1",
    ]
    failed = report["failed"]
    assert failed["states"] == ["error", "error"]
    assert failed["profileText"] == ja["activityUnavailable"]
    assert failed["note"] == ja["activityUnavailable"]
    assert failed["tools"] == []
    assert report["korean"] == [ko["activityTopTools"], ko["activityInsightsTitle"]]

    # The effort labels are looked up through a variable, which the parity test
    # cannot see: each language must name all four.
    for lang, row in report["efforts"].items():
        assert row["got"] == row["want"], lang
        assert not any(label.startswith("effort") for label in row["got"]), lang
    assert report["innerHTML"] == 0


def _run_activity_insights_js(
    tmp_path: Path, expression: str, payload: dict, labels: dict | None = None
) -> object:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        extract_js_function(source, signature)
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


@needs_node
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
