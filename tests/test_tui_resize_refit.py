"""A resize refits the page it is showing: at once, or when the chooser covering
it closes.

A resize is the one moment the page knows BOTH sizes, so everything measured
against the old one -- the cards, the table's columns, the footer's form, the
rows the pane is handed -- is measured again once the new one has landed, with no
tab switch or reload to prompt it.

A resize that arrives while a chooser is open is answered against the CHOOSER.
The page underneath keeps the layout of the width it no longer has, and nothing
on the way back asks again -- ScreenResume does not bubble to the App, and the
deferred refit the resize queued had already run. At 120x36 shrunk to 80x24
under the range picker that left 118 cells of card in a 78-cell pane ("450.0K"
arrived as "450"); grown the other way, the page stayed on the three-across cards
bought for eighty columns.

The fix hangs off the app's screen-change signal, which is published on push AND
on pop, so these pin the SHARED chooser lifecycle rather than the range picker's
success callback: the picker dismissed by Escape, a preset chosen through that
same picker, and a different chooser (Speed's source list) whose payload identity
has to survive the refit. Where nothing moved, no refit is owed: not at start-up,
and not for a chooser opened and closed at the same size.

Drive the control a reader uses, then read back what the pane SHOWS. Fixtures
are the contract-shaped ones in ``support.tui``; every figure is synthetic.
"""
from __future__ import annotations

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from rich.cells import cell_len  # noqa: E402
from textual.widgets import Button, DataTable, Tab, Tabs  # noqa: E402

import support.tui as fx  # noqa: E402  (the layout-law tests call it fx)
from support.tui import (  # noqa: E402
    NARROW,
    WIDE,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    month_of_buckets,
    press,
    rows_of,
    run,
    settle,
    visible,
)


async def _settled(app, pilot, pane: str) -> bool:
    """Wait for one pane's first paint, and say whether it landed.

    It answers a bool where support.tui's ``_settled`` raises, because the tests
    here assert on the answer.
    """
    for _ in range(40):
        if app._state.get(pane) == "ok" and not app._open_loads.get(pane):
            return True
        await pilot.pause(0.1)
    return app._state.get(pane) == "ok"


def widest(lines) -> int:
    return max((cell_len(line) for line in lines), default=0)


def series_line(app) -> str:
    """The series the Speed note names, without the clauses a width buys or drops.

    The note adds "source all measured" and a second row when it has room, so the
    whole paragraph is not comparable across two widths. What it names -- this
    model, this timing -- is, and that is the thing a re-fit must not restate.
    """
    note = " ".join(visible(app, "#sp-note").split())
    return note.split("\u00b7")[0].strip()


async def _agrees(app, pilot, claim, tries: int = 20) -> bool:
    """Wait for the page to agree with its frame, the way a reader would.

    A page is fitted to the geometry its neighbours END UP taking, and on a loaded
    runner the last of them lands a pass or two after the handler that started the
    repaint. Polling the claim asks the question a person asks -- does this page fit
    the terminal it is in -- instead of betting on how many refreshes a resize costs
    on one particular machine. Bounded, so a page that never agrees still fails.
    """
    for _ in range(tries):
        if claim():
            return True
        await pilot.pause(0.25)
    return claim()


def figures(table) -> list[tuple[str, str]]:
    """(bucket key, rate) for every row, however many columns are on show.

    A resize is allowed to move a column in or out of the picture; what it may
    not do is restate a rate, re-order the buckets or invent one.
    """
    out = []
    for row in rows_of(table):
        cells = [c for c in row if c.strip() not in ("", "\u2014")]
        if not cells:
            continue
        rate = next((c for c in cells[1:] if c.replace(".", "", 1).isdigit()), "")
        out.append((cells[0], rate))
    return out


