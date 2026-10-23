"""Third-review regressions (2026-10-19, against 4e84a66).

Round three found three Speed surfaces where the caption and the measurements
disagreed, or where a keystroke quietly broke a live build subscription:

* the Combined Trend named the model the reader picked while still painting the
  first model's rates, because the renderer was handed the PER-STREAM key;
* choosing a series ran an ungrouped exclusive worker, which cancelled the
  ungrouped SSE thread worker and lost the completion reread it owed;
* Enter on a Trend bucket or an Hour row looked the key up as an Across-models
  comparison identity, failed, and told the reader the row had disappeared.

Same house rules as the earlier rounds: drive the control a reader actually
uses, then read back what the pane SHOWS. Payloads are the contract-shaped
fixtures in ``test_tui_revision5``; every figure in them is synthetic.
"""
from __future__ import annotations

import copy
import threading

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import DataTable, ListView  # noqa: E402

import tokdash.tui.app as app_mod  # noqa: E402
from test_tui_revision5 import SHARED_MODEL, api, press, run, settle, visible  # noqa: E402
from test_tui_review_fixes import picker_options  # noqa: E402

SECOND_MODEL = "second-model"


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


# --------------------------------------------------------------------- R3-F2 --
# A series change is a view change. It may not cancel the build subscription.


@pytest.mark.parametrize("mode", ["trend", "hour"])
def test_choosing_a_series_keeps_the_build_subscription(api, monkeypatch, mode):
    gate = threading.Event()
    followers: list[str] = []

    def follow(job_id, on_event, **kwargs):
        followers.append(job_id)
        gate.wait(12)
        on_event({"id": job_id, "state": "ready"})
        return 1

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)

    async def body(app, pilot):
        try:
            app.action_show_pane("speed")
            await settle(pilot, 2)
            press(app, f"sp-mode-{mode}")
            await settle(pilot, 2)
            api.speed_across["cache"] = {"state": "building", "job_id": "series-job"}
            await pilot.press("r")
            await settle(pilot, 2)
            assert followers == ["series-job"], followers
            before = len(api.called("speed"))

            # A series change: new selection, new read -- and the stream stays.
            await pilot.press("k")
            await settle(pilot, 1)
            await pilot.press("enter")
            await settle(pilot, 3)
            assert app._sp_following, "the series change dropped a live subscription"
            assert app._sp_follow_job == "series-job"
            assert app._sp_follow_epoch == app._sp_follow_token

            # The build lands. Exactly one cache-only reread, and the flag down.
            api.speed_across["cache"] = {"state": "ready"}
            gate.set()
            await settle(pilot, 4)
            snapshot_rereads = [
                c for c in api.called("speed")[before:]
                if c[2].get("view") == "across-models" and c[2].get("cache_only")
            ]
            assert len(snapshot_rereads) == 1, {
                "rereads": len(snapshot_rereads),
                "note": app._sp_job_note,
                "followers": followers,
            }
            assert not app._sp_following, "a finished build still holds the flag"
        finally:
            gate.set()
            await settle(pilot, 1)

    run(api, body)


def test_a_cancelled_subscription_says_so_instead_of_staying_following(
    api, monkeypatch
):
    """The flag cannot survive the loss of the worker that owned it.

    Whatever takes the reader's stream away -- a cancellation, a dead connection
    -- the pane must stop claiming it is watching, or it never re-subscribes to
    the build that replaced it.
    """
    gate = threading.Event()
    worker_box: dict = {}

    def follow(job_id, on_event, **kwargs):
        worker_box["stop"] = kwargs.get("stop")
        gate.wait(12)
        return 0

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)

    async def body(app, pilot):
        try:
            app.action_show_pane("speed")
            await settle(pilot, 2)
            api.speed_across = copy.deepcopy(api.speed_across)
            api.speed_across["cache"] = {"state": "building", "job_id": "gone-job"}
            await pilot.press("r")
            await settle(pilot, 2)
            assert app._sp_following
            stream = next(
                w for w in app.workers._workers if w.group == "speed-follow-io"
            )
            stream.cancel()
            gate.set()
            await settle(pilot, 3)
            assert not app._sp_following, "the flag outlived its own worker"
            assert "update connection lost" in visible(app, "#sp-note"), visible(app, "#sp-note")
            assert "re-reading snapshot" not in visible(app, "#sp-note")
        finally:
            gate.set()
            await settle(pilot, 1)

    run(api, body)


# --------------------------------------------------------------------- R3-F3 --
# Enter answers for the row it stands on, in that row's own terms.


def test_enter_on_a_trend_bucket_shows_that_bucket(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-mode-trend")
        await settle(pilot, 2)
        table = app.query_one("#sp-table", DataTable)
        assert table.row_count == 2, table.row_count
        table.focus()
        await pilot.press("enter")
        await settle(pilot, 1)
        detail = visible(app, "#sp-detail")
        assert "no longer in this snapshot" not in detail, detail
        assert "2026-10-18T09" in detail, detail
        assert "88.0" in detail, detail          # the series' own bucket rate
        assert SHARED_MODEL in detail, detail    # whose series it is
        app.query_one("#sp-detail").scroll_visible()
        await settle(pilot, 1)
        assert "gap, not a zero" not in visible(app, "#sp-detail")  # a measured bucket

    run(api, body)


def test_enter_on_an_unmeasured_bucket_says_it_is_a_gap(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-mode-trend")
        await settle(pilot, 2)
        table = app.query_one("#sp-table", DataTable)
        table.focus()
        table.move_cursor(row=table.row_count - 1)  # the null bucket
        await pilot.press("enter")
        await settle(pilot, 1)
        detail = visible(app, "#sp-detail")
        assert "no longer in this snapshot" not in detail, detail
        assert "2026-10-18T10" in detail, detail
        assert "gap, not a zero" in detail, detail

    run(api, body)


@pytest.mark.parametrize(
    ("lens_button", "expected"),
    [("sp-lens-stream", "response window"), ("sp-lens-combined", SHARED_MODEL)],
)
def test_enter_on_an_hour_row_shows_that_hour(api, lens_button, expected):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, lens_button)
        await settle(pilot, 1)
        press(app, "sp-mode-hour")
        await settle(pilot, 3)
        table = app.query_one("#sp-table", DataTable)
        measured = next(
            row for row in range(table.row_count)
            if any(cell not in ("—", "") for cell in table.get_row_at(row)[1:])
        )
        table.focus()
        table.move_cursor(row=measured)
        await settle(pilot, 1)
        await pilot.press("enter")
        await settle(pilot, 1)
        detail = visible(app, "#sp-detail")
        assert "no longer in this snapshot" not in detail, detail
        assert "00:00" in detail, detail      # the hour it stands on
        assert expected in detail, detail     # and what that hour measured

    run(api, body)


def test_enter_still_opens_an_across_models_row(api):
    """The view this key was written for keeps working."""

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        table = app.query_one("#sp-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await settle(pilot, 1)
        detail = visible(app, "#sp-detail")
        assert "no longer in this snapshot" not in detail, detail
        assert "coverage" in detail, detail
        assert "response window" in detail, detail

    run(api, body)
