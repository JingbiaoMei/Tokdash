#!/usr/bin/env python3
"""Render the shipped TUI to text and SVG captures, for review against the design.

    TOKDASH_TUI_NO_REMOTE=1 PYTHONPATH=src python3 scripts/capture_tui_renders.py

Every pane is driven by a real Textual pilot over the contract-shaped payloads
the revision-5 suite already pins (tests/test_tui_revision5.py), with the fetch
seams patched -- so a capture is what the widgets actually draw, clipping and
all, not a styled mockup. The fixture data is SYNTHETIC; the prices are made up.

Two files per case in output/tui-rev5-captures/:

* ``.txt``  the terminal's own cells, one line per row (what a user reads, and
            the only honest check that a number is not cut in half);
* ``.svg``  Textual's own screenshot of the same frame, colour included.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
from pathlib import Path

os.environ.setdefault("TOKDASH_TUI_NO_REMOTE", "1")
os.environ.setdefault("TOKDASH_DATA_DIR", "/tmp/tui-rev5-capture-data")
# NO_COLOR is not a tokdash setting. Textual reads it when the App is built and
# installs a Monochrome line filter that bakes greyscale into every rendered
# strip, SVG screenshot included. A capture of the palette must be taken without
# it; the monochrome cases put it back on purpose, per app.
os.environ.pop("NO_COLOR", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
OUT = ROOT / "output" / "tui-rev5-captures"

import test_tui_revision5 as fx  # noqa: E402  (contract-shaped fixtures)
import tokdash.tui.app as app_mod  # noqa: E402
from tokdash.tui.app import TokdashApp  # noqa: E402


FAKE: fx.FakeApi | None = None  # the current frame's fake, for cases that edit it


def install_fakes() -> fx.FakeApi:
    """Answer every read from the fixtures, exactly as the suite does."""
    global FAKE
    fake = fx.FakeApi()
    FAKE = fake
    seams = {
        "fetch_usage": ("usage", lambda *a, **k: fake.usage),
        "fetch_active_time": ("active", lambda *a, **k: fake.active),
        "fetch_sessions": ("sessions", fake.sessions_for),
        "fetch_session_detail": ("session_detail", lambda *a, **k: fake.session_detail),
        "fetch_session_speed": ("session_speed", lambda *a, **k: fake.session_speed),
        "fetch_stats": ("stats", fake.stats_for),
        "fetch_insights": ("insights", lambda *a, **k: fake.insights),
        "fetch_output_speed": ("speed", fake.speed_for),
        "fetch_output_speed_job_status": ("job_status", lambda *a, **k: fake.job_status),
        "fetch_quota_state": ("quota", lambda *a, **k: fake.quota),
        "fetch_quota_history": ("quota_history", lambda *a, **k: fake.quota_history),
        "fetch_pricing_db": ("pricing", lambda *a, **k: fake.pricing),
    }
    for name, (key, pick) in seams.items():
        setattr(app_mod, name, fake.seam(key, pick))
    app_mod.follow_output_speed_job = fake.follow
    app_mod.db_summary = lambda: "usage db disabled (TOKDASH_USAGE_DB=0) - live parsing"
    app_mod.ensure_usage_db_compatible = lambda: None
    app_mod._local_today = lambda: fx.TODAY
    return fake


def screen_lines(app) -> list[str]:
    return [strip.text for strip in app.screen._compositor.render_strips(app.screen.size)]


def capture(name: str, size: tuple[int, int], body, *, initial: str = "today",
            mono: bool = False) -> None:
    """One pilot run, one frame, two files."""
    if mono:  # a real terminal asks for this with NO_COLOR=1
        os.environ["NO_COLOR"] = "1"
    else:
        os.environ.pop("NO_COLOR", None)
    app = TokdashApp(initial_period=initial)

    async def main():
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.6)
            await fx.settle(pilot, 3)
            if body is not None:
                await body(app, pilot)
                await fx.settle(pilot, 2)
            if mono:
                app.no_color = True  # drives the {nocolor} CSS variable as well
                app._repaint_pane(app._active_pane())
                await fx.settle(pilot, 1)
            rows = screen_lines(app)
            (OUT / f"{name}.txt").write_text(
                "\n".join(row.rstrip() for row in rows) + "\n", encoding="utf-8"
            )
            (OUT / f"{name}.svg").write_text(app.export_screenshot(), encoding="utf-8")
            print(f"{name:38} {size[0]:3}x{size[1]:<3} {len(rows)} rows")

    asyncio.run(main())


# --------------------------------------------------------------- interactions --

async def week(app, pilot):
    """The preview's seven-day window. Production opens on Today; the range
    strip and every pane re-anchor to whatever is selected."""
    from textual.widgets import Button
    app.query_one("#range-7d", Button).press()
    await fx.settle(pilot, 4)


async def models(app, pilot):
    await week(app, pilot)
    app.query_one("#ov-tab-models").press()
    await fx.settle(pilot, 2)


async def models_by_cost(app, pilot):
    await models(app, pilot)
    await pilot.press("s")  # tokens -> cost: the SAME table, re-sorted
    await fx.settle(pilot, 2)


async def harness_detail(app, pilot):
    await week(app, pilot)
    app.query_one("#ov-table").focus()
    await pilot.press("enter")
    await fx.settle(pilot, 2)


async def overview_search(app, pilot):
    await week(app, pilot)
    await pilot.press("/")
    for ch in "cod":
        await pilot.press(ch)
    await fx.settle(pilot, 2)


async def overview_scrolled(app, pilot):
    await week(app, pilot)
    table = app.query_one("#ov-table")
    table.focus()
    for _ in range(6):
        await pilot.press("down")
    await fx.settle(pilot, 2)


async def sessions(app, pilot):
    await week(app, pilot)
    app.action_show_pane("sessions")
    await fx.settle(pilot, 5)


async def session_detail(app, pilot):
    await sessions(app, pilot)
    app.query_one("#sn-table").focus()
    await pilot.press("enter")
    await fx.settle(pilot, 5)


async def sessions_search(app, pilot):
    await sessions(app, pilot)
    await pilot.press("/")
    for ch in "tok":
        await pilot.press(ch)
    await fx.settle(pilot, 3)


async def stats(app, pilot):
    app.action_show_pane("stats")
    await fx.settle(pilot, 4)
    await pilot.press("p")  # trailing year -> the named calendar year
    await fx.settle(pilot, 4)


async def stats_narrow(app, pilot):
    app.action_show_pane("stats")
    await fx.settle(pilot, 4)
    await pilot.press("p")
    await fx.settle(pilot, 3)
    await pilot.press("left_square_bracket")  # one month back, at 80 columns
    await fx.settle(pilot, 2)


async def report(app, pilot):
    await week(app, pilot)
    app.action_show_pane("report")
    await fx.settle(pilot, 5)


async def report_when(app, pilot):
    await report(app, pilot)
    app.query_one("#rp-tab-when").press()
    await fx.settle(pilot, 2)


async def report_projects(app, pilot):
    await report(app, pilot)
    app.query_one("#rp-tab-projects").press()
    await fx.settle(pilot, 2)


async def speed(app, pilot):
    await week(app, pilot)
    app.action_show_pane("speed")
    await fx.settle(pilot, 5)


async def speed_combined(app, pilot):
    await speed(app, pilot)
    app.query_one("#sp-lens-combined").press()
    await fx.settle(pilot, 5)


async def speed_trend(app, pilot):
    await speed(app, pilot)
    app.query_one("#sp-mode-trend").press()
    await fx.settle(pilot, 5)


async def speed_hour(app, pilot):
    await speed(app, pilot)
    app.query_one("#sp-mode-hour").press()
    await fx.settle(pilot, 5)


async def speed_detail(app, pilot):
    await speed(app, pilot)
    app.query_one("#sp-table").focus()
    await pilot.press("enter")
    await fx.settle(pilot, 5)


async def quota(app, pilot):
    app.action_show_pane("quota")
    await fx.settle(pilot, 4)


async def pricing(app, pilot):
    app.action_show_pane("pricing")
    await fx.settle(pilot, 4)
    await pilot.press("/")
    for ch in "gpt":
        await pilot.press(ch)
    await fx.settle(pilot, 2)


async def help_panel(app, pilot):
    await pilot.press("question_mark")
    await fx.settle(pilot, 2)


async def view_picker(app, pilot):
    await pilot.press("v")
    await fx.settle(pilot, 2)


# ---- the workflows the review found broken, now that they work -------------

async def sessions_sources(app, pilot):
    """``h`` opens a chooser over every supported source, marked where it is."""
    await sessions(app, pilot)
    await pilot.press("h")
    await fx.settle(pilot, 2)


async def session_turns(app, pilot):
    """Whole-session detail: every turn, and the arrows reach the last one."""
    detail = copy.deepcopy(fx.SESSION_DETAIL)
    detail["turns"] = [
        dict(detail["turns"][0], turn_index=index, model=f"unique-model-{index}")
        for index in range(1, 21)
    ]
    FAKE.session_detail = detail
    await session_detail(app, pilot)
    for _ in range(19):  # Pilot has no repeat= in this Textual
        await pilot.press("down")
    await fx.settle(pilot, 3)


async def quota_detail(app, pilot):
    """Enter on a meter names the account behind it and what that account says."""
    await quota(app, pilot)
    table = app.query_one("#quota-table")
    table.focus()
    rows = [table.get_row_at(row) for row in range(table.row_count)]
    target = next(
        (i for i, row in enumerate(rows) if any("academic" in str(c) for c in row)), 0
    )
    table.move_cursor(row=target)
    await pilot.press("enter")
    await fx.settle(pilot, 2)
    app.query_one("#quota-detail").scroll_visible()
    await fx.settle(pilot, 2)


async def report_projects_cost(app, pilot):
    """``s`` re-ranks the same list; the cost leader moves to the top."""
    await report_projects(app, pilot)
    await pilot.press("s")
    await fx.settle(pilot, 3)


async def stats_this_year(app, pilot):
    """One ``p`` from the trailing window to the calendar year in progress."""
    app.action_show_pane("stats")
    await fx.settle(pilot, 4)
    await pilot.press("p")
    await fx.settle(pilot, 4)


async def speed_whole_request(app, pilot):
    """``x`` widens the timing set and the note says which set is in force."""
    await speed(app, pilot)
    await pilot.press("x")
    await fx.settle(pilot, 3)


async def speed_combined_hour(app, pilot):
    """Hour of day for a MERGED model: its own 24 hours, not the time buckets."""
    await speed_combined(app, pilot)
    app.query_one("#sp-mode-hour").press()
    await fx.settle(pilot, 4)


CASES = [
    # name, size, body, initial period, monochrome
    ("01-overview-today-120x36", (120, 36), None, "today", False),
    ("02-overview-7d-120x36", (120, 36), week, "today", False),
    ("03-overview-models-120x36", (120, 36), models, "today", False),
    ("04-overview-models-by-cost-120x36", (120, 36), models_by_cost, "today", False),
    ("05-overview-harness-detail-120x36", (120, 36), harness_detail, "today", False),
    ("06-overview-search-120x36", (120, 36), overview_search, "today", False),
    ("07-overview-scrolled-120x36", (120, 36), overview_scrolled, "today", False),
    ("08-overview-80x24", (80, 24), week, "today", False),
    ("09-overview-100x30", (100, 30), week, "today", False),
    ("10-sessions-100x30", (100, 30), sessions, "today", False),
    ("11-session-detail-100x30", (100, 30), session_detail, "today", False),
    ("12-sessions-search-100x30", (100, 30), sessions_search, "today", False),
    ("13-sessions-80x24", (80, 24), sessions, "today", False),
    ("14-stats-year-100x30", (100, 30), stats, "today", False),
    ("15-stats-80x24", (80, 24), stats_narrow, "today", False),
    ("16-report-summary-100x30", (100, 30), report, "today", False),
    ("17-report-when-100x30", (100, 30), report_when, "today", False),
    ("18-report-projects-100x30", (100, 30), report_projects, "today", False),
    ("19-report-80x24", (80, 24), report, "today", False),
    ("20-speed-across-100x30", (100, 30), speed, "today", False),
    ("21-speed-combined-100x30", (100, 30), speed_combined, "today", False),
    ("22-speed-trend-100x30", (100, 30), speed_trend, "today", False),
    ("23-speed-hour-100x30", (100, 30), speed_hour, "today", False),
    ("24-speed-detail-100x30", (100, 30), speed_detail, "today", False),
    ("25-speed-80x24", (80, 24), speed, "today", False),
    ("26-quota-100x30", (100, 30), quota, "today", False),
    ("27-quota-80x24", (80, 24), quota, "today", False),
    ("28-pricing-100x30", (100, 30), pricing, "today", False),
    ("29-overview-monochrome-120x36", (120, 36), week, "today", True),
    ("30-sessions-monochrome-80x24", (80, 24), sessions, "today", True),
    ("31-overview-year-120x36", (120, 36), None, "year", False),
    ("32-help-panel-100x30", (100, 30), help_panel, "today", False),
    ("33-view-picker-80x24", (80, 24), view_picker, "today", False),
    ("34-sessions-sources-100x30", (100, 30), sessions_sources, "today", False),
    ("35-session-turns-100x30", (100, 30), session_turns, "today", False),
    ("36-session-turns-80x24", (80, 24), session_turns, "today", False),
    ("37-quota-account-detail-100x30", (100, 30), quota_detail, "today", False),
    ("38-report-projects-by-cost-100x30", (100, 30), report_projects_cost, "today", False),
    ("39-report-when-80x24", (80, 24), report_when, "today", False),
    ("40-stats-this-year-100x30", (100, 30), stats_this_year, "today", False),
    ("41-speed-whole-request-100x30", (100, 30), speed_whole_request, "today", False),
    ("42-speed-combined-hour-100x30", (100, 30), speed_combined_hour, "today", False),
    ("43-overview-month-120x36", (120, 36), None, "month", False),
]


def main() -> int:
    wanted = set(sys.argv[1:])
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size, body, initial, mono in CASES:
        if wanted and name not in wanted:
            continue
        install_fakes()  # a fresh fake per frame: no state bleeds between captures
        capture(name, size, body, initial=initial, mono=mono)
    print(f"\ncaptures in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
