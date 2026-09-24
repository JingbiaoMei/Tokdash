"""Textual pilot tests for ``tokdash tui`` (spec §10).

The whole module rides on importorskip("textual") so a stripped install (where
``report`` must still work) stays green. Every fetch is a canned FetchOutcome
monkeypatched at the tokdash.tui.app names — the tests never touch real disk
data. Async runs go through plain asyncio.run(app.run_test(...)) so no
pytest-asyncio plugin is required.

Fixtures mirror the REAL payload shapes (same field names as the report-test
fixtures, plus compute_stats' {summary, contributions, stats} shape) — the
panes are pure views over those dicts.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import datetime
import io
import sys
import threading
import time

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from rich.console import Console  # noqa: E402
from rich.markup import escape  # noqa: E402
from rich.text import Text  # noqa: E402  (round-4 cells: right-aligned digits, pills)
from textual import events  # noqa: E402  (wheel events for the scroll test)
from textual.widgets import DataTable, Static  # noqa: E402

import tokdash.tui.app as app_mod  # noqa: E402  (import lives after the skip)
from tokdash.tui.app import (  # noqa: E402
    STALE_REREAD_SECONDS,
    TokdashApp,
    _clamp_report_width,
    run_tui,
)
from tokdash.tui.data import FetchOutcome  # noqa: E402
from tokdash.tui.formatting import EM_DASH  # noqa: E402
from tokdash.usage_store import UsageDatabaseSchemaTooNewError  # noqa: E402

TODAY = datetime.date(2026, 9, 20)
WINDOWS = [
    ("2026-09-14", "2026-09-20"),  # week to date (Monday)
    ("2026-09-01", "2026-09-20"),  # month to date
    ("2026-01-01", "2026-09-20"),  # year to date
]

USAGE_PAYLOAD = {
    "period": "today",
    "range": {"from": "2026-09-14", "to": "2026-09-20", "days": 7, "recognized": True},
    "total_tokens": 12_400_000,
    "total_cost": 142.31,
    "total_messages": 4_820,
    "cache_hit_rate": 0.71,
    "by_tool": {
        "codex": {"tokens": 8_000_000, "cost": 90.0, "tokens_in": 6_000_000,
                  "tokens_cache": 4_000_000, "cache_hit_rate": 0.66},
        "claude": {"tokens": 4_400_000, "cost": 52.31, "tokens_in": 3_000_000,
                   "tokens_cache": 2_000_000, "cache_hit_rate": 0.78},
    },
    # Round 3: apps[] drives the tools table (Input/Output/Msgs) and the
    # Detail sections. Tests that add "openclaw" deliberately give it NO app
    # entry — those dash cells are the contract.
    "apps": {
        "codex": {"tokens_in": 6_000_000, "tokens_out": 2_000_000,
                  "tokens_cache": 4_000_000, "tokens": 8_000_000, "cost": 90.0,
                  "messages": 3_000, "cache_hit_rate": 0.66, "models": [
                      {"name": "gpt-5-codex", "tokens": 8_000_000,
                       "cost": 90.0, "cache_hit_rate": 0.66}]},
        "claude": {"tokens_in": 3_000_000, "tokens_out": 1_400_000,
                   "tokens_cache": 2_000_000, "tokens": 4_400_000,
                   "cost": 52.31, "messages": 1_820, "cache_hit_rate": 0.78,
                   "models": [{"name": "claude-opus", "tokens": 4_400_000,
                               "cost": 52.31, "cache_hit_rate": 0.78}]},
    },
    "top_models": [
        {"name": "gpt-5-codex", "tokens": 8_000_000, "cost": 90.0},
    ],
    "combined_models": [
        {"name": "gpt-5-codex", "tokens": 8_000_000, "tokens_in": 6_000_000,
         "tokens_out": 2_000_000, "tokens_cache": 4_000_000, "cost": 90.0,
         "messages": 3_000, "cache_hit_rate": 0.66, "source": "codex"},
        {"name": "claude-opus", "tokens": 4_400_000, "tokens_in": 3_000_000,
         "tokens_out": 1_400_000, "tokens_cache": 2_000_000, "cost": 52.31,
         "messages": 1_820, "cache_hit_rate": 0.78, "source": "claude"},
    ],
    "comparison": {"tokens_prev": 11_460_000, "cost_prev": 146.86,
                   "messages_prev": 4_820, "tokens_pct": 8.2, "cost_pct": -3.1,
                   "messages_pct": 0.0},
    "source_errors": [],
}

INSIGHTS_PAYLOAD = {
    "timezone": "UTC",
    "range": {"from": "2026-09-14", "to": "2026-09-20", "days": 7, "recognized": True},
    "daily": [
        {"date": f"2026-09-{day:02d}", "tokens": 1_500_000 * i, "cost": 12.0 * i,
         "messages": 600 * i, "entries": 1_200 * i, "intensity": (i % 4) + 1}
        for i, day in enumerate(range(14, 21), start=1)
    ],
    "streaks": {"current_streak": 6, "longest_streak": 12, "active_days": 6,
                "total_days": 7},
    "firsts": {"first_active_day": "2026-09-14", "last_active_day": "2026-09-20",
               "busiest_day": "2026-09-18", "busiest_day_tokens": 7_500_000,
               "peak_hour": 14},
    "hourly": {"buckets": [{"hour": h, "tokens": 900_000 - h * 10_000, "cost": 9.0,
                            "messages": 400, "entries": 800} for h in range(8, 20)],
               "peak_hour": 14, "night_share": 0.12, "night_hours": [0, 1, 2, 3, 4, 5]},
    "weekday": {"buckets": [{"weekday": i, "name": n, "tokens": 1_800_000 * (i + 1),
                             "cost": 20.0, "messages": 700, "entries": 1_400}
                            for i, n in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri",
                                                   "Sat", "Sun"])],
                "peak_weekday": 2},
    "tools": {"ranked": [
        {"tool": "codex", "tokens": 8_000_000, "cost": 90.0, "messages": 3_000,
         "entries": 6_000},
        {"tool": "claude", "tokens": 4_000_000, "cost": 52.0, "messages": 1_800,
         "entries": 3_600},
    ]},
    "models": {"ranked": [{"model": "gpt-5-codex", "tokens": 8_000_000, "cost": 90.0,
                           "messages": 3_000, "entries": 6_000}],
               "most_used": "gpt-5-codex", "highest_cost": "gpt-5-codex"},
    "projects": {"projects": [{"project": "tokdash", "tokens": 9_000_000,
                               "cost": 100.5, "messages": 3_400, "entries": 6_800}],
                 "unattributed": {"tokens": 3_000_000, "cost": 10.0, "messages": 900,
                                  "entries": 1_800},
                 "attributed_project_count": 1, "names_included": True},
}

ACTIVE_TIME_PAYLOAD = {
    "period": "today",
    "range": {"from": "2026-09-14", "to": "2026-09-20", "days": 7, "recognized": True},
    "active_ms": 3_600_000,
    "active_ms_sum": 5_400_000,
    "comparison": {"active_ms_prev": 3_400_000, "active_ms_sum_prev": 5_000_000,
                   "active_ms_pct": 5.9, "active_ms_sum_pct": 8.0},
    "by_tool": {
        "codex": {"tool_label": "Codex", "session_count": 42, "active_ms": 2_400_000,
                  "active_ms_sum": 3_000_000},
        "claude": {"tool_label": "Claude", "session_count": 17, "active_ms": 1_200_000,
                   "active_ms_sum": 2_400_000},
    },
    "unavailable_tools": [],
}

# Shape mirrors compute_stats EXACTLY (compute.py _contributions_from_entries):
# tokens/cost/messages are nested under "totals" — a flat "tokens" key here
# once masked a chart bug that read the flat key. Round 3: the contributions
# feed ONLY the week/month charts (the band + sparkline are gone).
STATS_PAYLOAD = {
    "summary": {"totalTokens": 90_000_000, "totalCost": 900.0, "activeDays": 40,
                "totalDays": 365},
    "contributions": [
        {"date": f"2026-08-{day:02d}",
         "totals": {"tokens": 250_000 * ((day % 7) + 1), "cost": 3.0,
                    "messages": 60},
         "tokenBreakdown": {"input": 100, "output": 50, "cacheRead": 40,
                            "cacheWrite": 10, "reasoning": 5},
         "sources": ["coding"],
         "intensity": (day % 4) + 1}
        for day in range(1, 32)
    ],
    "stats": {"favorite_model": "gpt-5-codex", "current_streak": 6,
              "longest_streak": 12, "active_days": 40, "total_days": 365},
}

DB_LINE = "usage db disabled (TOKDASH_USAGE_DB=0) — live parsing"


def _outcome(payload, status="hit", age=0.0):
    return FetchOutcome(value=copy.deepcopy(payload), status=status, age_seconds=age)


# Quota fixtures mirror tests/test_tui_charts.py's REAL quota_table-shaped
# payloads (normalized bucket keys, *_api sources, codex reset credits, a
# 2-account claude provider that must fold). Built around the live epoch so
# the app's time.time() reset countdown and 24h trend window both land.
def _quota_state_payload(now=None):
    now = int(now if now is not None else time.time())
    return {
        "enabled": True,
        "consent": {"codex_api": True},
        "poll": {"interval_minutes": 30, "last_run": None},
        "providers": {
            "codex": {"status": "ok", "detected": True, "buckets": [
                {"account": "default", "bucket": "5h", "bucket_label": "5-hour window",
                 "used_percent": 42.0, "resets_at": now + 7_200, "source": "codex_api"},
            ], "reset_credits": {"available_count": 2, "credits": [
                    {"title": "Full reset", "expires_at": now + 86_400},
                    {"title": "Full reset 2", "expires_at": now + 172_800},
                ]}},
            "claude": {"status": "ok", "detected": True, "buckets": [
                {"account": "work", "bucket": "weekly_all", "bucket_label": "Weekly All",
                 "used_percent": 30.0, "resets_at": now + 7_200, "source": "claude_api"},
                {"account": "home", "bucket": "session", "bucket_label": "Session",
                 "used_percent": 7.5, "resets_at": now + 7_200, "source": "session"},
            ]},
            "kimi": {"status": "unavailable", "detected": True, "buckets": []},
        },
    }


def _quota_history_payload(now=None):
    now = int(now if now is not None else time.time())
    return {"series": [
        {"provider": "codex", "account": "default", "bucket": "5h",
         "points": [
             {"captured_at": now - 7_200, "used_percent": 10.0},
             {"captured_at": now - 3_600, "used_percent": 20.0},
             {"captured_at": now - 60, "used_percent": 42.0},
         ]},
    ]}


class Calls:
    """Records every fetch call: (first-positional, refresh) per source.

    ``*_full`` lists record the WHOLE positional tuple for the round-2
    normalization checks (period, date_from, date_to[, include_review])."""

    def __init__(self):
        self.usage: list[tuple] = []
        self.insights: list[tuple] = []
        self.active: list[tuple] = []
        self.stats: list[tuple] = []
        self.usage_full: list[tuple] = []
        self.active_full: list[tuple] = []
        self.quota_state: list[tuple] = []
        self.quota_history: list[tuple] = []


def patch_fetchers(monkeypatch, calls=None, *, usage=USAGE_PAYLOAD,
                   insights=INSIGHTS_PAYLOAD, active=ACTIVE_TIME_PAYLOAD,
                   stats=STATS_PAYLOAD, status="hit", age=0.0, usage_gate=None,
                   quota_state=None, quota_history=None):
    """Install canned fetchers at the tokdash.tui.app names. An Exception
    instance as a payload means the source raises (worker-failure tests).
    usage_gate: a threading.Event every fetch blocks on (skeleton test).
    Quota payloads default to the charts-fixture shapes around time.time();
    remote delegation lives BELOW this seam (data.py), so patching here
    covers it: the app only ever calls these names."""
    calls = calls if calls is not None else Calls()
    now = int(time.time())

    def make(recorder, payload, full=None):
        def fetch(*a, **k):
            recorder.append((a[0] if a else k.get("period"), k.get("refresh", False)))
            if full is not None:
                full.append(a)
            if usage_gate is not None:
                assert usage_gate.wait(10.0), "test gate never opened"
            if isinstance(payload, Exception):
                raise payload
            return _outcome(payload, status=status, age=age)
        return fetch

    def make_quota(recorder, payload):
        def fetch(*a, **k):
            recorder.append((k.get("refresh", False),))
            if usage_gate is not None:
                assert usage_gate.wait(10.0), "test gate never opened"
            if isinstance(payload, Exception):
                raise payload
            return _outcome(payload, status=status, age=age)
        return fetch

    monkeypatch.setattr(app_mod, "fetch_usage",
                        make(calls.usage, usage, full=calls.usage_full))
    monkeypatch.setattr(app_mod, "fetch_insights", make(calls.insights, insights))
    monkeypatch.setattr(app_mod, "fetch_active_time",
                        make(calls.active, active, full=calls.active_full))
    monkeypatch.setattr(app_mod, "fetch_stats", make(calls.stats, stats))
    monkeypatch.setattr(app_mod, "fetch_quota_state", make_quota(
        calls.quota_state,
        quota_state if quota_state is not None else _quota_state_payload(now),
    ))
    monkeypatch.setattr(app_mod, "fetch_quota_history", make_quota(
        calls.quota_history,
        quota_history if quota_history is not None else _quota_history_payload(now),
    ))
    monkeypatch.setattr(app_mod, "report_windows", lambda today=None: list(WINDOWS))
    monkeypatch.setattr(app_mod, "db_summary", lambda: DB_LINE)
    monkeypatch.setattr(app_mod, "_local_today", lambda: TODAY)
    return calls


def drive(app, steps):
    """Run the app headless (80x24) and hand control to steps(pilot)."""

    async def main():
        async with app.run_test(headless=True, size=(80, 24)) as pilot:
            await pilot.pause()  # let on_mount / the initial worker start
            await steps(pilot)

    asyncio.run(main())


async def wait_for(app, pilot, predicate, tries=100):
    """Thread jobs + chained awaits need more than one pause to settle."""
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.02)
    return predicate()


def static_text(app, widget_id):
    return str(app.query_one(f"#{widget_id}").content)


def cell(c):
    """Plain text of one DataTable cell. Round 4 made most cells rich Text
    (the right-aligned numerics of app._num, the quota status color, the
    Detail section pills); str cells (escaped names) pass through."""
    return c.plain if isinstance(c, Text) else str(c)


def plain_row(table, i):
    """One DataTable row as plain strings (see cell())."""
    return [cell(c) for c in table.get_row_at(i)]


def render_table_text(table, color=False):
    """Plain (color=False) or ANSI text of the rich Table behind #ov-kpis."""
    buf = io.StringIO()
    console = Console(
        file=buf, width=100,
        color_system="256" if color else None,
        force_terminal=color,
        no_color=not color,
    )
    console.print(table)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Overview pane paint + status
