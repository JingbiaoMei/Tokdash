"""The Overview pane: the headline cards, the Tools and Models tables, the note
above them and the activity band below.

Each case drives the real pane through a Textual pilot over the contract-shaped
payloads in ``support.tui`` and reads back what the screen shows: one row per
normalized model with its share of the GLOBAL total, a measured zero kept apart
from a source nobody read, a stale or half-failed snapshot named in the note, and
cards that keep every figure whole at each width a terminal comes in. The
Tools&Models section has its own file, test_tui_tools_models.py.
"""
from __future__ import annotations

import copy

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import DataTable, Static  # noqa: E402

import tokdash.tui.app as app_mod  # noqa: E402
from tokdash.tui import views  # noqa: E402
from tokdash.tui.data import FetchOutcome  # noqa: E402
import support.tui as fx  # noqa: E402  (the layout-law tests call it fx)
from support.tui import (  # noqa: E402
    ACTIVE,
    SHARED_MODEL,
    TODAY,
    USAGE,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    headers_of,
    press,
    rows_of,
    run,
    settle,
    visible,
)


# --------------------------------------------------------------------------- #
# Models: one aggregate table, one ranking per glance
# --------------------------------------------------------------------------- #


def test_models_aggregate_one_row_per_normalized_model(api):
    """The same normalized name in three harnesses is ONE row, with no Harness
    column and no second leaderboard."""

    async def body(app, pilot):
        app.action_show_pane("overview")
        press(app, "ov-tab-models")
        await settle(pilot)
        table = app.query_one("#ov-table", DataTable)
        rows = rows_of(table)
        names = [row[0] for row in rows]
        assert names.count(SHARED_MODEL) == 1, rows
        assert len(names) == 3, rows  # combined_models has three merged rows
        assert not any("harness" in head.lower() for head in headers_of(table))
        shared = next(row for row in rows if row[0] == SHARED_MODEL)
        assert shared[1] == "800.0K"       # the merged token sum, not one source
        assert shared[2] == "80.0%"        # against the GLOBAL total_tokens
        assert shared[3] == "$11.50"
        # The pane carries ONE table: the duplicate cost leaderboard is gone.
        assert len(app.query("#pane-overview DataTable")) == 1
        assert "by cost" not in visible(app, "#ov-cards").lower()

    run(api, body)


def test_cost_is_a_sort_of_the_same_table(api):
    async def body(app, pilot):
        press(app, "ov-tab-models")
        await settle(pilot)
        by_tokens = rows_of(app.query_one("#ov-table", DataTable))
        assert by_tokens[0][0] == SHARED_MODEL
        app.action_cycle_sort()  # tokens -> cost
        await settle(pilot, 2)
        by_cost = rows_of(app.query_one("#ov-table", DataTable))
        assert by_cost[0][0] == "claude-opus-4-7", by_cost
        assert sorted(row[0] for row in by_cost) == sorted(row[0] for row in by_tokens)
        assert len(app.query("#pane-overview DataTable")) == 1

    run(api, body)


def test_most_used_card_follows_tokens_under_a_cost_sort(api):
    async def body(app, pilot):
        press(app, "ov-tab-models")
        await settle(pilot)
        while app._ov_sort != "cost":
            app.action_cycle_sort()
            await pilot.pause(0.2)
        cards = visible(app, "#ov-cards")
        assert SHARED_MODEL in cards, cards
        assert "claude-opus-4-7" not in cards  # the priciest model is not "most used"
        assert "token-ranked" in cards or "800.0K tokens" in cards

    run(api, body)


def test_search_filters_rows_but_not_the_denominator(api):
    async def body(app, pilot):
        press(app, "ov-tab-models")
        await settle(pilot)
        app._ov_search = "gpt-5-mini"
        app._paint_overview()
        await settle(pilot, 1)
        rows = rows_of(app.query_one("#ov-table", DataTable))
        assert [row[0] for row in rows] == ["gpt-5-mini"]
        assert rows[0][2] == "5.0%", rows  # 50,000 of the global million
        assert "1 of 3" in visible(app, "#ov-table"), headers_of(app.query_one("#ov-table", DataTable))

    run(api, body)


def test_harnesses_keep_measured_zero_apart_from_missing(api):
    async def body(app, pilot):
        app.action_show_pane("overview")
        await settle(pilot)
        table = app.query_one("#ov-table", DataTable)
        rows = {row[0]: row for row in rows_of(table)}
        time_cell = -1  # Est. time is the last column in both shapes
        assert rows["Codex"][time_cell] == "0s", rows        # measured zero
        assert rows["OpenClaw"][time_cell] == "—", rows       # never read
        assert rows["OpenClaw"][1] == "100.0K"               # tokens survive a time gap
        assert "kimi" in visible(app, "#ov-note")

    run(api, body)


