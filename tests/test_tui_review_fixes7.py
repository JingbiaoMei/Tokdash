"""Seventh-review regressions (CI failures on 2a5723a, 2026-10-23).

Two ways the page kept a fit for a frame it was no longer in, both of which the
runner found and Linux could not:

* a resize that arrived while a read was in flight was REFUSED -- correctly, since
  repainting across a live load pours retained figures over a cleared table -- and
  nothing paid that refit back. The guard that refused is the same guard that
  would have done the work, and it is never asked twice about the same frame;
* the footer re-wrapped to a second row (the data line is a real path, and CI's
  is long) AFTER the body had fitted. The pane's own box says nothing about it --
  same width, same height, same density -- so the guard between "fitted" and
  "stale" saw no change and left the activity band drawn four rows below the
  screen's last line.

Both are pinned here the way the earlier rounds do it: move the frame a reader
can move, then read back what the page SHOWS. Fixtures are the contract-shaped
ones in ``test_tui_revision5``; every figure is synthetic.
"""
from __future__ import annotations

import threading

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from tokdash.tui.data import FetchOutcome  # noqa: E402
from test_tui_revision5 import (  # noqa: E402
    USAGE,
    api,
    run,
    settle,
    visible,
)

WIDE = (120, 36)
NARROW = (80, 24)


async def press(app, pilot, key: str) -> None:
    """A tab key, with the pause the pane needs to have started its read."""
    await pilot.press(key)
    await settle(pilot, 1)


async def _settled(app, pilot, pane: str = "overview") -> None:
    for _ in range(40):
        if app._state.get(pane) == "ok" and not app._open_loads.get(pane):
            return
        await pilot.pause(0.1)
    raise AssertionError(f"{pane} never settled")


# --------------------------------------------------------------------- R7-F1 --
# A refused refit is only safe while it is REMEMBERED. The frame the refusal was
# for is the frame the page then has to live in, and no second ask is coming.
# -------------------------------------------------------------------------- #


def test_a_refit_refused_during_a_read_is_paid_when_that_read_lands(api):
    """Resize across a live reload, and the wide page has to arrive anyway."""
    release = threading.Event()

    def held(*args, **kwargs):
        assert release.wait(10.0), "the read was never released"
        return FetchOutcome(value=USAGE, status="hit", age_seconds=0.0)

    async def body(app, pilot):
        await _settled(app, pilot)
        api.hooks["usage"] = held
        await pilot.press("r")
        await settle(pilot, 1)
        assert app._open_loads.get("overview"), "the reload is not in flight"

        await pilot.resize_terminal(*WIDE)
        await settle(pilot, 2)
        # The refusal happened, and it was written down rather than dropped.
        assert "overview" in app._refit_owed, sorted(app._refit_owed)

        release.set()
        await settle(pilot, 8)
        await _settled(app, pilot)
        assert "overview" not in app._refit_owed, sorted(app._refit_owed)
        # The page now stands for the frame it is in, by its own ledger and on
        # the screen: the six-card band is the arrangement 120 columns buys.
        assert app._body_fitted["overview"] == app._body_stamp("overview")
        lines = str(app.query_one("#ov-cards").content).splitlines()
        assert any("TOTAL TOKENS" in line and "MOST USED MODEL" in line
                   for line in lines), lines
        assert "\u2026" not in visible(app, "#ov-table"), visible(app, "#ov-table")

    run(api, body, size=NARROW)


def test_a_refit_refused_under_a_chooser_is_paid_when_it_closes(api):
    """The chooser path already paid; it must keep paying through the same ledger."""

    async def body(app, pilot):
        await _settled(app, pilot)
        await pilot.press("p")
        await settle(pilot, 1)
        assert len(app.screen_stack) > 1
        await pilot.resize_terminal(*WIDE)
        await settle(pilot, 2)
        assert "overview" in app._refit_owed, sorted(app._refit_owed)
        await pilot.press("escape")
        await settle(pilot, 6)
        assert len(app.screen_stack) == 1
        assert "overview" not in app._refit_owed, sorted(app._refit_owed)
        assert app._body_fitted["overview"] == app._body_stamp("overview")

    run(api, body, size=NARROW)