# ---------------------------------------------------------------------------

def test_overview_paints_kpis_tables_status(monkeypatch):
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        # Round 3: the date line IS the section header — the "header" Run
        # (reverse fill) must reach the widget THROUGH emit_markup, wrapping
        # the exact window text. (Raw "[header]" would be a MarkupError;
        # this pins the translation to "reverse bold".)
        assert static_text(app, "ov-range") == \
            "[reverse bold]2026-09-14 → 2026-09-20 · 7 days[/]"
        kpis = app.query_one("#ov-kpis").content
        plain = render_table_text(kpis)
        assert "12.4M" in plain and "$142.31" in plain and "4,820" in plain
        assert "1h 30m" in plain  # agent SUM, not the 1h clock union
        assert "71.0%" in plain and "gpt-5-codex" in plain
        assert "↑ 8.2%" in plain and "↓ 3.1%" in plain
        # Overview delta colors are the OPPOSITE of the report tab: up = RED
        # (web renderDelta index.html:8323), down = GREEN — lock the split.
        ansi = render_table_text(kpis, color=True)
        assert "\x1b[31m" in ansi and "\x1b[32m" in ansi
        tools = app.query_one("#ov-tools")
        assert tools.row_count == len(USAGE_PAYLOAD["by_tool"])
        # Round 3 statistics table: 9 columns, normalized names, per-tool
        # Time joined from the active payload (codex 3_000_000 ms = "50m").
        assert len(tools.columns) == 9
        trows = [plain_row(tools, i) for i in range(tools.row_count)]
        assert trows[0] == ["Codex", "6.0M", "2.0M", "4.0M", "8.0M", "66.0%",
                            "$90.00", "3,000", "50m"]
        assert trows[1] == ["Claude Code", "3.0M", "1.4M", "2.0M", "4.4M",
                            "78.0%", "$52.31", "1,820", "40m"]
        # Round 4 alignment law: every numeric cell is a right-aligned Text
        # (8.2.8 DataTable has no column justify — alignment rides per cell).
        assert all(isinstance(c, Text) and c.justify == "right"
                   for c in tools.get_row_at(0)[1:])
        models = app.query_one("#ov-models")
        assert models.row_count == 2
        # Round 4: the models table carries the FULL field set (8 columns).
        assert len(models.columns) == 8
        assert plain_row(models, 0) == ["gpt-5-codex", "6.0M", "2.0M", "4.0M",
                                        "8.0M", "66.0%", "$90.00", "3,000"]
        assert all(isinstance(c, Text) and c.justify == "right"
                   for c in models.get_row_at(0)[1:])
        # Round 3: the trailing-365 band and the daily sparkline are GONE —
        # the Overview answers for the LOCAL window only.
        assert not app.query("#ov-band")
        assert not app.query("#ov-daily")
        status = static_text(app, "status")
        assert "cache hit" in status and DB_LINE in status
        assert "computing" not in status  # idle: no stuck spinner
        # Round 4 bottom hint: the compact echo of the top legend, words for
        # the shift keys (the old "t/w/m/y/a [/]/0" was unreadable shorthand).
        # escape() leaves "[ ]" bare (space after "[" can never be a tag —
        # the old "[/]" case was the one that needed the backslash).
        assert "t/w/m/y/a period · [ ] shift day · 0 today" in status
        # Round 4 top-of-pane legend, painted at mount (mutated wording or a
        # dropped Static both land here).
        hints = static_text(app, "ov-hints")
        assert "t today · w week · m month · y year · a all" in hints
        assert "\\[ earlier · ] later · 0 back to today" in hints  # literal "["
        # The serial web order actually ran: usage, active-time, stats.
        assert calls.usage and calls.active and calls.stats
        assert calls.usage[0] == ("today", False)

    drive(app, steps)


