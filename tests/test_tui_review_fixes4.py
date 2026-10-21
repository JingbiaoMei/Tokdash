"""Fourth-review regressions (2026-10-20 review of af3eb12).

Two Speed surfaces still described one thing while showing another:

* Hour of day named the model the reader had JUST picked while the table still
  held the previous model's rates. The chooser writes ``_sp_model`` / ``_sp_key``
  before its read completes, and any repaint (a resize, a build-progress tick)
  painted from those, so a slow -- or failed -- series read put one model's
  heading over another model's numbers. Permanently, in the failure case.
* the Combined row detail explained its rate as "summed tokens over summed
  duration", which is the PER-STREAM denominator. Combined divides by the UNION
  of busy time, so the sentence contradicted the figure above it.

House rules as in the earlier rounds: drive the control a reader uses, then read
back what the pane SHOWS, whitespace folded because the terminal wraps. Fixtures
are the contract-shaped ones in ``test_tui_revision5``; every figure is synthetic.
"""
from __future__ import annotations

import copy
import threading

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import DataTable, ListView  # noqa: E402

from tokdash.tui.data import FetchOutcome  # noqa: E402
from test_tui_revision5 import (  # noqa: E402
    SHARED_MODEL,
    api,
    press,
    rows_of,
    run,
    settle,
    visible,
)
from test_tui_review_fixes import picker_options  # noqa: E402

SECOND_MODEL = "second-model"
SECOND_RATE = "77.0"


def fold(app, selector: str) -> str:
    """What the pane shows, with the terminal's line wraps folded back out."""
    return " ".join(visible(app, selector).split())


def detail_of(app) -> str:
    return " ".join(str(app.query_one("#sp-detail").render()).split())


def second_hour_payload(api):
    """The hour payload for a SECOND model, with rates nothing else has.

    It is handed back UNINSTALLED: the pane must load the fixture's own model
    first, or the test would compare a payload against itself. Installing it in
    the chooser's inventory is a separate step, and the two lenses read that
    inventory from different places -- a per-stream comparison key for one lens,
    a merged model series for the other.
    """
    stream = copy.deepcopy(api.speed_trend["series"][0])
    stream.update(
        model=SECOND_MODEL,
        per_stream=[float(SECOND_RATE)] * 2, combined=[88.0] * 2,
        calls=[7, 0], parallelism=[1.1] * 2,
        spread={"p10": [60.0] * 2, "p50": [77.0] * 2, "p90": [90.0] * 2},
    )
    api.speed_trend["series"].append(stream)
    merged = copy.deepcopy(api.speed_trend["model_series"][0])
    merged.update(
        model=SECOND_MODEL,
        combined=[88.0] * 2, calls=[7, 0], parallelism=[1.1] * 2,
        hours={"combined": [float(SECOND_RATE)] * 24,
               "calls": [7] * 24, "parallelism": [1.1] * 24},
    )
    api.speed_trend["model_series"].append(merged)
    row = copy.deepcopy(api.speed_across["rows"][0])
    row["model"] = SECOND_MODEL
    api.speed_across["rows"].append(row)

    hour = copy.deepcopy(api.speed_hour)
    hour["selected_key"]["model"] = SECOND_MODEL
    for entry in hour["hourly"]:
        if entry.get("output_tok_per_s") is not None:
            entry["output_tok_per_s"] = float(SECOND_RATE)
    return hour


def series_hook(second_hour, gate=None, boom=None):
    """Answer ONLY the series reads, leaving the snapshot read alone.

    ``gate`` holds the series read open (a slow answer), ``boom`` fails it. Either
    way the across-models read and, once released, the series read itself, still
    get their fixtures.
    """
    def hook(**kwargs):
        view = kwargs.get("view")
        if view not in ("trend", "time-of-day"):
            return None
        if gate is not None:
            gate[0].set()
            gate[1].wait(12)
        if boom is not None:
            raise boom
        if view == "time-of-day":
            return FetchOutcome(value=copy.deepcopy(second_hour),
                                status="hit", age_seconds=1.0)
        return None
    return hook


async def open_hour(app, pilot, lens):
    app.action_show_pane("speed")
    await settle(pilot, 2)
    if lens == "combined":
        press(app, "sp-lens-combined")
        await settle(pilot, 1)
    press(app, "sp-mode-hour")
    await settle(pilot, 2)


async def choose_second(app, pilot):
    """Pick the second series with ``k``, the only way a reader can."""
    await pilot.press("k")
    await settle(pilot, 1)
    options = [str(v) for _label, v in picker_options(app)]
    wanted = next(i for i, v in enumerate(options) if SECOND_MODEL in v)
    picker = app.screen.query_one("#view-picker-list", ListView)
    for _ in range(abs(wanted - (picker.index or 0))):
        await pilot.press("down" if (picker.index or 0) < wanted else "up")
    assert SECOND_MODEL in str(picker_options(app)[picker.index or 0][1])
    await pilot.press("enter")
    await settle(pilot, 2)


# --------------------------------------------------------------------- R4-F1 --
# A pending or failed series read may not put the new name over the old rows.


