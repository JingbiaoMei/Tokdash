"""The Speed pane: server measurements, comparison keys, and no arithmetic of our own.

Three views of one snapshot (Across models, Trend, Hour of day) under two lenses
(per stream, combined). Two timing methods for one model stay two rows; the
combined lens prints the server's union rate and says what it divided by; a null
bucket stays a gap; an All Time window asks the endpoint for all time; ``x`` and
its button widen the timing set; Enter explains the row it stands on in that
row's own terms; and the picture moves above the table rather than taking its
columns. Choosing a series is test_tui_speed_series.py, and the build stream
behind a snapshot is test_tui_speed_builds.py. Pilots over the contract-shaped
payloads in ``support.tui``.
"""
from __future__ import annotations

import copy

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import Button, DataTable, Static  # noqa: E402

from tokdash.compute import resolve_period  # noqa: E402
from tokdash.tui.app import TokdashApp  # noqa: E402
from support.tui import (  # noqa: E402
    SHARED_MODEL,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    headers_of,
    month_of_buckets,
    _open,
    press,
    rows_of,
    run,
    settle,
    visible,
)


# --------------------------------------------------------------------------- #
# Speed: server measurements, comparison keys, and no arithmetic of our own
# --------------------------------------------------------------------------- #


def test_speed_keeps_two_measurement_methods_as_two_rows(api):
    """Same source, same model, two timing kinds: never averaged into one."""

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        table = app.query_one("#sp-table", DataTable)
        rows = rows_of(table)
        assert len(rows) == 2, rows
        rates = sorted(row[headers_of(table).index("tok/s")] for row in rows)
        assert rates == ["41.0", "88.4"], rates
        keys = [k.value for k in table.rows.keys()]
        assert len(set(keys)) == 2, keys  # the comparison keys stay distinct
        assert "separate methods stay separate rows" in visible(app, "#sp-detail")
        note = visible(app, "#sp-note")
        assert "cache ready" in note, note
        assert "kimi (timing_unavailable)" in note, note

    run(api, body)


