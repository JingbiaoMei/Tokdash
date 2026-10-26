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
from textual.color import Color  # noqa: E402
from textual.widgets import Button, DataTable, Input, Static, Tab  # noqa: E402

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
from support.tui import wait_for  # noqa: E402  (polls a predicate; shared)

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
         # Contract shape: compute.py builds sources[] as OBJECTS carrying
         # modelId/providerId/tokens, not names ("compute.py:1318 reads
         # s.get("modelId")), so the fixture carries what the route sends.
         "sources": [{"source": "coding", "modelId": "gpt-5-codex",
                      "providerId": "openai", "cost": 3.0,
                      "tokens": {"input": 100, "output": 50,
                                 "cacheRead": 40, "cacheWrite": 10}}],
         "intensity": (day % 4) + 1}
        for day in range(1, 32)
    ],
    "stats": {"favorite_model": "gpt-5-codex", "current_streak": 6,
              "longest_streak": 12, "active_days": 40, "total_days": 365},
}

DB_LINE = "usage db disabled (TOKDASH_USAGE_DB=0) — live parsing"


def _outcome(payload, status="hit", age=0.0):
    return FetchOutcome(value=copy.deepcopy(payload), status=status, age_seconds=age)


# Quota fixtures mirror tests/tui/test_tui_charts.py's REAL quota_table-shaped
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
    monkeypatch.setattr(app_mod, "db_summary", lambda: DB_LINE)
    monkeypatch.setattr(app_mod, "_local_today", lambda: TODAY)
    return calls


def drive(app, steps, size=(80, 24)):
    """Run the app headless (80x24 unless told otherwise) and hand
    control to steps(pilot)."""
    async def main():
        async with app.run_test(headless=True, size=size) as pilot:
            await pilot.pause()  # let on_mount / the initial worker start
            await steps(pilot)

    asyncio.run(main())


def style_words(text_style):
    """The words of a resolved text-style, e.g. {"bold", "underline"}.

    Textual hands back the CSS string for a rule it parsed and a rich Style for
    one set in code; both print as the same words.
    """
    return set(str(text_style).split())


def screen_text(app):
    """The whole screen as the terminal would show it, one line per row."""
    return "\n".join(
        strip.text for strip in app.screen._compositor.render_strips(app.screen.size)
    )


def static_text(app, widget_id):
    return str(app.query_one(f"#{widget_id}").content)


def cell(c):
    """Plain text of one DataTable cell. Round 4 made most cells rich Text
    (the right-aligned numerics of app._num, the quota status color, the
    Detail section pills); str cells (escaped names) pass through.

    A source cell is a BADGE -- the harness's own tint on an accent edge plus
    the label (app._source_cell) -- and the edge is presentation, so it is read
    back off here. The badge's colour has its own test, which asserts on the
    cell's spans rather than its plain text."""
    text = c.plain if isinstance(c, Text) else str(c)
    return text.removeprefix(app_mod._BADGE_EDGE)


def headers_of(table):
    """The on-screen column labels, in order (what the user actually sees)."""
    return [str(c.label) for c in table.ordered_columns]


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


def test_overview_paints_window_cards_and_status(monkeypatch):
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        # THE WINDOW PRINTS ONCE. The shared strip under the tabs owns the dates
        # (the window the APP resolved, not the payload's echo of another
        # window); the page head owns the page name. Repeating either one costs
        # a row on a 24-row screen and buys a second place to disagree.
        strip = static_text(app, "range-dates")
        assert strip.startswith("2026-09-20"), strip
        assert "Overview" not in strip
        assert static_text(app, "ov-title") == "Overview"
        assert static_text(app, "ov-scope") == "All tools"
        cards = str(app.query_one("#ov-cards").content)
        assert "12.4M" in cards and "$142.31" in cards and "4,820" in cards
        assert "1h 30m" in cards  # agent SUM, not the 1h clock union
        assert "71.0%" in cards and "gpt-5-codex" in cards
        assert "+8.2%" in cards and "-3.1%" in cards
        # Revision 5 palette law: the card VALUES carry the tints (the approved
        # design colours each figure) while the labels and deltas stay grey. The
        # pilot's terminal reports no colour, so the law is checked with the
        # colour path forced -- the monochrome path has its own test.
        app.no_color = False
        app._repaint_pane("overview")
        text = app.query_one("#ov-cards").content
        assert any("#c4a1ff" in str(span.style) for span in text.spans), text.spans
        assert not any("#b9a7ff" in str(span.style) for span in text.spans)
        app.no_color = True
        app._repaint_pane("overview")
        assert not any(
            "#c4a1ff" in str(span.style)
            for span in app.query_one("#ov-cards").content.spans
        )

        table = app.query_one("#ov-table", DataTable)
        assert table.row_count == len(USAGE_PAYLOAD["by_tool"])
        # At 80 columns the harness table keeps its five load-bearing columns;
        # the optional breakdown is what goes (see the width law below).
        assert headers_of(table) == ["Tool", "Tokens", "Share", "Cost",
                                     "Est. time"]
        assert plain_row(table, 0) == ["Codex", "8.0M", "64.5%", "$90.00", "50m"]
        assert plain_row(table, 1) == ["Claude Code", "4.4M", "35.5%", "$52.31",
                                       "40m"]
        # Alignment law: every numeric cell is a right-aligned Text, so the
        # digits line down without a column of their own to lean on.
        assert all(isinstance(c, Text) and c.justify == "right"
                   for c in table.get_row_at(0)[1:])
        # The Overview answers for the LOCAL window only: no band, no daily
        # sparkline, and no stats fetch of its own (Stats owns that read).
        assert not app.query("#ov-band")
        assert not app.query("#ov-daily")
        status = static_text(app, "status")
        assert "cache hit" in status and DB_LINE in status
        assert "computing" not in status  # idle: no stuck spinner
        # The status line names the keys THIS pane answers to.
        assert "/ search · s sort · enter open · esc close" in status
        assert "[ ] range · c dates" in status
        # Usage and active time load; stats is NOT the Overview's to ask for.
        assert calls.usage and calls.active
        assert calls.usage[0] == ("today", False)
        assert not calls.stats

    drive(app, steps)



def test_skeleton_copy_until_first_answer(monkeypatch):
    gate = threading.Event()
    patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        # A pane that has never painted says what a first scan costs; a pane
        # that only has to redraw says less. Both are set BEFORE the answer.
        assert "computing… first run scans session logs" in \
            static_text(app, "ov-note")
        gate.set()
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        assert "first run scans" not in static_text(app, "ov-note")
        # A healthy pane shows NO strip at all -- an empty tinted band is worse
        # than none -- and the dates were never the strip's to print.
        assert app.query_one("#ov-note").display is False
        assert "2026-09-20" in static_text(app, "range-dates")

        gate.clear()
        await pilot.press("2")  # Report opens for the first time, still blocked
        assert "computing… first run scans session logs" in \
            static_text(app, "rp-body")
        gate.set()
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        assert "first run scans" not in static_text(app, "rp-body")

    drive(app, steps)


# ---------------------------------------------------------------------------
# Superseded-pane recovery: a gen bump from the OTHER pane must not strand
# the in-flight load's pane on the skeleton forever (no successor exists, so
# re-activation has to re-issue the fetch).
# ---------------------------------------------------------------------------


