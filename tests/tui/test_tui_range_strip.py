"""The shared range strip, and the window it names for every dated pane.

Five shortcuts that print their own key, a chooser behind the last of them for
the rest, and the exact dates as the last thing the strip gives up. Stepping and
the chooser hand every window over as explicit dates; ``tokdash tui --period``
opens on the dates its token names; and a window change paints no old figures
under the new label -- a late reply is dropped, a sort while the new window
loads shows nothing yet, and the panes not on screen re-read only when shown.
Which button wears the selected fill is test_tui_selected_state.py, and the date
arithmetic itself is test_tui_ranges.py. Pilots over the contract-shaped payloads
in ``support.tui``.
"""
from __future__ import annotations

import datetime
import threading

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import Button, DataTable  # noqa: E402

from tokdash.tui.data import FetchOutcome  # noqa: E402
from tokdash.tui.ranges import MORE_KEY, STRIP_PRESETS  # noqa: E402
from tokdash.tui.ranges import PRESETS as _ALL_PRESETS  # noqa: E402
import support.tui as fx  # noqa: E402  (the layout-law tests call it fx)
from support.tui import (  # noqa: E402
    SIZES,
    TODAY,
    USAGE,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    _open,
    press,
    rows_of,
    run,
    settle,
    visible,
)


MONDAY = TODAY - datetime.timedelta(days=TODAY.weekday())


# --------------------------------------------------------------------------- #
# the range row: five shortcuts, each printing the key that presses it
# --------------------------------------------------------------------------- #


def test_the_strip_is_five_shortcuts_that_name_their_own_key(api):
    """Ten look-alike buttons is a menu; five is a shortcut row.

    The approved design prints the key INSIDE the button it belongs to, so the
    strip has to be checked for the letters as well as for the windows.
    """

    async def body(app, pilot):
        shown = [b for b in app.query("#rangebar Button") if b.display]
        assert [b.id for b in shown] == [
            "range-today", "range-yesterday", "range-thisweek", "range-month",
            "range-more",
        ], [b.id for b in shown]
        for preset in STRIP_PRESETS:
            label = str(app.query_one(f"#range-{preset.key}", Button).label)
            assert label == f"{preset.hint} {preset.label}", label
        assert str(app.query_one("#range-more", Button).label) == f"{MORE_KEY} More"

    fx.run(api, body, size=(120, 36))


def test_w_is_the_week_the_reader_is_standing_in_and_seven_days_is_behind_more(api):
    """"w" does what its button says, and the rolling week is still reachable.

    Both windows are real and they are not the same window, so only one of them
    can own a letter; the other lives in the chooser that the strip's last
    button opens.
    """

    async def body(app, pilot):
        await pilot.press("w")
        await settle(pilot, 3)
        assert app._win.preset == "thisweek", app._win
        assert app._win.date_from == MONDAY, app._win
        assert app._win.date_to == TODAY, app._win

        await pilot.press("p")
        await settle(pilot, 2)
        assert type(app.screen).__name__ == "PresetPicker"
        assert app.screen.query("#preset-7d"), "the rolling week is unreachable"
        app._on_preset("7d")
        await settle(pilot, 3)
        assert app._win.preset == "7d" and app._win.days == 7, app._win

    fx.run(api, body, size=(120, 36))


@pytest.mark.parametrize("size", [(120, 36), (100, 30), (80, 24)])
def test_the_exact_dates_are_the_last_thing_the_strip_gives_up(api, size):
    """A clipped date is a wrong date, so the buttons yield to the numbers."""

    async def body(app, pilot):
        dates = visible(app, "#range-dates")
        assert TODAY.isoformat() in dates, (size, dates)
        assert "\n" not in dates, (size, dates)
        assert app.query_one("#range-today", Button).display
        assert app.query_one("#range-more", Button).display
        for button in app.query("#rangebar Button"):
            if button.display:
                assert "\u2026" not in str(button.label), button.label

    fx.run(api, body, size=size)


def test_the_range_row_is_five_keys_one_cell_apart(api):
    """Keys sit together. A shortcut row with three cells between each button is
    a scatter of islands the eye has to assemble into a row by itself."""

    async def body(app, pilot):
        shown = [b for b in app.query("#rangebar Button") if b.display]
        assert len(shown) >= 4, [str(b.label) for b in shown]
        for left, right in zip(shown, shown[1:]):
            if right.id == "range-more":
                continue  # the door sits apart from the shortcuts on purpose
            gap = right.region.x - left.region.right
            assert gap <= 1, (str(left.label), str(right.label), gap)

    fx.run(api, body, size=(120, 36))


