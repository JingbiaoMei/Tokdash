"""The revision-5 review findings, re-driven through the real controls.

Every case here came from the 2026-10-19 review of the revision-5 TUI as a
reproduction of a reported failure. What they do differently from the review's
own scratch checks is the point: each one drives the key, the button or the
chooser a reader would actually press, then reads back what the pane SHOWS,
rather than assigning the private state that the fix happens to read. The
revision-5 fixtures and pilot helpers are imported so the payloads here stay
contract-shaped for the endpoints as they actually answer.
"""
from __future__ import annotations

import copy
import threading

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import Button, DataTable  # noqa: E402

import tokdash.tui.app as app_mod  # noqa: E402
from tokdash.compute import resolve_period  # noqa: E402
from tokdash.sessions import SESSION_TOOLS  # noqa: E402
from tokdash.tui import views  # noqa: E402
from tokdash.tui.app import ChoicePicker, TokdashApp  # noqa: E402
from tokdash.tui.data import FetchOutcome  # noqa: E402
from test_tui_revision5 import (  # noqa: E402
    SESSION_DETAIL,
    SHARED_MODEL,
    TODAY,
    _open,
    api,  # noqa: F401: the fixture these tests ask pytest for
    press,
    rows_of,
    run,
    settle,
    visible,
)


def picker_options(app):
    """The chooser on top of the stack, as the reader is offered it."""
    screen = app.screen
    assert isinstance(screen, ChoicePicker), f"no chooser is open ({screen!r})"
    return screen._options


def twenty_turns() -> dict:
    """A whole-session response with more turns than any viewport can hold."""
    detail = copy.deepcopy(SESSION_DETAIL)
    detail["turns"] = [
        dict(detail["turns"][0], turn_index=index, model=f"unique-model-{index}")
        for index in range(1, 21)
    ]
    return detail


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


# --------------------------------------------------------------------- F2 --
# A repaint while a new window was loading put the OLD window's figures under
# the new dates.


def test_sorting_while_a_new_window_loads_shows_no_figures_yet(api):
    gate = threading.Event()

    def blocked(*_args, **_kwargs):
        gate.wait(8)
        return None

    async def body(app, pilot):
        await settle(pilot, 1)
        table = app.query_one("#ov-table", DataTable)
        assert table.row_count > 0
        api.hooks["usage"] = blocked
        app.action_set_range("yesterday")
        await settle(pilot, 1)
        assert table.row_count == 0, rows_of(table)
        try:
            # Sort, search and the sub-view switch all reach the same painter,
            # which used to read the payloads still in hand without asking what
            # window produced them.
            app.action_cycle_sort()
            app.action_begin_search()
            await settle(pilot, 1)
            assert table.row_count == 0, (app._win.describe(), rows_of(table))
            # One strip prints the window for every dated pane, so the stale
            # date has nowhere left to sit next to the new one.
            strip = visible(app, "#range-dates")
            assert TODAY.isoformat() not in strip, strip
            assert "2026-10-17" in strip, strip
        finally:
            gate.set()
            await settle(pilot, 2)
        assert table.row_count > 0, "the unblocked load never painted"

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


# --------------------------------------------------------------------- F6 --
# A build subscription used to outlive the pane, the window and the source.


def test_leaving_speed_closes_the_live_build_subscription(api, monkeypatch):
    gate = threading.Event()
    started = threading.Event()

    def follow(job_id, on_event, **kwargs):
        started.set()
        gate.wait(8)
        return 0

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)
    api.speed_across["cache"] = {"state": "building", "job_id": "review-job"}

    async def body(app, pilot):
        try:
            app.action_show_pane("speed")
            await settle(pilot, 2)
            assert started.is_set()
            app.action_show_pane("overview")
            await settle(pilot, 1)
            assert not app._sp_following, "the hidden pane still followed the job"
        finally:
            gate.set()
            await settle(pilot, 1)

    run(api, body)


def test_the_follower_hands_the_transport_a_predicate_that_tracks_the_pane(
    api, monkeypatch
):
    """The stream is told, in its own terms, when it is no longer wanted.

    The follow runs on the thread it was declared for, so the predicate has to
    be readable from that thread: it is the only way a reader who left the pane
    stops a build that no longer has anywhere to be painted.
    """
    seen: dict = {}

    def follow(job_id, on_event, **kwargs):
        stop = kwargs.get("stop")
        seen["stop"] = stop
        seen["while_following"] = stop() if callable(stop) else None
        return 0

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        await app._speed_job_follow("a-queued-job").wait()
        stop = seen.get("stop")
        assert callable(stop), f"the reader was given no way to leave: {seen}"
        assert seen["while_following"] is False, "the guard reads as already-departed"
        app._stop_speed_follow()
        assert stop() is True, "the reader left and the stream cannot notice"

    run(api, body)


