"""Contract for the exact-value hover readouts on the Overview KPI row.

Every card in the row carries the figure behind its rounded value in the same
hover readout -- "186M" reads out as "185,952,048 tokens", "97.6%" as "97.59%" --
so the precise number is available without widening six narrow cards. These tests
pin the parts that break silently: that all six cards stay wired, that the
readout survives the cursor crossing onto it, and that it borrows its colour from
the card value it belongs to.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

# The six cards in the Overview KPI row, as (value id, readout id).
KPI_READOUTS = (
    ("totalTokens", "totalTokensExact"),
    ("totalCost", "totalCostExact"),
    ("totalMessages", "totalMessagesExact"),
    ("overviewActiveTime", "overviewActiveTimeExact"),
    ("avgCacheHitRate", "avgCacheHitRateExact"),
    ("topModel", "topModelExact"),
)

# The Tailwind colour each card's value is painted in, which its readout mirrors.
KPI_TONES = ("#818cf8", "#34d399", "#c084fc", "#38bdf8", "#22d3ee", "#fcd34d")


def _extract_js_function(source: str, signature: str) -> str:
    start = source.find(signature)
    assert start != -1, f"{signature} not found in index.html"
    depth = 0
    body_start = start + len(signature) - 1
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


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


def test_readout_stays_up_when_the_cursor_crosses_onto_it() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = "".join(source.split())
    # The wrap is the hover target rather than the value element. The readout is a
    # child of that wrap, so a cursor that crosses onto it keeps the wrap hovered
    # and the readout on screen; keying off the value element is what used to pull
    # the readout away the instant the pointer arrived on it.
    assert ".overview-token-value-wrap:hover.overview-token-exact-tooltip" in compact
    assert ".overview-token-value-wrap:focus-within.overview-token-exact-tooltip" in compact
    # pointer-events: none would leave the readout transparent to the cursor, so the
    # hover could never reach it however the selector above were written.
    assert "pointer-events:auto" in _tooltip_rule(source)
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


def test_readout_is_larger_than_the_number_it_reports() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    rule = _tooltip_rule(source)
    # 13px against the 36px card value and the 10px readout this replaced.
    assert "font-size:13px" in rule


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_exact_value_formatters_report_the_unrounded_figure(tmp_path: Path) -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    harness = tmp_path / "kpi-readout.js"
    harness.write_text(
        "function langLocale() { return 'en-US'; }\n"
        + _extract_js_function(source, "function formatExactCurrency(num) {")
        + "\n"
        + _extract_js_function(source, "function formatExactDuration(ms) {")
        + "\nprocess.stdout.write(JSON.stringify({"
        " cost: formatExactCurrency(7.8421),"
        " bigCost: formatExactCurrency(1234.5),"
        " nullCost: formatExactCurrency(null),"
        " hms: formatExactDuration(6954017),"
        " ms: formatExactDuration(45000),"
        " seconds: formatExactDuration(9000),"
        " zero: formatExactDuration(0),"
        " negative: formatExactDuration(-5),"
        "}));\n",
        encoding="utf-8",
    )
    output = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, check=True
    ).stdout

    assert json.loads(output) == {
        "cost": "$7.8421",  # the fraction the cents-rounding card drops
        "bigCost": "$1,234.5000",  # grouped, like every other figure on the row
        "nullCost": "$0.0000",
        "hms": "1h 55m 54s",  # the seconds the card rounds away
        "ms": "45s",
        "seconds": "9s",
        "zero": "0s",
        "negative": "0s",
    }