def test_speed_combined_uses_the_servers_union_rate_and_drops_the_harness_column(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        press(app, "sp-lens-combined")
        await settle(pilot, 3)
        table = app.query_one("#sp-table", DataTable)
        heads = headers_of(table)
        assert "Harness" not in heads, heads
        assert "Sources" in heads, heads
        rows = rows_of(table)
        assert len(rows) == 1, rows  # the payload's merged model
        assert rows[0][heads.index("tok/s")] == "101.2", rows
        assert "claude,codex" in rows[0][heads.index("Sources")], rows
        detail = visible(app, "#sp-detail")
        assert "UNION of busy time" in detail, detail
        assert "no pooled per-stream rate exists" in detail, detail

    run(api, body)


def test_speed_trend_keeps_null_buckets_as_gaps(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        press(app, "sp-mode-trend")
        await settle(pilot, 4)
        sent = api.last("speed")
        assert sent[2]["view"] == "trend", sent
        assert sent[2]["cache_only"] is True, sent  # a follow-up never queues work
        table = app.query_one("#sp-table", DataTable)
        rows = rows_of(table)
        assert len(rows) == 2, rows
        rate_col = headers_of(table).index("tok/s per stream")
        assert rows[0][rate_col] == "88.0", rows
        assert rows[1][rate_col] == "\u2014", rows  # null stays a gap, not a zero
        detail = visible(app, "#sp-detail")
        assert "duration-weighted" in detail and "30+ responses" in detail, detail

    run(api, body)


def test_hour_of_day_asks_for_one_exact_key_never_a_keyless_request(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        press(app, "sp-mode-hour")
        await settle(pilot, 4)
        sent = api.last("speed")
        assert sent[2]["view"] == "time-of-day", sent
        assert sent[2]["source"] == "codex", sent
        assert sent[2]["model"] == SHARED_MODEL, sent
        assert sent[2]["measurement_kind"] == "response_window", sent
        assert sent[2]["token_basis"] == "output_including_reasoning", sent
        table = app.query_one("#sp-table", DataTable)
        rows = rows_of(table)
        assert len(rows) == 24, rows  # the server's 24 hours, gaps included
        rates = {row[0]: row[1] for row in rows}
        assert rates["09:00"] == "88.4", rates
        assert rates["10:00"] == "0.0", rates      # a measured zero stays zero
        assert rates["03:00"] == "\u2014", rates   # and an unmeasured hour is null
        detail = visible(app, "#sp-detail")
        assert "days with measurements: 6" in detail, detail
        assert "an hour that measured nothing is a gap" in detail, detail

    run(api, body)


# --------------------------------------------------------------------- F3 --
# "All Time" used to be answered with a last-year query while the label stayed.


def test_all_time_speed_asks_the_endpoint_for_all_time():
    date_from, date_to = TokdashApp(initial_period="all")._speed_dates()
    expected = resolve_period("all")
    assert (date_from, date_to) == (expected["from"], expected["to"])


def test_all_time_speed_request_carries_the_resolved_bounds(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 3)
        expected = resolve_period("all")
        asked = api.last("speed")
        assert asked[2]["date_from"] == str(expected["from"]), asked
        assert asked[2]["date_to"] == str(expected["to"]), asked
        assert "All Time" in visible(app, "#range-dates")

    _open(api, "all", body)


# --------------------------------------------------------------------- F4 --
# Combined hour-of-day read the TIME-bucket arrays as hours (24 gaps), and the
# merged-model picker value raised inside the hour job.


def test_combined_hour_reads_the_merged_series_hour_arrays(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-lens-combined")
        await settle(pilot, 1)
        press(app, "sp-mode-hour")
        await settle(pilot, 3)
        rows = rows_of(app.query_one("#sp-table", DataTable))
        assert len(rows) == 24, [row[0] for row in rows]
        rates = [row[1] for row in rows]
        assert all(rate == "90.0" for rate in rates), rows
        assert "90.0" in visible(app, "#sp-table"), visible(app, "#sp-table")

    run(api, body)


def test_the_hour_job_accepts_a_merged_model_name_from_the_picker(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        app._sp_lens = "combined"
        # Exactly the str the combined series picker emits. Unpacked as a
        # four-part comparison key it raised before the lens was ever consulted.
        outcome = await app._speed_hour_job(SHARED_MODEL).wait()
        assert outcome.value.get("model_series")

    run(api, body)


def test_changing_the_lens_re_reads_the_hour_view(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-mode-hour")
        await settle(pilot, 3)
        before = len(api.called("speed"))
        press(app, "sp-lens-combined")
        await settle(pilot, 3)
        # A lens change changes WHICH population is measured, so it re-reads and
        # drops the selection that may not survive it. Repainting the previous
        # payload is what made the switch look like a working view.
        assert len(api.called("speed")) > before
        assert app._sp_key is None and app._sp_model is None
        merged = (app._sp_hours or {}).get("hours") or {}
        assert len(merged.get("combined") or []) == 24, app._sp_hours

    run(api, body)


def test_the_combined_hour_shows_only_columns_the_payload_can_fill(api):
    """A merged model has no per-hour coverage dict.

    The column that stands in for it has to carry a figure the merged arrays
    actually measure -- the server's own overlap -- or the table spends a fifth
    of its width printing dashes for every hour of the day.
    """
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        press(app, "sp-lens-combined")
        await settle(pilot, 1)
        press(app, "sp-mode-hour")
        await settle(pilot, 3)
        table = app.query_one("#sp-table", DataTable)

        def labels():
            return [str(table.columns[col].label) for col in table.columns]

        assert "Coverage" not in labels(), labels()
        assert "Parallelism" in labels(), labels()
        assert all(row[-1] == "1.10x" for row in rows_of(table)), rows_of(table)

        # The per-key lens DOES get a coverage ratio, and keeps its column.
        press(app, "sp-lens-stream")
        await settle(pilot, 3)
        assert "Coverage" in labels(), labels()
        assert any(row[-1] == "90%" for row in rows_of(table)), rows_of(table)

    run(api, body)


# -------------------------------------------------------------------- F15 --
# The advertised whole-request timing set had no control at all.


def test_x_widens_the_timing_set_and_says_so_on_the_request(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        assert api.last("speed")[2]["include_request_windows"] is False
        await pilot.press("x")
        await settle(pilot, 3)
        assert app._sp_include_windows is True
        assert api.last("speed")[2]["include_request_windows"] is True
        assert app.query_one("#sp-windows").has_class("-selected")
        assert "whole request" in visible(app, "#sp-note")

    run(api, body)


def test_the_timing_set_control_reveals_the_measurements_it_promises(api):
    """``x`` is the only way to a request-window-only source, so it must work.

    The route answers with ``hidden_request_windows`` while the filter is closed
    and moves those groups into ``rows`` once it is open; the empty state points
    at the key, and pressing it has to actually change what comes back.
    """
    hidden_only = {
        "schema_version": 7,
        "view": "across-models",
        "range": {"from": "2026-10-18", "to": "2026-10-18"},
        "cache": {"state": "ready", "job_id": None},
        "rows": [],
        "models": [],
        "hidden_request_windows": [
            {"source": "codex", "hidden_rows": 4,
             "measurement_kind": "request_window"},
        ],
        "source_status": [],
    }
    widened = {
        **hidden_only,
        "rows": [{
            "source": "codex", "model": "gpt-5.1-codex", "call_count": 4,
            "eligible_calls": 4, "coverage": 1.0, "peak_in_flight": 2,
            "output_tok_per_s": 61.5, "measurement_kind": "request_window",
            "token_basis": "output_including_reasoning",
        }],
        "hidden_request_windows": [],
    }

    def timing_set(*_args, **kwargs):
        from tokdash.tui.data import FetchOutcome

        payload = copy.deepcopy(
            widened if kwargs.get("include_request_windows") else hidden_only
        )
        return FetchOutcome(value=payload, status="hit", age_seconds=0)

    api.hooks["speed"] = timing_set

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        # Closed: nothing measured, and the pane says which key changes that.
        assert "press x" in visible(app, "#sp-detail"), visible(app, "#sp-detail")
        assert "hidden by the timing filter" in visible(app, "#sp-note")
        await pilot.press("x")
        await settle(pilot, 3)
        rows = rows_of(app.query_one("#sp-table", DataTable))
        assert any("61.5" in row for row in rows), rows
        # Open: the hint and the hidden-groups warning are both spent.
        assert "press x" not in visible(app, "#sp-detail"), visible(app, "#sp-detail")
        assert "hidden by the timing filter" not in visible(app, "#sp-note")
        assert "whole request" in visible(app, "#sp-note"), visible(app, "#sp-note")

    run(api, body)


# --------------------------------------------------------------------- R2-F3 --
# The pane shows a button; the key is only half of it.


def test_the_whole_request_button_and_the_key_do_the_same_thing(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        button = app.query_one("#sp-windows", Button)
        assert not button.has_class("-selected")
        press(app, "sp-windows")
        await settle(pilot, 3)
        assert app._sp_include_windows, "the visible button left the setting alone"
        assert button.has_class("-selected"), "the button does not show what is in force"
        asked = api.called("speed")[-1]
        assert asked[2].get("include_request_windows") is True, asked
        await pilot.press("x")
        await settle(pilot, 3)
        assert not app._sp_include_windows
        assert not button.has_class("-selected")

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


@pytest.mark.parametrize("size", [(120, 36), (100, 30), (80, 24)])
def test_an_opened_row_keeps_the_sentence_naming_its_denominator(api, size):
    """Enter is asked because of that sentence; the row's height never eats it.

    The two lenses divide by different things, and a caption packed to three rows
    used to deliver "summed tokens over the" and stop -- a rate standing on screen
    with no denominator, which is the one thing this view may not do.
    """
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 3)
        press(app, "sp-lens-combined")
        await settle(pilot, 2)
        table = app.query_one("#sp-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await settle(pilot, 2)
        detail = " ".join(visible(app, "#sp-detail").split())
        assert "UNION of busy time" in detail, (size, detail)

    run(api, body, size=size)


# --------------------------------------------------------------------- R5-F2 --
# The picture's columns are the buckets the route returned. It may not merge,
# drop or re-bucket them, so the arrangement has to answer the width they need:
# above the rows when beside them would not fit, beside them when it does.
# (The renderer's own budget is pinned in test_tui_charts.py.)
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# the note strip: packing is not deleting
# --------------------------------------------------------------------------- #


def test_the_speed_strip_wraps_instead_of_losing_a_fact(api):
    """The strip packs, and packing is not deleting.

    At eighty columns the series line and the source filter do not share a row, so
    the strip takes a second one. Dropping the filter instead left a pane claiming
    "all measured" while a filter was on screen -- two places to read the same
    fact, and only one of them updated.
    """
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 3)
        strip = app.query_one("#sp-note", Static)
        note = " ".join(visible(app, "#sp-note").split())
        # The fixture's speed payload names a harness with no measurement, so the
        # strip has a filter fact, a warning fact and a cache fact to place.
        assert "source all measured" in note, note
        assert "no measured speed: kimi" in note, note
        assert "cache ready" in note, note
        # None of it may be replaced by a tally of what gave way.
        assert "widen to read" not in note, note
        assert strip.region.height >= 1, strip.region

    run(api, body, size=(80, 24))
