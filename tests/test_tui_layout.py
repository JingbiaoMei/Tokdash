"""Geometry: the same meaning at 120x36, 100x30 and 80x24.

At each size a terminal comes in, every view opens by key with its figures whole,
a table drops optional columns before it touches a number, prints no header it
cannot fit, keeps the cursor row on screen as it scrolls, keeps the cell its own
scrollbar takes, and cuts a word too long for its column with a trace the reader
can see. What a resize or a covered chooser owes the page is
test_tui_resize_refit.py, and the footer's row is test_tui_footer_fit.py. Pilots
over the contract-shaped payloads in ``support.tui``.
"""
from __future__ import annotations

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import Button, DataTable  # noqa: E402

from tokdash.tui.app import fit_columns  # noqa: E402
from support.tui import (  # noqa: E402
    SIZES,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    press,
    run,
    screen_lines,
    settle,
    visible,
)


@pytest.mark.parametrize("size", SIZES)
def test_every_view_opens_by_key_and_keeps_its_figures(api, size):
    async def body(app, pilot):
        for key in ("f1", "f2", "f3", "f4", "f5", "f6", "f7", "f1"):
            await pilot.press(key)
            await settle(pilot, 3)
        cards = visible(app, "#ov-cards")
        assert "1.0M" in cards, cards        # tokens, whole
        assert "$12.50" in cards, cards      # cost, whole
        assert "75.0%" in cards, cards       # cache hit, whole
        table = app.query_one("#ov-table", DataTable)
        assert table.row_count, "no harness rows at " + str(size)
        # Nothing is drawn past the last column the terminal has.
        for line in screen_lines(app):
            assert len(line) <= size[0], (size, line)

    run(api, body, size=size)


def test_fit_columns_drops_optionals_before_it_touches_a_number():
    spec = [
        ("name", "Name", 24, False), ("tokens", "Tokens", 8, False),
        ("cost", "Cost", 10, False), ("msgs", "Msgs", 8, True),
        ("time", "Est. time", 14, True),
    ]
    wide = fit_columns(spec, 120)
    assert [c[0] for c in wide] == [c[0] for c in spec], wide
    tight = fit_columns(spec, 46)
    keys = [c[0] for c in tight]
    assert "tokens" in keys and "cost" in keys, tight   # numbers keep their home
    assert "time" not in keys and "msgs" not in keys, tight
    assert all(width >= 6 for _, _, width in tight), tight
    assert sum(w for _, _, w in tight) + 2 * len(tight) <= 46, tight


def test_a_long_table_scrolls_and_the_cursor_stays_on_screen(api):
    """24 server hours in a 30-row terminal: the cursor never walks off-view."""

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        press(app, "sp-mode-hour")
        await settle(pilot, 4)
        table = app.query_one("#sp-table", DataTable)
        table.focus()
        for _ in range(20):
            await pilot.press("down")
            await settle(pilot, 1)
        row = table.cursor_coordinate.row
        assert row >= 15, row  # the keyboard actually walked
        shown = visible(app, "#sp-table")
        label = str(table.get_row_at(row)[0])
        assert label in shown, (label, row, shown)

    run(api, body, size=(100, 30))


# --------------------------------------------------------------------------- #
# headers and cut words: a clipped label or value says it was cut
# --------------------------------------------------------------------------- #


def test_no_table_prints_a_header_it_cannot_fit(api):
    """A column narrower than its own label prints a header that lies.

    Speed's hour column asked for 18 cells and named itself in 19, so the pane
    read "Hour (server clock" -- the one word saying WHOSE clock it is, cut off.
    The same class of cut can happen to any of the six tables at any width, so
    the law is checked across all of them.
    """
    tables = {
        "overview": "#ov-table",
        "sessions": "#sn-table",
        "stats": "#st-days",
        "speed": "#sp-table",
        "quota": "#quota-table",
        "pricing": "#pr-table",
    }

    async def body(app, pilot):
        for pane, selector in tables.items():
            app.action_show_pane(pane)
            await settle(pilot, 5)
            table = app.query_one(selector, DataTable)
            header = (visible(app, selector).splitlines() or [""])[0]
            for column in table.columns.values():
                assert str(column.label) in header, (pane, column.label, header)

    run(api, body, size=(100, 30))


def test_the_hour_view_names_the_clock_it_is_using(api):
    """The specific cut: 'Hour (server clock)' in a column of 18."""

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 5)
        app.query_one("#sp-mode-hour", Button).press()
        await settle(pilot, 5)
        head = (visible(app, "#sp-table").splitlines() or [""])[0]
        assert "Hour (server clock)" in head, head
        assert "Hour (server clock " not in head.replace("Hour (server clock)", ""), head

    run(api, body, size=(100, 30))


def test_a_word_too_long_for_its_column_looks_cut(api):
    """Silent clipping turns one value into another.

    Speed's combined lens carries timing-kind names longer than any column the
    terminal can hold. Cut with a trace the reader can see, never mid-word with
    the word left looking complete.
    """

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 5)
        app.query_one("#sp-lens-combined", Button).press()
        await settle(pilot, 5)
        painted = visible(app, "#sp-table")
        assert "…" in painted, painted
        assert "respon " not in painted, painted

    run(api, body, size=(80, 24))


# --------------------------------------------------------------------------- #
# the room a table's own scrollbar takes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("size", [(120, 36), (100, 30), (80, 24)])
def test_a_scrolling_table_never_pays_for_its_scrollbar_with_a_column(api, size):
    """A vertical scrollbar arrives AFTER the columns are fitted.

    Sizing the columns to the full pane and then growing the bar takes a cell from
    the right-hand column, and the figure sitting there loses its last character
    with nothing on screen to say so. The fit keeps that cell, so the table scrolls
    sideways never.
    """
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 3)
        press(app, "sp-mode-hour")
        await settle(pilot, 3)
        table = app.query_one("#sp-table", DataTable)
        assert table.row_count > 0
        # A vertical scrollbar arrives after the fit and takes a cell from the
        # right; the table answers with a HORIZONTAL one when the fit did not
        # keep that cell, and a sideways-scrolling numeric column is a figure the
        # reader has to guess at. Rows the viewport scrolled out of are not
        # missing -- the sideways bar is the thing that must never appear.
        assert not table.show_horizontal_scrollbar, (size, visible(app, "#sp-table"))
        last = len(table.columns) - 1
        # One row of the content region is the header, the rest is the viewport.
        top = int(table.scroll_y)
        window = max(1, int(table.content_region.height) - 1)
        for row in range(top, min(top + window, table.row_count)):
            value = str(table.get_row_at(row)[last])
            if value and value != "\u2014":
                assert value in visible(app, "#sp-table"), (size, row, value)
        # And the row under the cursor is always inside what the widget drew.
        table.move_cursor(row=min(3, table.row_count - 1))
        await settle(pilot, 1)
        label = str(table.get_row_at(table.cursor_row)[0])
        assert label in visible(app, "#sp-table"), (size, label)

    run(api, body, size=size)