def test_reload_clears_painted_data_and_shows_computing(monkeypatch):
    # LOADING LAW: a reload never leaves the PREVIOUS window's figures standing
    # behind the new one. The moment a load starts its pane clears to a short
    # dim notice; the long first-run copy stays for the never-painted case.
    gate = threading.Event()
    gate.set()  # the first load passes through untouched
    patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        table = app.query_one("#ov-table", DataTable)
        assert table.row_count > 0
        app.query_one("#ov-table", DataTable).focus()
        await pilot.press("enter")  # open one harness's detail
        assert app.query_one("#ov-detail").display is True

        gate.clear()  # the reload's fetches block
        await pilot.press("r")
        assert await wait_for(app, pilot, lambda: table.row_count == 0)
        assert "[dim]computing…[/]" == static_text(app, "ov-note")
        assert str(app.query_one("#ov-cards").content) == ""
        assert static_text(app, "ov-activity") == ""
        assert app.query_one("#ov-detail").display is False
        # the status bar already carried the spinner (unchanged behavior)
        assert "computing…" in static_text(app, "status")

        gate.set()
        assert await wait_for(app, pilot, lambda: table.row_count > 0)
        # The window lives on the ONE shared strip (revision 5), not per pane.
        assert "2026-09-20" in static_text(app, "range-dates")
        assert "computing…" not in static_text(app, "range-dates")
        assert str(app.query_one("#ov-cards").content) != ""

    drive(app, steps)



def test_report_and_quota_panes_clear_on_reload(monkeypatch):
    gate = threading.Event()
    gate.set()
    patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")

        # --- report pane: rp-body is ONE Static, so the notice and the
        # clear are the same update.
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        assert "Tokdash report" in static_text(app, "rp-body")
        gate.clear()
        await pilot.press("r")  # r reloads the ACTIVE pane
        assert await wait_for(
            app, pilot,
            lambda: "Tokdash report" not in static_text(app, "rp-body"),
        )
        assert "computing…" in static_text(app, "rp-body")
        assert "first run scans" not in static_text(app, "rp-body")
        gate.set()
        assert await wait_for(
            app, pilot, lambda: "Tokdash report" in static_text(app, "rp-body")
        )

        # --- quota pane: table cleared AND the credits section hidden
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
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



def test_the_status_line_names_the_keys_the_active_pane_answers_to(monkeypatch):
    patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        assert "[ ] range · c dates" in static_text(app, "status")

        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        status = static_text(app, "status")
        assert "s sorts projects" in status
        assert "/ search" not in status  # the Overview hint left with its pane
        # The section picker is the Report's own, and it says what 's' sorts.
        hints = static_text(app, "rp-hints")
        assert "sorts projects" in hints or "s sorts" in hints

        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
        status = static_text(app, "status")
        assert "u poll" in status
        assert "[ ] range" not in status  # Quota has no date axis at all
        assert app.query_one("#rangebar").display is False

        await pilot.press("1")  # back: the Overview hints return
        assert await wait_for(
            app, pilot, lambda: "s sort" in static_text(app, "status")
        )
        assert app.query_one("#rangebar").display is True

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
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
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



def test_the_surface_keeps_its_layout_laws_in_css(monkeypatch):
    # Revision 5's approved look is carried by a handful of stylesheet rules.
    # A dropped rule collapses the sections or brings the outlines back, so the
    # laws are pinned here rather than trusted to a screenshot -- and read off
    # the widgets as the stylesheet resolved them, because a rule's text can sit
    # in TokdashApp.CSS long after its selector stopped matching anything.
    patch_fetchers(monkeypatch)
    app = TokdashApp("today")
    lavender = Color.parse("#b9a7ff")

    def no_border(widget):
        return tuple(widget.styles.border.spacing) == (0, 0, 0, 0)

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        # Thirty-six rows is the roomy page; at 24 the air gives way on purpose.
        assert not app.query_one("#tabs").has_class("-compact")
        table = app.query_one("#ov-table", DataTable)
        strip = app.query_one("#range-yesterday", Button)  # not the live window
        # 1. Sections separate by SPACING, not by a rule above each one: the
        #    toolbar, the table and the caption under it each sit a row down.
        for selector in ("#ov-subview", "#ov-table", "#ov-detail"):
            margin = app.query_one(selector).styles.margin
            assert margin.top == 1, (selector, margin)
        # 2. No panel outlines: the tables and the strip buttons carry no border.
        assert no_border(table), table.styles.border
        assert no_border(strip), strip.styles.border
        # 3. The active tab is the lavender selection colour...
        tabs = list(app.query(Tab))
        active = [tab for tab in tabs if tab.has_class("-active")]
        assert len(active) == 1, [str(tab.label) for tab in active]
        assert active[0].styles.background == lavender, active[0].styles.background
        assert all(tab.styles.background != lavender
                   for tab in tabs if tab is not active[0])
        # ...and so is a strip shortcut in its press flash, Textual's -active.
        assert strip.styles.background != lavender, strip.styles.background
        strip.add_class("-active")
        await pilot.pause()
        assert strip.styles.background == lavender, strip.styles.background
        strip.remove_class("-active")
        await pilot.pause()
        # Keyboard focus is still VISIBLE (underline) without being a border...
        toggle = app.query_one("#ov-tab-models", Button)
        toggle.focus()
        assert await wait_for(app, pilot, lambda: toggle.has_focus)
        assert "underline" in style_words(toggle.styles.text_style), toggle.styles.text_style
        assert no_border(toggle), toggle.styles.border
        # ...and an input says when it is live, in the selection colour.
        app.action_begin_search()
        box = app.query_one("#ov-search", Input)
        assert await wait_for(app, pilot, lambda: box.has_focus)
        assert box.styles.border_top == ("round", lavender), box.styles.border_top
        # 4. The table header keeps its separator tone and its weight; rows do not.
        header = table.get_component_styles("datatable--header")
        assert "bold" in style_words(header.text_style), header.text_style
        assert header.background == Color.parse("#18212f"), header.background
        for name in ("datatable--odd-row", "datatable--even-row"):
            row = table.get_component_styles(name)
            assert row.background != header.background, (name, row.background)
            assert "bold" not in style_words(row.text_style), (name, row.text_style)
        # 5. The dates are right-aligned so the preset row cannot shove them
        #    around as it reflows.
        dates = app.query_one("#range-dates").styles.text_align
        assert dates == "right", dates

    drive(app, steps, size=(120, 36))



def test_tables_mark_the_keyboard_position_with_a_quiet_row_tone(monkeypatch):
    # Round 5 banned the accidental highlight of row 0; revision 5 keeps a
    # VISIBLE cursor (it is the only keyboard-position affordance a DataTable
    # has) but the band is the quiet surface tone and never the lavender the
    # active tab wears, so a cursor can never be read as a selection.
    #
    # Rows DO alternate, one background step apart: a terminal cannot space its
    # lines, and a reader tracking one figure across ten columns loses the row
    # they started on. It is one step, one tone, and never a rule between rows.
    patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        tables = list(app.query(DataTable))
        assert len(tables) >= 6  # every pane ships its table
        for table in tables:
            assert table.cursor_type == "row", table.id
            assert table.zebra_stripes is True, table.id
        # Read off each table as the stylesheet resolved its row components.
        lavender = Color.parse("#b9a7ff")
        for table in tables:
            band = table.get_component_styles("datatable--cursor")
            hover = table.get_component_styles("datatable--hover")
            odd = table.get_component_styles("datatable--odd-row")
            even = table.get_component_styles("datatable--even-row")
            assert band.background == Color.parse("#263141"), (table.id, band.background)
            assert lavender not in (band.background, hover.background,
                                    odd.background, even.background), table.id
            # One tone each, one step apart, and no rule between rows -- the
            # separation the approved design draws with space and surface, not
            # with lines. An opaque tone of the app's own: Textual's defaults
            # leave odd rows bare and lay even ones at half alpha.
            assert odd.background.a == 1 and even.background.a == 1, (
                table.id, odd.background, even.background)
            assert odd.background != even.background, (table.id, odd.background)
            for row in (odd, even):
                assert tuple(row.border.spacing) == (0, 0, 0, 0), (table.id, row.border)
                assert "underline" not in style_words(row.text_style), (
                    table.id, row.text_style)

    drive(app, steps)



