"""Choosing a Speed series: the name over the rows is the name of the rows.

``h`` picks a source and ``k`` a series -- an exact comparison key on the
per-stream lens, a merged model on the combined one -- in whichever view the
reader is standing in, and the pick is what the next read asks for. The heading
follows the payload that LANDED: a slow series read keeps the old rows under the
old name and says it is loading, a failed one keeps them and says it failed, and
the rows always measure the series the heading names.

Drive the control a reader uses, then read back what the pane SHOWS, whitespace
folded because the terminal wraps. Fixtures are the contract-shaped ones in
``support.tui``; every figure is synthetic.
"""
from __future__ import annotations

import copy
import threading

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import DataTable, ListView  # noqa: E402

from tokdash.tui.data import FetchOutcome  # noqa: E402
from support.tui import (  # noqa: E402
    SHARED_MODEL,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    body_text,
    picker_options,
    press,
    rows_of,
    run,
    settle,
    visible,
)


SECOND_MODEL = "second-model"


SECOND_RATE = "77.0"


async def choose_series(app, pilot, value):
    """Open ``k`` and pick the option whose value is ``value``."""
    await pilot.press("k")
    await settle(pilot, 1)
    options = [v for _label, v in picker_options(app)]
    wanted = options.index(value)
    picker = app.screen.query_one("#view-picker-list", ListView)
    for _ in range(wanted - (picker.index or 0)):
        await pilot.press("down")
    assert [v for _label, v in picker_options(app)][picker.index or 0] == value
    await pilot.press("enter")
    await settle(pilot, 3)


def table_rows(app) -> list[list[str]]:
    table = app.query_one("#sp-table", DataTable)
    return [[str(cell) for cell in table.get_row_at(row)] for row in range(table.row_count)]


def second_merged_model(api):
    """A second merged series whose rates and counts match NOTHING else."""
    other = copy.deepcopy(api.speed_trend["model_series"][0])
    other.update(
        model=SECOND_MODEL,
        combined=[233.0, 244.0],
        calls=[7, 8],
        parallelism=[2.5, 2.5],
        hours={"combined": [77.0] * 24, "calls": [2] * 24, "parallelism": [3.0] * 24},
    )
    api.speed_trend["model_series"].append(other)
    return other


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


def _hour_rows(app) -> list[list[str]]:
    table = app.query_one("#sp-table", DataTable)
    return [[str(cell) for cell in table.get_row_at(row)] for row in range(table.row_count)]


# --------------------------------------------------------------------- F5 --
# The Speed source filter was derived from the rows it filtered, so the second
# source was unreachable.


def test_the_speed_chooser_reaches_a_source_the_filter_hides(api):
    api.speed_across["rows"].append(dict(api.speed_across["rows"][0], source="claude"))

    def filtered(*_args, **kwargs):
        payload = copy.deepcopy(api.speed_across)
        if kwargs.get("source"):
            payload["rows"] = [
                row for row in payload["rows"] if row["source"] == kwargs["source"]
            ]
        return FetchOutcome(value=payload, status="hit", age_seconds=0)

    api.hooks["speed"] = filtered

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        await pilot.press("h")
        await settle(pilot, 1)
        offered = [value for _label, value in picker_options(app)]
        assert set(offered) >= {"codex", "claude"}, offered
        await pilot.press("down")   # "All measured sources"
        await pilot.press("down")   # first real source
        await pilot.press("enter")
        await settle(pilot, 3)
        assert app._sp_source in {"claude", "codex"}, app._sp_source
        # Still both on offer while one of them is filtered in.
        await pilot.press("h")
        await settle(pilot, 1)
        offered = [value for _label, value in picker_options(app)]
        assert set(offered) >= {"codex", "claude"}, offered
        await pilot.press("escape")
        await settle(pilot, 1)

    run(api, body)


# --------------------------------------------------------------------- R2-F2 --
# k is advertised on the Speed pane. It has to open in the view the reader is
# actually standing in, and its pick has to be the thing the next read asks for.


def test_k_offers_the_merged_models_that_hour_mode_can_chart(api):
    second = copy.deepcopy(api.speed_trend["model_series"][0])
    second["model"] = "second-model"
    api.speed_trend["model_series"].append(second)

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-lens-combined")
        await settle(pilot, 1)
        press(app, "sp-mode-hour")
        await settle(pilot, 3)
        assert app._sp_trend is None, "Hour mode keeps its own payload"
        await pilot.press("k")
        await settle(pilot, 1)
        offered = [value for _label, value in picker_options(app)]
        assert set(offered) >= {SHARED_MODEL, "second-model"}, offered
        await pilot.press("down")
        await pilot.press("enter")
        await settle(pilot, 3)
        assert app._sp_model in {SHARED_MODEL, "second-model"}, app._sp_model
        assert app._sp_model in body_text(app, "#sp-detail"), body_text(app, "#sp-detail")

    run(api, body)