def test_skeleton_copy_until_first_answer(monkeypatch):
    gate = threading.Event()
    patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        # The skeleton is composed in place and stays until the first paint.
        assert "computing… first run scans session logs" in \
            static_text(app, "ov-range")
        assert "computing… first run scans session logs" in \
            static_text(app, "rp-body")  # report skeleton too
        gate.set()
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert "first run scans" not in static_text(app, "ov-range")
        assert "2026-09-14 → 2026-09-20 · 7 days" in static_text(app, "ov-range")

    drive(app, steps)


# ---------------------------------------------------------------------------
# Superseded-pane recovery: a gen bump from the OTHER pane must not strand
# the in-flight load's pane on the skeleton forever (no successor exists, so
# re-activation has to re-issue the fetch).
# ---------------------------------------------------------------------------

def test_reload_clears_painted_data_and_shows_computing(monkeypatch):
    # ROUND-4 LOADING LAW (user request): a reload must never leave the
    # PREVIOUS window's figures standing — the newest load clears its pane
    # and shows a short dim "computing…" (the long first-run skeleton stays
    # for the never-painted case). The old round-1 law ("refresh never
    # blanks painted data") is REVERSED here: stale figures read as live
    # data, which the user rejected.
    gate = threading.Event()
    gate.set()  # the first load must pass through untouched
    patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("week")  # week paints the chart slot — clear must hide it

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        tools = app.query_one("#ov-tools")
        models = app.query_one("#ov-models")
        detail = app.query_one("#ov-detail")
        assert tools.row_count > 0 and detail.row_count > 0
        assert app.query_one("#ov-chart").display is True

        gate.clear()  # the reload's fetches block
        await pilot.press("r")
        assert await wait_for(app, pilot, lambda: tools.row_count == 0)
        assert models.row_count == 0 and detail.row_count == 0
        assert "computing…" in static_text(app, "ov-range")
        assert "first run scans" not in static_text(app, "ov-range")
        assert str(app.query_one("#ov-kpis").content) == ""
        assert app.query_one("#ov-chart").display is False
        # the status bar already carried the spinner (unchanged behavior)
        assert "computing…" in static_text(app, "status")

        gate.set()
        assert await wait_for(app, pilot, lambda: tools.row_count > 0)
        assert await wait_for(app, pilot, lambda: detail.row_count > 0)
        assert "2026-09-14 → 2026-09-20" in static_text(app, "ov-range")
        assert "computing…" not in static_text(app, "ov-range")
        # The week chart is repainted LAST (by the STATS payload's tail, after
        # usage refilled the tables) — wait for the real postcondition instead
        # of assuming it already landed: CI runners proved that window real.
        assert await wait_for(app, pilot, lambda: app.query_one("#ov-chart").display is True)

    drive(app, steps)


def test_report_and_quota_panes_clear_on_reload(monkeypatch):
    gate = threading.Event()
    gate.set()
    patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")

        # --- report pane: rp-body is ONE Static, so the notice and the
        # clear are the same update.
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        assert "Top agent" in static_text(app, "rp-body")
        gate.clear()
        await pilot.press("r")  # r reloads the ACTIVE pane
        assert await wait_for(
            app, pilot,
            lambda: "Top agent" not in static_text(app, "rp-body"),
        )
        assert "computing…" in static_text(app, "rp-body")
        assert "first run scans" not in static_text(app, "rp-body")
        gate.set()
        assert await wait_for(
            app, pilot, lambda: "Top agent" in static_text(app, "rp-body")
        )

        # --- quota pane: table cleared AND the credits section hidden
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        table = app.query_one("#quota-table")
        assert table.row_count > 0
        assert app.query_one("#quota-credits").display is True
        gate.clear()
        await pilot.press("r")
        assert await wait_for(app, pilot, lambda: table.row_count == 0)
        assert "computing…" in static_text(app, "quota-note")
        assert app.query_one("#quota-credits").display is False
        gate.set()
        assert await wait_for(app, pilot, lambda: table.row_count > 0)
        assert app.query_one("#quota-credits").display is True

    drive(app, steps)


def test_top_hints_report_and_quota_panes(monkeypatch):
    patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        rp = static_text(app, "rp-hints")
        assert "w week · m month · y year" in rp
        assert "\\[ earlier · ] later · 0 back to today" in rp  # literal "["
        quota = static_text(app, "quota-hints")
        assert "u poll now" in quota
        assert "week" not in quota  # no period keys on Quota — one line only
        # compact bottom echo on Report after activation
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        assert "w/m/y period · [ ] shift day · 0 today" in static_text(app, "status")

    drive(app, steps)


def test_quota_credits_are_provider_labeled(monkeypatch):
    # The app passes TOOL_LABELS into charts.quota_table (labels arrive as
    # data; charts never imports the backend) — the rendered section names
    # its provider on every row, and the header sums across providers.
    state = _quota_state_payload()
    state["providers"]["claude"]["reset_credits"] = {
        "available_count": 1,
        "credits": [{"title": "Limit reset",
                     "expires_at": int(time.time()) + 600}],
    }
    patch_fetchers(monkeypatch, quota_state=state)
    app = TokdashApp("today")

    async def steps(pilot):
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        credits = app.query_one("#quota-credits")
        assert credits.display is True
        text = str(credits.content)
        # codex 2 + claude 1 — the header is two markup runs, assert parts.
        assert "reset credits" in text and "[green]3 available" in text
        assert "Claude Code · Limit reset" in text
        assert "Codex · Full reset" in text
        # sorted by expiry: the claude grant (now+600) precedes codex's
        # (now+86_400) — one global sort over the epoch/ISO duality.
        assert text.index("Claude Code ·") < text.index("Codex · Full reset")

    drive(app, steps)


def test_section_margins_and_hint_ids_in_css():
    # Mutation guard for the round-4 layout: the margin block and every
    # widget it names must exist (a dropped rule collapses the sections).
    css = TokdashApp.CSS
    assert "margin-top: 1" in css
    for wid in ("#ov-hints", "#ov-chart", "#ov-tools", "#ov-models",
                "#ov-detail", "#rp-hints", "#quota-hints", "#quota-table",
                "#quota-credits"):
        assert wid in css


def test_no_row_cursor_on_any_table(monkeypatch):
    # Round 5 (user: "the first item got background highlighted in tool /
    # model / tool /model, remove this highlighting"): that band WAS the
    # DataTable row cursor sitting on row 0 — every table now runs with
    # cursor_type="none" (the wheel is the only scroll axis).
    patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        for wid in ("#ov-tools", "#ov-models", "#ov-detail", "#quota-table"):
            assert app.query_one(wid, DataTable).cursor_type == "none"

    drive(app, steps)


def test_overview_tables_share_one_column_grid(monkeypatch):
    # Round 5 (user: "for these three sections, can you align the input
    # output ... rows?"): the three Overview tables are SEPARATE auto-
    # sizing DataTables — the only cross-table alignment lever is one
    # identical FIXED-width grid, built by one helper. Assert the grids
    # are literally equal, and equal to the module constants.
    patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        # `.columns` is a key→Column MAPPING (iterating it yields the
        # ColumnKey namedtuples) — `ordered_columns` is the width-bearing
        # Column objects, in on-screen order.
        widths = {
            wid: [c.width for c in app.query_one(wid, DataTable).ordered_columns]
            for wid in ("#ov-tools", "#ov-models", "#ov-detail")
        }
        grid = [app_mod._COL_NAME, app_mod._COL_TOK, app_mod._COL_TOK,
                app_mod._COL_TOK, app_mod._COL_TOK, app_mod._COL_PCT,
                app_mod._COL_COST, app_mod._COL_MSGS, app_mod._COL_TIME]
        assert widths["#ov-tools"] == grid
        assert widths["#ov-detail"] == grid
        assert widths["#ov-models"] == grid[:8]  # models drop only Time
        assert all(w is not None for w in widths["#ov-tools"])  # FIXED, not auto

    drive(app, steps)


def test_report_pane_recovers_after_other_pane_supersedes_it(monkeypatch):
    gate = threading.Event()  # every fetch blocks until the test opens it
    calls = patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        await pilot.pause()
        await pilot.press("2")  # report lazy-load starts (blocked on gate)
        await pilot.press("1")  # back to Overview
        await pilot.press("p")  # Overview-side gen bump: supersede the load
        gate.set()
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert app._rp_state == "empty"  # superseded load painted nothing
        insights_before = len(calls.insights)
        await pilot.press("2")  # re-activation must re-issue the fetch
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        assert "Top agent" in static_text(app, "rp-body")
        assert len(calls.insights) > insights_before  # a fetch really ran

    drive(app, steps)