def test_an_event_for_another_job_cannot_move_the_note(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        app._sp_following = True
        app._sp_follow_job = "the-job-in-force"
        app._sp_follow_epoch = app._sp_follow_token
        app._sp_job_note = "the note the reader is looking at"
        app._on_speed_event({"id": "a-job-from-an-older-load", "state": "error"})
        assert app._sp_job_note == "the note the reader is looking at"
        # Walking away drops the events even when the job id does match.
        app.action_show_pane("overview")
        await settle(pilot, 1)
        app._on_speed_event({"id": "the-job-in-force", "state": "error"})
        assert app._sp_job_note == "the note the reader is looking at"

    run(api, body)


# --------------------------------------------------------------------- F7 --
# One date step used to re-read every pane ever opened, Speed's build included.


def test_a_new_range_leaves_the_hidden_panes_alone(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        app.action_show_pane("sessions")
        await settle(pilot, 2)
        app.action_show_pane("overview")
        await settle(pilot, 1)
        speed_before = len(api.called("speed"))
        sessions_before = len(api.called("sessions"))
        app.action_set_range("yesterday")
        await settle(pilot, 2)
        assert len(api.called("speed")) == speed_before, api.called("speed")
        assert len(api.called("sessions")) == sessions_before
        # And each pane left standing on the old window knows it is out of date.
        assert {"speed", "sessions"} <= app._stranded, app._stranded
        # Coming back pays the debt, once.
        app.action_show_pane("speed")
        await settle(pilot, 2)
        assert len(api.called("speed")) == speed_before + 1
        assert "speed" not in app._stranded

    run(api, body)


# --------------------------------------------------------------------- F8 --
# Enter on a quota row opened nothing, and the detail read a schema the endpoint
# does not answer with.


def test_enter_on_a_quota_row_opens_that_account(api):
    async def body(app, pilot):
        app.action_show_pane("quota")
        await settle(pilot, 2)
        table = app.query_one("#quota-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await settle(pilot, 1)
        assert app._quota_detail, "Enter opened nothing"
        app.query_one("#quota-detail").scroll_visible()
        await settle(pilot, 1)
        detail = visible(app, "#quota-detail")
        assert "account" in detail, detail
        assert "source" in detail, detail

    run(api, body)


def test_enter_names_the_stale_account_behind_a_quiet_meter(api):
    async def body(app, pilot):
        app.action_show_pane("quota")
        await settle(pilot, 2)
        table = app.query_one("#quota-table", DataTable)
        table.focus()
        rows = rows_of(table)
        target = next(
            (index for index, row in enumerate(rows)
             if any("academic" in cell for cell in row)),
            None,
        )
        assert target is not None, rows
        table.move_cursor(row=target)
        await pilot.press("enter")
        await settle(pilot, 1)
        app.query_one("#quota-detail").scroll_visible()
        await settle(pilot, 1)
        detail = visible(app, "#quota-detail")
        assert "academic" in detail, detail
        # The account's own complaint, not only the percentage it still shows.
        assert "stale_token" in detail, detail
        assert "local logs can lag" in detail, detail

    run(api, body)


def test_a_provider_with_no_windows_answers_for_its_status_row(api):
    async def body(app, pilot):
        app.action_show_pane("quota")
        await settle(pilot, 2)
        table = app.query_one("#quota-table", DataTable)
        table.focus()
        rows = rows_of(table)
        target = next(
            (index for index, row in enumerate(rows)
             if any("kimi" in cell for cell in row)),
            None,
        )
        assert target is not None, rows
        table.move_cursor(row=target)
        app.action_open_row()
        await settle(pilot, 1)
        app.query_one("#quota-detail").scroll_visible()
        await settle(pilot, 1)
        detail = visible(app, "#quota-detail")
        assert "no usable quota windows" in detail, detail
        assert "not consented" in detail, detail

    run(api, body)


def test_a_troubled_account_survives_its_column_being_dropped(api):
    """A narrow table gives up the Status column; the news cannot live there."""

    async def body(app, pilot):
        app.action_show_pane("quota")
        await settle(pilot, 2)
        table = app.query_one("#quota-table", DataTable)
        headers = [str(column.label) for column in table.columns.values()]
        note = str(app.query_one("#quota-note").render())
        if "Status" not in headers:  # the narrow case the review captured
            assert "academic" in note, (headers, note)
        assert "reporting a problem" in note, note
        assert "enter a row" in note, note

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


# -------------------------------------------------------------------- F10 --
# Report -> When routed to the summary renderer: three hours, one weekday, and
# the rest of the facet unreachable.


def test_when_pane_reaches_the_quiet_hours_and_the_whole_week(api):
    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 2)
        press(app, "rp-tab-when")
        await settle(pilot, 2)
        rendered = str(app.query_one("#rp-body").render())
        for hour in range(24):
            assert f"{hour:02d}:00" in rendered, hour
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                    "Saturday", "Sunday"):
            assert day in rendered, day
        # The denominator and the clock are named, not implied.
        assert "analytics scan total" in rendered
        assert "BST" in rendered
        # The grid is taller than the pane and the pane scrolls, so "reachable"
        # means paged to, not clipped away.
        assert app.query_one("#rp-body").region.height >= 24

    run(api, body)


def test_when_pane_says_so_when_a_facet_never_answered(api):
    api.insights = {
        "timezone": "BST",
        "totals": {"tokens": 900_000},
        "hourly": {},
        "weekday": {},
    }

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 2)
        press(app, "rp-tab-when")
        await settle(pilot, 2)
        rendered = str(app.query_one("#rp-body").render())
        assert "no hourly facet" in rendered, rendered
        assert "no weekday facet" in rendered, rendered
        assert "00:00" not in rendered, rendered

    run(api, body)


