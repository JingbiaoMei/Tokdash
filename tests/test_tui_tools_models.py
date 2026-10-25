"""The Overview's Tools&Models section: every tool's OWN models, section by tool.

The dashboard has a section the terminal did not -- "Apps & Models Breakdown"
(index.html ``updateAppsBreakdown``) -- which sits between the tools table and
the merged model table and answers the question neither of them can: what did
THIS tool spend on THIS model. The tests here pin the two things that make it
that section rather than a reshaped copy of Models: a model used by two tools is
TWO rows here, and each section header says the same figures the Tools view says
for that tool.

The view-model tests drive ``views.tool_model_sections`` with contract-shaped
payloads; the pilot tests drive the real Textual pane through the same seams
``test_tui_revision5`` pins.
"""
from __future__ import annotations

import copy

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import Button, DataTable  # noqa: E402

from tokdash.sessions import TOOL_LABELS  # noqa: E402
from tokdash.tui import views  # noqa: E402
from tokdash.tui.formatting import EM_DASH  # noqa: E402

import test_tui_revision5 as fx
from test_tui_revision5 import lit, press, rows_of, settle, visible  # noqa: F401

api = fx.api
SHARED_MODEL = fx.SHARED_MODEL
USAGE = fx.USAGE
TOGGLES = ["ov-tab-harnesses", "ov-tab-models", "ov-tab-breakdown"]


def sections(usage=USAGE, **kwargs):
    return views.tool_model_sections(usage, labels=TOOL_LABELS, **kwargs)["sections"]


def section(tool, usage=USAGE, **kwargs):
    return next(s for s in sections(usage, **kwargs) if s["tool"] == tool)


# --------------------------------------------------------------------------- #
# the section list: per tool, never merged
# --------------------------------------------------------------------------- #


def test_a_model_two_tools_used_is_two_rows_here_and_one_row_in_models():
    """The whole reason the section exists.

    ``combined_models`` merges on the normalized name, so Models cannot say what
    one tool spent; this table must NOT merge, or it becomes a worse Models.
    """
    where = [(s["tool"], m["name"]) for s in sections() for m in s["models"]]
    shared = [t for t, name in where if name == SHARED_MODEL]
    assert shared == ["codex", "claude", "openclaw"], where
    merged = [r for r in views.model_rows(USAGE)["rows"] if r["name"] == SHARED_MODEL]
    assert len(merged) == 1
    # ... and the merged row is the SUM of the three per-tool rows, not a fourth.
    assert merged[0]["tokens"] == "800.0K"


def test_sections_are_ordered_by_the_tools_view_order():
    """Tokens descending, the same order the Tools table lists the tools in.

    Two tables that list the same tools in two orders read as two different
    sets of tools.
    """
    assert [s["tool"] for s in sections()] == ["codex", "claude", "openclaw"]


def test_openclaw_gets_a_section_from_its_own_model_list():
    """OpenClaw has no ``apps`` entry; ``openclaw_models`` stands in for it.

    Reading only ``apps`` would drop the tool that carries the window's priciest
    source, which is the mistake the web section explicitly works around.
    """
    openclaw = section("openclaw")
    assert [m["name"] for m in openclaw["models"]] == [SHARED_MODEL]
    assert openclaw["tokens"] == "100.0K"
    # Its messages have no apps entry, so they come from summing its models.
    assert openclaw["messages"] == "6"


def test_a_tool_with_nothing_to_list_gets_no_section():
    """The web drops them; a heading over an empty list is a broken heading."""
    usage = copy.deepcopy(USAGE)
    usage["apps"]["claude"]["models"] = [
        {"name": "quiet-model", "tokens": 0, "cost": 0.0, "messages": 0}
    ]
    assert [s["tool"] for s in sections(usage)] == ["codex", "openclaw"]


