"""The layout law the approved design is made of, one screen at a time.

The correctness suite asks what the numbers say. This file asks the other
question the design review asked twice: can a reader SEE the answer -- the key
that opens a window, which tool a row came from, which figure is the headline,
which name got cut? Every test reads the painted screen at the three sizes the
terminal actually comes in (120x36, 100x30, 80x24), because a layout that only
works at the width of its own screenshot is the failure this pass exists to
catch.

Fixture payloads come from test_tui_revision5, which pins the contract shapes.
"""
from __future__ import annotations

import copy
import datetime as dt

import pytest
from textual.widgets import Button, DataTable, Static, Tab

from tokdash.tui import views
from tokdash.sessions import SESSION_TOOLS, TOOL_LABELS
from tokdash.tui import app as app_mod
from tokdash.tui.ranges import MORE_KEY, PRESET_BY_KEY, STRIP_PRESETS

import test_tui_revision5 as fx
from test_tui_revision5 import settle, visible  # noqa: F401  (helpers)

# The revision-5 fixture seam: same answers, same recorded calls.
api = fx.api

TODAY = fx.TODAY
MONDAY = TODAY - dt.timedelta(days=TODAY.weekday())


def headers(table: DataTable) -> list[str]:
    return [str(column.label).strip() for column in table.columns.values()]


# -------------------------------------------------------------------------- #
# the range row: five shortcuts, each printing the key that presses it
# -------------------------------------------------------------------------- #


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


# -------------------------------------------------------------------------- #
# the navigation row: the F-key belongs on the tab it selects
# -------------------------------------------------------------------------- #


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


# -------------------------------------------------------------------------- #
# the headline band: figures with room to be read as figures
# -------------------------------------------------------------------------- #


def test_the_card_band_grows_with_the_room_it_is_given(api):
    """A terminal has no font size, so the size of a headline is its space.

    The approved cards are more panel than ink. At thirty-six rows the band
    pays for that; at twenty-four the row comes back to the table, which is the
    one line of the trade a reader cannot make themselves.
    """

    async def body(app, pilot):
        # Six rows for one row of six: air, label, air, figure, note, air. The
        # air BETWEEN the label and the figure is what makes the figure the
        # card's headline in a terminal that has no font size to lean on.
        cards = app.query_one("#ov-cards")
        assert cards.region.height == 6, cards.region
        painted = visible(app, "#ov-cards")
        for label in ("TOTAL TOKENS", "TOTAL COST", "TOTAL MESSAGES",
                      "EST. AGENT TIME", "CACHE HIT RATE", "MOST USED MODEL"):
            assert label in painted, (label, painted)

    fx.run(api, body, size=(120, 36))

    async def tight(app, pilot):
        # Eighty columns is where six across stops holding a figure whole: the
        # band goes to two rows of three (and gives its air back to the table),
        # because "gpt-5-c…" is a different model, not a shorter one.
        assert app.query_one("#ov-cards").region.height == 6
        painted = visible(app, "#ov-cards")
        assert "1.0M" in painted and "gpt-5.1-codex" in painted, painted
        assert "\u2026" not in painted, painted

    fx.run(api, tight, size=(80, 24))


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


# -------------------------------------------------------------------------- #
# the word on screen: these are tools
# -------------------------------------------------------------------------- #


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


# -------------------------------------------------------------------------- #
# Sessions: the tool is the first thing a reader narrows by
# -------------------------------------------------------------------------- #


def test_the_tool_chips_are_the_filter_the_pane_is_scrolled_with(api):
    """The chips do the job "h" used to do blindly, and agree with it."""

    async def body(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 5)
        all_chips = app.query_one("#sn-tool-all", Button)
        assert all_chips.has_class("-selected"), "the pane opens unfiltered"
        assert app.query_one("#sn-tool-codex", Button).display

        app.query_one("#sn-tool-claude", Button).press()
        await settle(pilot, 5)
        assert app._sn_sources == ("claude",), app._sn_sources
        assert app.query_one("#sn-tool-claude", Button).has_class("-selected")
        assert not app.query_one("#sn-tool-all", Button).has_class("-selected")
        # The head and the chip agree about what is in force; the strip underneath
        # is left to talk about the rows, not to repeat it.
        assert "1 of 20 session tools" in visible(app, "#sn-scope")
        # The rows' own caption, under the table, says what the rows are.
        assert "sorted by" in visible(app, "#sn-meta")
        assert not app.query_one("#sn-note", Static).display

        app.query_one("#sn-tool-all", Button).press()
        await settle(pilot, 5)
        assert set(app._sn_sources) == set(SESSION_TOOLS)
        assert app.query_one("#sn-tool-all", Button).has_class("-selected")

    fx.run(api, body, size=(120, 36))


