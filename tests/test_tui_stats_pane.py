"""The Stats pane: an annual view that owns its window.

Stats never reads the shared range strip. It names its own window -- the trailing
365 days, the year in progress, or a named calendar year walked to with ``p`` and
``P`` -- asks the endpoint for exactly that year, puts the annual figures on
cards, and pages its calendar over the months that window covers and no others.
Enter on a day is the one place it hands a window back: the shared one, opened on
the Overview. Pilots over the contract-shaped payloads in ``support.tui``.
"""
from __future__ import annotations

import copy
import datetime

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import DataTable  # noqa: E402

import tokdash.tui.app as app_mod  # noqa: E402
import support.tui as fx  # noqa: E402  (the layout-law tests call it fx)
from support.tui import (  # noqa: E402
    STATS_TRAILING,
    TODAY,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    rows_of,
    run,
    settle,
    visible,
)


# --------------------------------------------------------------------------- #
# Stats: an annual view that owns its window
# --------------------------------------------------------------------------- #

TRAILING_FROM = "2025-10-19"


def test_stats_names_its_own_window_and_ignores_the_shared_range(api):
    async def body(app, pilot):
        app.action_set_range("lastmonth")
        await settle(pilot, 1)
        app.action_show_pane("stats")
        await settle(pilot, 3)
        head = visible(app, "#st-scope")
        assert "Trailing 365 Days" in head, head
        assert f"{TRAILING_FROM} .. {TODAY.isoformat()}" in head, head
        # The shared range did not leak in, and Stats did not move it.
        assert app._win.date_to == datetime.date(2026, 9, 30)
        assert api.last("stats")[1][0] is None  # None = the route's trailing year

    run(api, body)


def test_stats_year_picker_walks_the_ladder_and_asks_for_that_year(api):
    """The key drives the ladder both ways, and every rung asks for its own year.

    The year the reader is STANDING IN used to be unreachable: 'p' stepped from
    the trailing window straight into last year and every press after that went
    further back, with nothing to come back with.
    """

    async def body(app, pilot):
        app.action_show_pane("stats")
        await settle(pilot, 3)
        ladder, heads = [], []
        for _ in range(4):
            await pilot.press("p")
            await settle(pilot, 2)
            ladder.append(app._stats_win.request())
            heads.append(visible(app, "#st-scope"))
        assert ladder == [2026, 2025, 2024, 2023], ladder
        assert api.last("stats")[1][0] == 2023
        # The in-progress year is named for the days it can actually answer.
        assert "2026 to date" in heads[0], heads[0]
        assert "2023  (2023-01-01 .. 2023-12-31)" in heads[3], heads[3]
        # 'P' walks back up and comes to rest on the trailing rung: a year the
        # reader has not lived through is not a window.
        for _ in range(6):
            await pilot.press("P")
            await settle(pilot, 2)
        assert app._stats_win.request() is None
        assert "Trailing 365 Days" in visible(app, "#st-scope")

    run(api, body)


def test_stats_reads_messages_and_never_the_deprecated_sessions(api):
    """stats.messages is the field; stats.sessions is a legacy alias of it."""

    async def body(app, pilot):
        app.action_show_pane("stats")
        await settle(pilot, 3)
        cards = visible(app, "#st-cards")
        assert "TOTAL MESSAGES" in cards and "5,000" in cards, cards
        assert "777" not in cards, cards
        assert "annual window only" in visible(app, "#st-summary")

    run(api, body)


def test_a_lapsed_streak_is_not_reported_as_current(api):
    async def body(app, pilot):
        api.stats = copy.deepcopy(STATS_TRAILING)
        api.stats["stats"]["current_streak"] = 0
        app.action_show_pane("stats")
        await settle(pilot, 3)
        summary = visible(app, "#st-summary")
        assert "no streak ending today or yesterday" in summary, summary
        cards = visible(app, "#st-cards")
        assert "LONGEST STREAK" in cards and "30" in cards, cards

    run(api, body)