def test_a_model_that_recorded_nothing_earns_no_row():
    usage = copy.deepcopy(USAGE)
    usage["apps"]["codex"]["models"].append(
        {"name": "zero-model", "tokens": 0, "tokens_in": 0, "tokens_out": 0,
         "tokens_cache": 0, "cost": 0.0, "messages": 0}
    )
    assert [m["name"] for m in section("codex", usage)["models"]] == [
        SHARED_MODEL, "gpt-5-mini",
    ]


# --------------------------------------------------------------------------- #
# the figures: one field each, missing kept apart from zero
# --------------------------------------------------------------------------- #


def test_a_section_header_says_what_the_tools_row_says():
    """Same payload, same fields, or the pane shows two totals for one tool."""
    headers = {r["tool"]: r for r in views.harness_rows(USAGE, labels=TOOL_LABELS)}
    for section_row in sections():
        head = headers[section_row["tool"]]
        for key in ("tokens", "cost", "hit", "messages"):
            assert section_row[key] == head[key], (section_row["tool"], key)


def test_missing_model_splits_stay_a_dash_not_a_zero():
    # The fixture's per-tool model rows carry no tokens_in/out/cache at all.
    row = next(m for m in section("codex")["models"] if m["name"] == SHARED_MODEL)
    assert row["tokens"] == "400.0K"
    assert row["input"] == EM_DASH and row["output"] == EM_DASH
    assert row["cache"] == EM_DASH
    assert row["hit"] == "n/a"


def test_a_model_share_divides_by_its_own_tool():
    """400.0K of Codex's 600.0K, not of the window's million.

    The merged Models table divides by the global total; a per-tool table that
    did the same would show a model as a fraction of work it did not do.
    """
    row = next(m for m in section("codex")["models"] if m["name"] == SHARED_MODEL)
    assert row["share"] == "66.7%"


def test_the_pane_sort_orders_the_models_inside_every_section():
    usage = copy.deepcopy(USAGE)
    usage["apps"]["codex"]["models"] = [
        {"name": "cheap-big", "tokens": 500_000, "cost": 0.5, "messages": 3},
        {"name": "dear-small", "tokens": 10_000, "cost": 9.0, "messages": 1},
    ]
    assert [m["name"] for m in section("codex", usage, sort="tokens")["models"]] == [
        "cheap-big", "dear-small",
    ]
    assert [m["name"] for m in section("codex", usage, sort="cost")["models"]] == [
        "dear-small", "cheap-big",
    ]
    assert [m["name"] for m in section("codex", usage, sort="name")["models"]] == [
        "cheap-big", "dear-small",
    ]


# --------------------------------------------------------------------------- #
# the pane's search box filters this view too
# --------------------------------------------------------------------------- #


def test_search_keeps_a_matching_tool_whole_and_drops_the_rest():
    """Typing "claude" asks about the tool, not for a name coincidence."""
    result = views.tool_model_sections(USAGE, search="claude", labels=TOOL_LABELS)
    assert [s["tool"] for s in result["sections"]] == ["claude"]
    assert [m["name"] for m in result["sections"][0]["models"]] == [SHARED_MODEL]
    assert result["searched"] is True


def test_search_narrows_to_the_matching_models_of_the_tools_that_have_them():
    result = views.tool_model_sections(USAGE, search="gpt-5-mini",
                                       labels=TOOL_LABELS)
    assert [s["tool"] for s in result["sections"]] == ["codex"]
    assert [m["name"] for m in result["sections"][0]["models"]] == ["gpt-5-mini"]
    assert result["models_listed"] == 1


# --------------------------------------------------------------------------- #
# the pane: the third toggle, and the table it paints
# --------------------------------------------------------------------------- #


def test_the_third_toggle_is_the_one_marked_and_it_replaces_the_table(api):
    async def body(app, pilot):
        app.action_show_pane("overview")
        await settle(pilot)
        assert lit(app, TOGGLES) == ["ov-tab-harnesses"], lit(app, TOGGLES)
        press(app, "ov-tab-breakdown")
        await settle(pilot, 3)
        assert lit(app, TOGGLES) == ["ov-tab-breakdown"], lit(app, TOGGLES)
        # Still ONE table on the pane: the sectioning is rows, not a second grid.
        assert len(app.query("#pane-overview DataTable")) == 1

    fx.run(api, body)


