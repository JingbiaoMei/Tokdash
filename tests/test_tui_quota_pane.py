"""The Quota pane: whose window, how old, and what was never observed.

Accounts fold under their provider and the account travels with every row once
there is more than one; a measured zero left prints 0.0%, and a provider with
only a status prints a dash. The note names the master switch, the missing
history and network polling nobody consented to. Enter opens the selected
account with its own plan and its own complaint. The only network call the TUI
makes is the poll the reader presses, and its sentence stays until a read
answers it. Pilots over the contract-shaped payloads in ``support.tui``; the
detail renderer's own cases are in test_tui_charts.py.
"""
from __future__ import annotations

import copy
import threading

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import DataTable  # noqa: E402

from support.tui import (  # noqa: E402
    QUOTA_STATE,
    WIDE,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    rows_of,
    run,
    settle,
    _settled,
    visible,
)


async def press(app, pilot, key: str) -> None:
    """A tab key, with the pause the pane needs to have started its read."""
    await pilot.press(key)
    await settle(pilot, 1)


# --------------------------------------------------------------------------- #
# Quota: whose window, how old, and what was never observed
# --------------------------------------------------------------------------- #


def test_quota_folds_accounts_and_keeps_a_measured_zero(api):
    async def body(app, pilot):
        app.action_show_pane("quota")
        await settle(pilot, 4)
        table = app.query_one("#quota-table", DataTable)
        rows = rows_of(table)
        text = "\n".join(" | ".join(r) for r in rows)
        assert "claude@default" in text, text      # >1 account: the account travels
        assert "@academic" in text, text
        assert "62.3% left" in text, text          # 100 - 37.7 used
        assert "0.0% left" in text, text           # a measured zero, not a dash
        assert "unlimited" in text, text           # codex's unlimited 5-hour
        assert "· local" in text, text             # a log-read window may lag
        assert "unavailable" in text, text         # kimi: status-only row
        kimi = [r for r in rows if r[0] == "kimi"]
        assert kimi and kimi[0][2] == "\u2014", kimi  # never a zero-usage claim

    run(api, body)


def test_quota_names_the_master_switch_and_the_missing_history(api):
    async def body(app, pilot):
        api.quota = copy.deepcopy(QUOTA_STATE)
        api.quota["enabled"] = False
        api.quota_history = {"series": []}
        app.action_show_pane("quota")
        await settle(pilot, 4)
        note = visible(app, "#quota-note")
        assert "quota tracking is off" in note, note
        assert "no 24h quota history yet" in note, note

    run(api, body)


def test_quota_names_network_polling_that_was_never_consented(api):
    """Tracking on but nobody said yes: the rows are local, and it says so."""

    async def body(app, pilot):
        api.quota = copy.deepcopy(QUOTA_STATE)
        api.quota["consent"] = {k: False for k in api.quota["consent"]}
        app.action_show_pane("quota")
        await settle(pilot, 4)
        note = visible(app, "#quota-note")
        assert "network quota polling is not consented" in note, note

    run(api, body)


def test_the_only_quota_network_call_is_the_key_you_press(api):
    async def body(app, pilot):
        app.action_show_pane("overview")
        await settle(pilot, 3)
        app.action_poll_quota()          # nowhere near the Quota tab
        await settle(pilot, 2)
        assert not api.calls_poll, api.calls_poll
        app.action_show_pane("quota")
        await settle(pilot, 4)
        before = len(api.called("quota"))
        app.action_poll_quota()
        await settle(pilot, 5)
        assert api.calls_poll == [{"include_network": True}], api.calls_poll
        # The poll is followed by a state read, so the pane shows what the poll
        # stored rather than a success word over stale rows.
        assert len(api.called("quota")) > before, api.called("quota")
        assert api.last("quota")[2]["refresh"] is True, api.last("quota")

    run(api, body)


def test_a_disabled_poll_is_reported_as_disabled_not_as_a_success(api):
    async def body(app, pilot):
        api.poll_result = {"disabled": True}
        app.action_show_pane("quota")
        await settle(pilot, 4)
        app.action_poll_quota()
        await settle(pilot, 4)
        note = visible(app, "#quota-note")
        assert "quota tracking disabled" in note, note
        assert "poll ok" not in note, note

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


# --------------------------------------------------------------------- R2-F7 --
# The card's plan names the DEFAULT install. A second account speaks for itself.


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


# --------------------------------------------------------------------- R7-F3 --
# A repaint is not a read. It may redraw every figure the snapshot holds, and it
# may not answer a question the snapshot was never asked -- which is what the
# poll's line does: it says what the LAST POLLS did, from the reader's own
# keystroke, and nothing in the accounts file knows about it.
# --------------------------------------------------------------------------- #


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
