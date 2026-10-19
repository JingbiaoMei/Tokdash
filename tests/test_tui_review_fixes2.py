"""Second-review regressions (2026-10-19, against b889cfa).

Round two found seven surfaces that worked in the fixture and not in the
terminal: a manual reload that orphaned a live build stream, a chooser key that
opened nothing, a button wired to nothing, a ratio printed as a percentage, a
failed facet scan that claimed to still be running, an hour table that could not
name the model it measured, and a quota heading that attributed one account's
plan to a different account.

Same house rules as the first round: drive the control a reader actually uses,
then read back what the pane SHOWS. Payloads are the contract-shaped fixtures in
``test_tui_revision5``; the prices and figures there are synthetic.
"""
from __future__ import annotations

import copy
import re
import threading

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import Button, DataTable  # noqa: E402

import tokdash.tui.app as app_mod  # noqa: E402
from tokdash.tui.charts import quota_detail_rows  # noqa: E402
from tokdash.tui.data import FetchOutcome  # noqa: E402
from test_tui_revision5 import (  # noqa: E402
    SHARED_MODEL, api, press, run, settle, visible,
)
from test_tui_review_fixes import picker_options  # noqa: E402


def body_text(app, selector="#rp-body") -> str:
    """The section's whole text, wrap and clipping aside."""
    return str(app.query_one(selector).render())


# --------------------------------------------------------------------- R2-F1 --
# A reload re-issues the read; it does not retire the build that was already
# queued for the very same scope.


def test_a_reload_during_a_build_keeps_the_stream_and_rereads_when_it_lands(api, monkeypatch):
    gate = threading.Event()
    followers: list[str] = []

    def follow(job_id, on_event, **kwargs):
        followers.append(job_id)
        gate.wait(8)
        on_event({"id": job_id, "state": "ready"})
        return 1

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)
    api.speed_across = copy.deepcopy(api.speed_across)
    api.speed_across["cache"] = {"state": "building", "job_id": "review-job"}

    async def body(app, pilot):
        try:
            app.action_show_pane("speed")
            await settle(pilot, 2)
            assert followers == ["review-job"], followers
            await pilot.press("r")
            await settle(pilot, 2)
            # The reload moved the generation; the build did not change, so the
            # stream that was already open is still the one in force.
            assert app._sp_following, "the reload dropped a live subscription"
            assert app._sp_follow_job == "review-job"
            assert app._sp_follow_epoch == app._sp_follow_token
            before = len(api.called("speed"))
            api.speed_across["cache"] = {"state": "ready"}
            gate.set()
            await settle(pilot, 3)
            assert len(api.called("speed")) > before, (
                "the build landed but nothing re-read the snapshot: "
                f"followers={followers} note={app._sp_job_note!r}"
            )
            assert not app._sp_following, "a finished build still holds the flag"
        finally:
            gate.set()
            await settle(pilot, 1)

    run(api, body)


def test_a_snapshot_naming_a_newer_build_hands_the_note_to_it(api, monkeypatch):
    gate = threading.Event()
    followers: list[str] = []

    def follow(job_id, on_event, **kwargs):
        followers.append(job_id)
        gate.wait(8)
        return 0

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)
    api.speed_across = copy.deepcopy(api.speed_across)
    api.speed_across["cache"] = {"state": "building", "job_id": "job-one"}

    async def body(app, pilot):
        try:
            app.action_show_pane("speed")
            await settle(pilot, 2)
            assert followers == ["job-one"], followers
            # The next snapshot names a DIFFERENT build. The old stream cannot be
            # allowed to keep the note, and the flag cannot block the new job.
            api.speed_across["cache"] = {"state": "building", "job_id": "job-two"}
            app._restart_speed()
            await settle(pilot, 2)
            assert followers == ["job-one", "job-two"], followers
            assert app._sp_follow_job == "job-two", app._sp_follow_job
        finally:
            gate.set()
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


# --------------------------------------------------------------------- R2-F4 --
# The facet answers with a ratio; the pane owes the reader a percentage.


@pytest.mark.parametrize(
    ("share", "expected"),
    [(0.5, "night share 50.0%"), (0.1234, "night share 12.3%"), (0.0, "night share 0.0%")],
)
def test_night_share_is_printed_as_the_share_it_measures(api, share, expected):
    api.insights["hourly"]["night_share"] = share

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 2)
        press(app, "rp-tab-when")
        await settle(pilot, 1)
        text = body_text(app)
        assert expected in text, text

    run(api, body)


def test_a_night_share_that_was_never_measured_is_not_printed(api):
    api.insights["hourly"]["night_share"] = None

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 2)
        press(app, "rp-tab-when")
        await settle(pilot, 1)
        text = body_text(app)
        assert "night share" not in text, text
        assert "nan" not in text.lower(), text

    run(api, body)


# --------------------------------------------------------------------- R2-F5 --
# "Still running" is a promise. A scan that failed has to say it failed.