def test_harness_detail_names_its_source_and_keeps_kpis_global(api):
    async def body(app, pilot):
        table = app.query_one("#ov-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await settle(pilot)
        assert app._ov_selected == "codex"
        header = headers_of(table)[0]
        assert "Codex" in header and TODAY.isoformat() in header, header
        assert rows_of(table)  # apps["codex"].models
        assert "stay" in visible(app, "#ov-detail").lower() or "global" in visible(app, "#ov-detail").lower()
        await pilot.press("escape")
        await settle(pilot, 1)
        assert app._ov_selected is None

    run(api, body)


def test_activity_strip_reports_the_buckets_the_payload_carries(api):
    async def body(app, pilot):
        await settle(pilot)
        text = visible(app, "#ov-activity")
        assert "2 hour buckets" in text, text
        assert "2026-10-18T09" in text
        assert "server local time" in text     # never the client's guessed zone
        assert "0 tokens" in text              # a real zero hour stays visible

    run(api, body)


def test_long_range_omits_the_chart_instead_of_inventing_one(api):
    async def body(app, pilot):
        api.usage = {**USAGE, "sparkline": None}
        app.action_set_range("all")
        await settle(pilot)
        assert app._win.request() == ("all", None, None)
        assert "Activity chart unavailable" in visible(app, "#ov-activity")
        # The totals stay; only the chart is withheld.
        assert "1.0M" in visible(app, "#ov-cards")

    run(api, body)


@pytest.mark.parametrize("size", [(120, 36), (100, 30)])
def test_a_caption_row_never_ends_in_the_middle_of_a_fact(api, size):
    """A number split across two rows reads as two wrong numbers.

    "peak 612.0K tokens · 1" over "bucket measured 0 tokens" is what packing the
    facts as ONE string does: the row breaks at the column, not at the fact.
    """
    async def body(app, pilot):
        band = app.query_one("#ov-activity")
        lines = str(band.render()).splitlines()
        tail = [ln for ln in lines if "\u00b7" in ln]
        assert tail, lines
        for line in tail:
            assert not line.rstrip().endswith(("\u00b7", "1", "0")), (size, line)
        assert "widen to read" not in "\n".join(lines), lines

    run(api, body, size=size)


# --------------------------------------------------------------------------- #
# the note: a stale or half-read snapshot says so
# --------------------------------------------------------------------------- #


def test_stale_snapshot_is_said_out_loud(api):
    async def body(app, pilot):
        await settle(pilot)
        api.status["usage"] = "stale"
        app.action_refresh()
        await settle(pilot, 4)
        assert "last computed snapshot" in visible(app, "#ov-note")

    run(api, body)


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


# --------------------------------------------------------------------------- #
# the headline band: figures with room to be read as figures
# --------------------------------------------------------------------------- #


def test_the_card_band_grows_with_the_room_it_is_given(api):
    """A terminal has no font size, so the size of a headline is its space.

    The approved cards are more panel than ink. At thirty-six rows the band
    pays for that; at twenty-four the row comes back to the table, which is the
    one line of the trade a reader cannot make themselves.
    """

    async def body(app, pilot):
        # Six rows for one row of six: air, label, air, figure, note, air. The
        # air BETWEEN the label and the figure is what makes the figure the
        # card's headline in a terminal that has no font size to lean on.
        cards = app.query_one("#ov-cards")
        assert cards.region.height == 6, cards.region
        painted = visible(app, "#ov-cards")
        for label in ("TOTAL TOKENS", "TOTAL COST", "TOTAL MESSAGES",
                      "EST. AGENT TIME", "CACHE HIT RATE", "MOST USED MODEL"):
            assert label in painted, (label, painted)

    fx.run(api, body, size=(120, 36))

    async def tight(app, pilot):
        # Eighty columns is where six across stops holding a figure whole: the
        # band goes to two rows of three (and gives its air back to the table),
        # because "gpt-5-c…" is a different model, not a shorter one.
        assert app.query_one("#ov-cards").region.height == 6
        painted = visible(app, "#ov-cards")
        assert "1.0M" in painted and "gpt-5.1-codex" in painted, painted
        assert "\u2026" not in painted, painted

    fx.run(api, tight, size=(80, 24))


def test_the_card_band_is_six_objects_not_six_lines_of_text(api, monkeypatch):
    # The accent edge is a colour affordance; in a monochrome terminal the block
    # geometry carries the hierarchy alone (see the mono law in test_tui_app).
    """Each figure carries its own colour down its left edge.

    Six tinted numbers on a shared background still read as six similar words
    first; the accent edge is what makes the band scannable as six kinds of
    figure, and it is the same mark a source wears everywhere else on screen.
    """

    monkeypatch.delenv("NO_COLOR", raising=False)

    async def body(app, pilot):
        assert not app.no_color, "NO_COLOR is set: this test asks about colour"
        text = app.query_one("#ov-cards", Static).content
        styles = [str(span.style) for span in text.spans]
        for key, tint in app_mod._CARD_COLORS.items():
            if key not in {c.get("key") for c in views.kpi_cards(
                app._ov_usage or {}, app._ov_active, window=""
            )}:
                continue
            assert any(tint in s and s.startswith("on ") for s in styles), (key, tint)
        # The band's right gutter: the band is narrower than the pane, so the
        # last card never touches the edge of the screen.
        for line in str(text).split("\n"):
            assert len(line) <= 118 - 1, (len(line), line)

    fx.run(api, body, size=(120, 36))


def test_a_tight_card_band_pays_its_gutter_in_figures(api):
    """A hundred columns is the width a laptop has.

    The second cell of gutter between cards is decoration; the cell it buys back
    is the difference between a model name and a model name with a piece gone,
    and the piece is what the reader was reading.
    """

    async def body(app, pilot):
        await settle(pilot, 2)
        painted = visible(app, "#ov-cards")
        assert "gpt-5.1-codex" in painted, painted
        assert "\u2026" not in painted, painted

    fx.run(api, body, size=(100, 30))


# --------------------------------------------------------------------------- #
# card captions: a crop that lands mid-word reads as a broken build
# --------------------------------------------------------------------------- #

def _card_lines(cards, pane_width, mono=False):
    per_row = 6 if pane_width >= 116 else 3 if pane_width >= 76 else 2
    block = app_mod._card_block(cards, per_row, pane_width, mono)
    return str(block).splitlines()


@pytest.mark.parametrize("pane_width", [118, 98, 78, 58])
def test_a_card_caption_never_breaks_in_the_middle_of_a_word(pane_width):
    """Six cards share one row at 120 columns, so a caption has 16 cells.

    A sub-caption the user cannot finish reading is worse than none: "prompt-
    token we..." leaves the denominator open, which is exactly the confusion
    the caption exists to close. So the short wording has to be short.
    """
    import tokdash.tui.views as views_mod

    cards = views_mod.kpi_cards(copy.deepcopy(USAGE), copy.deepcopy(ACTIVE))
    assert cards, "the fixture has to feed the headline row"
    lines = _card_lines(cards, pane_width)
    cropped = [ln for ln in lines if ln.rstrip().endswith("\u2026")]
    assert not cropped, (pane_width, cropped)


@pytest.mark.parametrize("pane_width", [118, 98, 78, 58])
def test_every_card_keeps_its_full_value_at_every_width(pane_width):
    """Cropping a caption is a wording decision; cropping a FIGURE is a bug."""
    import tokdash.tui.views as views_mod

    cards = views_mod.kpi_cards(copy.deepcopy(USAGE), copy.deepcopy(ACTIVE))
    painted = "\n".join(_card_lines(cards, pane_width))
    for card in cards:
        assert str(card["value"]) in painted, (pane_width, card["key"], card["value"])


def test_the_monochrome_cards_still_separate_their_figures():
    """No tints in a mono terminal: the geometry alone has to group the row."""
    import tokdash.tui.views as views_mod

    cards = views_mod.kpi_cards(copy.deepcopy(USAGE), copy.deepcopy(ACTIVE))
    painted = "\n".join(_card_lines(cards, 118, mono=True))
    for label in ("TOTAL TOKENS", "TOTAL COST", "TOTAL MESSAGES",
                  "EST. AGENT TIME", "CACHE HIT RATE", "MOST USED MODEL"):
        assert label in painted, label


def test_a_monochrome_terminal_loses_the_tints_not_the_figures(api):
    async def body(app, pilot):
        app.no_color = True
        app._repaint_pane("overview")
        await settle(pilot, 3)
        cards = visible(app, "#ov-cards")
        assert "1.0M" in cards and "$12.50" in cards, cards
        assert "gpt-5.1-codex" in cards, cards   # the name, not a colour swatch
        cal = app.query_one("#st-calendar", Static)
        assert cal is not None

    run(api, body)


# --------------------------------------------------------------------------- #
# the rows: what the design spends its vertical space on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("size,air", [((120, 36), 2), ((100, 30), 1), ((80, 24), 1)])
def test_an_overview_row_buys_its_air_as_a_second_line(api, size, air):
    """The approved table spaces its rows; a terminal has no line spacing.

    The air IS a second line, and the page order says what it is bought from:
    the pane scrolls to the activity picture before the rows lose theirs. Under
    thirty-two rows the page is dense on purpose -- there the second line would
    cost a figure the reader came to read.
    """

    async def body(app, pilot):
        table = app.query_one("#ov-table", DataTable)
        keys = list(table.rows.keys())
        assert len(keys) >= 3, [str(k) for k in keys]
        for key in keys:
            assert table.get_row_height(key) == air, (key, table.get_row_height(key))
        # The air is on the SCREEN, not just in the row record: the table's own
        # region is header + one line per row, or header + two.
        region = table.region.height
        assert region == 1 + air * len(keys), (region, air, len(keys))

    fx.run(api, body, size=size)
