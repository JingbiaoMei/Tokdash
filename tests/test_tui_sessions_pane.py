"""The Sessions pane: one list fanned out over the session tools, and the whole
session behind a row.

A source that did not answer is named, never counted as zero sessions; two tools'
identical session ids stay two rows, because identity is the (tool, session_id)
pair; the tool chips and ``h`` choose the sources the list reads; and the detail
is the whole session -- every turn reachable on screen at every size, asked for
by tool and id with no dates. Pilots over the contract-shaped payloads in
``support.tui``.
"""
from __future__ import annotations

import copy
import threading

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import Button, DataTable, Input, Static  # noqa: E402

from tokdash.sessions import SESSION_TOOLS  # noqa: E402
import support.tui as fx  # noqa: E402  (the layout-law tests call it fx)
from support.tui import (  # noqa: E402
    SESSION_DETAIL,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    picker_options,
    rows_of,
    run,
    settle,
    visible,
)


# --------------------------------------------------------------------------- #
# Sessions: one merged list, partial truth, whole-session detail
# --------------------------------------------------------------------------- #

SESSION_SOURCES = ("codex", "claude", "kimi")  # kimi's source is unreadable


def _sessions_body(app, pilot, sources=SESSION_SOURCES):
    app._sn_sources = sources
    app.action_show_pane("sessions")


def headers(table: DataTable) -> list[str]:
    return [str(column.label).strip() for column in table.columns.values()]


def twenty_turns() -> dict:
    """A whole-session response with more turns than any viewport can hold."""
    detail = copy.deepcopy(SESSION_DETAIL)
    detail["turns"] = [
        dict(detail["turns"][0], turn_index=index, model=f"unique-model-{index}")
        for index in range(1, 21)
    ]
    return detail


def test_sessions_fan_out_keeps_partial_truth(api):
    """A source that did not answer is named, not counted as zero sessions."""

    async def body(app, pilot):
        api.failing_sources = ("kimi",)
        _sessions_body(app, pilot)
        await settle(pilot, 4)
        note = visible(app, "#sn-note")
        # The count that stays is the one that is NEWS: three tools were asked,
        # two answered. (Which tools are in force is the head's line to say.)
        assert "2 of 3 tools answered" in note, note
        assert "Partial" in note and "kimi" not in note.split("Partial")[1][:3]
        assert "unavailable" in note and "r Retry" in note, note
        rows = rows_of(app.query_one("#sn-table", DataTable))
        assert len(rows) == 3, rows  # codex 2 + claude 1; the failure adds none

    run(api, body)


def test_identical_session_ids_in_two_sources_stay_two_rows(api):
    """Identity is the (tool, session_id) PAIR, never the bare id."""

    async def body(app, pilot):
        api.failing_sources = ()
        _sessions_body(app, pilot, ("codex", "claude"))
        await settle(pilot, 4)
        table = app.query_one("#sn-table", DataTable)
        keys = [k.value for k in table.rows.keys()]
        assert ("codex", "shared-id") in keys, keys
        assert ("claude", "shared-id") in keys, keys
        assert len(keys) == len(set(keys))
        # The two rows are visibly different sessions of different sources.
        rows = rows_of(table)
        # The session cell is "project / name  id" (the id rides after the name
        # so two tools' identical ids never read as one session), so the filter
        # reads the name the cell starts with.
        assert {"Codex", "Claude Code"} == {
            row[0] for row in rows
            if row[1].split()[0] in ("other", "reports")
        }, rows

    run(api, body)


def test_session_detail_is_the_whole_session_and_asks_for_no_dates(api):
    """/api/session takes tool + id only; the pane must say so and send no range."""

    async def body(app, pilot):
        api.failing_sources = ()
        _sessions_body(app, pilot, ("codex", "claude"))
        await settle(pilot, 4)
        table = app.query_one("#sn-table", DataTable)
        table.focus()
        # The list is grouped by tool, so the newest row of the window is not
        # necessarily row 0: move to the session this test is about by key.
        position = next(
            index for index, stored in enumerate(table.rows.keys())
            if getattr(stored, "value", stored) == ("claude", "shared-id")
        )
        table.move_cursor(row=position)
        await pilot.press("enter")
        await settle(pilot, 4)
        assert app._sn_detail_key == ("claude", "shared-id"), app._sn_detail_key
        detail = visible(app, "#sn-detail")
        assert "Entire session" in detail, detail
        assert "Claude Code · shared-id" in detail
        # The detail read carried no dates at all.
        name, args, kwargs = api.last("session_detail")
        assert list(args) == ["claude", "shared-id"], args
        assert not kwargs.get("date_from") and not kwargs.get("date_to")
        # Timing was asked for the same scope: tool + id, no dates.
        speed = api.last("session_speed")
        assert speed is not None and list(speed[1]) == ["claude", "shared-id"], speed
        # A turn with no timestamp prints a dash, never an invented time.
        assert "—" in detail

    run(api, body)