def test_a_wide_pane_gains_the_breakdown_a_narrow_one_drops(monkeypatch):
    # The Overview answers for one population in ONE table whose optional
    # breakdown columns appear only when the pane can hold them. Numbers are
    # fitted, never clipped: a narrow terminal loses Input..Msgs, not digits.
    patch_fetchers(monkeypatch)
    narrow = TokdashApp("today")

    async def steps_narrow(pilot):
        assert await wait_for(app := narrow, pilot,
                              lambda: narrow._state["overview"] == "ok")
        table = narrow.query_one("#ov-table", DataTable)
        assert headers_of(table) == ["Tool", "Tokens", "Share", "Cost",
                                     "Est. time"]
        assert plain_row(table, 0) == ["Codex", "8.0M", "64.5%", "$90.00", "50m"]
        assert all(c.width for c in table.ordered_columns)  # FIXED, not auto

    drive(narrow, steps_narrow)

    wide = TokdashApp("today")

    async def steps_wide(pilot):
        assert await wait_for(app := wide, pilot,
                              lambda: wide._state["overview"] == "ok")
        table = wide.query_one("#ov-table", DataTable)
        assert headers_of(table) == ["Tool", "Tokens", "Share", "Cost",
                                     "Input", "Output", "Cache", "Hit", "Msgs",
                                     "Est. time"]
        assert plain_row(table, 0) == ["Codex", "8.0M", "64.5%", "$90.00",
                                       "6.0M", "2.0M", "4.0M", "66.0%",
                                       "3,000", "50m"]

    drive(wide, steps_wide, size=(140, 40))



def test_report_pane_recovers_after_other_pane_supersedes_it(monkeypatch):
    # A gen bump must never strand the pane whose load it cancelled: the
    # Overview's reload supersedes the Report's in-flight fetch, and coming back
    # to the Report has to re-issue it rather than sit on the skeleton forever.
    gate = threading.Event()  # every fetch blocks until the test opens it
    calls = patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        await pilot.pause()
        await pilot.press("2")  # report lazy-load starts (blocked on gate)
        # The bump only supersedes a load that is already in flight; one still
        # queued on a busy runner would start in the new generation instead.
        assert await wait_for(app, pilot, lambda: "report" in app._inflight)
        await pilot.press("1")  # back to Overview
        await pilot.press("r")  # Overview-side gen bump: supersede the load
        gate.set()
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        # The superseded read records itself when its blocked thread wakes,
        # which is not tied to when Overview's reload lands.
        assert await wait_for(app, pilot, lambda: "report" in app._stranded)
        assert app._state["report"] == "empty"  # superseded load painted nothing
        insights_before = len(calls.insights)
        await pilot.press("2")  # re-activation must re-issue the fetch
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        assert "Tokdash report" in static_text(app, "rp-body")
        assert len(calls.insights) > insights_before  # a fetch really ran

    drive(app, steps)



def test_overview_pane_recovers_after_other_pane_supersedes_it(monkeypatch):
    # The same law seen from the other side: a Report-side reload supersedes the
    # Overview load that is still in flight, and the Overview is off screen, so
    # the recovery belongs to the tab hook rather than to a silent repaint.
    gate = threading.Event()
    calls = patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        await pilot.pause()  # initial Overview load in flight (blocked)
        await pilot.press("2")  # report lazy-load starts
        await pilot.pause(0.05)
        assert "overview" not in app._stranded  # nothing has superseded it yet
        await pilot.press("r")  # Report-side reload: gen bump supersedes Overview
        gate.set()
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        assert app._state["overview"] == "empty"  # superseded load painted nothing
        assert "overview" in app._stranded
        usage_before = len(calls.usage)
        await pilot.press("1")  # re-activation must re-issue the fetch
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        assert "first run scans" not in static_text(app, "ov-note")
        assert "2026-09-20" in static_text(app, "range-dates")
        assert len(calls.usage) > usage_before

    drive(app, steps)


# ---------------------------------------------------------------------------
# Report pane lazy-load + keys
# ---------------------------------------------------------------------------


def test_report_tab_lazy_loads_with_shared_seam(monkeypatch):
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        assert not calls.insights  # lazy: no report fetch before activation
        await pilot.press("2")
        assert app.query_one("#tabs").active == "pane-report"
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        body = static_text(app, "rp-body")
        assert "Tokdash report" in body
        # The body prints the window the shared strip names, not its own guess.
        assert "2026-09-20" in body
        # Final paint: every source filled → no soft-fail warn lines left.
        assert "\\[warn]" not in body
        # The exact warm triples (spec §6): usage("today", f, t),
        # insights("year", f, t, REPORT_FACETS, True), active("today", f, t).
        assert calls.usage[-1] == ("today", False)
        assert calls.insights[0][0] == "year"
        assert calls.active[0][0] == "today"
        # The foot answers "what can I press here" -- the page name belongs to
        # the head, and the report body spells the document out for itself.
        assert static_text(app, "rp-title") == "Report"
        assert "← → section" in static_text(app, "status")

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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("2")
        # Usage answered → first progressive paint; insights still blocked.
        assert await wait_for(
            app, pilot, lambda: "Tokdash report" in static_text(app, "rp-body"))
        mid = static_text(app, "rp-body")
        assert "scan failed" not in mid
        assert "analytics scan running" in mid
        gate.set()
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        final = static_text(app, "rp-body")
        assert "scan failed" not in final and "running" not in final
        assert "Tokdash report" in final

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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        # Revision 5 moved the store line off the report body and onto the strip
        # at the foot of EVERY pane: the database a figure came from belongs to
        # all the numbers on screen, not to the one section that prints last.
        # What this test still carries is the guard above -- the cached string
        # arrives here, and the builder never opens the store to get it.
        assert DB_LINE in static_text(app, "status")
        assert DB_LINE not in static_text(app, "rp-body")

    drive(app, steps)


def test_db_line_never_runs_on_the_event_loop(monkeypatch):
    # Round 6: the FIRST status paint used to call db_summary() inline — on a
    # real 500k-row store that opened/counted SQLite ON the UI loop and froze
    # the app before the first frame. The read belongs to the thread worker;
    # the loop only ever reads the cached string.
    calls = patch_fetchers(monkeypatch)
    threads = []

    def watched_summary():
        # EVERY call is recorded: a last-writer-wins flag would be masked when
        # the worker's off-loop call overwrites a mutant's inline one.
        threads.append(threading.current_thread() is threading.main_thread())
        return DB_LINE

    monkeypatch.setattr(app_mod, "db_summary", watched_summary)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._db_line is not None)
        await pilot.pause(0.1)  # let any stray second call happen
        assert threads == [False]  # EXACTLY one read, and off the loop
        assert DB_LINE in static_text(app, "status")

    drive(app, steps)