def test_the_pane_names_the_scope_the_sections_cover(api):
    async def body(app, pilot):
        press(app, "ov-tab-breakdown")
        await settle(pilot, 3)
        assert "Each tool's own models" in visible(app, "#ov-scope")
        assert "sorted by" in visible(app, "#ov-sort")

    fx.run(api, body)


def test_the_table_is_the_tools_with_their_models_underneath(api):
    async def body(app, pilot):
        press(app, "ov-tab-breakdown")
        await settle(pilot, 3)
        rows = rows_of(app.query_one("#ov-table", DataTable))
        # A header row names a tool; a model row is that name indented under it.
        heads = [i for i, row in enumerate(rows) if not row[0].startswith("  ")]
        assert [rows[i][0] for i in heads] == ["Codex", "Claude Code", "OpenClaw"]
        # The shared model appears under EVERY tool that recorded it: three
        # rows of one name, each of which says which tool it belongs to.
        owners = [i for i, row in enumerate(rows)
                  if row[0].strip() == SHARED_MODEL]
        assert len(owners) == 3, rows
        assert all(rows[i][0].startswith("  ") for i in owners), rows
        # A header row carries the tool's OWN totals in the same columns as its
        # models, which is what makes a section add up where the reader looks.
        codex = rows[heads[0]]
        assert codex[1] == "600.0K", codex
        assert codex[2] == "$3.00", codex

    fx.run(api, body)


def test_enter_on_a_model_row_says_which_tool_the_figure_belongs_to(api):
    async def body(app, pilot):
        press(app, "ov-tab-breakdown")
        await settle(pilot, 3)
        table = app.query_one("#ov-table", DataTable)
        rows = rows_of(table)
        # The first model row under the Codex header: same name as two other
        # rows, so this is the case a plain (name) key could not tell apart.
        target = next(
            i for i, row in enumerate(rows)
            if i > 0 and row[0].strip() == SHARED_MODEL
        )
        table.focus()
        table.move_cursor(row=target)
        await settle(pilot, 1)
        await pilot.press("enter")
        await settle(pilot, 2)
        detail = visible(app, "#ov-detail")
        assert SHARED_MODEL in detail and "Codex" in detail, detail
        # The number is this tool's, and the caption says so: the merged figure
        # a reader might expect lives in the Models view, and it is different.
        assert "400.0K" in detail, detail
        assert "every tool" in detail.lower(), detail

    fx.run(api, body)


def test_enter_on_a_tool_header_opens_that_tool_in_the_tools_view(api):
    async def body(app, pilot):
        press(app, "ov-tab-breakdown")
        await settle(pilot, 3)
        table = app.query_one("#ov-table", DataTable)
        table.focus()
        table.move_cursor(row=0)  # the first section header
        await settle(pilot, 1)
        await pilot.press("enter")
        await settle(pilot, 2)
        assert lit(app, TOGGLES) == ["ov-tab-harnesses"], lit(app, TOGGLES)
        assert app._ov_selected == "codex", app._ov_selected
        assert "codex" in visible(app, "#ov-scope").lower()

    fx.run(api, body)


def test_search_narrows_the_sections_on_screen_too(api):
    async def body(app, pilot):
        press(app, "ov-tab-breakdown")
        await settle(pilot, 3)
        table = app.query_one("#ov-table", DataTable)
        # 3 tool headers + the 4 model rows under them.
        assert table.row_count == 7, rows_of(table)
        app._ov_search = "gpt-5-mini"
        app._paint_overview()
        await settle(pilot, 2)
        rows = rows_of(table)
        assert [row[0] for row in rows] == ["Codex", "  gpt-5-mini"], rows
        # The column header says how much of the window the filter left.
        assert "1 of 3 tools · 1 model" in str(
            list(table.columns.values())[0].label
        ), str(list(table.columns.values())[0].label)

    fx.run(api, body)