def test_search_box_takes_the_letters_that_are_also_shortcuts(api):
    """/ then typing "q", "r" or a digit must TEXT, not quit, reload or jump."""

    async def body(app, pilot):
        api.failing_sources = ()
        _sessions_body(app, pilot, ("codex", "claude"))
        await settle(pilot, 4)
        app.action_begin_search()
        await settle(pilot, 1)
        box = app.query_one("#sn-search", Input)
        assert box.display and box.has_focus
        for ch in "qr5t":
            await pilot.press(ch)
        await settle(pilot, 1)
        assert box.value == "qr5t", box.value
        rows = rows_of(app.query_one("#sn-table", DataTable))
        assert rows == [], rows  # nothing matches, and the app never quit
        await pilot.press("backspace", "backspace", "backspace", "backspace")
        await settle(pilot, 1)
        assert len(rows_of(app.query_one("#sn-table", DataTable))) == 3

    run(api, body)


def test_sessions_and_speed_keep_separate_source_selections(api):
    """Sessions fans out over its own sources; Speed filters by its own."""

    async def body(app, pilot):
        api.failing_sources = ()
        # Both selections are made the way a reader makes them: h, then the
        # chooser. Setting the two fields by hand would prove the model holds two
        # values and say nothing about whether either control can be reached.
        app.action_show_pane("sessions")
        await settle(pilot, 3)
        await pilot.press("h")
        await settle(pilot, 1)
        await pilot.press("down")  # the first supported source
        await pilot.press("enter")
        await settle(pilot, 4)
        sessions_choice = app._sn_sources
        assert sessions_choice == (SESSION_TOOLS[0],), sessions_choice
        app.action_show_pane("speed")
        await settle(pilot, 3)
        await pilot.press("h")
        await settle(pilot, 1)
        await pilot.press("down")  # "All measured sources"
        await pilot.press("down")  # codex, which has rows
        await pilot.press("down")  # kimi, which has NONE -- and is still offered
        await pilot.press("enter")
        await settle(pilot, 4)
        assert app._sp_source == "kimi", app._sp_source
        sent = api.last("speed")
        assert sent[2]["source"] == "kimi", sent
        assert "source kimi" in visible(app, "#sp-note"), visible(app, "#sp-note")
        # Back to Sessions: its fan-out is untouched by Speed's filter.
        app.action_show_pane("sessions")
        await settle(pilot, 3)
        assert app._sn_sources == sessions_choice
        # Sessions still stands where the reader left it: one tool in force, and
        # the head says so in the same words the chips use.
        assert "session tools" in visible(app, "#sn-scope"), visible(app, "#sn-scope")

    run(api, body)


# --------------------------------------------------------------------- F1 --
# The advertised Sessions source key raised NameError on the first press.


def test_h_lists_every_supported_source_and_the_pane_reads_only_that_one(api):
    async def body(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 2)
        # Wait for the supported-source fan-out to STOP issuing. Twenty reads are
        # still leaving the thread pool half a second after the tab opens, and
        # counting them mid-flight made a request of the FIRST load look like a
        # request of the filter that came after it.
        for _ in range(12):
            issued = len(api.called("sessions"))
            await settle(pilot, 1)
            if len(api.called("sessions")) == issued:
                break
        before = len(api.called("sessions"))
        await pilot.press("h")
        await settle(pilot, 1)
        offered = [value for _label, value in picker_options(app)]
        # The whole supported vocabulary, not just whatever the last response
        # happened to include.
        assert offered[0] == "" and set(offered) >= set(SESSION_TOOLS), offered
        await pilot.press("down")
        await pilot.press("enter")
        await settle(pilot, 3)
        assert app._sn_sources == (SESSION_TOOLS[0],), app._sn_sources
        asked = {call[1][0] for call in api.called("sessions")[before:]}
        assert asked == {SESSION_TOOLS[0]}, asked

    run(api, body)


# --------------------------------------------------------------------------- #
# Sessions: the tool is the first thing a reader narrows by
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# the warning strip: news, said once, and nothing when there is none
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# the detail: the whole session, every turn of it on screen
# --------------------------------------------------------------------------- #


def test_closing_a_detail_returns_you_to_the_row_you_were_on(api):
    async def body(app, pilot):
        app._sn_sources = ("codex", "claude")
        app.action_show_pane("sessions")
        await settle(pilot, 4)
        table = app.query_one("#sn-table", DataTable)
        table.focus()
        table.move_cursor(row=2)
        await settle(pilot, 1)
        await pilot.press("enter")
        await settle(pilot, 4)
        assert app._sn_detail_key is not None
        await pilot.press("escape")
        await settle(pilot, 3)
        assert app._sn_detail_key is None
        assert table.cursor_coordinate.row == 2, table.cursor_coordinate
        assert app._sn_selected_key is not None

    run(api, body, size=(100, 30))