def test_status_shows_pending_until_the_db_line_lands(monkeypatch):
    # The off-loop read buys a one-beat placeholder — never a freeze and
    # never a silent absence: once the read lands, the footer updates itself.
    gate = threading.Event()
    calls = patch_fetchers(monkeypatch)

    def slow_summary():
        assert gate.wait(10.0), "test gate never opened"
        return DB_LINE

    monkeypatch.setattr(app_mod, "db_summary", slow_summary)
    app = TokdashApp("today")

    async def steps(pilot):
        await pilot.pause(0.1)  # db read blocked; the overview load is running
        assert "db status pending" in static_text(app, "status")
        gate.set()
        assert await wait_for(
            app, pilot, lambda: DB_LINE in static_text(app, "status"))

    drive(app, steps)



def test_db_line_landing_mid_report_reload_never_repaints_stale_body(
    monkeypatch,
):
    # The watcher repaints the report footer when its read lands — but ONLY
    # while the pane is idle. The pane's state is sticky across reloads, so an
    # unguarded repaint would re-emit the PREVIOUS window's body right on top
    # of the loading clear (the exact stale-figures violation the reload law
    # forbids). Deterministic interleaving: block the report's fetches, start
    # the reload, THEN land the db line — the body must stay on "computing…"
    # until the reload itself refills it.
    gate = threading.Event()
    gate.set()  # opens for the first load; cleared for the reload below
    db_gate = threading.Event()  # closed: the refresh's db read blocks
    calls = patch_fetchers(monkeypatch, usage_gate=gate)

    def slow_summary():
        assert db_gate.wait(10.0), "test db gate never opened"
        return DB_LINE

    monkeypatch.setattr(app_mod, "db_summary", slow_summary)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        assert "Tokdash report" in static_text(app, "rp-body")
        gate.clear()  # the reload's fetches will block
        await pilot.press("r")  # refresh: reload starts + db re-read fires
        assert await wait_for(app, pilot, lambda: (
            "computing…" in static_text(app, "rp-body")))
        db_gate.set()  # db line LANDS while the reload is still in flight
        for _ in range(20):
            await pilot.pause(0.02)
            body = static_text(app, "rp-body")
            assert "Tokdash report" not in body  # the stale body stays dead
        gate.set()  # let the reload finish; ITS paints refill the body
        assert await wait_for(app, pilot, lambda: (
            "Tokdash report" in static_text(app, "rp-body")))

    drive(app, steps)



def test_markup_hostile_names_render_literally(monkeypatch):
    # Model names flow into the card block, the table cells and the detail
    # panel, all of which parse rich markup: a name like "claude [bold]…" would
    # either raise MarkupError (the pane drops to its error state) or silently
    # restyle. Both surfaces must print it as TEXT.
    usage = copy.deepcopy(USAGE_PAYLOAD)
    usage["top_models"] = [{"name": "claude [bold]1m[/]", "tokens": 8_000_000,
                            "cost": 90.0}]
    usage["combined_models"] = [dict(usage["combined_models"][0],
                                     name="claude [bold]1m[/]")]
    patch_fetchers(monkeypatch, usage=usage)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        assert app.is_running  # no MarkupError → no pane-fail fallback
        assert "claude [bold]1m[/]" in str(app.query_one("#ov-cards").content)
        app.query_one("#ov-tab-models", Button).press()
        await pilot.pause(0.1)
        table = app.query_one("#ov-table", DataTable)
        # The stored cell keeps the escape (a DataTable parses markup in str
        # cells); what matters is that the SCREEN shows the name as typed.
        assert "claude [bold]1m[/]" in screen_text(app)
        assert plain_row(table, 0)[0].startswith("claude ")

    drive(app, steps)



def test_p_is_the_stats_year_key_and_never_moves_the_shared_window(monkeypatch):
    # 'p' is the strip's "More" on the four panes that have a strip, and Stats'
    # year key on the one pane that does not. Neither meaning MOVES a window by
    # itself: the chooser opens and waits, because a letter that quietly
    # re-anchored whatever was on screen is the surprise this strip removes.
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        window_before = app._win
        usage_before = len(calls.usage)
        await pilot.press("p")
        await pilot.pause(0.15)
        # The strip's fifth control is "p More": the window does not change, the
        # list of the rest of them opens.
        assert type(app.screen).__name__ == "PresetPicker", type(app.screen).__name__
        assert app._win == window_before
        assert len(calls.usage) == usage_before  # no reload, no refetch
        await pilot.press("escape")
        await pilot.pause(0.15)

        app.action_show_pane("stats")
        assert await wait_for(app, pilot, lambda: app._state["stats"] == "ok")
        assert app._stats_win.year is None  # trailing 365 is the top rung
        assert "Trailing 365 Days" in static_text(app, "st-scope")
        # 'p' walks DOWN the ladder, and the rung below the trailing window is
        # the calendar year the reader is standing in.
        await pilot.press("p")
        assert app._stats_win.year == TODAY.year
        assert await wait_for(app, pilot, lambda: (TODAY.year, False) in calls.stats)
        assert app._win == window_before  # Stats' year stays inside Stats
        await pilot.press("p")
        assert app._stats_win.year == TODAY.year - 1
        assert await wait_for(app, pilot, lambda: (TODAY.year - 1, False) in calls.stats)
        # 'P' walks back UP, and comes to rest on the trailing rung: a year
        # nobody has lived through is not a window.
        await pilot.press("P")
        assert app._stats_win.year == TODAY.year
        await pilot.press("P")
        assert app._stats_win.year is None
        await pilot.press("P")
        assert app._stats_win.year is None
        assert app._win == window_before

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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "error")
        assert "error: boom" in static_text(app, "ov-note")
        # ONE prefix, not "error: error: boom".
        assert static_text(app, "ov-note").count("error:") == 1
        assert app.is_running  # app stays alive on ordinary worker errors
        await pilot.press("q")

    drive(app, steps)