def test_overview_pane_recovers_after_other_pane_supersedes_it(monkeypatch):
    gate = threading.Event()
    calls = patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        await pilot.pause()  # initial Overview load in flight (blocked)
        await pilot.press("2")  # report lazy-load starts
        await pilot.press("p")  # Report-side gen bump: supersede Overview's
        gate.set()
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        assert app._ov_state == "empty"  # superseded load painted nothing
        usage_before = len(calls.usage)
        await pilot.press("1")  # re-activation must re-issue the fetch
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert "first run scans" not in static_text(app, "ov-range")
        assert "→ 2026-09-20" in static_text(app, "ov-range")
        assert len(calls.usage) > usage_before

    drive(app, steps)


# ---------------------------------------------------------------------------
# Report pane lazy-load + keys
# ---------------------------------------------------------------------------

def test_report_tab_lazy_loads_with_shared_seam(monkeypatch):
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert not calls.insights  # lazy: no report fetch before activation
        await pilot.press("2")
        assert app.query_one("#tabs").active == "pane-report"
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        body = static_text(app, "rp-body")
        assert "Top agent" in body
        assert "2026-09-14 → 2026-09-20 · 7 days" in body
        # Final paint: every source filled → no soft-fail warn lines left.
        assert "\\[warn]" not in body
        # The exact warm triples (spec §6): usage("today", f, t),
        # insights("year", f, t, REPORT_FACETS, True), active("today", f, t, True).
        assert calls.usage[-1] == ("today", False)
        assert calls.insights[0][0] == "year"
        assert calls.active[0][0] == "today"
        assert "Report · week" in static_text(app, "status")

    drive(app, steps)


def test_report_mid_load_says_running_not_failed(monkeypatch):
    # Progressive paints happen with insights/active still None — the body
    # must show a dim "running" note, NEVER the one-shot's "scan failed" text
    # (a cold first scan can take tens of seconds; the false failure line was
    # the first thing users saw).
    calls = patch_fetchers(monkeypatch)
    gate = threading.Event()
    real_insights = app_mod.fetch_insights

    def gated_insights(*a, **k):
        assert gate.wait(10.0), "gate never opened"
        return real_insights(*a, **k)

    monkeypatch.setattr(app_mod, "fetch_insights", gated_insights)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("2")
        # Usage answered → first progressive paint; insights still blocked.
        assert await wait_for(app, pilot, lambda: "Tokdash report" in static_text(app, "rp-body"))
        mid = static_text(app, "rp-body")
        assert "scan failed" not in mid
        assert "analytics scan running" in mid
        gate.set()
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        final = static_text(app, "rp-body")
        assert "scan failed" not in final and "running" not in final
        assert "Top agent" in final

    drive(app, steps)


def test_report_pane_footer_uses_cached_db_line_and_never_opens_store(monkeypatch):
    # The builder must read window["db_line"] (app-cached) instead of calling
    # db_summary itself: on a fresh machine that call mkdirs ~/.tokdash and
    # builds the schema — and the Report pane repaints several times per load
    # on the UI event loop.
    calls = patch_fetchers(monkeypatch)

    def forbidden():
        raise AssertionError("report builder must not open the usage store")

    monkeypatch.setattr("tokdash.tui.report.db_summary", forbidden)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        body = static_text(app, "rp-body")
        assert DB_LINE in body  # the app's cached line reached the footer

    drive(app, steps)


def test_markup_hostile_names_render_literally(monkeypatch):
    # Model/tool names flow into rich Table cells and DataTable str cells,
    # both of which parse markup — a name like "claude [bold]…" would either
    # raise MarkupError (pane drops to error state) or silently restyle.
    usage = copy.deepcopy(USAGE_PAYLOAD)
    usage["top_models"] = [{"name": "claude [bold]1m[/]", "tokens": 8_000_000,
                            "cost": 90.0}]
    usage["combined_models"] = [dict(usage["combined_models"][0],
                                     name="claude [bold]1m[/]")]
    patch_fetchers(monkeypatch, usage=usage)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert app.is_running and app._ov_state == "ok"  # no pane-fail fallback
        kpi_text = render_table_text(app.query_one("#ov-kpis").content)
        assert "claude [bold]1m[/]" in kpi_text  # literal, not restyled

    drive(app, steps)


def test_window_cycle_p_forward_only(monkeypatch):
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert calls.usage[0] == ("today", False)
        await pilot.press("p")
        assert await wait_for(app, pilot, lambda: len(calls.usage) >= 2)
        # Round-2 cycle order is data.OVERVIEW_PERIODS — the calendar "week"
        # replaced the rolling "7d" so p and the t/w/m/y/a keys and the
        # warmer all mean the same window (semantics change, plan §Locked).
        # The fetch PERIOD is "today" (the resolved route period); the token
        # lives on _ov_period and the pair on usage_full.
        assert app._ov_period() == "week"
        assert calls.usage_full[1] == ("today", "2026-09-14", "2026-09-20")
        await pilot.press("p")
        assert await wait_for(app, pilot, lambda: len(calls.usage) >= 3)
        # Round 3 REMOVED Shift+P: p cycles FORWARD ONLY; the backward axis
        # is the DATE shift ( [ ] ), never a reverse period cycle.
        assert app._ov_period() == "month"
        assert calls.usage_full[2] == ("today", "2026-09-01", "2026-09-20")
        # Cycling is NEVER forced: new keys recompute naturally.
        assert all(refresh is False for _, refresh in calls.usage)

    drive(app, steps)


def test_help_panel_on_question_mark(monkeypatch):
    patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        await pilot.press("?")
        await pilot.pause()
        assert app.query("HelpPanel")  # built-in panel, zero custom screens

    drive(app, steps)


# ---------------------------------------------------------------------------
# Failure matrix (§7)
# ---------------------------------------------------------------------------

def test_worker_error_is_pane_local_and_app_survives(monkeypatch):
    patch_fetchers(monkeypatch, usage=RuntimeError("boom"))
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "error")
        assert "error: boom" in static_text(app, "ov-range")
        assert app.is_running  # app stays alive on ordinary worker errors
        await pilot.press("q")

    drive(app, steps)


def test_backpressure_is_busy_line_press_r(monkeypatch):
    from tokdash.api import CacheBackpressureError

    patch_fetchers(monkeypatch, usage=CacheBackpressureError("busy"))
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "error")
        assert "busy computing — press r" in static_text(app, "ov-range")

    drive(app, steps)


def test_schema_too_new_exits_one(monkeypatch):
    err = UsageDatabaseSchemaTooNewError(path="/tmp/usage.sqlite3", found=99,
                                         supported=9)
    patch_fetchers(monkeypatch, usage=err)
    app = TokdashApp("today")

    async def steps(pilot):
        # The terminal error's remediation text goes to sys.stderr (which the
        # headless driver captures); the LOCKED observable is the exit code.
        assert await wait_for(app, pilot, lambda: app.return_code == 1)

    drive(app, steps)


def test_run_tui_propagates_app_return_code(monkeypatch):
    # App.run() returns None; the fatal-exit code lives on App.return_code
    # (textual's docstring prescribes sys.exit(app.return_code)). A mid-run
    # self.exit(return_code=1) must reach the process, or the IDENTICAL
    # startup error exits 1 while the mid-run one exits 0 — automation trap.
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr(app_mod, "ensure_usage_db_compatible", lambda: None)

    def fake_run(self):  # no real driver: the contract under test is run_tui
        self._return_code = 1

    monkeypatch.setattr(TokdashApp, "run", fake_run)
    assert run_tui(argparse.Namespace(period="today")) == 1