def test_k_offers_every_comparison_key_and_the_pick_asks_for_it(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-mode-hour")
        await settle(pilot, 3)
        await pilot.press("k")
        await settle(pilot, 1)
        labels = [label for label, _value in picker_options(app)]
        offered = [value for _label, value in picker_options(app)]
        # Both timing kinds of the snapshot, not just the one being charted --
        # and named the way the table above names them, not as wire enums.
        kinds = {key[2] for key in offered}
        assert {"response_window", "request_window"} <= kinds, offered
        assert any("response window" in label for label in labels), labels
        assert not any("response_window" in label for label in labels), labels
        await pilot.press("down")
        await pilot.press("enter")
        await settle(pilot, 3)
        assert app._sp_key is not None
        asked = [c for c in api.called("speed") if c[2].get("view") == "time-of-day"]
        assert asked, api.called("speed")
        assert asked[-1][2].get("measurement_kind") == app._sp_key[2], asked[-1]

    run(api, body)


# --------------------------------------------------------------------- R2-F6 --
# Rates without an identity measure nobody.


def test_the_combined_hour_table_names_the_model_it_measured(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-lens-combined")
        await settle(pilot, 1)
        press(app, "sp-mode-hour")
        await settle(pilot, 3)
        assert any(row[1] == "90.0" for row in _hour_rows(app)), "no measurements painted"
        detail = body_text(app, "#sp-detail")
        assert f"merged model {SHARED_MODEL}" in detail, detail
        assert "sources claude, codex" in detail, detail
        # The timing label arrives already joined; spelling it out a character at
        # a time was the other half of this finding.
        assert "timing after first token, response window" in detail, detail
        assert "a, w, " not in detail, detail
        # And it is ON screen: a bounded table scrolls itself, so the caption
        # underneath it stays in the pane instead of below the clip.
        assert SHARED_MODEL in visible(app, "#sp-detail"), visible(app, "#sp-detail")

    run(api, body)


@pytest.mark.parametrize(
    ("lens_button", "expected"),
    [("sp-lens-combined", SHARED_MODEL), ("sp-lens-stream", "response window")],
)
def test_the_hour_note_names_the_selection_without_scrolling(api, lens_button, expected):
    """The note above the rows says WHAT the rows measure.

    Twenty-four hour rows fill a terminal pane, so a caption placed underneath
    them is not a caption the reader reaches -- and rates with no identity
    attached are the same bug in a different place.
    """
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, lens_button)
        await settle(pilot, 1)
        press(app, "sp-mode-hour")
        await settle(pilot, 3)
        note = visible(app, "#sp-note")
        assert "measuring" in note, note
        assert expected in note, note

    run(api, body)


# --------------------------------------------------------------------- R3-F1 --
# The heading names the series; the rows have to measure the SAME series.


def test_the_combined_trend_paints_the_model_it_names(api):
    second_merged_model(api)

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-lens-combined")
        await settle(pilot, 1)
        press(app, "sp-mode-trend")
        await settle(pilot, 2)
        assert any("95.0" in row for row in table_rows(app)), table_rows(app)

        await choose_series(app, pilot, SECOND_MODEL)
        assert app._sp_model == SECOND_MODEL
        rows = table_rows(app)
        # The picked model's OWN rates and response counts, not model one's.
        assert [r for r in rows if "233.0" in r], rows
        assert [r for r in rows if "244.0" in r], rows
        assert not [r for r in rows if "95.0" in r], rows
        assert not [r for r in rows if r[-3] == "40"], rows  # model one's 40 responses
        # And the words around the table name the same series the rows measure.
        assert SECOND_MODEL in visible(app, "#sp-note"), visible(app, "#sp-note")
        detail = str(app.query_one("#sp-detail").render())
        assert SECOND_MODEL in detail, detail
        assert SHARED_MODEL not in detail, detail

    run(api, body)


def test_the_per_stream_trend_still_follows_its_exact_key(api):
    """The other lens resolves its selection the same way, from its own field."""
    other = copy.deepcopy(api.speed_trend["series"][0])
    other.update(
        source="claude",
        per_stream=[12.5, None],
        combined=[13.5, None],
        calls=[3, 0],
        parallelism=[1.0, None],
        spread={"p10": [9.0, None], "p50": [12.5, None], "p90": [15.0, None]},
    )
    api.speed_trend["series"].append(other)

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-mode-trend")
        await settle(pilot, 2)
        assert any("88.0" in row for row in table_rows(app)), table_rows(app)
        await choose_series(app, pilot, ("claude", SHARED_MODEL, "response_window",
                                         "output_including_reasoning"))
        rows = table_rows(app)
        assert [r for r in rows if "12.5" in r], rows
        assert not [r for r in rows if "88.0" in r], rows
        assert "claude" in visible(app, "#sp-note"), visible(app, "#sp-note")

    run(api, body)


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