def test_a_turn_row_keeps_a_gap_between_the_clock_and_the_model(api):
    """The timestamp field was exactly 19 wide, so the model ran into it.

    The turns live in their own table now, so the collision this guards against
    is a table one: the clock and the model are separate cells, and neither can
    eat the other's width.
    """

    async def body(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 5)
        app.query_one("#sn-table").focus()
        await pilot.press("enter")
        await settle(pilot, 5)
        turns = app.query_one("#sn-turns", DataTable)
        rows = rows_of(turns)
        assert rows, "the turns table painted nothing"
        row = rows[0]
        assert any(cell.endswith("11:00:00") for cell in row), row
        assert any(cell == "claude-opus-4-7" for cell in row), row
        shown = visible(app, "#sn-turns")
        assert "11:00:00" in shown and "claude-opus-4-7" in shown, shown
        assert "11:00:00claude" not in shown, shown

    run(api, body, size=(100, 30))


# --------------------------------------------------------------------- F9 --
# Whole-session detail printed the last eight turns and discarded the rest, so
# the work behind the session total was unreadable.


def test_session_detail_lists_every_turn_of_the_session(api):
    api.session_detail = twenty_turns()

    async def body(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 2)
        app.query_one("#sn-table", DataTable).focus()
        await pilot.press("enter")
        await settle(pilot, 3)
        turns = app.query_one("#sn-turns", DataTable)
        assert turns.display
        rows = rows_of(turns)
        assert len(rows) == 20, [row[0] for row in rows]
        assert rows[0][0].lstrip("#") == "1", rows[0]
        assert rows[-1][0].lstrip("#") == "20", rows[-1]
        # The pane says what it is showing instead of naming an API parameter.
        assert "20 turns" in visible(app, "#sn-detail"), visible(app, "#sn-detail")
        assert "entire_session" not in visible(app, "#sn-detail")

    run(api, body)


@pytest.mark.parametrize("size", [(120, 36), (100, 30), (80, 24)])
def test_the_earliest_turn_reaches_the_screen(api, size):
    api.session_detail = twenty_turns()

    async def body(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 2)
        # Seven days, so the summary block above the turns wraps to an extra
        # line: the tightest arrangement the reveal has to fit.
        await pilot.press("w")
        await settle(pilot, 2)
        app.query_one("#sn-table", DataTable).focus()
        await pilot.press("enter")
        await settle(pilot, 4)
        turns = app.query_one("#sn-turns", DataTable)
        assert turns.row_count == 20
        assert turns.has_focus, "the keyboard never followed the row that opened"
        # Focus is only worth having where the reader can see it. The pane brings
        # the table it just revealed fully on screen -- at 80x24 the last rows
        # used to sit under the fold while the arrow keys were busy in it.
        assert "unique-model-1" in visible(app, "#sn-turns"), visible(app, "#sn-turns")
        # Arrow to the newest turn, then all the way back to the oldest: both
        # reach the screen at every width, which is the reading the old
        # eight-row view could not give at any.
        for _ in range(19):  # Pilot has no repeat= in this Textual
            await pilot.press("down")
        await settle(pilot, 2)
        assert turns.cursor_row == 19, turns.cursor_row
        assert "unique-model-20" in visible(app, "#sn-turns"), visible(app, "#sn-turns")
        for _ in range(22):
            await pilot.press("up")
        await settle(pilot, 2)
        assert turns.cursor_row == 0, turns.cursor_row
        assert "unique-model-1" in visible(app, "#sn-turns"), visible(app, "#sn-turns")

    run(api, body, size=size)


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
def test_the_last_turn_stays_on_screen_when_the_timing_lands_late(api, size):
    # The session's timing is a fetch of its own, after the turns. Landing once
    # the pane had already scrolled the turns into view, it grew the summary by
    # a line and pushed the last turn under the footer (a loaded CI runner, at
    # 100x30).
    api.session_detail = twenty_turns()
    gate = threading.Event()

    def held(*_args, **_kwargs):
        gate.wait(8)
        return None

    api.hooks["session_speed"] = held

    async def body(app, pilot):
        try:
            app.action_show_pane("sessions")
            await settle(pilot, 2)
            await pilot.press("w")
            await settle(pilot, 2)
            app.query_one("#sn-table", DataTable).focus()
            await pilot.press("enter")
            await settle(pilot, 4)
            turns = app.query_one("#sn-turns", DataTable)
            assert turns.has_focus
            assert app._sn_detail_speed is None, "the timing was not held back"
        finally:
            gate.set()
        await settle(pilot, 2)
        assert app._sn_detail_speed is not None
        for _ in range(19):
            await pilot.press("down")
        await settle(pilot, 2)
        assert turns.cursor_row == 19, turns.cursor_row
        assert "unique-model-20" in visible(app, "#sn-turns"), visible(app, "#sn-turns")

    run(api, body, size=size)