@pytest.mark.parametrize("lens", ["stream", "combined"])
def test_a_delayed_hour_read_keeps_the_name_of_the_rows_it_shows(api, lens):
    """The heading follows the LOADED payload, and the wait is said out loud."""
    second_hour = second_hour_payload(api)
    entered, release = threading.Event(), threading.Event()

    async def body(app, pilot):
        try:
            await open_hour(app, pilot, lens)
            before = rows_of(app.query_one("#sp-table", DataTable))
            api.hooks["speed"] = series_hook(second_hour, gate=(entered, release))
            await choose_second(app, pilot)
            assert entered.is_set(), "the delayed read must actually have started"
            await pilot.resize_terminal(100, 30)  # a repaint must not relabel
            await settle(pilot, 1)
            note = fold(app, "#sp-note")
            table = rows_of(app.query_one("#sp-table", DataTable))
            assert table == before, "a resize must not invent new measurements"
            # The old figures keep the OLD name, and the pane says it is waiting.
            assert SECOND_MODEL not in note, note
            assert "still loading" in note, note
            assert SHARED_MODEL in note, note
            # the pick IS named, but under the table, not as its heading
            assert SECOND_MODEL in detail_of(app), detail_of(app)
            release.set()
            await settle(pilot, 5)
            # When it lands, heading, note and rows all move together.
            note = fold(app, "#sp-note")
            assert SECOND_MODEL in note, note
            assert "still loading" not in note, note
            assert [r for r in rows_of(app.query_one("#sp-table", DataTable))
                    if SECOND_RATE in r], rows_of(app.query_one("#sp-table", DataTable))
        finally:
            release.set()
            await settle(pilot, 2)

    run(api, body)


@pytest.mark.parametrize("lens", ["stream", "combined"])
def test_a_failed_hour_read_says_so_instead_of_reusing_the_name(api, lens):
    """A series read that dies keeps its rows, names them honestly, and offers r."""
    second_hour = second_hour_payload(api)
    boom = RuntimeError("series read exploded")

    async def body(app, pilot):
        await open_hour(app, pilot, lens)
        before = rows_of(app.query_one("#sp-table", DataTable))
        api.hooks["speed"] = series_hook(second_hour, boom=boom)
        await choose_second(app, pilot)
        await pilot.resize_terminal(100, 30)
        await settle(pilot, 2)
        note = fold(app, "#sp-note")
        assert not (SECOND_MODEL in note
                    and rows_of(app.query_one("#sp-table", DataTable)) == before), {
            "note": note, "rows": before,
        }
        # The strip is the ONE amber line and it stays readable at a glance; the
        # reason the read died is said under the table, where it cannot be read as
        # a heading over another series' rates. Both are on screen; neither is a
        # third line above the picture.
        assert "update failed" in note, note
        assert "r Retry" in note, note
        assert SHARED_MODEL in note, note  # the rows still belong to model one
        assert rows_of(app.query_one("#sp-table", DataTable)) == before
        assert SECOND_MODEL in detail_of(app), detail_of(app)
        assert "series read exploded" in detail_of(app), detail_of(app)

    run(api, body)


def test_the_next_snapshot_read_pays_the_series_debt(api):
    """A stale failure note cannot outlive the read that replaces it."""
    second_hour = second_hour_payload(api)

    async def body(app, pilot):
        await open_hour(app, pilot, "combined")
        api.hooks["speed"] = series_hook(second_hour, boom=RuntimeError("series read exploded"))
        await choose_second(app, pilot)
        await settle(pilot, 2)
        assert "update failed" in fold(app, "#sp-note")
        api.hooks.pop("speed", None)
        app.action_refresh()
        await settle(pilot, 5)
        note = fold(app, "#sp-note")
        assert "update failed" not in note, note
        assert "still loading" not in note, note

    run(api, body)


# --------------------------------------------------------------------- R4-F2 --
# Each lens explains the denominator it actually divided by.


def test_combined_detail_explains_busy_time_not_summed_durations(api):
    # Two overlapping one-second responses of 100 tokens: 200 tok/s of wall-clock
    # throughput over ONE busy second, and 100 tok/s per stream over TWO.
    model = api.speed_across["models"][0]
    model.update(
        speed_tokens=200, speed_ms=2000.0, busy_ms=1000.0,
        speed_calls=2, eligible_calls=2, coverage={"ratio": 1.0},
        combined_tok_per_s=200.0, parallelism=2.0, peak_in_flight=2,
    )

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-lens-combined")
        await settle(pilot, 2)
        table = app.query_one("#sp-table", DataTable)
        assert any("200.0" in row for row in rows_of(table)), rows_of(table)
        table.focus()
        await pilot.press("ctrl+end")  # the merged row, not a per-stream one
        await pilot.press("enter")
        await settle(pilot, 2)
        detail = " ".join(visible(app, "#sp-detail").split())
        assert "summed tokens over summed duration" not in detail, detail
        assert "UNION" in detail, detail
        assert "measured" in detail and "eligible" in detail, detail

    run(api, body)


def test_per_stream_detail_keeps_its_own_denominator(api):
    """The other lens still says SUMMED durations, because that is its math."""
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        table = app.query_one("#sp-table", DataTable)
        table.focus()
        await pilot.press("ctrl+end")
        await pilot.press("enter")
        await settle(pilot, 2)
        detail = " ".join(visible(app, "#sp-detail").split())
        assert "SUMMED response durations" in detail, detail
        assert "UNION" not in detail, detail

    run(api, body)