def test_run_tui_clean_exit_returns_zero(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr(app_mod, "ensure_usage_db_compatible", lambda: None)

    def fake_run(self):  # normal quit never set return_code -> property is None
        pass

    monkeypatch.setattr(TokdashApp, "run", fake_run)
    assert run_tui(argparse.Namespace(period="today")) == 0


# ---------------------------------------------------------------------------
# Stale re-read: exactly one silent 12 s non-forced pass
# ---------------------------------------------------------------------------

def test_stale_reread_once_per_load(monkeypatch):
    assert STALE_REREAD_SECONDS == 12.0
    calls = patch_fetchers(monkeypatch, status="stale", age=42.0)
    app = TokdashApp("today")
    # Spy BEFORE the app runs: the canned fetchers are instant, so the first
    # load can finish during run_test startup — after that the scheduling
    # call is already gone.
    timers: list[tuple] = []
    real_set_timer = app.set_timer

    def spy(delay, callback=None, **kwargs):
        if delay == STALE_REREAD_SECONDS:
            timers.append((delay, callback))
            return None  # never fires on its own; the test invokes it
        return real_set_timer(delay, callback, **kwargs)

    app.set_timer = spy

    async def steps(pilot):
        # A stale answer schedules exactly one silent 12 s re-read…
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert await wait_for(app, pilot, lambda: len(timers) == 1)
        delay, callback = timers[0]
        assert delay == STALE_REREAD_SECONDS
        assert "cache stale age 42s" in static_text(app, "status")
        # …and invoking it re-fetches with refresh=False (never forced).
        n_before = len(calls.usage)
        callback()
        assert await wait_for(app, pilot, lambda: len(calls.usage) == n_before + 1)
        assert calls.usage[-1] == ("today", False)
        # Same gen, already re-read: the second stale outcome does NOT
        # reschedule for the same window-load.
        await pilot.pause(0.1)
        assert len(timers) == 1

    drive(app, steps)


# ---------------------------------------------------------------------------
# Day rollover (the day-pinned-keys answer for a long-lived TUI)
# ---------------------------------------------------------------------------

def test_day_rollover_on_keypress_restarts_active_pane(monkeypatch):
    holder = {"today": TODAY}
    calls = patch_fetchers(monkeypatch)
    monkeypatch.setattr(app_mod, "_local_today", lambda: holder["today"])
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        before = len(calls.usage)
        holder["today"] = TODAY + datetime.timedelta(days=1)
        await pilot.press("r")  # keypress-triggered — no midnight timer
        assert await wait_for(app, pilot, lambda: len(calls.usage) > before)
        assert app._today == holder["today"]

    drive(app, steps)


# ---------------------------------------------------------------------------
# run_tui tty preflight (also protects the rest of the suite from ever
# entering the alt screen — the refusal happens before textual runs)
# ---------------------------------------------------------------------------

def test_run_tui_refuses_non_tty(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        run_tui(argparse.Namespace(period="today"))
    assert "interactive terminal" in str(exc.value)


def test_run_tui_refuses_dumb_term(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "dumb")
    with pytest.raises(SystemExit) as exc:
        run_tui(argparse.Namespace(period="today"))
    assert "interactive terminal" in str(exc.value)


# ---------------------------------------------------------------------------
# Live headless smoke: drive BOTH panes through the real render pipeline and
# capture the composited screen text (the closest thing to "the user saw this").
# ---------------------------------------------------------------------------

def test_smoke_rendered_screen_both_panes(monkeypatch):
    patch_fetchers(monkeypatch)
    app = TokdashApp("today")
    captured = {}

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        await pilot.pause(0.05)
        captured["lines"] = [
            strip.text for strip in app.screen._compositor.render_strips()
        ]
        await pilot.press("1")
        assert await wait_for(
            app, pilot, lambda: static_text(app, "status").startswith("Overview")
        )

    drive(app, steps)
    screen = "\n".join(captured["lines"])
    print("\n=== rendered report pane (headless 80x24) ===")
    print(screen)
    assert "Tokdash report" in screen


# ---------------------------------------------------------------------------
# Round 2: period keys (t/w/m/y/a) — token, call-site normalization, status
# ---------------------------------------------------------------------------

def test_period_keys_set_token_and_normalize_fetch_args(monkeypatch):
    # Mutation proof #1: normalization happens AT THE FETCH CALL SITE. If
    # _load_overview passed the raw token to fetch_usage, the triples below
    # would read ("week", None, None) instead of the warmer's calendar pair.
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        # Initial "today": the explicit (D, D) pair _usage_warm_target warms.
        assert calls.usage_full[0] == ("today", "2026-09-20", "2026-09-20")
        assert "t/w/m/y/a" in static_text(app, "status")

        seen = len(calls.usage)
        await pilot.press("w")
        assert await wait_for(app, pilot, lambda: len(calls.usage) > seen)
        assert app._ov_period() == "week"
        assert calls.usage_full[-1] == ("today", "2026-09-14", "2026-09-20")
        assert calls.active_full[-1] == ("today", "2026-09-14", "2026-09-20", None)

        seen = len(calls.usage)
        await pilot.press("m")
        assert await wait_for(app, pilot, lambda: len(calls.usage) > seen)
        assert app._ov_period() == "month"
        assert calls.usage_full[-1] == ("today", "2026-09-01", "2026-09-20")

        seen = len(calls.usage)
        await pilot.press("y")
        assert await wait_for(app, pilot, lambda: len(calls.usage) > seen)
        assert app._ov_period() == "year"
        assert calls.usage_full[-1] == ("today", "2026-01-01", "2026-09-20")

        seen = len(calls.usage)
        await pilot.press("a")
        assert await wait_for(app, pilot, lambda: len(calls.usage) > seen)
        assert app._ov_period() == "all"
        assert calls.usage_full[-1] == ("all", None, None)

        seen = len(calls.usage)
        await pilot.press("t")
        assert await wait_for(app, pilot, lambda: len(calls.usage) > seen)
        assert app._ov_period() == "today"
        assert calls.usage_full[-1] == ("today", "2026-09-20", "2026-09-20")
        # Keys never force: warm keys recompute on their own.
        assert all(refresh is False for _, refresh in calls.usage)

    drive(app, steps)


def test_direct_period_keys_both_panes_and_status_hints(monkeypatch):
    # Round 3 contract, REPLACING round 2's pane-only rule: t/w/m/y/a pick
    # the period directly on BOTH period panes — on Report the same letter
    # picks the calendar window AND RELOADS IT (the old test asserted "no
    # reload" — inverted here on purpose, see plan). t/a stay inert on
    # Report (its windows are week/month/year only); Quota has no period
    # axis at all. The status hint names exactly what the pane answers to.
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert "t/w/m/y/a" in static_text(app, "status")

        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        status = static_text(app, "status")
        assert "Report · week" in status and "w/m/y" in status
        assert "t/w/m/y/a" not in status  # the Overview hint left with its pane

        ov_before = app._ov_period()
        before = len(calls.usage)
        await pilot.press("y")  # direct year window ON THE REPORT PANE
        assert await wait_for(app, pilot, lambda: len(calls.usage) > before)
        assert calls.usage_full[-1] == ("today", "2026-01-01", "2026-09-20")
        assert "Report · year" in static_text(app, "status")
        assert app._ov_period() == ov_before  # the Overview token is untouched

        before = len(calls.usage)
        await pilot.press("t")  # t/a: no such report window → inert
        await pilot.press("a")
        await pilot.pause(0.15)
        assert len(calls.usage) == before
        assert "Report · year" in static_text(app, "status")

        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        before = len(calls.usage)
        await pilot.press("m")
        await pilot.pause(0.15)
        assert len(calls.usage) == before
        assert app._ov_period() == ov_before
        assert "t/w/m/y/a" not in static_text(app, "status")
        assert "Quota · quota · u poll" in static_text(app, "status")

        await pilot.press("1")  # back: hint returns
        assert await wait_for(
            app, pilot, lambda: "t/w/m/y/a" in static_text(app, "status")
        )

    drive(app, steps)


# ---------------------------------------------------------------------------
# Round 2: adaptive chart slot
# ---------------------------------------------------------------------------

def test_chart_slot_per_token(monkeypatch):
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        # Round 3: NO chart on the day view (the user picked "no chart on
        # day view" — per-tool time lives in the tools table's Time column),
        # and the trailing sparkline is gone with the band: neither widget
        # exists any more, only the adaptive #ov-chart slot remains.
        assert not app.query("#ov-daily")
        assert not app.query("#ov-band")
        assert app.query_one("#ov-chart").display is False
        assert len(calls.active) == 1  # active still loads once (Time column)

        await pilot.press("w")
        assert await wait_for(
            app, pilot, lambda: "Mon 14" in static_text(app, "ov-chart")
        )
        chart = static_text(app, "ov-chart")
        # Day bars are LOCAL to the resolved window: STATS_PAYLOAD contribs
        # are all August — every September day is a gap → em-dash day rows
        # (day enumeration, not active-days-only).
        assert EM_DASH in chart
        assert len(chart.splitlines()) >= 7
        assert app.query_one("#ov-chart").display

        await pilot.press("m")
        assert await wait_for(
            app, pilot,
            lambda: "Mon Tue Wed Thu Fri Sat Sun" in static_text(app, "ov-chart"),
        )

        await pilot.press("y")
        assert await wait_for(
            app, pilot, lambda: "Jan" in static_text(app, "ov-chart")
        )

        await pilot.press("a")  # "all": no local chart — the slot hides
        assert await wait_for(
            app, pilot, lambda: app.query_one("#ov-chart").display is False
        )

    drive(app, steps)


def test_year_period_fires_second_stats_fetch_only_for_year(monkeypatch):
    # Mutation proof #2: the extra fetch_stats(CY) job runs for token "year"
    # and for nothing else (month below must stay at one stats call).
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert [c[0] for c in calls.stats] == [None]  # trailing-365 only
        await pilot.press("m")
        assert await wait_for(app, pilot, lambda: app._ov_period() == "month")
        await pilot.pause(0.2)
        assert [c[0] for c in calls.stats] == [None, None]  # still no CY fetch
        await pilot.press("y")
        # The year load = trailing-365 stats + the ONE calendar-year fetch.
        assert await wait_for(app, pilot, lambda: any(c[0] == 2026 for c in calls.stats))
        assert [c[0] for c in calls.stats] == [None, None, None, 2026]
        assert calls.stats[3] == (2026, False)  # the calendar-year fetch
        assert await wait_for(
            app, pilot, lambda: "Jan" in static_text(app, "ov-chart")
        )  # heatmap month header row rendered

    drive(app, steps)


def test_year_empty_contribs_still_render_skeleton(monkeypatch):
    empty_stats = {"summary": {}, "contributions": [], "stats": {}}
    patch_fetchers(monkeypatch, stats=empty_stats)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("y")
        assert await wait_for(
            app, pilot, lambda: "no activity 2026" in static_text(app, "ov-chart")
        )
        # skeleton = header + 7 weekday rows: an empty year is a MEASURED
        # zero, so the grid stays visible, it does not collapse.
        assert len(static_text(app, "ov-chart").splitlines()) == 9

    drive(app, steps)


# ---------------------------------------------------------------------------
# Round 2: Detail table (Tool / Model)
# ---------------------------------------------------------------------------

def test_ov_detail_sections_rows_openclaw_and_escaping(monkeypatch):
    usage = copy.deepcopy(USAGE_PAYLOAD)
    usage["by_tool"]["openclaw"] = {
        "tokens": 1_000_000, "cost": 3.0, "cache_hit_rate": None,
    }
    # Markup-hostile tool key: a raw "[bold]" would raise MarkupError at
    # render or silently restyle; escaped at the add_row site it renders
    # literally.
    usage["by_tool"]["weird [bold]name[/]"] = {
        "tokens": 500_000, "cost": 1.0, "cache_hit_rate": None,
    }
    usage["apps"] = {
        "codex": {"models": [
            {"name": "gpt-5", "tokens": 5_000_000, "cost": 10.0, "cache_hit_rate": 0.5},
            {"name": "gpt-5-codex", "tokens": 3_000_000, "cost": 80.0,
             "cache_hit_rate": 0.7},
        ]},
        "claude": {"models": [
            {"name": f"m{i}", "tokens": i * 1_000, "cost": float(i),
             "cache_hit_rate": None}
            for i in range(1, 6)
        ]},
    }
    usage["openclaw_models"] = [
        {"name": "ocl-a", "tokens": 400, "cost": 1.0},
        {"name": "ocl-b", "tokens": 600, "cost": 2.0},
    ]
    patch_fetchers(monkeypatch, usage=usage)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert app.is_running  # no MarkupError → no pane-fail fallback
        table = app.query_one("#ov-detail", DataTable)
        # rows: codex 1+2, claude 1+5, openclaw 1+2, weird 1+0 — round 3 has
        # NO cap ("always display full page"), so the old claude "…+2 more"
        # overflow row is gone (13 rows, not the round-2 12).
        assert table.row_count == 13
        assert len(table.columns) == 9  # round 4: the full field set
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        # Round 4: section name cells are background PILLS (the user asked
        # to separate tools). Round 5: the pill pads to _COL_NAME — the
        # shared name-column width — so every band is exactly as wide as
        # the column (and the other two tables' name column), no longer to
        # whichever section name happened to be longest.
        pill = rows[0][0]
        assert isinstance(pill, Text) and "on #334155" in str(pill.style)
        pill_widths = {cell(rows[i][0]).rstrip() for i in range(13)
                       if isinstance(rows[i][0], Text)}
        assert "Codex" in pill_widths and "Claude Code" in pill_widths
        assert {len(cell(rows[i][0])) for i in range(13)
                if isinstance(rows[i][0], Text)} == {app_mod._COL_NAME}
        # Section cells: full field set (9). The overridden apps rows carry
        # no breakdown — Input/Cache still land via the by_tool fallback,
        # Output/Msgs have no fallback and dash; total/hit/cost from by_tool;
        # Time from the active payload (codex 50m) — the detail repaint.
        assert plain_row(table, 0) == [
            "Codex" + " " * (app_mod._COL_NAME - len("Codex")),
            "6.0M", "—", "4.0M", "8.0M", "66.0%", "$90.00", "—", "50m",
        ]
        assert all(isinstance(c, Text) and c.justify == "right"
                   for c in rows[0][1:])
        # cost desc inside the section: gpt-5-codex ($80) before gpt-5 ($10)
        assert rows[1][0] == "  gpt-5-codex"  # model rows: plain escaped str
        assert cell(rows[1][-1]) == EM_DASH  # model Time ALWAYS dash (no source)
        assert not [r for r in rows if "more" in cell(r[0])]  # no overflow ever
        # the WHOLE claude list, cost desc
        assert [r[0] for r in rows
                if isinstance(r[0], str) and r[0].startswith("  m")] == [
            "  m5", "  m4", "  m3", "  m2", "  m1"]
        assert any(cell(r[0]).rstrip() == "OpenClaw"
                   for r in rows)  # synthesized section
        # Markup-hostile name: the Text pill is LITERAL (never markup-parsed),
        # so the raw brackets arrive un-escaped and render literally — the
        # Text path needs no escape(), the str path (model names) does.
        assert any(cell(r[0]).rstrip() == "Weird [bold]name[/]"
                   for r in rows)

    drive(app, steps)


# ---------------------------------------------------------------------------
# Round 3: wheel scrolling (the pane is the scroll container, not the screen)
# ---------------------------------------------------------------------------

def test_wheel_scrolls_the_pane_not_the_screen(monkeypatch):
    # Round 3 scroll contract (spike-proven): TabbedContent is height:1fr and
    # each PANE is the scroll container (height:1fr + overflow-y:auto) — the
    # wheel moves pane.scroll_y while the tab strip stays pinned (the screen
    # itself must never scroll). Size 80x14 ON PURPOSE: at 80x24 the Overview
    # fits without overflow and the wheel is legitimately dead — the test
    # would pass vacuously. Pilot has no mouse-scroll in textual 8.2.8, so
    # the events go in through post_message, exactly as the driver would
    # deliver them.
    patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def main():
        async with app.run_test(headless=True, size=(80, 14)) as pilot:
            await pilot.pause()
            assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
            pane = app.query_one("#pane-overview")
            assert pane.show_vertical_scrollbar  # content overflows the pane
            base = pane.scroll_y
            for _ in range(3):
                pane.post_message(
                    events.MouseScrollDown(pane, 10, 5, 0, 1, 0, False, False, False)
                )
                await pilot.pause(0.02)
            assert pane.scroll_y > base
            assert app.screen.scroll_y == 0  # the tab strip never slides off
            top = pane.scroll_y
            pane.post_message(
                events.MouseScrollUp(pane, 10, 5, 0, 1, 0, False, False, False)
            )
            await pilot.pause(0.02)
            assert pane.scroll_y < top
            await pilot.press("q")

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Round 3: the date-shift axis ( [ ] / 0 ) — today→yesterday→…
# ---------------------------------------------------------------------------

def test_date_shift_brackets_and_zero(monkeypatch):
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert calls.usage_full[-1] == ("today", "2026-09-20", "2026-09-20")
        # ] at today is INERT — the window never moves into the future.
        before = len(calls.usage)
        await pilot.press("right_square_bracket")
        await pilot.pause(0.15)
        assert len(calls.usage) == before and app._day_shift == 0

        await pilot.press("left_square_bracket")  # yesterday
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-09-19", "2026-09-19"),
        )
        assert app._day_shift == 1
        assert "· -1d" in static_text(app, "status")  # ASCII marker (cp1252 law)
        assert "viewing 1d back" in static_text(app, "ov-range")

        await pilot.press("left_square_bracket")  # two days back
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-09-18", "2026-09-18"),
        )
        await pilot.press("right_square_bracket")  # one day forward again
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-09-19", "2026-09-19"),
        )
        assert app._day_shift == 1

        await pilot.press("0")  # 0 = back to today
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-09-20", "2026-09-20"),
        )
        assert app._day_shift == 0
        assert "-1d" not in static_text(app, "status")
        assert "viewing" not in static_text(app, "ov-range")

    drive(app, steps)


