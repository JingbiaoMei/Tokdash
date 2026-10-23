"""Fifth-review regressions (2026-10-22 review of 042b54e).

Three ways the pane still told the truth about a size it was not in:

* a resize refitted the CHROME once the layout settled but painted the BODY
  against the geometry being replaced, so a 120x36 Overview squeezed to eighty
  columns kept 112 cells of card in a 78-cell widget and "450.0K" arrived "450";
* the Speed picture promised one cell per bucket plus one cell of air whether or
  not its box had them, so thirty daily buckets drew fifty-nine cells into a
  fifty-two-cell allocation and the numeric table beside them lost its response
  counts off the right-hand edge;
* a Speed source badge looked up its colour by DISPLAY name, so "Codex" -- a
  string the palette has never heard -- hashed to another tool's hue and the
  same source wore mint in Overview and amber here.

House rules as in the earlier rounds: drive the control a reader uses, then read
back what the pane SHOWS. Fixtures are the contract-shaped ones in
``test_tui_revision5``; every figure is synthetic.
"""
from __future__ import annotations

import datetime as dt

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from rich.cells import cell_len  # noqa: E402
from rich.text import Text  # noqa: E402
from textual.widgets import DataTable  # noqa: E402

from tokdash.tui.charts import column_chart, column_chart_needs  # noqa: E402
from tokdash.tui.formatting import tool_tint  # noqa: E402
from test_tui_revision5 import (  # noqa: E402
    api,
    press,
    rows_of,
    run,
    settle,
    visible,
)