# -------------------------------------------------------------------- F11 --
# Report advertised project sorting but the renderer hardcoded the tokens order.


def test_the_project_sort_changes_the_rendered_ranking(api):
    api.insights["projects"]["projects"] = [
        {"project": "Token leader", "tokens": 500_000, "cost": 1.0, "messages": 9},
        {"project": "Cost leader", "tokens": 100_000, "cost": 100.0, "messages": 2},
    ]

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 2)
        press(app, "rp-tab-projects")
        await settle(pilot, 2)
        before = str(app.query_one("#rp-body").render())
        assert before.index("Token leader") < before.index("Cost leader"), before
        await pilot.press("s")
        await settle(pilot, 2)
        after = str(app.query_one("#rp-body").render())
        assert after.index("Cost leader") < after.index("Token leader"), after
        assert "ranked by cost" in after, after

    run(api, body)


def test_the_project_list_reaches_past_the_tenth_project(api):
    api.insights["projects"]["projects"] = [
        {"project": f"project-{index:02d}", "tokens": 1_000_000 - index * 1_000,
         "cost": float(index), "messages": index}
        for index in range(1, 15)
    ]

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 2)
        press(app, "rp-tab-projects")
        await settle(pilot, 2)
        rendered = str(app.query_one("#rp-body").render())
        assert "project-14" in rendered, rendered
        # The residual lives OUTSIDE the ranked rows, and never as a fake project.
        assert "unattributed" in rendered, rendered
        assert rendered.index("project-14") < rendered.index("unattributed"), rendered

    run(api, body)


# -------------------------------------------------------------------- F12 --
# ``--period month`` opened a 30-day rolling window where the endpoint means the
# current calendar month.


def test_the_month_period_opens_the_calendar_month(api):
    async def body(app, pilot):
        assert (app._win.date_from, app._win.date_to) == (
            TODAY.replace(day=1), TODAY,
        ), app._win.describe()
        assert app.query_one("#range-month", Button).has_class("-selected")
        assert TODAY.replace(day=1).isoformat() in visible(app, "#range-dates")

    _open(api, "month", body)


def test_the_month_period_request_asks_for_the_same_days(api):
    async def body(app, pilot):
        await settle(pilot, 2)
        asked = api.last("usage")
        # The route's warm key stays the window token; the DATES are what the
        # month has to move, and they are the calendar month.
        assert asked[1][1:] == (
            TODAY.replace(day=1).isoformat(), TODAY.isoformat(),
        ), asked

    _open(api, "month", body)


# -------------------------------------------------------------------- F13 --
# A usage scan that half-failed printed complete-looking totals with no hint.


def test_a_source_that_failed_to_scan_is_named_in_the_note(api):
    note = views.status_note(dict(api.usage, source_errors=["codex"]), api.active)
    assert note and "codex" in note, note
    assert "short by" in note, note


def test_the_partial_usage_warning_reaches_the_screen(api):
    def half_read(*_args, **_kwargs):
        payload = copy.deepcopy(api.usage)
        payload["source_errors"] = ["codex"]
        return FetchOutcome(value=payload, status="hit", age_seconds=0)

    async def body(app, pilot):
        api.hooks["usage"] = half_read
        app.action_refresh()
        await settle(pilot, 3)
        text = visible(app, "#ov-note")
        assert "codex" in text, text

    run(api, body)


def test_a_clean_read_says_nothing_about_missing_sources(api):
    """The warning is for a read that failed, not for every read.

    ``source_errors`` answers (0, "") when the scan was whole, and a filled
    tuple is truthy -- so an early version of the fix put "usage partial: 0
    sources unreadable ()" under the headline of every healthy Overview.
    """
    assert not api.usage["source_errors"]
    note = views.status_note(api.usage, api.active)
    assert "partial" not in (note or ""), note

    async def body(app, pilot):
        await settle(pilot, 2)
        text = visible(app, "#ov-note")
        assert "partial" not in text, text
        assert "unreadable" not in text, text

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