# --------------------------------------------------------------------- R5-F1 --
# A resize is the one moment the page knows BOTH sizes. Everything measured
# against the old one has to be measured again once the new one has landed.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("start,end", [((120, 36), (80, 24)), ((80, 24), (120, 36))])
def test_a_resized_window_refits_the_page_it_is_showing(api, start, end):
    """No tab switch, no reload: the resize alone must re-fit the body."""

    async def body(app, pilot):
        await pilot.resize_terminal(*end)
        await settle(pilot, 4)
        cards = visible(app, "#ov-cards")
        assert "1.0M" in cards, cards           # a figure, not "1.0"
        assert "gpt-5.1-codex" in cards, cards  # a model, not "gpt-5.1…"
        assert "\u2026" not in cards, cards

        lines = (str(app.query_one("#ov-cards").content)).splitlines()
        room = app.query_one("#ov-cards").content_region.width
        over = [line for line in lines if cell_len(line) > room]
        assert not over, {"room": room, "lines": over}

        if end[0] == 120:
            # The roomy arrangement is six cards on one band, and it comes back
            # on its own: a page that grew kept its letters in the earlier fix,
            # but its cards stayed stacked in the shape bought for eighty.
            assert any("TOTAL TOKENS" in line and "MOST USED MODEL" in line
                       for line in lines), lines
        else:
            # And the table under it: the fit that chose those columns chose them
            # for 112 cells, and a number the widget clips is a number missing.
            painted = visible(app, "#ov-table")
            assert "\u2026" not in painted, painted

    run(api, body, size=start)


def test_a_widened_window_hands_back_the_rows_the_frame_was_owing(api):
    """Grow the window and the page must USE it: strip, pane height, chart.

    The strip's key tail, a pane's explicit height and the body's row budget are
    all measured against the frame as it stood, and each one is measured on a
    different pass -- so a resize that re-fits only the body still leaves the
    wide window wearing the narrow page: the footer keeps the tail-less form it
    chose at eighty columns, its second row keeps a row from every pane, and the
    rows those two took are the activity band at the bottom of the scroll.
    """

    async def body(app, pilot):
        strip = app.query_one("#status")
        pane = app.query_one("#pane-overview")
        assert "q quit" not in str(strip.content), str(strip.content)
        await pilot.resize_terminal(*WIDE)
        await settle(pilot, 5)

        # 1. The strip re-chose its own form for the width it now has.
        assert "q quit" in str(strip.content), str(strip.content)
        # 2. The pane was handed the rows the frame had been drawing over, and
        # 3. the band those rows buy is on the screen rather than below the fold.
        #    The two settle together: the band is sized against the rows the
        #    sections above it END UP taking, and on a loaded host the last of them
        #    lands after the resize handler has already run -- which is the state
        #    Windows CI caught, the band drawn on past the footer's row.
        act = app.query_one("#ov-activity")
        assert await _agrees(app, pilot, lambda: pane.region.height
                             == strip.region.y - pane.region.y), (
            pane.region, strip.region)
        assert await _agrees(app, pilot,
                             lambda: act.region.bottom <= strip.region.y), (
            "the activity band is drawn past the row the footer owns: "
            f"{act.region} vs strip {strip.region}")
        assert "Token activity" in visible(app, "#ov-activity"), visible(app, "#ov-activity")
        # 4. Squeezed back, it gives the rows up rather than spilling.
        await pilot.resize_terminal(80, 24)
        await settle(pilot, 5)
        assert "q quit" not in str(strip.content), str(strip.content)
        assert await _agrees(app, pilot, lambda: pane.region.height
                             == strip.region.y - pane.region.y), (
            pane.region, strip.region)
        assert await _agrees(app, pilot,
                             lambda: act.region.bottom <= strip.region.y), (
            act.region, strip.region)
        lines = [line for line in str(strip.content).splitlines()]
        assert all(cell_len(line) <= 80 for line in lines), lines

    run(api, body, size=(80, 24))