def test_date_shift_fans_out_to_report_with_the_anchor(monkeypatch):
    # The date-pinned panes reload TOGETHER (a stale-anchor Report body would
    # be anchor-stale data — the same bug class as midnight), and the report
    # window builder receives the SHIFTED ANCHOR, not today.
    calls = patch_fetchers(monkeypatch)
    seen = []

    def spy_windows(today=None):
        seen.append(today)
        return WINDOWS

    monkeypatch.setattr(app_mod, "report_windows", spy_windows)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        assert seen[-1] == TODAY  # shift 0 = today's windows (warm parity)
        before = len(calls.usage)
        await pilot.press("left_square_bracket")
        # The Overview's own resolved pair followed the anchor…
        assert await wait_for(
            app, pilot,
            lambda: any(t[1] == "2026-09-19" for t in calls.usage_full),
        )
        # …and so did the Report: its report_windows call got the anchor.
        assert await wait_for(
            app, pilot,
            lambda: seen[-1] == TODAY - datetime.timedelta(days=1),
        )
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        assert len(calls.usage) >= before + 2  # both panes refetched

    drive(app, steps)


def test_date_shift_inert_on_rolling_all(monkeypatch):
    # "all" has no window to translate — [ must not churn the Overview body.
    # (The shift still lands: the Report pane, if started, is date-pinned —
    # see the fan-out test.)
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("all")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert app._ov_period() == "all"
        before = len(calls.usage)
        await pilot.press("left_square_bracket")
        assert await wait_for(app, pilot, lambda: app._day_shift == 1)
        await pilot.pause(0.15)
        assert len(calls.usage) == before  # no Overview reload fired
        assert calls.usage[-1] == ("all", False)

    drive(app, steps)


