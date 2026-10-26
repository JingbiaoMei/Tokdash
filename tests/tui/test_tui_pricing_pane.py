"""The Pricing pane: a mirror of the price book, never an editor and never a guess.

A rate the book never recorded prints as a gap, not as a free model; the note
names the active version and warns when an override replaces the bundled
baseline; search reaches a model through its alias; and the cursor stays on
screen down a list of a few hundred models. Pilots over the contract-shaped
payloads in ``support.tui``.
"""
from __future__ import annotations

import copy

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import DataTable, Input  # noqa: E402

from support.tui import (  # noqa: E402
    SHARED_MODEL,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    headers_of,
    rows_of,
    run,
    settle,
    visible,
)


# --------------------------------------------------------------------------- #
# Pricing: a mirror of the price book, never an editor and never a guess
# --------------------------------------------------------------------------- #


def test_pricing_prints_a_missing_rate_as_a_gap_not_a_zero(api):
    async def body(app, pilot):
        app.action_show_pane("pricing")
        await settle(pilot, 4)
        table = app.query_one("#pr-table", DataTable)
        heads = headers_of(table)
        rows = {r[0]: r for r in rows_of(table)}
        full = rows[SHARED_MODEL]
        assert full[heads.index("Input")] == "1.2500", full
        # gpt-5-mini records input and output only. A rate that was never
        # recorded is not a free model, and 0.0000 would say it is.
        mini = rows["gpt-5-mini"]
        assert mini[heads.index("Cache read")] == "\u2014", mini
        assert mini[heads.index("Cache write")] == "\u2014", mini
        assert "read-only" in visible(app, "#pr-note")

    run(api, body)


def test_pricing_names_the_active_version_and_warns_about_an_override(api):
    async def body(app, pilot):
        app.action_show_pane("pricing")
        await settle(pilot, 4)
        note = visible(app, "#pr-note")
        assert "version 2026-09-01" in note, note
        assert "source override" in note, note
        # An override is not the shipped book: it replaces it and freezes it.
        assert "fully replaces the bundled baseline" in note, note
        assert "1 aliases" in note, note

    run(api, body)


def test_pricing_search_reaches_a_model_through_its_alias(api):
    async def body(app, pilot):
        app.action_show_pane("pricing")
        await settle(pilot, 4)
        box = app.query_one("#pr-search", Input)
        box.focus()
        box.value = "gpt-5-codex"          # the alias, not the canonical name
        await settle(pilot, 3)
        rows = rows_of(app.query_one("#pr-table", DataTable))
        assert [r[0] for r in rows] == [SHARED_MODEL], rows
        assert "1 of 2 models" in visible(app, "#pr-note")

    run(api, body)


def test_a_long_pricing_list_keeps_its_cursor_on_screen(api):
    """The arrow key may not walk the reader into the clip.

    A few hundred models is an ordinary pricing database. A table sized to all of
    its rows believes there is nothing left to scroll, so everything past the
    bottom of the pane sits under the clip WITH the cursor on it -- and a reader
    who cannot see the row they are on cannot search it or read it.
    """
    long_pricing = copy.deepcopy(api.pricing)
    models = long_pricing["data"]["models"]
    first = next(iter(models.values()))
    for index in range(80):
        models[f"probe-{index:02d}"] = copy.deepcopy(first)
    api.pricing = long_pricing

    async def body(app, pilot):
        app.action_show_pane("pricing")
        await settle(pilot, 3)
        table = app.query_one("#pr-table", DataTable)
        table.focus()
        for _ in range(24):
            await pilot.press("down")
            await settle(pilot, 1)
        row = table.cursor_coordinate.row
        assert row >= 20, row
        label = str(table.get_row_at(row)[0])
        assert label in visible(app, "#pr-table"), (label, row)

    run(api, body, size=(100, 30))