@pytest.mark.parametrize("section", ["when", "projects"])
def test_a_failed_facet_scan_says_so_where_the_facet_would_have_been(api, section):
    api.insights = RuntimeError("analytics reader exploded")

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 3)
        assert app._state["report"] == "ok", "a soft facet failure is not a dead pane"
        press(app, f"rp-tab-{section}")
        await settle(pilot, 1)
        text = body_text(app)
        assert "still running" not in text, text
        assert "analytics scan failed" in text, text
        assert "analytics reader exploded" in text, text
        assert "r Reload" in text, text

    run(api, body)


def test_a_facet_that_is_genuinely_running_still_says_so(api):
    gate = threading.Event()

    def slow(*_args, **_kwargs):
        gate.wait(8)
        return None  # fall through to the fixture answer

    api.hooks["insights"] = slow

    async def body(app, pilot):
        try:
            app.action_show_pane("report")
            await settle(pilot, 2)
            press(app, "rp-tab-when")
            await settle(pilot, 2)
            assert app._rp_insights is None, "the read was not actually outstanding"
            text = body_text(app)
            assert "still running" in text, text
            gate.set()
            await settle(pilot, 4)
            assert "Tokens by hour of day" in body_text(app), body_text(app)
        finally:
            gate.set()

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


def _hour_rows(app) -> list[list[str]]:
    table = app.query_one("#sp-table", DataTable)
    return [[str(cell) for cell in table.get_row_at(row)] for row in range(table.row_count)]


# --------------------------------------------------------------------- R2-F7 --
# The card's plan names the DEFAULT install. A second account speaks for itself.


@pytest.mark.parametrize(
    ("account", "bucket", "expected", "forbidden"),
    [
        ("academic", "academic_session", "plan Pro", "plan Max 5x"),
        ("default", "session", "plan Max 5x", "plan Pro"),
    ],
)
def test_the_quota_detail_names_the_selected_account_own_plan(
    account, bucket, expected, forbidden
):
    from test_tui_revision5 import QUOTA_NOW, QUOTA_STATE

    rows = quota_detail_rows(
        QUOTA_STATE,
        {"provider": "claude", "account": account, "bucket": bucket},
        now_ts=QUOTA_NOW,
    )
    text = "\n".join("".join(part.text for part in row) for row in rows)
    assert f"account {account}" in text, text
    assert expected in text, text
    assert forbidden not in text, text


def test_an_account_that_never_reported_a_plan_borrows_none(api):
    from test_tui_revision5 import QUOTA_NOW, QUOTA_STATE

    state = copy.deepcopy(QUOTA_STATE)
    for entry in state["providers"]["claude"]["accounts"]:
        if entry["account"] == "academic":
            entry.pop("plan")
    rows = quota_detail_rows(
        state,
        {"provider": "claude", "account": "academic", "bucket": "academic_session"},
        now_ts=QUOTA_NOW,
    )
    text = "\n".join("".join(part.text for part in row) for row in rows)
    assert "plan" not in text, text
    assert "account academic" in text, text


def test_enter_on_the_second_account_shows_that_account_s_plan(api):
    async def body(app, pilot):
        app.action_show_pane("quota")
        await settle(pilot, 2)
        table = app.query_one("#quota-table", DataTable)
        table.focus()
        target = next(
            row for row in range(table.row_count)
            if any("academic" in str(cell) for cell in table.get_row_at(row))
        )
        table.move_cursor(row=target)
        await settle(pilot, 1)
        await pilot.press("enter")
        await settle(pilot, 1)
        app.query_one("#quota-detail").scroll_visible()
        await settle(pilot, 1)
        detail = visible(app, "#quota-detail")
        assert "account academic" in detail, detail
        assert "plan Pro" in detail, detail
        assert "plan Max 5x" not in detail, detail

    run(api, body)


# ----------------------------------------------------------------- layout law --
# A bar is the only variable-length thing in these rows; un-padded, it slid the
# numbers -- and the second hour column -- a few cells per row.


def test_the_hour_rows_keep_their_columns(api):
    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 2)
        press(app, "rp-tab-when")
        await settle(pilot, 1)
        lines = app._report_when_lines(80)
        # A row whose FIRST cell is a clock hour, and nothing that merely
        # mentions one ("peak hour 09:00" is a heading, not a data row).
        rows = [
            ln for ln in lines
            if ln and re.fullmatch(r"\d\d:00 ", str(ln[0].text))
        ]
        assert len(rows) >= 12, rows
        starts = {sum(len(str(run.text)) for run in row[:3]) for row in rows}
        assert len(starts) == 1, sorted(starts)
        weekday_rows = [
            ln for ln in lines
            if ln and str(ln[0].text).rstrip() in {
                "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
            }
        ]
        assert len(weekday_rows) == 7, weekday_rows
        weekday_starts = {sum(len(str(run.text)) for run in row[:3]) for row in weekday_rows}
        assert len(weekday_starts) == 1, sorted(weekday_starts)

    run(api, body)


def test_a_slow_facet_never_leaks_a_failure_note(api):
    """The mirror of the failure test: a healthy scan states no failure."""

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 3)
        press(app, "rp-tab-when")
        await settle(pilot, 1)
        text = body_text(app)
        assert "failed" not in text, text
        assert "still running" not in text, text

    run(api, body)