# --------------------------------------------------------------------- R7-F2 --
# The footer owns the screen's last row, and the page may not draw into it. When
# the footer takes a row the pane's own box did not move, so the only witness is
# the row the strip sits on.
# -------------------------------------------------------------------------- #


def test_a_footer_that_changes_its_depth_leaves_the_page_fitted(api):
    """The page ends fitted to the frame it is in, footer included.

    The footer owns the screen's last row, and the body's row budget runs DOWN to
    that row, so a strip that re-wraps -- the database line landing late, the
    freshness text appearing or going -- takes or gives back a row on every page.
    Windows CI is where this showed: the pane's box measured right, the strip two
    rows deep, and the activity band still drawn for the frame the picture had
    been sized in, because the one guard between "fitted" and "stale" had never
    seen the strip move. A short database line and a long one are the two states
    a running app actually passes through, so both directions are driven here.
    """

    async def body(app, pilot):
        await _settled(app, pilot)
        strip = app.query_one("#status")
        pane = app.query_one("#pane-overview")
        band = app.query_one("#ov-activity")
        await settle(pilot, 3)

        for line in ("usage.db " + "x" * 200, "usage.db", "usage.db " + "y" * 200):
            app._db_line = line
            app._update_status()
            await settle(pilot, 5)
            rows = app._strip_rows
            assert rows in (1, 2), rows
            # The row the footer owns is part of the frame the page was measured
            # at. Without it, "one row of footer" and "two" are the same frame to
            # the guard, which is how a correctly-sized pane ended up drawn into
            # the strip.
            stamp = app._body_stamp("overview")
            assert stamp and stamp[-1] == strip.region.y, (stamp, strip.region)
            # Every row the footer took, the page gave back, and the ledger knows.
            assert pane.region.height == strip.region.y - pane.region.y, (
                pane.region, strip.region)
            assert band.region.bottom <= strip.region.y, (band.region, strip.region)
            assert app._body_fitted["overview"] == app._body_stamp("overview"), (
                app._body_fitted.get("overview"), app._body_stamp("overview"))

    run(api, body, size=WIDE)


def test_the_row_budget_uses_the_footer_row_it_knows(api):
    """A footer the layout has not caught up with must not buy rows from nowhere.

    The last section on a page is sized to the rows between its own top and the
    row the footer owns, and the app ALREADY knows that row -- it is the number of
    rows the strip was written with, subtracted from the height of the screen,
    which is exactly how the panes are sized. Asking the layout instead is asking
    a question that has no answer mid-resize: the strip has been rewritten and not
    yet re-laid-out, and an answer of "no rows at all" used to fall through to a
    full-height guess. That is the frame the band was drawn into the footer in.
    """

    async def body(app, pilot):
        await _settled(app, pilot)
        strip = app.query_one("#status")
        widget = app.query_one("#ov-activity")
        await settle(pilot, 3)
        assert app._strip_rows in (1, 2), app._strip_rows
        before = app._rows_above_strip(widget)
        assert before > 0, before

        # The strip knows it is two rows deep; the layout has not been told yet.
        app._strip_rows = 2
        strip.styles.height = "1"
        await settle(pilot, 3)
        assert app._rows_above_strip(widget) == before, (
            "the row budget followed a footer measurement instead of the footer")

    run(api, body, size=WIDE)


def test_a_new_size_gets_its_own_settle_allowance(api):
    """The chain's bound stops a chase, not a session: every resize starts at zero."""

    async def body(app, pilot):
        await _settled(app, pilot)
        app._fit_rearms = app._FIT_MAX_REARMS  # the last frame's leftovers
        await pilot.resize_terminal(*WIDE)
        await settle(pilot, 6)
        strip = app.query_one("#status")
        pane = app.query_one("#pane-overview")
        assert pane.region.height == strip.region.y - pane.region.y, (
            pane.region, strip.region)
        assert app._body_fitted["overview"] == app._body_stamp("overview")
        lines = str(app.query_one("#ov-cards").content).splitlines()
        assert any("TOTAL TOKENS" in line and "MOST USED MODEL" in line
                   for line in lines), lines

    run(api, body, size=NARROW)