def test_the_chip_row_holds_every_tool_behind_one_button(api):
    """Twenty supported tools do not fit a row; the door has to be on it."""

    async def body(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 5)
        shown = [b for b in app.query("#sn-tools Button") if b.display]
        assert len(shown) < len(SESSION_TOOLS) + 2, "the row overflowed"
        assert app.query_one("#sn-tool-more", Button).display
        # The chips that are shown fit the pane: none is clipped by the edge.
        edge = app.query_one("#sn-tools").region.right
        assert all(b.region.right <= edge for b in shown), [
            (str(b.label), b.region.right) for b in shown
        ]

    fx.run(api, body, size=(80, 24))


def test_the_identity_columns_survive_the_squeeze_one_at_a_time(api):
    """Dropping the stamp and the model together was an all-or-nothing gate.

    At a hundred columns there is room for the stamp on its own; the fit is
    what decides, and the numbers never pay for a name column.
    """

    async def body(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 5)
        heads = headers(app.query_one("#sn-table", DataTable))
        assert "Tool" in heads and "Project / session" in heads
        assert "Last recorded" in heads, heads
        assert "Tokens" in heads and "Cost" in heads

    fx.run(api, body, size=(100, 30))


def test_a_session_names_its_project_and_keeps_its_id_whole(api):
    """The id is the disambiguator; a cropped one reads as a different session."""

    async def body(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 5)
        painted = visible(app, "#sn-table")
        assert "tokdash" in painted, painted
        assert "cx-1" in painted, painted  # room at 120: name AND id
        assert "cx-\u2026" not in painted, painted

    fx.run(api, body, size=(120, 36))

    async def tight(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 5)
        painted = visible(app, "#sn-table")
        assert "tokdash" in painted, painted
        assert "\u20261" not in painted.replace(" ", ""), painted

    fx.run(api, tight, size=(80, 24))


# -------------------------------------------------------------------------- #
# Report: the shared report, without a printed page's furniture
# -------------------------------------------------------------------------- #


def test_the_report_pane_shows_no_rules_and_no_store_footer(api):
    """The pane and the piped report share the figures, not the page furniture.

    Rules separate sections on paper; here spacing and surface tone do it, and
    the store line is already at the foot of every pane. The [warn] lines and the
    note about which scan each figure came from stay, because those are news.
    """

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 6)
        painted = visible(app, "#rp-body")
        assert "\u2500" not in painted, painted
        assert "usage db" not in painted and "db status" not in painted, painted
        assert "Tokens" in painted, painted

    fx.run(api, body, size=(100, 30))


def test_the_piped_report_still_carries_its_rules_and_footer(api):
    """The pane's trim is the pane's business: the document is unchanged."""

    from tokdash.tui.report import build_report_segments, emit_plain

    segments = build_report_segments(
        copy.deepcopy(fx.USAGE), copy.deepcopy(fx.INSIGHTS),
        copy.deepcopy(fx.ACTIVE),
        window={"period": "range", "from": "2026-10-12", "to": "2026-10-18",
                "days": 7, "db_line": "usage db: test"},
        width=80,
    )
    text = emit_plain(segments)
    assert "\u2500" in text, text
    assert "usage db: test" in text, text


# --------------------------------------------------------------------------- #
# the four things the design review asked for by name
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


def test_the_card_band_is_six_objects_not_six_lines_of_text(api, monkeypatch):
    # The accent edge is a colour affordance; in a monochrome terminal the block
    # geometry carries the hierarchy alone (see the mono law in test_tui_app).
    """Each figure carries its own colour down its left edge.

    Six tinted numbers on a shared background still read as six similar words
    first; the accent edge is what makes the band scannable as six kinds of
    figure, and it is the same mark a source wears everywhere else on screen.
    """

    monkeypatch.delenv("NO_COLOR", raising=False)

    async def body(app, pilot):
        assert not app.no_color, "NO_COLOR is set: this test asks about colour"
        text = app.query_one("#ov-cards", Static).content
        styles = [str(span.style) for span in text.spans]
        for key, tint in app_mod._CARD_COLORS.items():
            if key not in {c.get("key") for c in views.kpi_cards(
                app._ov_usage or {}, app._ov_active, window=""
            )}:
                continue
            assert any(tint in s and s.startswith("on ") for s in styles), (key, tint)
        # The band's right gutter: the band is narrower than the pane, so the
        # last card never touches the edge of the screen.
        for line in str(text).split("\n"):
            assert len(line) <= 118 - 1, (len(line), line)

    fx.run(api, body, size=(120, 36))


