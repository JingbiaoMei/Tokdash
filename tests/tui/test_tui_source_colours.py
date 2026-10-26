"""A source wears one colour: the same hue in every pane and every table.

A source's colour is its identity across the dashboard. The palette is keyed on
the canonical id, never on the display label (so "Codex" cannot hash to another
tool's hue), the badge carries the tint on both its edge and its word, and each
tool's chip is a ground of its own hue rather than one grey pill with different
words in it. What a monochrome terminal keeps instead is in
test_tui_overview_pane.py. Pilots over the contract-shaped payloads in
``support.tui``.
"""
from __future__ import annotations

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import DataTable  # noqa: E402

import tokdash.tui.app as app_mod  # noqa: E402
from tokdash.tui.formatting import tool_tint  # noqa: E402
import support.tui as fx  # noqa: E402  (the layout-law tests call it fx)
from support.tui import (  # noqa: E402
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    rows_of,
    run,
    settle,
)


# ---------------------------------------------------------------- presentation
# The review's one carry-over: keep the source identity the approved design
# gives each harness, now that the tables are the whole view.


def test_each_source_keeps_its_own_colour_in_the_tables(api):
    async def body(app, pilot):
        app.action_show_pane("overview")
        await settle(pilot, 2)
        table = app.query_one("#ov-table", DataTable)
        edge = app_mod._BADGE_EDGE
        # Revision 5's badge puts the tint on TWO runs -- the accent edge and the
        # label -- so the law is read off the cell's runs, and a source is only
        # badged when the edge and the word carry the same colour.
        styles = {
            cell.plain.removeprefix(edge): " ".join(
                [str(cell.style)] + [str(span.style) for span in cell.spans]
            )
            for cell in table.get_row_at(0) + table.get_row_at(1)
            if hasattr(cell, "plain")
        }
        assert "bold #79dfbd" in styles.get("Codex", ""), styles
        assert "bold #f2bd78" in styles.get("Claude Code", ""), styles
        assert styles["Codex"].count("#79dfbd") == 2, styles   # edge AND label
        assert styles["Codex"] != styles["Claude Code"], styles

        app.action_show_pane("sessions")
        await settle(pilot, 2)
        sessions = app.query_one("#sn-table", DataTable)
        first = sessions.get_row_at(0)
        assert any(
            getattr(cell, "plain", "").removeprefix(edge) in {"Codex", "Claude Code"}
            and "#" in str(getattr(cell, "style", ""))
            + " ".join(str(span.style) for span in getattr(cell, "spans", ()))
            for cell in first
        ), first

    run(api, body)


# --------------------------------------------------------------------- R5-F3 --
# A source's colour IS its identity across the dashboard. The palette is keyed
# on the canonical id; the screen's words are the display label.
# --------------------------------------------------------------------------- #


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