def test_every_month_of_the_annual_window_is_reachable_at_80_columns(api):
    """No month is silently trimmed to make the year fit an 80-column pane."""

    async def body(app, pilot):
        app.action_show_pane("stats")
        await settle(pilot, 3)
        seen = {visible(app, "#st-calendar")}
        for _ in range(20):  # walk to the oldest month
            app.action_shift_range(-1)
            await settle(pilot, 1)
            seen.add(visible(app, "#st-calendar"))
        assert any("October 2025" in text for text in seen), sorted(seen)[-1]
        for _ in range(60):  # and back to the newest
            app.action_shift_range(1)
            await settle(pilot, 1)
            seen.add(visible(app, "#st-calendar"))
        assert any("October 2026" in text for text in seen)
        # Stepping never leaves the window: the rungs are counted, not guessed.
        assert app._stats_month is None or 0 <= app._stats_month <= 13

    run(api, body, size=(80, 24))


def test_enter_on_a_day_moves_the_shared_window_and_opens_the_overview(api):
    async def body(app, pilot):
        app.action_show_pane("stats")
        await settle(pilot, 3)
        table = app.query_one("#st-days", DataTable)
        rows = rows_of(table)
        assert rows, "the day table is empty"
        wanted = rows[0][0]
        table.focus()
        await pilot.press("enter")
        await settle(pilot, 3)
        assert app._active_pane() == "overview"
        assert app._win.custom and app._win.date_from == app._win.date_to
        assert app._win.date_from.isoformat() == wanted, (app._win, wanted)
        assert wanted in visible(app, "#range-dates")
        # Overview then asks for exactly that one day.
        sent = api.last("usage")  # fetch_usage(period, date_from, date_to, refresh=)
        assert (sent[1][1], sent[1][2]) == (wanted, wanted), sent

    run(api, body)


def test_the_annual_calendar_pages_only_over_the_months_it_asked_for(api):
    """A named calendar year is twelve months, and every one stays reachable.

    Whole weeks pad the grid edges, so a month list built from the CELLS grows a
    December into the neighbouring January. Paging to that phantom month paints an
    all-blank grid under a heading that names the year, which reads as a month of
    nothing recorded. The fake answers a year request with that year's own days,
    so a calendar left over from the previous window cannot pass either.
    """
    year = TODAY.year - 1

    async def body(app, pilot):
        app.action_show_pane("stats")
        await settle(pilot, 4)
        # Two rungs now: trailing 365 days, then the calendar year in progress,
        # then the year this test wants. (It used to be one press straight past
        # the year the reader is standing in.)
        await pilot.press("p")
        await settle(pilot, 4)
        assert app._stats_win.request() == TODAY.year, app._stats_win
        await pilot.press("p")
        await settle(pilot, 4)
        assert app._stats_win.request() == year, app._stats_win
        start, end = app._stats_win.bounds(app._today)
        cal = app_mod.views.stats_calendar(
            app._stats_payload or {},
            window_from=start.isoformat(),
            window_to=end.isoformat(),
            today=app._today.isoformat(),
        )
        months = [str(month["month"]) for month in cal["month_view"]]
        assert months == [f"{year}-{m:02d}" for m in range(1, 13)], months
        grid = visible(app, "#st-calendar")
        assert f"December {year}" in grid, grid
        days = rows_of(app.query_one("#st-days"))
        assert days and all(row[0].startswith(f"{year}-") for row in days), days
        # Every month is one keystroke closer, so none is silently unreachable.
        app.action_stats_month(-13)
        await settle(pilot, 2)
        assert f"January {year}" in visible(app, "#st-calendar")
        assert f"month 1 of {len(months)}" in visible(app, "#st-calendar")

    run(api, body, size=(100, 30))


def test_stats_puts_the_annual_figures_on_cards(api):
    """The annual headline was a paragraph; the design says it is a band."""

    async def body(app, pilot):
        app.action_show_pane("stats")
        await settle(pilot, 4)
        cards = visible(app, "#st-cards")
        for label in ("ACTIVE DAYS", "CURRENT STREAK", "LONGEST STREAK",
                      "TOTAL TOKENS", "TOTAL MESSAGES", "MOST USED MODEL"):
            assert label in cards, (label, cards)
        # The denominators the paragraph used to carry inline survive underneath.
        caption = visible(app, "#st-summary")
        assert "annual window only" in caption, caption
        assert "coverage" in caption, caption

    fx.run(api, body, size=(120, 36))