def test_date_shift_year_fetch_follows_the_anchor_year(monkeypatch):
    # Mutation lock for the New Year gotcha the plan spike caught: with a
    # Jan-5 "today" pulled back 10 days the ANCHOR is in 2025 — the year
    # heatmap's calendar-year fetch must follow the anchor, never _today
    # (self._today alone would render the wrong CY).
    calls = patch_fetchers(monkeypatch)
    monkeypatch.setattr(app_mod, "_local_today",
                        lambda: datetime.date(2026, 1, 5))
    app = TokdashApp("year")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert [c for c in calls.stats if c[0] is not None] == [(2026, False)]
        for _ in range(10):
            await pilot.press("left_square_bracket")
        assert await wait_for(app, pilot, lambda: app._day_shift == 10)
        assert await wait_for(  # anchor = 2025-12-26 → CY 2025
            app, pilot, lambda: (2025, False) in calls.stats
        )
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")

    drive(app, steps)


def test_day_rollover_preserves_the_shift(monkeypatch):
    holder = {"today": TODAY}
    calls = patch_fetchers(monkeypatch)
    monkeypatch.setattr(app_mod, "_local_today", lambda: holder["today"])
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("left_square_bracket")
        assert await wait_for(app, pilot, lambda: app._day_shift == 1)
        holder["today"] = TODAY + datetime.timedelta(days=1)
        before = len(calls.usage)
        await pilot.press("r")  # midnight crossed; detected on the keypress
        assert await wait_for(app, pilot, lambda: len(calls.usage) > before)
        assert app._today == holder["today"]
        # "viewing yesterday" is the user's standing intent — midnight must
        # not silently reset it; the ANCHOR moves with the new day instead.
        assert app._day_shift == 1
        assert "· -1d" in static_text(app, "status")

    drive(app, steps)


# ---------------------------------------------------------------------------
# Round 3: tools-table Time column — the null-vs-zero OVERRIDE (dash for 0)
# ---------------------------------------------------------------------------

def test_tools_table_time_zero_and_missing_are_dashes(monkeypatch):
    # ROUND-3 OVERRIDE of the null-vs-zero law, Time column ONLY: a MEASURED
    # 0 and a missing active row BOTH print EM_DASH (user: "0s should not be
    # displayed"). The report agents table keeps "0s" (null-vs-zero law intact
    # there) — locked opposite in test_tui_report.py; do not unify.
    # openclaw has no apps entry: Input/Output/Cache/Msgs dashes, hit n/a.
    usage = copy.deepcopy(USAGE_PAYLOAD)
    usage["by_tool"]["openclaw"] = {"tokens": 500_000, "cache_hit_rate": None}
    active = copy.deepcopy(ACTIVE_TIME_PAYLOAD)
    active["by_tool"]["claude"]["active_ms_sum"] = 0  # measured zero
    del active["by_tool"]["codex"]  # no active row at all
    patch_fetchers(monkeypatch, usage=usage, active=active)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        table = app.query_one("#ov-tools")
        rows = {
            cell(table.get_row_at(i)[0]): plain_row(table, i)
            for i in range(table.row_count)
        }
        assert rows["Codex"][8] == EM_DASH  # missing active row → dash
        assert rows["Claude Code"][8] == EM_DASH  # measured 0 → dash (override)
        oc = rows["OpenClaw"]
        assert oc[1] == oc[2] == oc[3] == EM_DASH  # Input/Output/Cache
        assert oc[7] == EM_DASH  # Msgs
        assert oc[5] == "n/a"  # hit rate: the formatting law's own sentinel
        assert oc[6] == EM_DASH  # no cost → dash (fmt_cost_row)

    drive(app, steps)


# ---------------------------------------------------------------------------
# Round 2: Quota pane
# ---------------------------------------------------------------------------

def test_quota_tab_lazy_once_and_renders_companion_cells(monkeypatch):
    # Mutation proof #3: the load is lazy (nothing fetched before "3") and
    # fires EXACTLY once (re-activation must not re-fetch).
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        assert not calls.quota_state and not calls.quota_history  # lazy
        await pilot.press("3")
        assert app.query_one("#tabs").active == "pane-quota"
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        assert len(calls.quota_state) == 1 and len(calls.quota_history) == 1
        # Banner runs are styled separately ("[bold]quota [/][green]enabled…")
        # — assert the enabled reading + the poll interval, not a joined string.
        note = static_text(app, "quota-note")
        assert "enabled" in note and "poll every 30 min" in note

        table = app.query_one("#quota-table", DataTable)
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        # Round 5 provider grouping: claude OPENS (claude@work) and its
        # second account CONTINUES ("@home"), then an ALL-EMPTY spacer,
        # codex, spacer, kimi — 6 rows, not the round-2 flat 4. Codex
        # credits are a BOTTOM SECTION now, never a row of the table.
        assert table.row_count == 6
        assert not [r for r in rows if r[1] == "credits"]
        spacers = [i for i, r in enumerate(rows) if not any(str(c) for c in r)]
        assert spacers == [2, 4]  # between groups only — never first/last
        codex = next(r for r in rows if r[0] == "codex" and r[1] == "5-hour")
        assert "58.0% left" in codex[2] and "█" * 6 in codex[2]  # LEFT, not used
        assert codex[1] == "5-hour"  # normalized, not raw "5-hour window"
        assert len(codex[4]) == 3  # 3 history points → 3 trend glyphs
        # Companion status colors: ok GREEN, anything else RED. The spacer
        # rows carry an EMPTY status and stay plain (no red-Text override).
        assert codex[5].plain == "ok" and codex[5].style == "green"
        kimi = next(r for r in rows if r[0] == "kimi")[5]
        assert kimi.plain == "unavailable" and kimi.style == "red"
        assert [c for c in rows[spacers[0]]] == [""] * 6
        assert any(r[0] == "claude@work" for r in rows)  # group opener folded
        assert any(r[0] == "@home" for r in rows)  # bare-account continuation
        # Runs are styled separately ("[bold]reset credits [/][green]2 …"),
        # so assert the fragments, not a joined string (banner-test precedent).
        credits = static_text(app, "quota-credits")
        assert "reset credits" in credits and "2 available" in credits
        assert "Full reset" in credits and "expires" in credits

        await pilot.press("1")
        await pilot.press("3")
        await pilot.pause(0.2)
        assert len(calls.quota_state) == 1  # lazy-ONCE
        assert "Quota · quota · u poll" in static_text(app, "status")

    drive(app, steps)


def test_quota_status_stale_token_is_red(monkeypatch):
    # The reported pairing: stale_token RED, ok GREEN (companion colors).
    payload = _quota_state_payload()
    payload["providers"]["codex"]["status"] = "stale_token"
    patch_fetchers(monkeypatch, quota_state=payload)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        table = app.query_one("#quota-table", DataTable)
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        stale = next(r for r in rows if r[0] == "codex")[5]
        assert stale.plain == "stale_token" and stale.style == "red"
        assert next(r for r in rows if r[0].startswith("claude"))[5].style == "green"

    drive(app, steps)


def test_quota_credits_section_hidden_without_credits(monkeypatch):
    payload = _quota_state_payload()
    del payload["providers"]["codex"]["reset_credits"]
    patch_fetchers(monkeypatch, quota_state=payload)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        assert app.query_one("#quota-credits").display is False

    drive(app, steps)


def test_quota_stranded_recovery(monkeypatch):
    gate = threading.Event()
    gate.set()
    calls = patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        gate.clear()
        await pilot.press("3")            # quota load starts, blocks on gate
        await pilot.pause(0.1)
        await pilot.press("1")            # back to Overview
        await pilot.press("t")            # Overview-side gen bump: supersede
        gate.set()
        assert await wait_for(
            app, pilot,
            lambda: "quota" in app._stranded and app._qp_state == "empty",
        )
        before = len(calls.quota_state)
        await pilot.press("3")            # re-activation must re-issue it
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        assert len(calls.quota_state) > before

    drive(app, steps)


def test_schema_too_new_from_quota_exits_one(monkeypatch):
    err = UsageDatabaseSchemaTooNewError(path="/tmp/usage.sqlite3", found=99,
                                         supported=9)
    patch_fetchers(monkeypatch, quota_state=err)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("3")
        # Terminal by design wherever it raises — the Quota job included.
        assert await wait_for(app, pilot, lambda: app.return_code == 1)

    drive(app, steps)


# ---------------------------------------------------------------------------
# Round 2: quota poll ("u") — the TUI's ONLY network entry point
# ---------------------------------------------------------------------------

def _poll_ok_result():
    return {"snapshots": 3, "inserted": 1, "network_sources": ["codex_api"]}