def test_backpressure_is_busy_line_press_r(monkeypatch):
    from tokdash.api import CacheBackpressureError

    patch_fetchers(monkeypatch, usage=CacheBackpressureError("busy"))
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "error")
        note = static_text(app, "ov-note")
        assert "busy computing — press r" in note
        # Back-pressure is not a fault: the strip says what to do, not "error".
        assert "error:" not in note

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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        await pilot.pause(0.05)
        captured["lines"] = [
            strip.text for strip in app.screen._compositor.render_strips()
        ]
        await pilot.press("1")
        assert await wait_for(
            app, pilot,
            lambda: static_text(app, "status").startswith("/ search · s sort")
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
    # Mutation proof #1: normalization happens AT THE FETCH CALL SITE. A preset
    # never travels as its own name -- an endpoint label the resolver does not
    # know would quietly widen to all time -- so every dated window travels as
    # an explicit (date_from, date_to) pair under the route's default period.
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        # Initial "today": the explicit (D, D) pair _usage_warm_target warms.
        assert calls.usage_full[0] == ("today", "2026-09-20", "2026-09-20")

        seen = len(calls.active_full)
        await pilot.press("d")
        # Active time loads after usage; seeing the usage call does not mean
        # the active-time arguments below belong to the new window yet.
        assert await wait_for(app, pilot, lambda: len(calls.active_full) > seen)
        assert app._win.preset == "yesterday"
        assert calls.usage_full[-1] == ("today", "2026-09-19", "2026-09-19")
        assert calls.active_full[-1] == ("today", "2026-09-19", "2026-09-19", None)

        for key, preset, pair in (
            ("w", "thisweek", ("2026-09-14", "2026-09-20")),  # Monday so far
            ("m", "month", ("2026-09-01", "2026-09-20")),   # calendar month so far
            ("y", "year", ("2026-01-01", "2026-09-20")),    # calendar year so far
        ):
            seen = len(calls.usage)
            await pilot.press(key)
            assert await wait_for(app, pilot, lambda: len(calls.usage) > seen)
            assert app._win.preset == preset, key
            assert calls.usage_full[-1] == ("today", *pair), key

        seen = len(calls.usage)
        await pilot.press("a")
        assert await wait_for(app, pilot, lambda: len(calls.usage) > seen)
        assert app._win.preset == "all"
        assert calls.usage_full[-1] == ("all", None, None)

        seen = len(calls.usage)
        await pilot.press("t")
        assert await wait_for(app, pilot, lambda: len(calls.usage) > seen)
        assert app._win.preset == "today"
        assert calls.usage_full[-1] == ("today", "2026-09-20", "2026-09-20")
        # Keys never force: warm keys recompute on their own.
        assert all(refresh is False for _, refresh in calls.usage)

    drive(app, steps)



def test_range_keys_move_the_shared_window_and_are_inert_elsewhere(monkeypatch):
    # One window for the dated panes: a letter picked on Report re-anchors the
    # Overview too (they read the same window), and the panes with no date axis
    # -- Stats, Quota, Pricing -- refuse the keys rather than pretending.
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")

        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        before = len(calls.usage)
        await pilot.press("y")  # a calendar-year window, asked from Report
        assert await wait_for(app, pilot, lambda: len(calls.usage) > before)
        assert calls.usage_full[-1] == ("today", "2026-01-01", "2026-09-20")
        assert app._win.preset == "year"  # Overview shares what Report asked for

        # "a" is a window like any other now: the old "no such report window"
        # asymmetry belonged to the per-pane period tokens that are gone.
        before = len(calls.usage)
        await pilot.press("a")
        assert await wait_for(app, pilot, lambda: len(calls.usage) > before)
        assert calls.usage_full[-1] == ("all", None, None)

        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
        before = len(calls.usage)
        for key in ("m", "w", "t"):
            await pilot.press(key)
        await pilot.pause(0.15)
        assert len(calls.usage) == before  # Quota answers to no range key
        assert app._win.preset == "all"
        assert app.query_one("#rangebar").display is False

        await pilot.press("1")  # the dated pane takes its strip back
        assert await wait_for(app, pilot, lambda: app._win.preset == "all")
        assert app.query_one("#rangebar").display is True

    drive(app, steps)


# ---------------------------------------------------------------------------
# Round 2: adaptive chart slot
# ---------------------------------------------------------------------------

def test_the_overview_never_borrows_the_annual_read(monkeypatch):
    # /api/stats answers for a trailing year and for nothing else, so the
    # Overview may not ask for it: its figures all carry the LOCAL window's
    # label. Stats owns that route, and its own test drives the year ladder.
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        for key in "dmwya":
            await pilot.press(key)
            await pilot.pause(0.05)
        assert app._win.preset == "all"
        assert not calls.stats
        assert calls.usage and calls.active

    drive(app, steps)



def test_an_empty_annual_window_keeps_the_calendar(monkeypatch):
    # An annual window with no recorded days is a MEASURED zero: the calendar
    # keeps its grid and the summary keeps its dashes. It does not collapse into
    # an empty pane, and it does not invent a day the route never reported.
    empty_stats = {"summary": {}, "contributions": [], "stats": {}}
    patch_fetchers(monkeypatch, stats=empty_stats)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        app.action_show_pane("stats")
        assert await wait_for(app, pilot, lambda: app._state["stats"] == "ok")
        calendar = static_text(app, "st-calendar")
        # The grid is still drawn: month title, weekday row, and every day of
        # the window carrying its number with the "none" glyph.
        assert calendar.strip(), calendar
        assert "September 2026" in calendar, calendar
        assert "M  T  W  T  F  S  S" in calendar, calendar
        # Every day of the window prints its number under the "none" glyph:
        # a day that recorded nothing still shows that it was looked at.
        none_glyph = calendar.split()[0][0]
        packed = calendar.replace(" ", "")
        assert f"{none_glyph}1" in packed and f"{none_glyph}20" in packed, calendar
        assert EM_DASH in static_text(app, "st-summary")  # nothing was measured
        # A window with no days has no rows to invent either.
        assert app.query_one("#st-days", DataTable).row_count == 0

    drive(app, steps)


# ---------------------------------------------------------------------------
# Round 2: Detail table (Tool / Model)
# ---------------------------------------------------------------------------

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
            assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
            pane = app.query_one("#pane-overview")
            assert pane.show_vertical_scrollbar  # content overflows the pane
            base = pane.scroll_y
            # Wheel until the pane answers, within a bound: the scroll is applied
            # on the next refresh, and inside a full suite that refresh can lose
            # the race with a fixed two-hundredth pause. A wheel that never moves
            # the pane still fails here, it just gets leave to be late.
            for _ in range(12):
                pane.post_message(
                    events.MouseScrollDown(pane, 10, 5, 0, 1, 0, False, False, False)
                )
                await pilot.pause(0.05)
                if pane.scroll_y > base:
                    break
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
# The period-shift axis ( [ ] / 0 ) — WHOLE calendar periods, in each pane's
# own unit: month view steps months (one [ = the ENTIRE previous month), week
# view whole weeks, year view whole years, "today" view single days.
# ---------------------------------------------------------------------------


def test_date_shift_brackets_and_zero(monkeypatch):
    # ``[``/``]`` walk the shared window one WHOLE step at a time and ``0``
    # returns it to the present. The window text names the shifted window in
    # exact dates, so a step is never a mystery move.
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        assert calls.usage_full[-1] == ("today", "2026-09-20", "2026-09-20")
        # ] at today is INERT — the window never moves into the future.
        before = len(calls.usage)
        await pilot.press("right_square_bracket")
        await pilot.pause(0.15)
        assert len(calls.usage) == before and app._win.shift == 0

        await pilot.press("left_square_bracket")  # yesterday
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-09-19", "2026-09-19"),
        )
        assert app._win.shift == 1
        assert "2026-09-19" in static_text(app, "range-dates")
        assert "Yesterday" in static_text(app, "range-dates")

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
        assert app._win.shift == 1

        await pilot.press("0")  # 0 = back to today
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-09-20", "2026-09-20"),
        )
        assert app._win.shift == 0
        assert "Yesterday" not in static_text(app, "range-dates")

    drive(app, steps)



def test_period_shift_steps_whole_calendar_periods(monkeypatch):
    # THE BUG this replaced: ``[`` pulled the window's END DATE one day back
    # (a September view 09-01..09-20 became 09-01..09-19). A step is a WHOLE
    # calendar window: from any September day one ``[`` is the ENTIRE previous
    # month, Aug 1 → Aug 31 — never clamped to the anchor day — and ``]`` walks
    # forward into the partial current month again.
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("m")  # calendar month so far
        assert await wait_for(app, pilot, lambda: app._win.preset == "month")
        assert calls.usage_full[-1] == ("today", "2026-09-01", "2026-09-20")
        await pilot.press("left_square_bracket")
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-08-01", "2026-08-31"),
        )
        assert app._win.shift == 1
        assert "Last Month" in static_text(app, "range-dates")

        await pilot.press("left_square_bracket")  # two months back: July, full
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-07-01", "2026-07-31"),
        )
        await pilot.press("right_square_bracket")  # forward one whole period
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-08-01", "2026-08-31"),
        )
        await pilot.press("0")  # 0 = the shared default: Today
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-09-20", "2026-09-20"),
        )
        assert app._win.preset == "today" and app._win.shift == 0
        # The preset itself is one key away, and lands on its own current window.
        await pilot.press("m")
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-09-01", "2026-09-20"),
        )

    drive(app, steps)



