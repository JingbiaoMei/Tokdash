"""The Overview KPI row must contain no invented figures in its markup.

Reported in discussion #169 and filed as #175: the *EST. agent time* and *Avg
Cache Hit Rate* cards each shipped a corner pill whose text was a hardcoded
`-61%` and `92%`. Neither element had an `id` or a `data-*` hook, so nothing
could ever update it -- every user, range and locale saw the same two invented
numbers, sitting close enough to the real values below them to read as derived.

String assertions on the page could not see that, because the strings looked
like normal content. This asserts the structural invariant instead: no
data-bearing card on that row may carry a number in its static markup. A card
value is either empty markup filled by a renderer or a placeholder (`-`, `$-`,
`—`), never a literal measurement.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

# Values the renderers own. If one of these disappears the invariant below is
# being satisfied by deleting a card rather than by wiring it up.
CARD_VALUE_IDS = (
    "totalTokens",
    "totalCost",
    "totalMessages",
    "overviewActiveTime",
    "avgCacheHitRate",
    "topModel",
)

# A digit or a percent sign in card markup is a measurement someone typed.
LITERAL_FIGURE = re.compile(r"[0-9]|%")


class _VisibleText(HTMLParser):
    """Collect text nodes from the KPI grid, skipping what a renderer draws.

    Attributes are ignored on purpose: `viewBox`, `stroke-width` and `title`
    prose carry numbers that belong to the drawing and the tooltip, not to a
    figure the user reads as data.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[tuple[str, int, int]] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in {"svg", "script", "style", "defs"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"svg", "script", "style", "defs"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        self.text.append((data.strip(), *self.getpos()))


def _kpi_grid_markup() -> str:
    source = INDEX_HTML.read_text(encoding="utf-8")
    marker = source.find('id="overviewKpiGrid"')
    assert marker != -1, "overviewKpiGrid section not found in index.html"
    # Slice from after the opening tag, so the parser sees card content rather
    # than the tail of the section element's own attribute list.
    start = source.index(">", marker) + 1
    end = source.find("</section>", start)
    assert end != -1, "overviewKpiGrid section is never closed"
    return source[start:end]


def test_kpi_markup_holds_no_handwritten_figures():
    parser = _VisibleText()
    parser.feed(_kpi_grid_markup())
    offenders = [
        f"line {line}:{column} {text!r}"
        for text, line, column in parser.text
        if LITERAL_FIGURE.search(text)
    ]
    assert not offenders, (
        "Static markup inside #overviewKpiGrid carries a handwritten figure. A "
        "number that no renderer writes is a fabricated metric -- give the "
        "element an id and fill it from the payload, or drop it. Offenders: "
        + "; ".join(offenders)
    )


@pytest.mark.parametrize("value_id", CARD_VALUE_IDS)
def test_card_values_are_still_rendered_targets(value_id: str):
    assert f'id="{value_id}"' in _kpi_grid_markup(), (
        f"{value_id} left #overviewKpiGrid; the no-literals rule is satisfied "
        "only while the card still renders its real value"
    )