def test_a_refit_does_not_rename_the_series_a_reader_picked(api):
    """The pass that re-fits the page may not re-label the Speed pane.

    Round four pinned the identity of a series to the payload that LANDED. A
    deferred repaint is allowed while the geometry is genuinely new, and it must
    still say which rows are whose -- here, while a build is streaming.
    """

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-mode-trend")
        await settle(pilot, 3)
        table = app.query_one("#sp-table", DataTable)
        before = figures(table)
        heading = " ".join(visible(app, "#sp-note").split())
        await pilot.resize_terminal(110, 32)
        await settle(pilot, 4)
        # The MEASUREMENTS are what a resize may not touch. Which columns carry
        # them is the opposite: a pane 110 wide cannot hold the picture beside
        # the rows, so the picture moves above them and the columns it was
        # squeezing come back. That re-fit is the point of the pass.
        assert figures(table) == before, "a resize may not invent measurements"
        assert " ".join(visible(app, "#sp-note").split()) == heading

    run(api, body, size=(120, 36))


def test_a_window_that_grows_gives_its_letters_back(api):
    """A fit made while the window was narrow must not outlive the narrow window.

    The key chips and the full window names are the two things the layout gives
    up at eighty columns. If the repaint that follows a resize measures against
    the geometry the layout has not produced yet, a terminal dragged wide keeps
    the crowded-window labels for the rest of the session.
    """

    async def body(app, pilot):
        tabs = list(app.query(Tabs).first().query(Tab))
        assert [str(t.label) for t in tabs][:2] == ["Overview", "Sessions"]
        assert "d Yest." in str(app.query_one("#range-yesterday", Button).label)

        await pilot.resize_terminal(120, 36)
        await settle(pilot, 2)
        labels = [str(t.label) for t in app.query(Tabs).first().query(Tab)]
        assert labels[0].endswith("Overview") and labels[0] != "Overview", labels
        assert labels[1].endswith("Sessions") and not labels[1].startswith("S"), labels
        assert "d Yesterday" in str(app.query_one("#range-yesterday", Button).label)

    fx.run(api, body, size=(80, 24))


# --------------------------------------------------------------------- R6-F1 --
# A chooser COVERS the page instead of moving it, so the page learns its new
# geometry only on the way back. That is the moment a re-fit is owed.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("start,end", [(WIDE, NARROW), (NARROW, WIDE)])
def test_a_resize_under_an_open_chooser_refits_when_it_closes(api, start, end):
    """Escape changes the range by nothing at all, and must still re-fit the page."""

    async def body(app, pilot):
        await pilot.press("p")
        await settle(pilot, 1)
        assert len(app.screen_stack) > 1
        before = len(api.calls)
        await pilot.resize_terminal(*end)
        await settle(pilot, 2)
        await pilot.press("escape")
        await settle(pilot, 6)

        assert len(app.screen_stack) == 1
        assert len(api.calls) == before, api.calls[before:]  # a dismissal re-reads nothing
        cards = app.query_one("#ov-cards")
        lines = str(cards.content).splitlines()
        assert widest(lines) <= cards.content_region.width, {
            "room": cards.content_region.width, "lines": lines}
        assert "1.0M" in visible(app, "#ov-cards"), visible(app, "#ov-cards")
        # The ledger that decides whether a re-fit is owed has to agree with the
        # box, or the next resize would take the stale one for granted.
        assert app._body_fitted.get("overview") == app._body_stamp("overview")
        if end == WIDE:
            assert any("TOTAL TOKENS" in line and "MOST USED MODEL" in line
                       for line in lines), lines
        else:
            assert "\u2026" not in visible(app, "#ov-table"), visible(app, "#ov-table")

    run(api, body, size=start)


def test_a_preset_picked_under_a_resized_chooser_lands_fitted(api):
    """The same lifecycle the other way round: choosing a window, not cancelling one.

    Shrunk to eighty BEFORE the pick, so the preset's own reload and the re-fit
    both land on a page whose geometry changed while it was covered.
    """

    async def body(app, pilot):
        await pilot.press("p")
        await settle(pilot, 1)
        await pilot.resize_terminal(*NARROW)
        await settle(pilot, 2)
        await pilot.press("down")
        await pilot.press("enter")
        await settle(pilot, 6)

        assert len(app.screen_stack) == 1
        assert "2026-10-17" in visible(app, "#rangebar"), visible(app, "#rangebar")
        cards = app.query_one("#ov-cards")
        assert widest(str(cards.content).splitlines()) <= cards.content_region.width
        assert "\u2026" not in visible(app, "#ov-table"), visible(app, "#ov-table")
        assert app._body_fitted.get("overview") == app._body_stamp("overview")

    run(api, body, size=WIDE)