def test_rolling_seven_days_steps_a_whole_seven_days(monkeypatch):
    # A rolling window steps by its own inclusive length: one ``[`` from the
    # last seven days is the seven days before them, never six and never the
    # calendar week (which is its own named preset).
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("week")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        assert app._win.preset == "7d"
        assert calls.usage_full[-1] == ("today", "2026-09-14", "2026-09-20")
        await pilot.press("left_square_bracket")
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-09-07", "2026-09-13"),
        )
        assert app._win.days == 7
        assert "2026-09-07" in static_text(app, "range-dates")
        # ...and the calendar week is still a window of its own, one key away.
        await pilot.press("0")
        assert app._win.preset == "today"

    drive(app, steps)



def test_the_shared_window_steps_the_visible_pane_and_marks_the_rest(monkeypatch):
    # A pane left standing on the old window would show another window's data,
    # so a date step has to reach every pane that reads the range. It does that
    # by RELOADING the one on screen and marking the visited-but-hidden ones out
    # of date: refetching them in the background used to include Speed's read,
    # which can queue a timing build for a view nobody is looking at.
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        await pilot.press("1")  # visit Report, then leave it hidden
        assert await wait_for(app, pilot, lambda: app._active_pane() == "overview")
        before = len(calls.usage)
        await pilot.press("left_square_bracket")
        assert await wait_for(
            app, pilot,
            lambda: ("today", "2026-09-19", "2026-09-19") in calls.usage_full,
        )
        assert app._win.label == "Yesterday" and app._win.shift == 1
        # The active-time job carried the pair too.
        assert any(t[:3] == ("today", "2026-09-19", "2026-09-19")
                   for t in calls.active_full)
        # Only the visible pane re-read; Report waits for the tab.
        assert len(calls.usage) == before + 1, calls.usage
        assert "report" in app._stranded
        # Going back pays the debt at the NEW window, and the body prints the
        # window the strip names.
        await pilot.press("2")
        assert await wait_for(
            app, pilot, lambda: "2026-09-19" in static_text(app, "rp-body")
        )
        assert "report" not in app._stranded
        assert len(calls.usage) >= before + 2, calls.usage

    drive(app, steps)


def test_date_shift_inert_on_rolling_all(monkeypatch):
    # "all" has no window to step — [ is FULLY inert: no counter (a stored
    # shift would silently start a later Report lazy-load already shifted by
    # presses that visibly did nothing), no gen bump (a bump with no
    # successor would strand the in-flight load), no body churn.
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("all")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        assert app._win.preset == "all"
        before = len(calls.usage)
        gen_before = app._gen
        await pilot.press("left_square_bracket")
        await pilot.press("left_square_bracket")
        await pilot.pause(0.15)
        assert app._win.shift == 0
        assert app._gen == gen_before  # no supersede: the finished load stands
        assert len(calls.usage) == before  # no Overview reload fired
        assert calls.usage[-1] == ("all", False)
        assert app._state["overview"] == "ok" and not app._stranded

    drive(app, steps)


def test_shift_during_inflight_all_load_does_not_strand_the_pane(monkeypatch):
    # Reviewer finding: ``[`` while the (windowless) Overview load is in
    # flight bumped the gen and superseded it with NO successor — the visible
    # pane was stranded on "computing…" forever. The inert step now returns
    # BEFORE the gen bump, so the in-flight load lands normally.
    gate = threading.Event()
    calls = patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("all")

    async def steps(pilot):
        await pilot.pause(0.05)  # first load started, blocked on the gate
        await pilot.press("left_square_bracket")
        await pilot.press("right_square_bracket")
        gate.set()
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        assert not app._stranded
        assert calls.usage  # the ORIGINAL load's result still stands

    drive(app, steps)



def test_a_superseded_load_on_the_visible_pane_is_re_issued_at_once(monkeypatch):
    # The visible half of the stranded law: when a gen bump cancels the load of
    # the pane on screen and nothing else reloads it, the pane must get a fresh
    # load immediately rather than wait for a tab round-trip. Revision 5 gives
    # each pane its own keys, so no single keystroke bumps the generation out
    # from under a Quota load any more -- the bump is delivered directly, which
    # is what the race between two panes actually looks like.
    gate = threading.Event()
    calls = patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        await pilot.pause(0.05)
        await pilot.press("3")  # Quota tab: its first load starts, blocked
        await pilot.pause(0.1)
        assert "quota" in app._inflight or app._open_loads.get("quota")
        # Overview has to be MID-FLIGHT for the bump to supersede anything. On a
        # busy runner its worker can still be queued here, and a load that STARTS
        # after the bump belongs to the new generation -- nothing to strand, so
        # the last assertion below would be checking nothing.
        assert await wait_for(app, pilot, lambda: "overview" in app._inflight)
        app._gen += 1  # the bump a rival pane's reload would deliver
        gate.set()
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
        assert "quota" not in app._stranded  # the visible pane was served again
        assert len(calls.quota_state) >= 2  # superseded flight + re-issue
        # Overview — superseded by the same bump but OUT OF SIGHT — correctly
        # waits for the tab hook instead of reloading behind the user's back. It
        # gets there when the blocked thread wakes, which is not tied to when
        # Quota's re-issue lands, so the bookkeeping is awaited rather than
        # inferred from the other pane's timing.
        assert await wait_for(app, pilot, lambda: "overview" in app._stranded)
        assert len(calls.usage) == 1  # no re-read while the pane sat unwatched

    drive(app, steps)



def test_reload_resets_late_payload_slots(monkeypatch):
    # The loading law at the SLOT level: the late-joining payloads (Overview
    # active time, Report insights) keep the PREVIOUS window's dicts unless the
    # load START drops them — otherwise the mid-reload usage repaint mixes last
    # window's Time/KPI/insight figures into the new one.
    calls = patch_fetchers(monkeypatch)
    slow = threading.Event()  # CLOSED: the re-patched fetcher below blocks

    def gated(payload):
        def fetch(*a, **k):
            assert slow.wait(10.0), "test gate never opened"
            return _outcome(payload)
        return fetch

    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        assert app._ov_active is not None
        table = app.query_one("#ov-table", DataTable)
        assert plain_row(table, 0)[-1] == "50m"  # codex Time from the payload
        monkeypatch.setattr(app_mod, "fetch_active_time",
                            gated(ACTIVE_TIME_PAYLOAD))
        await pilot.press("w")  # a window change reloads Overview
        assert await wait_for(app, pilot, lambda: app._ov_active is None)
        # Usage repaints while active is still in flight: the Time column is
        # DASHES, not the previous window's "50m".
        assert await wait_for(app, pilot, lambda: (
            table.row_count > 0 and plain_row(table, 0)[-1] == EM_DASH))
        slow.set()
        # NOTE: the pane state stays "ok" across reloads (only failures move it)
        # — completion must be watched at the SLOT, never at the state.
        assert await wait_for(app, pilot, lambda: (
            app._ov_active is not None and app._state["overview"] == "ok"))
        assert await wait_for(
            app, pilot, lambda: plain_row(table, 0)[-1] == "50m")

        # The Report half: a blocked insights fetch must put the "running…"
        # note back (pending = slots dropped at load start), and the slots
        # refill when it lands.
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        monkeypatch.setattr(app_mod, "fetch_insights",
                            gated(INSIGHTS_PAYLOAD))
        slow.clear()  # CLOSE the gate again (the overview half opened it)
        await pilot.press("y")  # the shared window widens: Report reloads
        assert await wait_for(app, pilot, lambda: app._rp_insights is None)
        assert await wait_for(app, pilot, lambda: (
            "running…" in static_text(app, "rp-body")))
        slow.set()
        assert await wait_for(app, pilot, lambda: (
            app._rp_insights is not None and app._state["report"] == "ok"))

    drive(app, steps)




