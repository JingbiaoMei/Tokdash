"""Contract for the exact-value hover readouts on the Overview KPI row.

Every card in the row carries the figure behind its rounded value in the same
hover readout -- "186M" reads out as "185,952,048 tokens", "97.6%" as "97.59%" --
so the precise number is available without widening six narrow cards. These tests
pin the parts that break silently: that every card with a readout stays wired,
that the readout survives the cursor crossing onto it, and that it borrows its
colour from the card value it belongs to.

The Cost card deliberately has no readout: `/api/usage` rounds `total_cost` to
two decimals before it reaches the browser, so a four-decimal readout would only
restate the card's own rounded figure with false precision.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


import tokdash  # type: ignore[import-untyped]

from support.frontend import extract_js_function, needs_node


INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

# The five cards in the Overview KPI row that carry a readout, as
# (value id, readout id). Cost is absent by design -- see the module docstring.
KPI_READOUTS = (
    ("totalTokens", "totalTokensExact"),
    ("totalMessages", "totalMessagesExact"),
    ("overviewActiveTime", "overviewActiveTimeExact"),
    ("avgCacheHitRate", "avgCacheHitRateExact"),
    ("topModel", "topModelExact"),
)

# The Tailwind colour each card's value is painted in, which its readout mirrors.
# Cost's #34d399 is absent with its readout.
KPI_TONES = ("#818cf8", "#c084fc", "#38bdf8", "#22d3ee", "#fcd34d")


def _tooltip_rule(source: str) -> str:
    rule = source.split(".overview-token-exact-tooltip {")[1].split("}")[0]
    return "".join(rule.split())


def test_every_kpi_card_has_an_exact_readout() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    # The calls are wrapped differently line to line, so compare without the
    # whitespace rather than pinning one call's formatting.
    compact = "".join(source.split())
    for value_id, tooltip_id in KPI_READOUTS:
        assert f'id="{value_id}"' in source, f"the {value_id} card is gone"
        assert f'id="{tooltip_id}"' in source, f"{value_id} lost its exact readout"
        assert (
            f"'{value_id}','{tooltip_id}'" in compact
        ), f"nothing writes the {value_id} readout"
    # The Cost card keeps its value but not a readout: the API rounds the figure
    # to cents, so four decimals would restate the card's own number.
    assert 'id="totalCost"' in source
    assert 'id="totalCostExact"' not in source


def test_readout_stays_up_when_the_cursor_crosses_onto_it() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = "".join(source.split())
    # The wrap is the hover target rather than the value element. The readout is a
    # child of that wrap, so a cursor that crosses onto it keeps the wrap hovered
    # and the readout on screen; keying off the value element is what used to pull
    # the readout away the instant the pointer arrived on it.
    assert ".overview-token-value-wrap:hover.overview-token-exact-tooltip" in compact
    # Keyboard focus only: :focus-within also matches a mouse click on the value,
    # which left the readout open after the cursor moved away.
    assert (
        ".overview-token-value-wrap:has(:focus-visible).overview-token-exact-tooltip"
        in compact
    )
    # The readout must not take the pointer: it sits over the label row on the
    # agent-time card, and capturing the cursor there makes the info hint
    # underneath unreachable from below.
    assert "pointer-events:none" in _tooltip_rule(source)
    # The gap between the number and its readout is dead space a cursor has to cross;
    # without the bridge covering it the hover ends mid-crossing and the readout goes.
    assert ".overview-token-exact-tooltip::before" in compact


def test_readout_mirrors_its_card_colour_and_lets_the_card_show_through() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    rule = _tooltip_rule(source)
    # The readout borrows the card value's own tone instead of a fixed one, so it
    # reads as part of the card it belongs to rather than a foreign label on it.
    assert "--kpi-tone" in rule
    assert "color:var(--kpi-tooltip-tone)" in rule
    # A translucent tint over a backdrop blur, so the card stays legible through it
    # instead of disappearing behind an opaque slab.
    assert "color-mix(" in rule
    assert "backdrop-filter:blur(" in rule
    for tone in KPI_TONES:
        assert f"--kpi-tone: {tone};" in source, f"a KPI card is missing tone {tone}"
    # The tones are the dark-theme shades; light mode remaps them to the same
    # hue's dark step so the readout keeps its contrast on a pale card.
    compact = "".join(source.split())
    for wrap in (
        "totalTokensWrap",
        "totalMessagesWrap",
        "overviewActiveTimeWrap",
        "avgCacheHitRateWrap",
        "topModelWrap",
    ):
        assert f"html:not(.dark)#{wrap}" in compact, (
            f"{wrap} has no light-mode tone override"
        )


def test_readout_is_larger_than_the_number_it_reports() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    rule = _tooltip_rule(source)
    # 13px against the 36px card value and the 10px readout this replaced.
    assert "font-size:13px" in rule


def test_values_start_unfocusable_and_readouts_start_hidden() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    # Every value ships without tabindex/aria-describedby and every readout with
    # `hidden`, so a card that has not loaded yet is not a focus stop and does not
    # announce a placeholder. setKpiExactReadout adds the wiring only when there is
    # a figure to read out.
    for value_id, tooltip_id in KPI_READOUTS:
        assert f'id="{value_id}" tabindex' not in source
        assert f'id="{value_id}" aria-describedby' not in source
        assert (
            f'id="{tooltip_id}" class="overview-token-exact-tooltip"' in source
            and "hidden>-</div>" in source.split(f'id="{tooltip_id}"')[1][:200]
        ), f"{tooltip_id} should start hidden"
    readout = extract_js_function(
        source, "function setKpiExactReadout(valueId, tooltipId, text, note = '') {"
    )
    assert "valueElement.tabIndex = 0" in readout
    assert "valueElement.removeAttribute('tabindex')" in readout
    assert "tooltip.hidden = true" in readout


@needs_node
def test_exact_duration_formatter_reports_the_seconds_the_card_drops(tmp_path: Path) -> None:
    """The agent-time readout is the only exact formatter left.

    The Cost card has no readout (the API rounds its figure to cents), so
    ``formatExactCurrency`` and the ``exact`` parameter on ``formatCurrency``
    went with it. Duration keeps its readout, so its formatter still reports the
    seconds the card's h/m form rounds away.
    """
    source = INDEX_HTML.read_text(encoding="utf-8")
    harness = tmp_path / "kpi-readout.js"
    harness.write_text(
        "function langLocale() { return 'en-US'; }\n"
        + extract_js_function(source, "function formatDuration(ms, exact = false) {")
        + "\n"
        + extract_js_function(source, "function formatExactDuration(ms) {")
        + "\nprocess.stdout.write(JSON.stringify({"
        " hms: formatExactDuration(6954017),"
        " ms: formatExactDuration(45000),"
        " seconds: formatExactDuration(9000),"
        " zero: formatExactDuration(0),"
        " negative: formatExactDuration(-5),"
        "}));\n",
        encoding="utf-8",
    )
    output = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    ).stdout

    assert json.loads(output) == {
        "hms": "1h 55m 54s",  # the seconds the card rounds away
        "ms": "45s",
        "seconds": "9s",
        "zero": "0s",
        "negative": "0s",
    }