@pytest.mark.parametrize("start,end", [(WIDE, NARROW), (NARROW, WIDE)])
def test_a_dismissed_chooser_may_not_rename_the_speed_series(api, start, end):
    """A different chooser, and the payload a reader picked is not yours to restate.

    Speed names the series a payload ANSWERED for. This refit repaints from the
    retained payload, so heading and measurements both have to come back the same.
    """

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 3)
        press(app, "sp-mode-trend")
        await settle(pilot, 3)
        table = app.query_one("#sp-table")
        heading = series_line(app)
        assert heading.startswith("measuring "), heading
        before = figures(table)
        press(app, "sp-pick-source")
        await settle(pilot, 1)
        assert len(app.screen_stack) > 1
        await pilot.resize_terminal(*end)
        await settle(pilot, 2)
        await pilot.press("escape")
        await settle(pilot, 6)

        assert len(app.screen_stack) == 1
        assert series_line(app) == heading, visible(app, "#sp-note")
        assert figures(table) == before, "a re-fit may not invent measurements"
        assert app._body_fitted.get("speed") == app._body_stamp("speed")
        # The rows too: the fit chain measured the chooser, not this pane.
        assert app.query_one("#pane-speed").region.bottom <= \
            app.query_one("#status").region.y

    run(api, body, size=start)


def test_a_chooser_that_was_not_resized_leaves_the_page_alone(api):
    """The re-fit is guarded: open and close with no resize must cost nothing.

    Forcing a fresh measurement every time a chooser closes must not turn into a
    repaint, a re-read, or a moving cursor on the ordinary path.
    """

    async def body(app, pilot):
        cards = str(app.query_one("#ov-cards").content)
        before = len(api.calls)
        await pilot.press("p")
        await settle(pilot, 2)
        await pilot.press("escape")
        await settle(pilot, 5)
        assert len(app.screen_stack) == 1
        assert len(api.calls) == before, api.calls[before:]
        assert str(app.query_one("#ov-cards").content) == cards

    run(api, body, size=WIDE)


# --------------------------------------------------------------------- R6-F2 --
# The rows the frame hands the page belong to the page. A modal that covers it,
# or chrome above it that changes height, moves the page with no Resize event
# for the App to answer, and a table that believes its viewport is shorter than
# it is stops one row early for ever.
# --------------------------------------------------------------------------- #


PATHS = ("plain", "modal-escape", "modal-pick", "dates-editor", "dates-then-modal")


LAST_BUCKET = "2026-10-15"  # the 45th daily bucket the fixture writes


@pytest.mark.parametrize("how", PATHS)
def test_the_page_keeps_every_row_the_frame_gives_it(api, how):
    """After each of these, the last row of a long table has to reach the screen.

    A pane that kept an old frame's height is invisible in the abstract -- the
    clip is silent -- but it shows the moment a reader walks to the end of a
    series: the cursor stops, and the row it stopped short of never arrives.
    """

    async def body(app, pilot):
        month_of_buckets(api, 45)
        app.action_show_pane("speed")
        await settle(pilot, 3)
        press(app, "sp-mode-trend")
        await settle(pilot, 3)
        if how.startswith("dates"):
            await pilot.press("c")
            await settle(pilot, 2)
        if how.startswith("modal") or how == "dates-then-modal":
            await pilot.press("p")
            await settle(pilot, 2)
        await pilot.resize_terminal(*WIDE)
        await settle(pilot, 2)
        if how == "modal-pick":
            await pilot.press("enter")  # choose the highlighted preset
        else:
            await pilot.press("escape")
        # TWO pauses, not eight: the page must take the rows the new frame gave
        # it, not wait for the next unrelated repaint to hand them over.
        await settle(pilot, 2)

        box = app.query_one("#pane-speed")
        strip = app.query_one("#status")
        assert box.region.bottom == strip.region.y, (how, box.region, strip.region)
        table = app.query_one("#sp-table")
        table.focus()
        await pilot.press("ctrl+end")
        await settle(pilot, 2)
        assert table.cursor_row == 44, (how, table.cursor_row)
        assert LAST_BUCKET in visible(app, "#sp-table"), how
        assert app._fit_rearms == 0, how  # the chain settled; it is not chasing

    run(api, body, size=NARROW)


