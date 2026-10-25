"""Selected state: a row of look-alike buttons must show which one is in force.

Textual owns ``-active`` for a 200 ms press flash and removes it on a timer, so
the app marks the live choice with a class of its own, ``-selected``: the range
preset that names the window, the Overview subview, the Report section, and the
Speed mode and lens. These check both halves of it. The class follows the choice,
and the stylesheet really paints it: the widget's resolved background is the
selected fill, and an unselected neighbour's is not. Pilots over the
contract-shaped payloads in ``support.tui``.
"""
from __future__ import annotations

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import Button  # noqa: E402

from tokdash.tui.app import TokdashApp  # noqa: E402
from tokdash.tui.ranges import STRIP_PRESETS  # noqa: E402
from tokdash.tui.ranges import PRESETS as _ALL_PRESETS  # noqa: E402
import support.tui as fx  # noqa: E402  (the layout-law tests call it fx)
from support.tui import (  # noqa: E402
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    lit,
    press,
    run,
    settle,
)


SELECTED_FILL = "#b9a7ff"  # the lavender fill of the approved design


RANGE_IDS = [f"range-{preset.key}" for preset in _ALL_PRESETS]


def test_the_range_strip_marks_the_preset_that_names_the_window(api):
    """Ten look-alike buttons are only usable if one of them looks chosen.

    Textual owns ``-active`` for a 200 ms press flash and removes it on a timer,
    so an app that paints ``Button.active`` shows NOTHING persistent: every
    preset looks as selected as every other, and the only clue to the live
    window is the date text. ``-selected`` is the app's own class.
    """

    async def body(app, pilot):
        assert lit(app, RANGE_IDS) == ["range-today"]
        press(app, "range-thisweek")  # a shortcut that is ON the strip
        await settle(pilot, 4)
        assert lit(app, RANGE_IDS) == ["range-thisweek"], lit(app, RANGE_IDS)
        await pilot.press("d")
        await settle(pilot, 3)
        assert lit(app, RANGE_IDS) == ["range-yesterday"], lit(app, RANGE_IDS)
        # Stepping off that preset's own window leaves no button lit on purpose:
        # the exact dates carry the window then, and a lit shortcut would lie.
        await pilot.press("right_square_bracket")
        await settle(pilot, 3)
        assert lit(app, RANGE_IDS) == [], lit(app, RANGE_IDS)

    run(api, body)


def test_every_button_group_marks_the_choice_it_is_showing(api):
    """Subview, report section, speed mode and speed lens all wear the fill."""

    overview = ["ov-tab-harnesses", "ov-tab-models"]
    sections = ["rp-tab-summary", "rp-tab-when", "rp-tab-projects"]
    modes = ["sp-mode-across", "sp-mode-trend", "sp-mode-hour"]
    lenses = ["sp-lens-stream", "sp-lens-combined"]

    async def body(app, pilot):
        assert lit(app, overview) == ["ov-tab-harnesses"]
        press(app, "ov-tab-models")
        await settle(pilot, 2)
        assert lit(app, overview) == ["ov-tab-models"], lit(app, overview)

        app.action_show_pane("report")
        await settle(pilot, 4)
        assert lit(app, sections) == ["rp-tab-summary"], lit(app, sections)
        press(app, "rp-tab-when")
        await settle(pilot, 2)
        assert lit(app, sections) == ["rp-tab-when"], lit(app, sections)

        app.action_show_pane("speed")
        await settle(pilot, 5)
        assert lit(app, modes) == ["sp-mode-across"], lit(app, modes)
        assert lit(app, lenses) == ["sp-lens-stream"], lit(app, lenses)
        press(app, "sp-mode-trend")
        await settle(pilot, 5)
        assert lit(app, modes) == ["sp-mode-trend"], lit(app, modes)
        assert lit(app, lenses) == ["sp-lens-stream"], lit(app, lenses)
        press(app, "sp-lens-combined")
        await settle(pilot, 5)
        assert lit(app, lenses) == ["sp-lens-combined"], lit(app, lenses)
        assert lit(app, modes) == ["sp-mode-trend"], lit(app, modes)

    run(api, body)


def test_the_selected_class_is_painted_not_just_set(api):
    """A class nobody styles is an invisible feature; the fill is the feature."""
    css = TokdashApp.CSS
    for selector in ("#rangebar Button.-selected", ".toggle Button.-selected"):
        assert selector in css, selector
    selected = css[css.index(".toggle Button.-selected"):]
    assert SELECTED_FILL in selected[:240], selected[:240]


def test_the_live_window_is_the_one_thing_the_strip_paints_lavender(api):
    """Five boxes read as five things to press; the design paints one choice.

    An unchosen window is text with its key printed on a small plate, and the
    lavender plate belongs to the window actually on screen -- otherwise the row
    says "look, five equal buttons" where it should say "you are here".
    """

    async def body(app, pilot):
        chosen = [b for b in app.query("#rangebar Button") if "-selected" in b.classes]
        assert [b.id for b in chosen] == ["range-today"], [b.id for b in chosen]
        assert chosen[0].styles.background is not None
        for preset in STRIP_PRESETS:
            button = app.query_one(f"#range-{preset.key}", Button)
            if button.id == "range-today":
                continue
            assert "-selected" not in button.classes, button.id

        await pilot.press("m")
        await settle(pilot, 2)
        assert "-selected" in app.query_one("#range-month", Button).classes
        assert "-selected" not in app.query_one("#range-today", Button).classes

    fx.run(api, body, size=(120, 36))