# --------------------------------------------------------------------- R7-F3 --
# A repaint is not a read. It may redraw every figure the snapshot holds, and it
# may not answer a question the snapshot was never asked -- which is what the
# poll's line does: it says what the LAST POLLS did, from the reader's own
# keystroke, and nothing in the accounts file knows about it.
# -------------------------------------------------------------------------- #


def test_a_repaint_leaves_the_poll_note_alone_until_a_read_answers_it(
    api, monkeypatch
):
    """Polling, then a repaint: the reader still sees the poll's own sentence."""
    import tokdash.cli as cli_mod

    release = threading.Event()
    calls = {"n": 0}

    def held(**kwargs):
        calls["n"] += 1
        assert release.wait(10.0), "the poll was never released"
        return {"snapshots": 1, "inserted": 1, "network_sources": []}

    monkeypatch.setattr(cli_mod, "_quota_poll_once", held)

    async def body(app, pilot):
        await press(app, pilot, "3")
        await _settled(app, pilot, "quota")
        note = app.query_one("#quota-note")
        await pilot.press("u")
        await settle(pilot, 2)
        assert "polling" in str(note.content), str(note.content)

        # The two things a running app does to a pane that are not a read.
        app._repaint_pane("quota")
        await settle(pilot, 2)
        assert "polling" in str(note.content), (
            "a repaint replaced the poll's own line with a derived banner: "
            f"{note.content}")

        release.set()
        await settle(pilot, 8)
        await _settled(app, pilot, "quota")
        assert calls["n"] == 1, calls
        # A READ is what retires the sentence, and the reload's banner says so.
        assert "polling" not in str(note.content), str(note.content)

    run(api, body, size=WIDE)


# --------------------------------------------------------------------- R7-F5 --
# The section at the foot of the page is sized against the rows its neighbours
# leave it. It used to be given a FLOOR as well, and a floor is a promise the
# rest of the page did not agree to: the shortest picture the band will draw is
# eight rows, and a page holding five draws three rows of it through the status
# strip. Windows CI met it at 120x36 (a two-row footer, airy table rows); the
# same arithmetic is met here at 80x24, where it is not a race at all.
# -------------------------------------------------------------------------- #


@pytest.mark.parametrize("size", [(80, 24), (90, 26), (100, 30), (120, 32),
                                  (120, 36), (140, 40)])
def test_the_band_at_the_foot_of_the_page_stops_above_the_footer(api, size):
    """Whatever a terminal is given, the page's last section stays inside it."""

    async def body(app, pilot):
        await _settled(app, pilot)
        band = app.query_one("#ov-activity")
        strip = app.query_one("#status")
        await settle(pilot, 4)
        assert band.region.bottom <= strip.region.y, (
            f"{size}: the band is drawn past the row the footer owns: "
            f"{band.region} vs strip {strip.region}")
        # Refusing the PICTURE is allowed when the rows are not there. Losing what
        # the band is for is not: the window's shape has to be said somewhere.
        shown = visible(app, "#ov-activity")
        assert "Token activity" in shown, shown

    run(api, body, size=size)


def test_a_page_squeezed_onto_short_rows_keeps_its_last_section_inside(api):
    """The same promise after the frame shrinks, when the rows are taken back."""

    async def body(app, pilot):
        await _settled(app, pilot)
        band = app.query_one("#ov-activity")
        strip = app.query_one("#status")
        await settle(pilot, 3)
        await pilot.resize_terminal(*NARROW)
        healed = False
        for _ in range(20):
            await pilot.pause(0.25)
            if band.region.bottom <= strip.region.y:
                healed = True
                break
        assert healed, f"{band.region} vs strip {strip.region}"
        assert "Token activity" in visible(app, "#ov-activity")

    run(api, body, size=WIDE)