def test_period_shift_year_steps_one_whole_year(monkeypatch):
    # Mutation lock for the New Year gotcha: ONE ``[`` on a calendar-year window
    # lands on the FULL previous year (2025-01-01 → 2025-12-31 — under the old
    # day-shift this took 371 presses), and it does so on 5 January too, when
    # the current window is only five days long.
    calls = patch_fetchers(monkeypatch)
    monkeypatch.setattr(app_mod, "_local_today",
                        lambda: datetime.date(2026, 1, 5))
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("y")  # the calendar year so far
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2026-01-01", "2026-01-05"),
        )
        await pilot.press("left_square_bracket")
        assert await wait_for(
            app, pilot,
            lambda: calls.usage_full[-1] == ("today", "2025-01-01", "2025-12-31"),
        )
        assert app._win.days == 365
        assert "Last Year" in static_text(app, "range-dates")
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")

    drive(app, steps)


def test_day_rollover_preserves_the_shift(monkeypatch):
    holder = {"today": TODAY}
    calls = patch_fetchers(monkeypatch)
    monkeypatch.setattr(app_mod, "_local_today", lambda: holder["today"])
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("left_square_bracket")
        assert await wait_for(app, pilot, lambda: app._win.shift == 1)
        holder["today"] = TODAY + datetime.timedelta(days=1)
        before = len(calls.usage)
        await pilot.press("r")  # midnight crossed; detected on the keypress
        assert await wait_for(app, pilot, lambda: len(calls.usage) > before)
        assert app._today == holder["today"]
        # "yesterday" is the user's standing intent — midnight must not silently
        # reset it; the shifted window moves with the new day instead.
        assert app._win.shift == 1
        assert calls.usage_full[-1] == ("today", "2026-09-20", "2026-09-20")
        assert "Yesterday" in static_text(app, "range-dates")

    drive(app, steps)


# ---------------------------------------------------------------------------
# Round 3: tools-table Time column — the null-vs-zero OVERRIDE (dash for 0)
# ---------------------------------------------------------------------------


def test_harness_table_keeps_a_measured_zero_apart_from_no_measurement(monkeypatch):
    # The null-vs-zero law, Time column: a MEASURED 0 prints "0s" and a tool the
    # active-time scan never answered prints EM_DASH. Revision 5 restored the
    # difference on purpose -- a dash for a measured zero made "we timed you and
    # you did nothing" indistinguishable from "we have no timing for you" -- and
    # the piped report agrees.
    # openclaw is a usage-only source: no apps entry, so Input/Output/Cache/Msgs
    # dash and the hit rate says n/a rather than 0%.
    usage = copy.deepcopy(USAGE_PAYLOAD)
    usage["by_tool"]["openclaw"] = {"tokens": 500_000, "cache_hit_rate": None}
    active = copy.deepcopy(ACTIVE_TIME_PAYLOAD)
    active["by_tool"]["claude"]["active_ms_sum"] = 0  # measured zero
    del active["by_tool"]["codex"]  # no active row at all
    patch_fetchers(monkeypatch, usage=usage, active=active)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        table = app.query_one("#ov-table", DataTable)
        at = {label: i for i, label in enumerate(headers_of(table))}
        assert "Input" in at, headers_of(table)  # the wide pane shows the split
        rows = {
            cell(table.get_row_at(i)[0]): plain_row(table, i)
            for i in range(table.row_count)
        }
        assert rows["Codex"][at["Est. time"]] == EM_DASH  # never timed at all
        assert rows["Claude Code"][at["Est. time"]] == "0s"  # timed, and zero
        oc = rows["OpenClaw"]
        assert oc[at["Input"]] == oc[at["Output"]] == oc[at["Cache"]] == EM_DASH
        assert oc[at["Msgs"]] == EM_DASH
        assert oc[at["Hit"]] == "n/a"  # the formatting law's own sentinel
        assert oc[at["Cost"]] == EM_DASH  # no cost → dash (fmt_cost_row)
        assert oc[at["Tokens"]] == "500.0K"  # the gap never eats the total

    drive(app, steps, size=(140, 40))


# ---------------------------------------------------------------------------
# Round 2: Quota pane
# ---------------------------------------------------------------------------


def _quota_cell(table, label, header):
    """One quota cell by column LABEL, at the row whose provider matches."""
    at = {str(c.label): i for i, c in enumerate(table.ordered_columns)}
    for row in range(table.row_count):
        cells = table.get_row_at(row)
        if cell(cells[0]) == label:  # cell() reads the label off the badge
            return cells[at[header]]
    raise AssertionError(f"no quota row for {label!r}")



def test_quota_tab_lazy_once_and_renders_companion_cells(monkeypatch):
    # Mutation proof: the load is lazy (nothing fetched before "3") and fires
    # EXACTLY once (re-activation must not re-fetch). The table itself keeps the
    # companion's readings: provider grouping with spacers between groups, a
    # normalized window label, "LEFT" phrasing with the meter, and the ok/not-ok
    # status colours. Credits live in their own section, never as a row.
    calls = patch_fetchers(monkeypatch)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        assert not calls.quota_state and not calls.quota_history  # lazy
        await pilot.press("3")
        assert app.query_one("#tabs").active == "pane-quota"
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
        assert len(calls.quota_state) == 1 and len(calls.quota_history) == 1
        # Widening the terminal re-fits the table: the optional columns come
        # back, so a squeeze can never outlive the narrow window that caused it.
        await pilot.resize_terminal(140, 40)
        await pilot.pause(0.1)
        # Banner runs are styled separately ("[bold]quota [/][green]enabled…")
        # — assert the enabled reading + the poll interval, not a joined string.
        note = static_text(app, "quota-note")
        assert "enabled" in note and "poll every 30 min" in note

        table = app.query_one("#quota-table", DataTable)
        headers = headers_of(table)
        assert headers == ["Provider", "Window", "Remaining", "Resets in", "24h",
                           "Status"], headers  # the wide pane shows all six
        rows = [plain_row(table, i) for i in range(table.row_count)]
        # Provider grouping: claude OPENS (claude@work) and its second account
        # CONTINUES ("@home"), then an ALL-EMPTY spacer, codex, spacer, kimi.
        assert table.row_count == 6
        assert not [r for r in rows if r[1] == "credits"]
        spacers = [i for i, r in enumerate(rows) if not any(str(c).strip() for c in r)]
        assert spacers == [2, 4]  # between groups only — never first/last
        codex = next(r for r in rows if r[0] == "codex" and r[1] == "5-hour")
        assert "58.0% left" in codex[2] and "█" * 6 in codex[2]  # LEFT, not used
        assert codex[1] == "5-hour"  # normalized, not raw "5-hour window"
        assert len(codex[4]) == 3  # 3 history points → 3 trend glyphs
        # Companion status colors: ok GREEN, anything else RED. Spacer rows
        # carry no status and stay plain (no red-Text override).
        assert _quota_cell(table, "codex", "Status").plain == "ok"
        assert _quota_cell(table, "codex", "Status").style == "green"
        assert _quota_cell(table, "kimi", "Status").plain == "unavailable"
        assert _quota_cell(table, "kimi", "Status").style == "red"
        assert not any(str(c).strip() for c in table.get_row_at(spacers[0]))
        assert any(r[0] == "claude@work" for r in rows)  # group opener folded
        assert any(r[0] == "@home" for r in rows)  # bare-account continuation
        # Runs are styled separately ("[bold]reset credits [/][green]2 …"), so
        # assert the fragments, not a joined string (banner-test precedent).
        credits = static_text(app, "quota-credits")
        assert "reset credits" in credits and "2 available" in credits
        assert "Full reset" in credits and "expires" in credits

        await pilot.press("1")
        await pilot.press("3")
        await pilot.pause(0.2)
        assert len(calls.quota_state) == 1  # lazy-ONCE
        assert "u poll" in static_text(app, "status")

    drive(app, steps, size=(140, 40))