def test_the_strip_stays_five_after_a_window_from_the_chooser(api):
    """The strip is the same five keys whatever the reader is standing in.

    A row that gains a button when "Last Month" is chosen is a row whose layout
    has to be re-learned every time the window changes, and the exact dates at
    the right already say where the reader is. This is the second half of the
    five-shortcut law: the five are the whole row, not the default row.
    """

    async def body(app, pilot):
        app.action_set_range("lastmonth")
        await settle(pilot, 2)
        shown = [b for b in app.query("#rangebar Button") if b.display]
        assert [b.id for b in shown] == [
            "range-today", "range-yesterday", "range-thisweek", "range-month",
            "range-more",
        ], [str(b.label) for b in shown]
        # The window it replaced still names itself, in dates.
        dates = visible(app, "#range-dates")
        assert "2026-09" in dates, dates
        assert "Last Month" in dates, dates

    fx.run(api, body, size=(120, 36))


def test_the_live_window_is_named_in_exact_dates_at_every_width(api):
    async def body(app, pilot):
        dates = visible(app, "#range-dates")
        assert TODAY.isoformat() in dates, dates
        app._on_preset("7d")  # the chooser's answer: the row a pick hands back
        await settle(pilot, 2)
        dates = visible(app, "#range-dates")
        assert "2026-10-12" in dates and TODAY.isoformat() in dates, dates

    for size in SIZES:
        run(api, body, size=size)


# --------------------------------------------------------------------------- #
# stepping and the chooser: every window travels as explicit dates
# --------------------------------------------------------------------------- #


def test_range_shortcuts_and_stepping(api):
    async def body(app, pilot):
        yesterday = TODAY - datetime.timedelta(days=1)
        assert app._win.request() == ("today", TODAY.isoformat(), TODAY.isoformat())
        app.action_set_range("yesterday")
        await settle(pilot, 1)
        assert app._win.request() == ("today", yesterday.isoformat(), yesterday.isoformat())
        app.action_set_range("month")
        await settle(pilot, 1)
        app.action_shift_range(-1)
        await settle(pilot, 1)
        moved = app._win
        assert moved.date_to < TODAY
        assert moved.date_from.day == 1 and moved.date_to.day == 30, moved
        app.action_shift_range(1)
        await settle(pilot, 1)
        assert app._win.date_to == TODAY  # back to the current PARTIAL month
        app.action_set_range("all")
        await settle(pilot, 1)
        app.action_shift_range(-1)
        assert app._win.all_time and app._win.request() == ("all", None, None)

    run(api, body)


def test_more_picker_resolves_a_preset_to_explicit_dates(api):
    async def body(app, pilot):
        press(app, "range-more")
        await settle(pilot, 1)
        items = list(app.screen.query("ListItem"))
        assert len(items) == 11  # the four shortcuts and the seven windows behind them
        # "Last Month" is a COMPLETE month, never the current one a day short.
        app._on_preset("lastmonth")
        await settle(pilot, 1)
        assert app._win.date_from == datetime.date(2026, 9, 1)
        assert app._win.date_to == datetime.date(2026, 9, 30)
        period, date_from, date_to = app._win.request()
        assert (date_from, date_to) == ("2026-09-01", "2026-09-30")
        assert period != "lastMonth"  # never a label the resolver would forget

    # A 120-column strip has room for every preset, so "More..." never appears;
    # the overflow picker is an 80-column reality.
    run(api, body, size=(80, 24))


# --------------------------------------------------------------------------- #
# startup: the range the command line asked for
# --------------------------------------------------------------------------- #


def test_the_period_flag_opens_on_the_dates_it_names(api):
    """``tokdash tui --period week`` may not open on Today.

    run_tui validates the token and hands it to the app. Dropping it on the way
    in shows one day's figures under a claim of a week, which is the same
    silent truncation the HTTP resolver was fixed for.
    """
    async def body(app, pilot):
        dates = visible(app, "#range-dates")
        week_ago = (TODAY - datetime.timedelta(days=6)).isoformat()
        assert week_ago in dates and TODAY.isoformat() in dates, dates
        assert app._win.preset == "7d", app._win
        call = api.last("usage")
        assert call is not None
        assert call[1][:3] == ("today", week_ago, TODAY.isoformat()), call
        # the button for the live window is lit, so the strip agrees with it
        assert app.query_one("#range-7d", Button).has_class("-selected")

    _open(api, "week", body)


def test_a_period_length_the_presets_do_not_own_still_reaches_the_panes(api):
    """--period 14 is a trailing fortnight, not the nearest button's window."""
    async def body(app, pilot):
        dates = visible(app, "#range-dates")
        start = (TODAY - datetime.timedelta(days=13)).isoformat()
        assert start in dates and TODAY.isoformat() in dates, dates
        lit = [p.key for p in _ALL_PRESETS
               if app.query_one(f"#range-{p.key}", Button).has_class("-selected")]
        assert lit == [], lit
        call = api.last("usage")
        assert call[1][1] == start, call

    _open(api, "14", body)