def test_a_full_sessions_pane_says_nothing_in_its_warning_strip(api):
    """The strip above the rows is for news. A line that is always there is a
    band across the page that teaches the reader to ignore it."""

    async def body(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 4)
        note = app.query_one("#sn-note", Static)
        assert not note.display, visible(app, "#sn-note")
        caption = visible(app, "#sn-meta")
        assert "session" in caption and "sorted by" in caption, caption
        table = app.query_one("#sn-table", DataTable)
        assert table.region.bottom <= app.query_one("#sn-meta").region.y

    fx.run(api, body, size=(120, 36))


def test_a_partial_sessions_pane_says_it_once_in_the_strip(api):
    async def body(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 4)
        app._sn_failed = ["cursor"]
        app._paint_sessions()
        await settle(pilot, 2)
        strip = visible(app, "#sn-note")
        assert "Partial" in strip and "cursor" in strip, strip
        assert strip.count("Partial") == 1, strip

    fx.run(api, body, size=(120, 36))


# -------------------------------------------------------------------------- #
# what the rows wear, and what the shortcut row keeps when the window changes
# -------------------------------------------------------------------------- #


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


def test_a_source_wears_its_own_hue_in_the_rows(api, monkeypatch):
    """One grey pill with different words in it is not a badge.

    The tool column is the column a reader scans to follow one tool down the
    page, and the approved design marks it with a chip of that tool's own hue.
    A shared ground made every chip the same object, which is the thing the
    colour was there to prevent.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)

    async def body(app, pilot):
        await settle(pilot, 2)
        table = app.query_one("#ov-table", DataTable)
        keys = list(table.rows.keys())
        assert len(keys) >= 2, keys
        grounds = {}
        for key in keys[:2]:
            cell = table.get_cell(key, list(table.columns.keys())[0])
            styles = {str(span.style) for span in cell.spans}
            ground = {s.split("on ")[1] for s in styles if "on " in s}
            assert len(ground) == 1, styles
            grounds[str(getattr(key, "value", key))] = ground.pop()
        assert len(set(grounds.values())) == len(grounds), grounds
        for ground in grounds.values():
            assert ground.startswith("#") and len(ground) == 7, ground

    fx.run(api, body, size=(120, 36))


def test_a_tight_card_band_pays_its_gutter_in_figures(api):
    """A hundred columns is the width a laptop has.

    The second cell of gutter between cards is decoration; the cell it buys back
    is the difference between a model name and a model name with a piece gone,
    and the piece is what the reader was reading.
    """

    async def body(app, pilot):
        await settle(pilot, 2)
        painted = visible(app, "#ov-cards")
        assert "gpt-5.1-codex" in painted, painted
        assert "\u2026" not in painted, painted

    fx.run(api, body, size=(100, 30))


# -------------------------------------------------------------------------- #
# the row and the chosen window: what the design spends its vertical space on
# -------------------------------------------------------------------------- #


@pytest.mark.parametrize("size,air", [((120, 36), 2), ((100, 30), 1), ((80, 24), 1)])
def test_an_overview_row_buys_its_air_as_a_second_line(api, size, air):
    """The approved table spaces its rows; a terminal has no line spacing.

    The air IS a second line, and the page order says what it is bought from:
    the pane scrolls to the activity picture before the rows lose theirs. Under
    thirty-two rows the page is dense on purpose -- there the second line would
    cost a figure the reader came to read.
    """

    async def body(app, pilot):
        table = app.query_one("#ov-table", DataTable)
        keys = list(table.rows.keys())
        assert len(keys) >= 3, [str(k) for k in keys]
        for key in keys:
            assert table.get_row_height(key) == air, (key, table.get_row_height(key))
        # The air is on the SCREEN, not just in the row record: the table's own
        # region is header + one line per row, or header + two.
        region = table.region.height
        assert region == 1 + air * len(keys), (region, air, len(keys))

    fx.run(api, body, size=size)


def test_the_live_window_is_the_one_thing_the_strip_paints_lavender(api):
    """Five boxes read as five things to press; the design paints one choice.

    An unchosen window is text with its key printed on a small plate, and the
    lavender plate belongs to the window actually on screen -- otherwise the row
    says "look, five equal buttons" where it should say "you are here".
    """

    async def body(app, pilot):
        chosen = [b for b in app.query("#rangebar Button") if "-selected" in b.classes]
        assert [b.id for b in chosen] == ["range-today"], [b.id for b in chosen]
        assert chosen[0].styles.background is not None
        for preset in STRIP_PRESETS:
            button = app.query_one(f"#range-{preset.key}", Button)
            if button.id == "range-today":
                continue
            assert "-selected" not in button.classes, button.id

        await pilot.press("m")
        await settle(pilot, 2)
        assert "-selected" in app.query_one("#range-month", Button).classes
        assert "-selected" not in app.query_one("#range-today", Button).classes

    fx.run(api, body, size=(120, 36))
