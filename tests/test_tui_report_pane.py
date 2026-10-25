"""The Report pane: the shared report, read one section at a time.

Two scans stand behind the page -- usage for the headline, analytics for the
facets -- and each figure divides by its own; the page says which. The When
section reaches the whole hour grid and the whole week, Projects follows the sort
it advertises, a facet that failed says it failed (and one still running, that it
is running), and the pane leaves out the rules and store footer that the piped
``tokdash report`` keeps. The piped report itself is test_tui_report.py's.
Pilots over the contract-shaped payloads in ``support.tui``.
"""
from __future__ import annotations

import copy
import re
import threading

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

import support.tui as fx  # noqa: E402  (the layout-law tests call it fx)
from support.tui import (  # noqa: E402
    USAGE,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    body_text,
    press,
    run,
    settle,
    visible,
)


# --------------------------------------------------------------------------- #
# Report: one page, two scans, and the denominators that go with them
# --------------------------------------------------------------------------- #


def test_report_agents_read_their_sessions_from_active_time(api):
    """The Sessions column is active-time's, and the clock is not the sum."""

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 4)
        text = visible(app, "#rp-body")
        assert "Tokens 1.0M" in text, text  # usage scan
        assert "Agent  1h 00m clock · 1h 30m sum" in text, text
        rows = [ln for ln in text.splitlines() if ln.strip().startswith(("Codex", "Claude"))]
        assert len(rows) == 2, rows
        assert "3" in rows[0].split(), rows[0]      # by_tool.session_count
        assert "2" in rows[1].split(), rows[1]
        assert "0s" in rows[0], rows[0]              # codex measured zero time
        # A source active time could not read is absent, not a zero row.
        assert "kimi" not in text.lower(), text

    run(api, body)


def test_report_keeps_the_two_scans_denominators_apart(api):
    """Attribution divides by the analytics scan, the headline by the usage scan."""

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 4)
        # The summary names the usage scan as the headline's denominator.
        summary = visible(app, "#rp-body")
        assert "Headline totals are the usage scan" in summary, summary
        press(app, "rp-tab-projects")
        await settle(pilot, 3)
        text = visible(app, "#rp-body")
        # Attribution divides the ANALYTICS scan, named above the rows it ranks.
        assert "shares divide the analytics scan total 900.0K" in text, text
        assert "tokdash" in text and "500.0K" in text, text
        assert "unattributed" in text and "200.0K" in text, text
        # The two scans do not add up, and the page says so instead of hiding it.
        assert "differs from the scan total by +100,000 tokens" in text, text

    run(api, body)


def test_report_when_uses_the_full_hour_grid_and_the_named_weekday(api):
    """The WHOLE facet: 24 hours, 7 named weekdays, the share denominator, the clock.

    The piped report prints the three busiest hours and one weekday because a
    report is a summary. A PANE is where the rest of the facet lives, so the
    claim this test has to actually carry is that the quiet end of the day and
    the other six weekdays are reachable here -- not just the peak line.
    """

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 4)
        press(app, "rp-tab-when")
        await settle(pilot, 3)
        text = visible(app, "#rp-body")
        assert "peak hour 09:00" in text, text
        assert "09:00" in text and "400.0K" in text, text
        # The weekday bucket key is "weekday" + "name"; a "day" key would read
        # as an empty facet and print a dash where Monday belongs.
        assert "busiest weekday Monday" in text, text
        assert "night share" in text, text
        # Every hour of the clock, including the ones nobody clicked on.
        for hour in range(24):
            assert f"{hour:02d}:00" in text, (hour, text)
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                    "Saturday", "Sunday"):
            assert day in text, (day, text)
        # Shares divide the analytics scan total, named above the rows.
        assert "analytics scan total" in text, text
        # The clock is named, not implied: the facet's own timezone.
        rendered = str(app.query_one("#rp-body").render())
        assert "BST" in rendered, rendered
        # The grid is taller than the pane and the pane scrolls, so "reachable"
        # means paged to, not clipped away.
        assert app.query_one("#rp-body").region.height >= 24

    run(api, body)


def test_report_surfaces_a_source_that_only_half_read(api):
    """A window whose scan half-failed is never a quiet zero."""

    async def body(app, pilot):
        api.usage = copy.deepcopy(USAGE)
        api.usage["source_errors"] = [
            {"source": "kimi", "error": "unreadable"}, "grok",
        ]
        app.action_show_pane("report")
        await settle(pilot, 4)
        text = visible(app, "#rp-body")
        assert "2 source(s) failed this window: kimi, grok" in text, text

    run(api, body)


def test_report_names_a_soft_failure_rather_than_dashing_it_out(api):
    """The analytics facet failed to read; that is a sentence, not an em dash."""

    async def body(app, pilot):
        api.insights = RuntimeError("scan exploded")
        app.action_show_pane("report")
        await settle(pilot, 4)
        # Usage still paints (it is the hard source); the facet failure is a
        # warning and the facets print as dashes beside it.
        text = visible(app, "#rp-body")
        assert "analytics scan failed" in text, text

    run(api, body)


# -------------------------------------------------------------------- F10 --
# Report -> When routed to the summary renderer: three hours, one weekday, and
# the rest of the facet unreachable. The whole grid is pinned above, by
# test_report_when_uses_the_full_hour_grid_and_the_named_weekday; a facet that
# never answered says so instead of drawing an empty grid.


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


# --------------------------------------------------------------------------- #
# Report: the shared report, without a printed page's furniture
# --------------------------------------------------------------------------- #


def test_the_report_pane_shows_no_rules_and_no_store_footer(api):
    """The pane and the piped report share the figures, not the page furniture.

    Rules separate sections on paper; here spacing and surface tone do it, and
    the store line is already at the foot of every pane. The [warn] lines and the
    note about which scan each figure came from stay, because those are news.
    """

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 6)
        painted = visible(app, "#rp-body")
        assert "\u2500" not in painted, painted
        assert "usage db" not in painted and "db status" not in painted, painted
        assert "Tokens" in painted, painted

    fx.run(api, body, size=(100, 30))


def test_the_piped_report_still_carries_its_rules_and_footer(api):
    """The pane's trim is the pane's business: the document is unchanged."""

    from tokdash.tui.report import build_report_segments, emit_plain

    segments = build_report_segments(
        copy.deepcopy(fx.USAGE), copy.deepcopy(fx.INSIGHTS),
        copy.deepcopy(fx.ACTIVE),
        window={"period": "range", "from": "2026-10-12", "to": "2026-10-18",
                "days": 7, "db_line": "usage db: test"},
        width=80,
    )
    text = emit_plain(segments)
    assert "\u2500" in text, text
    assert "usage db: test" in text, text