def test_quota_poll_notes_before_polling_then_reloads_refreshed(monkeypatch):
    # Mutation proof #4 (note-before-poll ordering) + the reload arg.
    import tokdash.cli as cli_mod

    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")
    seen = {}

    def fake_poll(*, include_network=True, network_sources=None):
        # Runs in the worker thread: reading the note here proves what the
        # user could see AT POLL TIME, before any result existed.
        seen["note_at_call"] = static_text(app, "quota-note")
        seen["include_network"] = include_network
        seen["network_sources"] = network_sources
        return _poll_ok_result()

    monkeypatch.setattr(cli_mod, "_quota_poll_once", fake_poll)

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        note = app.query_one("#quota-note", Static)
        updates = []
        real_update = note.update
        monkeypatch.setattr(
            note, "update",
            lambda content: (updates.append(str(content)), real_update(content))[1],
        )
        before = len(calls.quota_state)
        await pilot.press("u")
        assert await wait_for(
            app, pilot, lambda: len(calls.quota_state) == before + 1
        )
        assert seen["include_network"] is True   # mirrors `tokdash quota poll`
        assert seen["network_sources"] is None
        assert seen["note_at_call"] == "polling…"  # the note LED the result
        ok_idx = updates.index("poll ok")
        assert updates[0] == "polling…"          # order: polling → poll ok
        assert ok_idx < len(updates) - 1         # …then the reload repainted
        assert "enabled" in updates[-1]          # the fresh state's banner
        assert calls.quota_state[-1] == (True,)  # reload IS forced
        assert not app._polling

    drive(app, steps)


def test_quota_poll_disabled_is_said_never_faked_ok(monkeypatch):
    import tokdash.cli as cli_mod

    calls = patch_fetchers(monkeypatch)
    monkeypatch.setattr(
        cli_mod, "_quota_poll_once",
        lambda **k: {"snapshots": 0, "inserted": 0, "network_sources": [],
                     "disabled": True},
    )
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        before = len(calls.quota_state)
        await pilot.press("u")
        assert await wait_for(
            app, pilot,
            lambda: "quota tracking disabled" in static_text(app, "quota-note"),
        )
        assert "tokdash quota consent" in static_text(app, "quota-note")
        assert "poll ok" not in static_text(app, "quota-note")
        await pilot.pause(0.2)
        assert len(calls.quota_state) == before  # disabled ⇒ no reload
        assert not app._polling

    drive(app, steps)


def test_quota_poll_error_keeps_app_alive(monkeypatch):
    import tokdash.cli as cli_mod

    calls = patch_fetchers(monkeypatch)

    def boom(**k):
        raise RuntimeError("net down")

    monkeypatch.setattr(cli_mod, "_quota_poll_once", boom)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        before = len(calls.quota_state)
        await pilot.press("u")
        assert await wait_for(
            app, pilot,
            lambda: "poll failed: net down" in static_text(app, "quota-note"),
        )
        assert app.is_running          # ordinary errors are a NOTE, never fatal
        await pilot.pause(0.2)
        assert len(calls.quota_state) == before  # and NO reload storm
        assert not app._polling

    drive(app, steps)


def test_quota_poll_reentrancy_guard(monkeypatch):
    # Mutation proof #5: without the _polling flag the second u would start a
    # second poll while the first still runs.
    import tokdash.cli as cli_mod

    patch_fetchers(monkeypatch)
    release = threading.Event()
    hits = []

    def slow_poll(**k):
        hits.append(1)
        assert release.wait(10.0), "poll never released"
        return _poll_ok_result()

    monkeypatch.setattr(cli_mod, "_quota_poll_once", slow_poll)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        await pilot.press("u")
        await pilot.pause(0.1)
        assert app._polling
        await pilot.press("u")  # while polling: ignored
        await pilot.press("u")
        await pilot.pause(0.1)
        assert len(hits) == 1
        assert static_text(app, "quota-note") == "polling…"  # 2nd u didn't repaint
        release.set()
        assert await wait_for(app, pilot, lambda: not app._polling)

    drive(app, steps)


def test_no_timer_ever_polls_quota(monkeypatch):
    # The locked decision: polling is EXPLICIT ONLY. Spy set_timer — the
    # stale-reread (12 s) is the ONLY timer the app may schedule, and the
    # poll count stays at the one explicit u forever.
    import tokdash.cli as cli_mod

    patch_fetchers(monkeypatch)
    hits = []
    monkeypatch.setattr(
        cli_mod, "_quota_poll_once",
        lambda **k: (hits.append(1), _poll_ok_result())[1],
    )
    app = TokdashApp("today")
    scheduled = []
    real_set_timer = app.set_timer

    def spy(delay, callback=None, **kwargs):
        scheduled.append(delay)
        return real_set_timer(delay, callback, **kwargs)

    app.set_timer = spy

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        await pilot.press("u")
        assert await wait_for(app, pilot, lambda: len(hits) == 1)
        await pilot.pause(0.3)
        await pilot.pause(0.3)
        assert len(hits) == 1              # nothing re-fired it
        assert all(d == STALE_REREAD_SECONDS for d in scheduled)  # only stale reread

    drive(app, steps)


def test_quota_poll_inert_on_other_panes(monkeypatch):
    import tokdash.cli as cli_mod

    calls = patch_fetchers(monkeypatch)
    monkeypatch.setattr(
        cli_mod, "_quota_poll_once", lambda **k: _poll_ok_result(),
    )
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("u")   # Overview: inert
        await pilot.pause(0.15)
        assert not calls.quota_state and not app._polling
        await pilot.press("2")
        await pilot.press("u")   # Report: inert
        await pilot.pause(0.15)
        assert not calls.quota_state and not app._polling

    drive(app, steps)


# ---------------------------------------------------------------------------
# Round 2: refresh re-probes the service; rollover reloads started quota
# ---------------------------------------------------------------------------

def test_refresh_resets_remote_probe_then_reloads(monkeypatch):
    calls = patch_fetchers(monkeypatch)
    order = []
    real_reset = app_mod.reset_remote

    def spy_reset():
        order.append("reset")
        real_reset()

    monkeypatch.setattr(app_mod, "reset_remote", spy_reset)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        before = len(calls.usage)
        await pilot.press("r")
        assert await wait_for(app, pilot, lambda: len(calls.usage) > before)
        assert order == ["reset"]              # r re-probes before reloading
        assert calls.usage[-1] == ("today", True)

    drive(app, steps)


def test_day_rollover_reloads_started_quota(monkeypatch):
    holder = {"today": TODAY}
    calls = patch_fetchers(monkeypatch)
    monkeypatch.setattr(app_mod, "_local_today", lambda: holder["today"])
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._qp_state == "ok")
        before = len(calls.quota_state)
        holder["today"] = TODAY + datetime.timedelta(days=1)
        await pilot.press("z")  # unbound key: on_key rollover path
        assert await wait_for(app, pilot, lambda: len(calls.quota_state) > before)
        assert app._today == holder["today"]
        assert calls.quota_state[-1] == (False,)  # rollover never forces

    drive(app, steps)


# ---------------------------------------------------------------------------
# Round 2: report pane dynamic width
# ---------------------------------------------------------------------------

def test_clamp_report_width_pure():
    # Mutation proof #6: clamp bounds and the un-laid-out fallback.
    assert _clamp_report_width(0) == 80    # headless / never-shown pane
    assert _clamp_report_width(-1) == 80
    assert _clamp_report_width(40) == 60   # floor: a 40-col pane still reads
    assert _clamp_report_width(78) == 78
    assert _clamp_report_width(200) == 200
    assert _clamp_report_width(9999) == 200  # ceiling: the builder caps here too


def test_report_width_follows_pane_size(monkeypatch):
    patch_fetchers(monkeypatch)
    widths = []
    real_build = app_mod.build_report_segments

    def spy(*a, **k):
        widths.append(k["width"])
        return real_build(*a, **k)

    monkeypatch.setattr(app_mod, "build_report_segments", spy)
    app = TokdashApp("today")

    def pane_content_width():
        return app.query_one("#pane-report").content_region.width

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._ov_state == "ok")
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._rp_state == "ok")
        # The first load paints can legitimately race the activation layout
        # (instant canned fetches finish before Textual lays the pane out) —
        # then it is the documented un-laid-out 80 fallback; either way the
        # round-1 FIXED 72 is gone (a 78/76 paint proves dynamic reading).
        assert widths and all(w != 72 for w in widths)
        await pilot.pause(0.1)              # let the pane lay out
        await pilot.press("p")              # report-side cycle → repaint
        assert await wait_for(app, pilot, lambda: len(widths) >= 4)
        # Laid out: the BUILDER width equals the pane's content width
        # (padding and any scrollbar included — asserted against the live
        # geometry, so it cannot rot with theme details). 78/76 ≠ 72: dynamic.
        assert widths[-1] == _clamp_report_width(pane_content_width())
        assert widths[-1] != 72
        await pilot.resize_terminal(300, 40)
        await pilot.pause(0.1)
        await pilot.press("p")
        assert await wait_for(app, pilot, lambda: widths[-1] == 200)  # ceiling
        await pilot.resize_terminal(60, 30)
        await pilot.pause(0.1)
        await pilot.press("p")
        assert await wait_for(app, pilot, lambda: widths[-1] == 60)   # floor

    drive(app, steps)