def month_of_buckets(api, count=30):
    """Turn the fixture's two-hour trend into a month of DAILY buckets.

    Thirty-one days is what a "Last Month" window asks for, and it is the first
    series long enough to want more cells than the Speed picture has beside its
    table. The rates are written distinct so a merged or dropped bucket shows.
    """
    days = [(dt.date(2026, 9, 1) + dt.timedelta(days=i)).isoformat()
            for i in range(count)]
    api.speed_trend["granularity"] = "day"
    api.speed_trend["buckets"] = [
        {"key": day, "from": day, "to": day} for day in days
    ]
    api.speed_trend["offered_buckets"] = [{"bucket": "day", "count": count},
                                          {"bucket": "week", "count": count // 7 + 1}]
    for series in api.speed_trend["series"]:
        series.update(per_stream=[30.0 + i for i in range(count)],
                      combined=[60.0 + i for i in range(count)],
                      calls=[40 + i for i in range(count)],
                      parallelism=[1.4] * count, spread={})
    for series in api.speed_trend["model_series"]:
        series.update(combined=[60.0 + i for i in range(count)],
                      calls=[40 + i for i in range(count)],
                      parallelism=[1.2] * count)
    return days


WIDE = (120, 36)



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


# --------------------------------------------------------------------- R5-F2 --
# The picture's columns are the buckets the route returned. It may not merge,
# drop or re-bucket them, so the WIDTH they need is a fact about the response,
# and the arrangement is what has to answer it.
# -------------------------------------------------------------------------- #


@pytest.mark.parametrize("count", [2, 7, 24, 30, 31, 45, 168])
def test_the_picture_spends_no_more_cells_than_it_is_given(count):
    for width in (20, 48, 60, 116):
        chart = column_chart(
            [{"label": f"b{i}", "value": float(i + 1), "text": ""} for i in range(count)],
            width=width, height=5,
        )
        if chart["available"]:
            assert chart["used_width"] <= width, (count, width, chart["used_width"])
            # Never a merged bucket, never a dropped one.
            assert chart["columns"] == count, (count, width, chart["columns"])
        else:
            # It refuses ONLY when one cell per bucket will not fit: that is the
            # point where a picture would have to lie about the series.
            assert count > width, (count, width)


def test_the_picture_says_what_it_needs_before_it_is_asked():
    """The arrangement and the renderer agree on one cell each plus the air."""
    assert column_chart_needs(0) == 0
    assert column_chart_needs(1) == 1
    assert column_chart_needs(2) == 3
    assert column_chart_needs(30) == 59
    assert column_chart_needs(31) == 61


def test_a_month_of_buckets_leaves_the_table_its_figures(api):
    """The picture moves ABOVE the rows rather than taking their columns."""

    async def body(app, pilot):
        month_of_buckets(api)
        app.action_set_range("lastmonth")
        await settle(pilot, 2)
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-mode-trend")
        await settle(pilot, 3)

        chart = app.query_one("#sp-chart")
        table = app.query_one("#sp-table", DataTable)
        assert chart.display, "a month of buckets still gets its picture"
        assert not table.show_horizontal_scrollbar, visible(app, "#sp-table")

        # The responses behind each rate are the whole point of the column, and
        # they are distinct per bucket here: a clipped one reads as a shorter bar.
        painted = visible(app, "#sp-table")
        assert "Responses" in painted, painted
        # The first buckets' counts are in the viewport; a column squeezed by a
        # picture that overran its box loses them first, and loses its header
        # before that.
        assert "40" in painted and "41" in painted, painted
        rows = rows_of(table)
        assert len(rows) == 30, len(rows)

        # Above, not beside: the picture got the pane's own width rather than
        # the half of it a side-by-side box leaves, so its thirty columns keep
        # the air that makes a BLANK one read as an unmeasured bucket rather
        # than as a very short bar.
        assert chart.has_class("-stacked") and chart.region.width > 80, chart.region
        assert chart.region.y < table.region.y, (chart.region, table.region)

    run(api, body, size=(120, 36))


def test_a_series_with_room_still_stands_beside_its_rows(api):
    """The new rule may not stack what fits: the preview draws them together."""

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-mode-trend")
        await settle(pilot, 3)
        chart = app.query_one("#sp-chart")
        table = app.query_one("#sp-table", DataTable)
        assert chart.display
        # Side by side: the two share the same band of rows.
        assert abs(chart.region.y - table.region.y) <= 1, (chart.region, table.region)
        assert chart.region.width < app.query_one("#pane-speed").region.width - 20

    run(api, body, size=(120, 36))


# --------------------------------------------------------------------- R5-F3 --
# A source's colour IS its identity across the dashboard. The palette is keyed
# on the canonical id; the screen's words are the display label.
# -------------------------------------------------------------------------- #


def test_a_source_wears_one_colour_in_every_pane(api):
    def tint_of(cell) -> str:
        spans = str(getattr(cell, "spans", ""))
        found = [tint for tint in (tool_tint("codex"), tool_tint("claude"))
                 if tint in spans]
        assert len(found) == 1, spans  # exactly one source hue, or the cell lies
        return found[0]

    async def body(app, pilot):
        # Overview lists the same harness first, in its own table.
        overview = app.query_one("#ov-table", DataTable)
        first = next(overview.get_row_at(i) for i in range(overview.row_count)
                     if "Codex" in str(overview.get_row_at(i)[0]))
        assert tint_of(first[0]) == tool_tint("codex"), str(first[0].spans)

        app.action_show_pane("speed")
        await settle(pilot, 3)
        speed = app.query_one("#sp-table", DataTable)
        column = next(i for i, c in enumerate(speed.columns.values())
                      if str(c.label) == "Source")
        rows = [speed.get_row_at(i) for i in range(speed.row_count)
                if "Codex" in str(speed.get_row_at(i)[column])]
        assert rows, rows_of(speed)
        for row in rows:
            assert tint_of(row[column]) == tool_tint("codex"), str(row[column].spans)

        app.action_show_pane("sessions")
        await settle(pilot, 3)
        sessions = app.query_one("#sn-table", DataTable)
        tool_column = next(i for i, c in enumerate(sessions.columns.values())
                           if str(c.label) == "Tool")
        for index in range(sessions.row_count):
            row = sessions.get_row_at(index)
            cell = str(row[tool_column])
            if "Codex" in cell:
                assert tint_of(row[tool_column]) == tool_tint("codex"), cell

    run(api, body, size=(120, 36))


# --------------------------------------------------------------------- R5-F1 --
# A resize is the one moment the page knows BOTH sizes. Everything measured
# against the old one has to be measured again once the new one has landed.
# -------------------------------------------------------------------------- #


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