@pytest.mark.parametrize("key", ["p", "c"])
def test_a_close_that_changed_nothing_leaves_the_page_alone(api, key):
    """Open and close with no resize: no re-read, no repaint, no spinning chain."""

    async def body(app, pilot):
        cards = str(app.query_one("#ov-cards").content)
        before = len(api.calls)
        await pilot.press(key)
        await settle(pilot, 2)
        await pilot.press("escape")
        await settle(pilot, 4)
        assert len(app.screen_stack) == 1
        assert len(api.calls) == before, api.calls[before:]
        assert str(app.query_one("#ov-cards").content) == cards
        assert app._fit_rearms == 0
        box = app.query_one("#pane-overview")
        assert box.region.bottom == app.query_one("#status").region.y

    run(api, body, size=WIDE)


# --------------------------------------------------------------------- R6-F3 --
# The refit a dismissed chooser owes must not become a refit that happens to
# every page at start-up. The app publishes a screen change for its FIRST screen
# too, and a forced pass there re-fits a page that never moved -- and a pane that
# is re-fitted is a pane whose scroll position can move before a key is pressed.
# --------------------------------------------------------------------------- #


def test_the_page_opens_at_its_top_with_no_chooser_ever_opened(api):
    """No chooser has been opened: nothing may re-fit the page out from under it."""

    async def body(app, pilot):
        assert await _settled(app, pilot, "overview")
        assert app._chooser_was_up is False
        assert app._fit_forced is False
        pane = app.query_one("#pane-overview")
        # Eight rows of terminal: the page overflows, so a scroll that was not
        # asked for is a scroll that can be seen.
        assert pane.scroll_y == 0, pane.scroll_y

    run(api, body, size=(80, 14))


def test_only_a_dismissed_chooser_owes_the_refit(api):
    """The flag follows the chooser: up while it covers the page, down once it is gone."""

    async def body(app, pilot):
        assert await _settled(app, pilot, "overview")
        await pilot.press("p")
        await settle(pilot, 1)
        assert app._chooser_was_up is True
        await pilot.press("escape")
        await settle(pilot, 2)
        assert app._chooser_was_up is False
        assert app._body_fitted.get("overview") == app._body_stamp("overview")

    run(api, body, size=(80, 14))


def test_handing_over_the_keys_does_not_move_the_page(api):
    """Focus is a hand-off of the keyboard, not a trip somewhere else.

    Eight rows of terminal: the Overview table starts below the fold, so a focus
    that scrolls it into view puts the card band -- the reason the pane exists --
    off the top of the screen. Textual animates that scroll too, so a reader (or
    a test) that measures the page right afterwards catches it mid-flight.
    """

    async def body(app, pilot):
        assert await _settled(app, pilot, "overview")
        pane = app.query_one("#pane-overview")
        assert pane.show_vertical_scrollbar, "eight rows must overflow the page"
        assert pane.scroll_y == 0, pane.scroll_y
        for pane_name in ("sessions", "speed", "overview"):
            app.action_show_pane(pane_name)
            await settle(pilot, 3)
            box = app.query_one(f"#pane-{pane_name}")
            assert box.scroll_y == 0, (pane_name, box.scroll_y)
        # Closing a detail hands the keys back to its list. That is not a journey
        # either: the list is already on screen.
        app.action_show_pane("sessions")
        await settle(pilot, 3)
        app.action_close_detail()
        await settle(pilot, 3)
        assert app.query_one("#pane-sessions").scroll_y == 0

    run(api, body, size=(80, 14))