def test_quota_status_stale_token_is_red(monkeypatch):
    # The reported pairing: stale_token RED, ok GREEN (companion colors). The
    # lookup is by column LABEL because a narrow terminal legitimately drops the
    # optional columns -- which must never change what a status LOOKS like.
    payload = _quota_state_payload()
    payload["providers"]["codex"]["status"] = "stale_token"
    patch_fetchers(monkeypatch, quota_state=payload)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
        table = app.query_one("#quota-table", DataTable)
        assert "Status" in headers_of(table)
        stale = _quota_cell(table, "codex", "Status")
        assert stale.plain == "stale_token" and stale.style == "red"
        fresh = _quota_cell(table, "claude@work", "Status")
        assert fresh.plain == "ok" and fresh.style == "green"

    # A squeezed pane loses the OPTIONAL columns (Status among them) before it
    # touches a number, so the colour law is checked where the column lives.
    drive(app, steps, size=(140, 40))


def test_quota_credits_section_hidden_without_credits(monkeypatch):
    payload = _quota_state_payload()
    del payload["providers"]["codex"]["reset_credits"]
    patch_fetchers(monkeypatch, quota_state=payload)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
        assert app.query_one("#quota-credits").display is False

    drive(app, steps)


def test_quota_stranded_recovery(monkeypatch):
    gate = threading.Event()
    gate.set()
    calls = patch_fetchers(monkeypatch, usage_gate=gate)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        gate.clear()
        await pilot.press("3")            # quota load starts, blocks on gate
        await pilot.pause(0.1)
        await pilot.press("1")            # back to Overview
        await pilot.press("t")            # Overview-side gen bump: supersede
        gate.set()
        assert await wait_for(
            app, pilot,
            lambda: "quota" in app._stranded and app._state["quota"] == "empty",
        )
        before = len(calls.quota_state)
        await pilot.press("3")            # re-activation must re-issue it
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
        assert len(calls.quota_state) > before

    drive(app, steps)



def test_schema_too_new_from_quota_exits_one(monkeypatch):
    err = UsageDatabaseSchemaTooNewError(path="/tmp/usage.sqlite3", found=99,
                                         supported=9)
    patch_fetchers(monkeypatch, quota_state=err)
    app = TokdashApp("today")

    async def steps(pilot):
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        # This load intentionally shuts the app down. Pilot.press/pause wait
        # for screen messages that shutdown can discard, so do not await a
        # rendering barrier after triggering the fatal error. The ordinary
        # quota tests above already cover the "3" key binding.
        app.action_show_pane("quota")  # the pane name, not the widget id

        async def exited():
            while app.return_code != 1:
                await asyncio.sleep(0.01)

        # Still exercise the real quota worker and assert the terminal status.
        await asyncio.wait_for(exited(), timeout=5)

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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
        note = app.query_one("#quota-note", Static)
        # Every write to the note, with whether a poll was running when it came.
        updates = []
        real_update = note.update
        monkeypatch.setattr(
            note, "update",
            lambda content: (updates.append((app._polling, str(content))),
                             real_update(content))[1],
        )
        # "ok" is not "quiet". The load that just finished leaves a refit behind
        # it (_end_load -> call_after_refresh -> _refit_body), and that repaint
        # writes the pre-poll banner -- rightly, since nothing has been pressed.
        # On a slow runner it lands here, after the spy and ahead of the "u"
        # queued behind it. This repaint stands in for it, so the interleaving
        # that failed on Windows runs every time instead of now and then.
        app._repaint_pane("quota")
        before = len(calls.quota_state)
        await pilot.press("u")
        # The reload has LANDED, not merely started: its fetch is counted when it
        # is called, a loop turn before the answer is painted over "computing…".
        assert await wait_for(
            app, pilot,
            lambda: len(calls.quota_state) == before + 1
            and not app._open_loads.get("quota"),
        )
        assert seen["include_network"] is True   # mirrors `tokdash quota poll`
        assert seen["network_sources"] is None
        assert seen["note_at_call"] == "polling…"  # the note LED the result
        # The order is the poll's own, read from the first write the poll made.
        start = [polling for polling, _ in updates].index(True)
        notes = [text for _, text in updates[start:]]
        ok_idx = notes.index("poll ok")
        assert notes[0] == "polling…"            # order: polling → poll ok
        assert ok_idx < len(notes) - 1           # …then the reload repainted
        assert "enabled" in notes[-1]            # the fresh state's banner
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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
        before = len(calls.quota_state)
        await pilot.press("u")
        assert await wait_for(
            app, pilot,
            lambda: "quota tracking disabled" in static_text(app, "quota-note"),
        )
        assert "tokdash quota consent" in static_text(app, "quota-note")
        assert "poll ok" not in static_text(app, "quota-note")
        # The note rides emit_markup like every other pane text: "warn" is a
        # SEMANTIC run name, and raw markup showed the literal "[warn]" tag
        # (it is not a Textual style). The rendered markup carries the
        # TRANSLATED style only.
        note_text = static_text(app, "quota-note")
        assert "[warn]" not in note_text
        assert "[yellow]" in note_text
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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("3")
        assert await wait_for(app, pilot, lambda: app._state["quota"] == "ok")
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
        assert await wait_for(app, pilot, lambda: app._state["overview"] == "ok")
        await pilot.press("2")
        assert await wait_for(app, pilot, lambda: app._state["report"] == "ok")
        # The first load paints can legitimately race the activation layout
        # (instant canned fetches finish before Textual lays the pane out) —
        # then it is the documented un-laid-out 80 fallback; either way the
        # round-1 FIXED 72 is gone (a 78/76 paint proves dynamic reading).
        assert widths and all(w != 72 for w in widths)
        await pilot.pause(0.1)              # let the pane lay out
        await pilot.press("s")              # report-side sort cycle → repaint
        assert await wait_for(app, pilot, lambda: len(widths) >= 3)
        # Laid out: the BUILDER width equals the pane's content width
        # (padding and any scrollbar included — asserted against the live
        # geometry, so it cannot rot with theme details). 78/76 ≠ 72: dynamic.
        assert widths[-1] == _clamp_report_width(pane_content_width())
        assert widths[-1] != 72
        await pilot.resize_terminal(300, 40)
        await pilot.pause(0.1)
        await pilot.press("s")
        assert await wait_for(app, pilot, lambda: widths[-1] == 200)  # ceiling
        await pilot.resize_terminal(60, 30)
        await pilot.pause(0.1)
        await pilot.press("s")
        assert await wait_for(app, pilot, lambda: widths[-1] == 60)   # floor

    drive(app, steps)