def test_period_all_opens_on_every_recorded_session(api):
    async def body(app, pilot):
        assert app._win.all_time
        assert "All Time" in visible(app, "#range-dates")
        assert app.query_one("#range-all", Button).has_class("-selected")
        call = api.last("usage")
        assert call[1][:3] == ("all", None, None), call

    _open(api, "all", body)


# -------------------------------------------------------------------- F12 --
# ``--period month`` opened a 30-day rolling window where the endpoint means the
# current calendar month.


def test_the_month_period_opens_the_calendar_month(api):
    async def body(app, pilot):
        assert (app._win.date_from, app._win.date_to) == (
            TODAY.replace(day=1), TODAY,
        ), app._win.describe()
        assert app.query_one("#range-month", Button).has_class("-selected")
        assert TODAY.replace(day=1).isoformat() in visible(app, "#range-dates")

    _open(api, "month", body)


def test_the_month_period_request_asks_for_the_same_days(api):
    async def body(app, pilot):
        await settle(pilot, 2)
        asked = api.last("usage")
        # The route's warm key stays the window token; the DATES are what the
        # month has to move, and they are the calendar month.
        assert asked[1][1:] == (
            TODAY.replace(day=1).isoformat(), TODAY.isoformat(),
        ), asked

    _open(api, "month", body)


# --------------------------------------------------------------------------- #
# a new window: no stale paint under its label
# --------------------------------------------------------------------------- #


def test_a_late_reply_cannot_be_painted_under_new_dates(api):
    """The generation guard: switch ranges mid-flight and the abandoned answer
    must never reach the screen."""
    import time

    def stamp(*args, **kwargs):
        _, date_from, date_to = args[0], args[1], args[2]
        time.sleep(0.5)
        return FetchOutcome(
            value={**USAGE, "range": {"from": date_from, "to": date_to,
                                      "days": 1, "recognized": True}},
            status="hit", age_seconds=0.0,
        )

    api.hooks["usage"] = stamp

    async def body(app, pilot):
        await settle(pilot, 1)  # a slow Today load is in flight
        app.action_set_range("yesterday")
        await settle(pilot, 4)
        yesterday = (TODAY - datetime.timedelta(days=1)).isoformat()
        assert app._win.date_from.isoformat() == yesterday
        assert app._ov_usage["range"]["from"] == yesterday, app._ov_usage["range"]
        strip = visible(app, "#range-dates")  # the ONE shared window label
        assert yesterday in strip, strip
        assert TODAY.isoformat() not in strip, strip

    run(api, body)


# --------------------------------------------------------------------- F2 --
# A repaint while a new window was loading put the OLD window's figures under
# the new dates.


def test_sorting_while_a_new_window_loads_shows_no_figures_yet(api):
    gate = threading.Event()

    def blocked(*_args, **_kwargs):
        gate.wait(8)
        return None

    async def body(app, pilot):
        await settle(pilot, 1)
        table = app.query_one("#ov-table", DataTable)
        assert table.row_count > 0
        api.hooks["usage"] = blocked
        app.action_set_range("yesterday")
        await settle(pilot, 1)
        assert table.row_count == 0, rows_of(table)
        try:
            # Sort, search and the sub-view switch all reach the same painter,
            # which used to read the payloads still in hand without asking what
            # window produced them.
            app.action_cycle_sort()
            app.action_begin_search()
            await settle(pilot, 1)
            assert table.row_count == 0, (app._win.describe(), rows_of(table))
            # One strip prints the window for every dated pane, so the stale
            # date has nowhere left to sit next to the new one.
            strip = visible(app, "#range-dates")
            assert TODAY.isoformat() not in strip, strip
            assert "2026-10-17" in strip, strip
        finally:
            gate.set()
            await settle(pilot, 2)
        assert table.row_count > 0, "the unblocked load never painted"

    run(api, body)


# --------------------------------------------------------------------- F7 --
# One date step used to re-read every pane ever opened, Speed's build included.


def test_a_new_range_leaves_the_hidden_panes_alone(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        app.action_show_pane("sessions")
        await settle(pilot, 2)
        app.action_show_pane("overview")
        await settle(pilot, 1)
        speed_before = len(api.called("speed"))
        sessions_before = len(api.called("sessions"))
        app.action_set_range("yesterday")
        await settle(pilot, 2)
        assert len(api.called("speed")) == speed_before, api.called("speed")
        assert len(api.called("sessions")) == sessions_before
        # And each pane left standing on the old window knows it is out of date.
        assert {"speed", "sessions"} <= app._stranded, app._stranded
        # Coming back pays the debt, once.
        app.action_show_pane("speed")
        await settle(pilot, 2)
        assert len(api.called("speed")) == speed_before + 1
        assert "speed" not in app._stranded

    run(api, body)
