"""The frame every pane shares: the top row, the tab row, the keyboard and search.

The brand row states the palette the terminal agreed to; the tab row prints the
F-key that selects each view and drops those hints, never a view, when the row is
narrow; a pane switch hands the keyboard to the pane being shown; the search box
a pane opens with ``/`` shows what was typed; and no pane calls a tool a
"harness". Pilots over the contract-shaped payloads in ``support.tui``.
"""
from __future__ import annotations

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import Button, Input, Static, Tab  # noqa: E402

import support.tui as fx  # noqa: E402  (the layout-law tests call it fx)
from support.tui import (  # noqa: E402
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    run,
    settle,
    visible,
)


# --------------------------------------------------------------------------- #
# the top row: the name, and the palette the terminal agreed to
# --------------------------------------------------------------------------- #


def test_the_top_row_names_the_page_and_the_palette_it_got(api, monkeypatch):
    """The design's first row: the name, and what colour the terminal agreed to.

    A reader who cannot tell a grey dashboard from an empty one has no reason to
    trust either, so the palette is stated where it is always on screen.
    """

    monkeypatch.delenv("NO_COLOR", raising=False)

    async def body(app, pilot):
        brand = app.query_one("#brand")
        assert brand.display and brand.region.height == 1
        assert str(app.query_one("#brand-name", Static).content) == "Tokdash"
        note = visible(app, "#brand-note").strip()
        assert note.startswith("colour:"), note
        assert "truecolour" in note, note
        # Textual's command-palette glyph is not part of this design's row.
        assert "⭘" not in "\n".join(fx.screen_lines(app))

    fx.run(api, body, size=(120, 36))


# --------------------------------------------------------------------------- #
# the navigation row: the F-key belongs on the tab it selects
# --------------------------------------------------------------------------- #


def test_the_navigation_row_prints_the_key_that_selects_it(api):
    async def body(app, pilot):
        labels = [str(tab.label) for tab in app.query(Tab)]
        assert labels == [
            "F1 Overview", "F2 Sessions", "F3 Stats", "F4 Report",
            "F5 Speed", "F6 Quota", "F7 Pricing",
        ], labels

    fx.run(api, body, size=(120, 36))


def test_a_narrow_row_drops_the_hints_before_it_drops_a_view(api):
    """At eighty columns the seven names are all the row can carry."""

    async def body(app, pilot):
        tabs = list(app.query(Tab))
        assert len(tabs) == 7
        assert all(not str(tab.label).startswith("F") for tab in tabs), [
            str(t.label) for t in tabs
        ]
        assert all(tab.display for tab in tabs)
        # Nothing scrolls out of reach either: the whole row fits the screen.
        assert tabs[-1].region.right <= app.screen.size.width

    fx.run(api, body, size=(80, 24))


# --------------------------------------------------------------------------- #
# the keyboard: a pane switch hands it over, and search takes it
# --------------------------------------------------------------------------- #


def test_a_pane_switch_hands_the_keyboard_to_the_pane_being_shown(api):
    """A search box keeps focus when its pane is hidden.

    The next keystroke then belongs to a widget the user cannot see: "/" never
    reaches the app, and the new pane looks like it has no search at all.
    """

    async def body(app, pilot):
        await pilot.press("slash")  # overview search, as a user would open it
        await settle(pilot, 1)
        assert isinstance(app.focused, Input)
        app.action_show_pane("pricing")
        await settle(pilot, 4)
        assert not isinstance(app.focused, Input), app.focused
        await pilot.press("slash")
        for char in "gpt":
            await pilot.press(char)
        await settle(pilot, 2)
        assert app._pr_search == "gpt", app._pr_search
        assert "gpt" in visible(app, "#pr-search")

    run(api, body, size=(100, 30))


SEARCH_BOXES = (("overview", "#ov-search"), ("sessions", "#sn-search"),
                ("pricing", "#pr-search"))


def test_a_search_box_shows_the_characters_the_user_typed(api):
    """A frame with nowhere to paint the text is a search box with no value.

    The box is three rows: a round border top and bottom and ONE line between
    them. Vertical padding inside that box eats the line, so the pane filters,
    the model holds the query, and the terminal still shows an empty field.
    """

    async def body(app, pilot):
        for pane, box in SEARCH_BOXES:
            app.action_show_pane(pane)
            await settle(pilot, 4)
            await pilot.press("slash")
            for char in "gpt":
                await pilot.press(char)
            await settle(pilot, 2)
            painted = visible(app, box)
            assert "gpt" in painted, (pane, painted)
            assert app.query_one(box, Input).value == "gpt", pane

    run(api, body, size=(80, 24))


# --------------------------------------------------------------------------- #
# the word on screen: these are tools
# --------------------------------------------------------------------------- #


def test_no_pane_asks_the_reader_about_harnesses(api):
    """The tools are the agents; "harness" was internal vocabulary.

    A screen that names a column one thing and its filter another is a screen
    the reader has to translate. Internals keep the old word; the terminal does
    not show internals.
    """

    async def body(app, pilot):
        for pane in ("overview", "sessions", "report"):
            app.action_show_pane(pane)
            await settle(pilot, 4)
            painted = "\n".join(fx.screen_lines(app)).lower()
            assert "harness" not in painted, (pane, painted)
        app.action_show_pane("overview")
        await settle(pilot, 3)
        assert "All tools" in visible(app, "#ov-scope")
        assert str(app.query_one("#ov-tab-harnesses", Button).label) == "Tools"
        app.action_show_pane("sessions")
        await settle(pilot, 3)
        assert "All session tools" in visible(app, "#sn-scope")

    fx.run(api, body, size=(120, 36))
