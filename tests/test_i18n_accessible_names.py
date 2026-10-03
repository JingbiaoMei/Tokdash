"""Accessible-name and attribute-translation tests for the dashboard.

`applyI18n` translates four kinds of markup: text nodes (`data-i18n`) and
three attribute forms (`data-i18n-placeholder`, `data-i18n-title`,
`data-i18n-aria`). These tests pin the properties that are easy to break
silently, because none of them raise at runtime:

* every attribute selector in the page stylesheet is a single valid
  selector -- two attribute conditions in one ``[...]`` bracket make the
  whole rule invalid, and the browser drops it without a warning;
* no element that JavaScript already labels also carries a
  ``data-i18n-aria`` / ``data-i18n-title`` hook, so a language switch
  cannot leave a stale or divergent label behind;
* the inline English fallback of every translated attribute equals the
  ``en`` dictionary value, so the untranslated page and the translated one
  say the same thing;
* the accessible names the page used before the i18n work stay specific
  (a canvas is not called "Pricing" and a dialog close is not just
  "Close").
"""

from __future__ import annotations

import json
import re
import subprocess
import shutil
from html.parser import HTMLParser
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"
REPO_ROOT = Path(__file__).resolve().parents[1]

# HTML elements that never have a closing tag.
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# data-i18n-<suffix> -> the HTML attribute that suffix writes.
ATTRIBUTE_FOR_KEY = {
    "data-i18n-placeholder": "placeholder",
    "data-i18n-title": "title",
    "data-i18n-aria": "aria-label",
}

# Elements whose accessible name JavaScript owns. Adding a data-i18n-*
# hook to one of these creates a second source of truth for the same
# attribute; `showUsageRefreshReport` and `renderProfileActivityInsights`
# overwrite whatever the translation pass wrote, and a language switch
# while the panel is hidden would otherwise leave the old language up.
JS_SET_LABELS = {
    "refreshReportClose": ("showUsageRefreshReport", "aria-label", "refreshReportClose"),
    "refresh-report-deltas": (
        "showUsageRefreshReport",
        "aria-label",
        "refreshReportDiffLabel",
    ),
    "profileActivityToolRanking": (
        "renderProfileActivityInsights",
        "aria-label",
        "activityTopTools",
    ),
    # Same key on both sides, so the two writes never disagreed -- but it is
    # still a second source of truth for one attribute, and the JS side is the
    # one that runs on every re-render.
    "overviewActivityInsights": (
        "renderOverviewActivityInsights",
        "aria-label",
        "activityInsightsTitle",
    ),
}

# Accessible names the untranslated page shipped before. A translation
# pass must not make these vaguer: "Toggle theme" says less than "Toggle
# light or dark theme", and "Pricing" says nothing about a JSON editor.
# Each entry is (key now used, English it must render), so a test catches
# both a wrong key and a stale fallback.
PRESERVED_ACCESSIBLE_NAMES = {
    # The quick theme toggle reuses toggleLightDark, which already named
    # this button's tooltip, so its two labels stay in step.
    "themeToggleQuick": ("toggleLightDark", "Toggle Light / Dark mode"),
    "pricingDbEditor": ("pricingDbEditor", "pricing_db.json editor"),
    "closeDayDetails": ("closeDayDetails", "Close day details"),
    "sessionModalClose": ("closeDialog", "Close dialog"),
}


@pytest.fixture(scope="module")
def source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def static_html(source: str) -> str:
    """The markup that is not inside the app's main <script> block."""
    marker = source.find("function applyI18n()")
    assert marker != -1, "applyI18n() not found in index.html"
    start = source.rfind("<script>", 0, marker)
    assert start != -1, "enclosing <script> for applyI18n not found"
    return source[:start]


@pytest.fixture(scope="module")
def english_dictionary(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """The ``en`` block of the I18N literal, evaluated by node."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - node ships with the dev extra
        pytest.skip("node not available")
    source = INDEX_HTML.read_text(encoding="utf-8")
    literal = _i18n_literal(source)
    harness = tmp_path_factory.mktemp("i18n-en") / "en.js"
    harness.write_text(
        literal + "\nprocess.stdout.write(JSON.stringify(I18N.en));",
        encoding="utf-8",
    )
    output = subprocess.run(
        [node, str(harness)], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(output)


def _i18n_literal(source: str) -> str:
    start = source.find("const I18N = {")
    assert start != -1, "const I18N = { not found in index.html"
    end = source.find("\n    };", start)
    assert end != -1, "unterminated I18N object in index.html"
    return source[start : end + 6]


def _style_blocks(source: str) -> list[str]:
    return re.findall(r"<style[^>]*>(.*?)</style>", source, re.S)


class _ElementScanner(HTMLParser):
    """Collect start tags, optionally filtered to the i18n markers."""

    def __init__(self, markers: tuple[str, ...] = ()) -> None:
        super().__init__(convert_charrefs=True)
        self.markers = markers
        self.found: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = {name: value for name, value in attrs if value is not None}
        if not self.markers or any(name in element for name in self.markers):
            self.found.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _tagged(static_html: str) -> list[dict[str, str]]:
    """Every element carrying a data-i18n-* attribute hook."""
    parser = _ElementScanner(tuple(ATTRIBUTE_FOR_KEY))
    parser.feed(static_html)
    return parser.found


def _element_by_id(static_html: str, element_id: str) -> dict[str, str] | None:
    parser = _ElementScanner()
    parser.feed(static_html)
    for element in parser.found:
        if element.get("id") == element_id:
            return element
    return None


def _selectors(css: str) -> list[str]:
    """Split a rule's prelude into selectors, ignoring commas inside ()/[]/''."""
    selectors: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in css:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            selectors.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        selectors.append(tail)
    return [selector for selector in selectors if selector]


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.S)


# --- CSS selector validity -------------------------------------------------


def test_no_selector_packs_two_attributes_into_one_bracket(source: str) -> None:
    """A ``[a="1" b="2"]`` selector is invalid; the browser drops the rule.

    This is the exact shape that regressed the desktop tab row: the hide
    rule became ``nav[data-i18n-aria="..." aria-label="..."]``, so the
    whole ``display: none !important`` declaration was discarded and the
    row reappeared next to the sidebar at 768px and wider.
    """
    offenders: list[str] = []
    for block in _style_blocks(source):
        for rule in re.finditer(r"([^{}]+)\{[^{}]*\}", _strip_css_comments(block)):
            for selector in _selectors(rule.group(1)):
                for bracket in re.findall(r"\[([^\]]*)\]", selector):
                    conditions = re.findall(
                        r"[A-Za-z_][\w-]*\s*(?:[~^$*|]?=)?\s*(?:\"[^\"]*\"|'[^']*'|[^\s\]]+)?",
                        bracket,
                    )
                    if len(conditions) > 1:
                        offenders.append(selector.strip())
    assert not offenders, (
        "selectors with more than one attribute condition inside a single [] "
        f"bracket are invalid and the rule is dropped: {sorted(set(offenders))}"
    )


def test_attribute_selectors_contain_at_most_one_condition(
    source: str,
) -> None:
    """Every ``[...]`` holds exactly one attribute condition.

    Per the CSS syntax spec an attribute selector is
    ``[ ws* ident ws* [ op ws* [ ident | string ] ws* ]? ws* ]`` -- a single
    condition. A second ``name=value`` inside the same bracket makes the
    whole selector invalid, and the browser discards the entire rule
    without logging anything, which is how the desktop tab row came back.
    """
    for block in _style_blocks(source):
        for rule in re.finditer(r"([^{}]+)\{[^{}]*\}", _strip_css_comments(block)):
            for selector in _selectors(rule.group(1)):
                for bracket in re.findall(r"\[([^\]]*)\]", selector):
                    conditions = re.findall(
                        r"[A-Za-z_][\w-]*\s*(?:[~^$*|]?=)?\s*(?:\"[^\"]*\"|'[^']*'|[^\s\]]+)?",
                        bracket,
                    )
                    assert len(conditions) <= 1, (
                        f"{selector.strip()!r}: the bracket holds "
                        f"{len(conditions)} conditions {conditions!r}; only one is "
                        "valid, so the browser drops the rule"
                    )


def test_desktop_tab_row_rule_targets_a_stable_hook(source: str) -> None:
    """The tab row is hidden by id, not by a translated string."""
    block = re.search(r"@media \(min-width: 768px\) \{(.*?)\n    \}", source, re.S)
    assert block, "the min-width: 768px media block is gone"
    rules = re.findall(
        r"([^{}]+)\{([^{}]*)\}", _strip_css_comments(block.group(1))
    )
    hiding = [
        selector.strip()
        for selector, body in rules
        if "display: none !important" in body
    ]
    assert hiding, "the desktop tab row is no longer hidden at >=768px"
    assert all(selector.startswith("#") for selector in hiding), (
        "the hide rule must key on an element id; a translated attribute value "
        f"would stop matching in the other five languages: {hiding}"
    )
    for selector in hiding:
        element_id = selector[1:]
        assert f'id="{element_id}"' in source, (
            f"{selector} no longer matches anything; the id was renamed or removed"
        )


def test_desktop_tab_row_nav_is_not_hidden_by_its_translated_label(
    source: str,
) -> None:
    """No layout rule keys on a value the translation pass rewrites.

    Restoring ``nav[aria-label="Dashboard sections"]`` would look correct
    but silently un-hide the tab row in every language except English, so
    the rule has to key on something ``applyI18n`` never touches.
    """
    translated_values = re.findall(r"data-i18n-aria=\"([^\"]+)\"", source)
    assert translated_values, "no data-i18n-aria hooks found"
    for block in _style_blocks(source):
        for rule in re.finditer(r"([^{}]+)\{[^{}]*\}", _strip_css_comments(block)):
            for selector in _selectors(rule.group(1)):
                for key in translated_values:
                    assert f'aria-label="{key}"' not in selector, (
                        f"{selector.strip()!r} selects on a translated aria-label; "
                        "it only matches in the default language"
                    )


# --- single source of truth for JS-set labels ------------------------------


def test_js_set_labels_carry_no_translation_hook(static_html: str) -> None:
    """No data-i18n-aria hook may point at a key other than the JS one.

    ``showUsageRefreshReport`` and ``renderProfileActivityInsights``
    overwrite the attribute during their own render pass, so a hook on
    these elements is either dead (``mostUsedTools``) or a language
    switch away from the panel's language leaves the wrong string up
    (``close`` vs ``refreshReportClose``; in ja the deltas label flipped
    between two different translations).
    """
    parser = _ElementScanner()
    parser.feed(static_html)
    offenders = []
    for element in parser.found:
        identity = element.get("id") or (element.get("class") or "").split(" ")[0]
        if identity not in JS_SET_LABELS:
            continue
        hook = element.get("data-i18n-aria")
        if hook is None:
            continue
        _function, _attribute, key = JS_SET_LABELS[identity]
        offenders.append(
            f"{identity}: data-i18n-aria={hook!r} conflicts with the JS label "
            f"from {key!r}"
        )
    assert not offenders, (
        "these elements are labelled by JavaScript, so a data-i18n-aria hook "
        f"is a second source of truth: {offenders}"
    )


@pytest.mark.parametrize("element_id", sorted(JS_SET_LABELS))
def test_js_set_label_elements_have_no_attribute_translation_markers(
    element_id: str, static_html: str
) -> None:
    """Direct, per-element assertion of the #128 guidance."""
    _function, attribute, key = JS_SET_LABELS[element_id]
    parser = _ElementScanner()
    parser.feed(static_html)
    owners = [
        element
        for element in parser.found
        if element.get("id") == element_id
        or element.get("class", "").split(" ")[:1] == [element_id]
    ]
    assert owners, f"#{element_id} is no longer in the static markup"
    for element in owners:
        for marker, target in ATTRIBUTE_FOR_KEY.items():
            if target != attribute:
                continue
            assert marker not in element, (
                f"#{element_id} is labelled by {_function}() from {key!r}; the "
                f"{marker} hook makes the label language-dependent in a second way"
            )


def test_refresh_report_labels_come_from_the_javascript_pass(source: str) -> None:
    """The JS pass still writes both refresh-report labels."""
    assert "t('refreshReportClose')" in source
    assert "t('refreshReportDiffLabel')" in source
    assert "ranking.setAttribute('aria-label', t('activityTopTools'))" in source
    assert "shell.setAttribute('aria-label', t('activityInsightsTitle'))" in source


# --- inline fallbacks agree with the dictionary ---------------------------


def test_inline_attribute_fallbacks_match_the_english_dictionary(
    static_html: str, english_dictionary: dict[str, str]
) -> None:
    """Every `data-i18n-*` attribute ships the text the en dictionary holds.

    A fallback that disagrees with the dictionary means the label silently
    changes the first time the page runs applyI18n, and a translator
    reading the markup sees a string that is not in any locale.
    """
    mismatches = []
    for element in _tagged(static_html):
        for marker, attribute in ATTRIBUTE_FOR_KEY.items():
            if marker not in element:
                continue
            key = element[marker]
            assert key in english_dictionary, f"{marker}={key!r} is not in en"
            if attribute not in element:
                continue
            if element[attribute] != english_dictionary[key]:
                mismatches.append(
                    f"{element.get('id') or element.get('class') or '?'}: "
                    f"{attribute}={element[attribute]!r} != en[{key}]="
                    f"{english_dictionary[key]!r}"
                )
    assert not mismatches, (
        "inline English fallbacks must equal the en dictionary value: "
        f"{mismatches}"
    )


def test_inline_text_fallbacks_match_the_english_dictionary(
    static_html: str, english_dictionary: dict[str, str]
) -> None:
    """The same rule for `data-i18n` text, not just the three attributes.

    Checking only the attributes would have missed the sidebar's
    "What's New" against a dictionary that says "What's new": the text
    pass rewrites it on load, so the markup was quietly lying.
    """
    leaf_keys: list[tuple[str, str]] = []

    class Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.open: list[tuple[str, str, list[str]]] = []
            self.pending: tuple[str, str, list[str]] | None = None

        def handle_starttag(self, tag, attrs):  # type: ignore[no-untyped-def]
            element = {name: value for name, value in attrs if value is not None}
            entry = (tag, element.get("data-i18n", ""), [])
            if "data-i18n" in element and not any(
                name in element for name in ATTRIBUTE_FOR_KEY
            ):
                self.pending = entry
            else:
                self.open.append(entry)

        def handle_endtag(self, tag):  # type: ignore[no-untyped-def]
            if self.pending is not None:
                _tag, key, chunks = self.pending
                if key:
                    leaf_keys.append((key, "".join(chunks).strip()))
                self.pending = None
                return
            for index in range(len(self.open) - 1, -1, -1):
                if self.open[index][0] == tag:
                    del self.open[index:]
                    return

        def handle_data(self, data):  # type: ignore[no-untyped-def]
            if self.pending is not None:
                self.pending[2].append(data)

    collector = Collector()
    collector.feed(static_html)
    mismatches = [
        f"{key}: {text!r} != en[{key}]={english_dictionary.get(key)!r}"
        for key, text in leaf_keys
        if key in english_dictionary and text != english_dictionary[key]
    ]
    assert not mismatches, (
        f"inline English text must equal the en dictionary value: {mismatches}"
    )


# --- accessible names stay specific ---------------------------------------


@pytest.mark.parametrize("element_id", sorted(PRESERVED_ACCESSIBLE_NAMES))
def test_preserved_accessible_names_are_not_reduced(
    element_id: str, static_html: str, english_dictionary: dict[str, str]
) -> None:
    """The untranslated accessible names survive, and are translated.

    Each of these was narrowed by an earlier revision -- "Toggle theme"
    for "Toggle light or dark theme", "Pricing" for
    "pricing_db.json editor", "Close" for "Close day details" and
    "Close dialog" -- which is a regression for screen reader users even
    though every label is now translated.
    """
    key, expected = PRESERVED_ACCESSIBLE_NAMES[element_id]
    owner = _element_by_id(static_html, element_id)
    assert owner is not None, f"#{element_id} is no longer in the static markup"
    assert owner.get("data-i18n-aria") == key, (
        f"#{element_id} uses {owner.get('data-i18n-aria')!r}; expected {key!r}"
    )
    assert owner.get("aria-label") == expected, (
        f"#{element_id} aria-label is {owner.get('aria-label')!r}; expected "
        f"{expected!r}"
    )
    assert english_dictionary[key] == expected, (
        f"en[{key!r}] is {english_dictionary[key]!r}; the untranslated page said "
        f"{expected!r}"
    )


def test_poll_interval_select_is_named_for_the_control(
    static_html: str, english_dictionary: dict[str, str]
) -> None:
    """#quotaIntervalSelect announces "Poll every", not where it came from.

    ``quotaIntervalConfig`` describes the *source* of the value ("saved
    interval"), so using it as the control's accessible name told screen
    reader users nothing about what the select does.
    """
    select = _element_by_id(static_html, "quotaIntervalSelect")
    assert select is not None, "#quotaIntervalSelect is gone"
    assert select.get("data-i18n-aria") == "pollInterval"
    assert english_dictionary["pollInterval"] == "Poll every"
    assert select.get("aria-label") == "Poll every"


def test_session_modal_close_does_not_overwrite_its_icon(
    static_html: str, english_dictionary: dict[str, str]
) -> None:
    """#sessionModalClose keeps its glyph; only the name is translated.

    ``data-i18n`` writes textContent, so leaving it on an icon-only button
    replaced the X with the word "Close" on every language switch.
    """
    button = _element_by_id(static_html, "sessionModalClose")
    assert button is not None, "#sessionModalClose is gone"
    assert "data-i18n" not in button, (
        "the text pass would replace the close glyph with the translated word"
    )
    assert button.get("data-i18n-aria") == "closeDialog"
    assert button.get("data-i18n-title") == "closeDialog"
    assert english_dictionary["closeDialog"] == "Close dialog"


def test_token_composition_bars_share_one_wording(
    static_html: str, english_dictionary: dict[str, str]
) -> None:
    """The four composition bars reuse input/cacheRead/output/reasoning.

    ``promptTokens`` read "Prompt Tokens" while its three siblings used
    the short metric names, so one legend was inconsistent with the rest.
    """
    expected = {
        "compPromptBar": "input",
        "compCacheBar": "cacheRead",
        "compOutputBar": "output",
        "compReasoningBar": "reasoning",
    }
    for element_id, key in expected.items():
        bar = _element_by_id(static_html, element_id)
        assert bar is not None, f"#{element_id} is gone"
        assert bar.get("data-i18n-title") == key, (
            f"#{element_id} uses {bar.get('data-i18n-title')!r}, expected {key!r}"
        )
        assert bar.get("title") == english_dictionary[key]
    assert "promptTokens" not in english_dictionary, (
        "promptTokens is no longer referenced; drop it from all six locales"
    )


def test_brand_text_is_static(source: str, english_dictionary: dict[str, str]) -> None:
    """"Tokdash" is the product name, not a translatable string.

    ``appTitle`` and ``appName`` were "Tokdash" in all six locales, so the
    keys only implied the brand could be translated, and applyI18n wrote
    document.title on every language switch for no effect.
    """
    assert "document.title =" not in source, (
        "applyI18n must not write document.title; the brand is the same in "
        "every locale and <title> already carries it"
    )
    assert 'data-i18n="appName"' not in source
    assert 'data-i18n="appTitle"' not in source
    assert "<title>Tokdash</title>" in source
    assert "appTitle" not in english_dictionary
    assert "appName" not in english_dictionary


# --- JavaScript-written names must go through t() --------------------------


# Attributes whose value is the accessible name or the tooltip. A bare
# English literal assigned to one of these is invisible to every other
# check in this file -- there is no element in the static markup to
# inspect, so nothing fails when the six locales all keep saying English.
JS_WRITTEN_NAME_ATTRIBUTES = ("aria-label", "title", "placeholder")


def test_js_written_names_are_never_hardcoded_english(source: str) -> None:
    """No aria-label / title / placeholder may be set to a bare literal.

    ``createQuotaVisibilityControl`` named its provider menu with
    ``setAttribute('aria-label', 'Show providers')`` while every other
    name in the same function went through ``t()``, so that menu announced
    itself in English in all six locales.

    Only *literals* are rejected. The other writers legitimately pass a
    variable that already came from ``t()`` -- ``refreshBtn``'s ``label``,
    the server rows' ```${t('servers')}: ${host.label}``` -- and
    ``formatToolName`` returns brand proper nouns ("Claude Code", "Codex"),
    which no locale translates.
    """
    offenders: list[str] = []
    for number, line in enumerate(source.splitlines(), 1):
        for attribute in JS_WRITTEN_NAME_ATTRIBUTES:
            patterns = (
                rf"""setAttribute\(\s*['"]{attribute}['"]\s*,\s*(['"])(.*?)\1""",
                rf"""\.{attribute}\s*=\s*(['"])(.*?)\1""",
            )
            for pattern in patterns:
                for match in re.finditer(pattern, line):
                    literal = match.group(2)
                    if not re.search(r"[A-Za-z]", literal):
                        continue  # '', '—', '...' carry no words to translate
                    if "t(" in literal:
                        continue
                    offenders.append(
                        f"line {number}: {attribute} = {literal!r} never goes "
                        "through t()"
                    )
    assert not offenders, (
        "these names are written by JavaScript, so nothing in the static "
        f"markup catches them: {offenders}"
    )


def test_quota_provider_menu_name_comes_from_the_dictionary(
    source: str, english_dictionary: dict[str, str]
) -> None:
    """The provider-visibility menu is named in all six locales."""
    assert "setAttribute('aria-label', t('quotaVisibilityMenu'))" in source
    assert english_dictionary["quotaVisibilityMenu"] == "Show providers"
    # Sits with the Show: / Show: All / Show: None family it labels.
    for sibling in ("quotaVisibilityPrefix", "quotaVisibilityAll", "quotaVisibilityNone"):
        assert sibling in english_dictionary


# --- the translation mechanism itself -------------------------------------


def test_applyi18n_translates_every_attribute_form_through_one_table(
    source: str,
) -> None:
    """One loop over a marker -> attribute table, not three copies."""
    assert "I18N_ATTR_TARGETS" in source, (
        "applyI18n should drive the attribute passes from a single table"
    )
    for marker, attribute in ATTRIBUTE_FOR_KEY.items():
        assert f"'{marker}': '{attribute}'" in source, (
            f"{marker} is missing from I18N_ATTR_TARGETS"
        )
    body = source[
        source.index("function applyI18n()") : source.index("function applyI18n()") + 1200
    ]
    for marker in ATTRIBUTE_FOR_KEY:
        assert f"[{marker}]" not in body, (
            f"{marker} still has its own querySelectorAll loop; the table was "
            "bypassed"
        )
    assert "querySelectorAll(`[${marker}]`)" in body, (
        "the attribute passes must read the marker from I18N_ATTR_TARGETS"
    )
    assert "el.setAttribute(attribute, t(el.getAttribute(marker)))" in body


def test_static_markup_stays_well_formed(static_html: str) -> None:
    """The translated markup is still well-formed HTML.

    Editing a long single-page template by hand can quietly weld two
    elements onto one line, or drop a tag, without any test noticing --
    the page still parses, it just stops looking like the rest of the
    file and, in the worst case, nests one control inside another.
    """

    class Balance(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[tuple[str, int]] = []
            self.errors: list[str] = []

        def handle_starttag(self, tag, attrs):  # type: ignore[no-untyped-def]
            if tag.lower() not in VOID_ELEMENTS:
                self.stack.append((tag, self.getpos()[0]))

        def handle_endtag(self, tag):  # type: ignore[no-untyped-def]
            if tag.lower() in VOID_ELEMENTS:
                return
            if not self.stack:
                self.errors.append(f"stray </{tag}> at line {self.getpos()[0]}")
                return
            opened, line = self.stack.pop()
            if opened != tag:
                self.errors.append(
                    f"</{tag}> at line {self.getpos()[0]} closes <{opened}> "
                    f"opened at line {line}"
                )

    parser = Balance()
    # The slice ends inside <body>, so close the two ancestors it truncates;
    # anything still unbalanced after that is a real defect in the markup.
    parser.feed(static_html + "</body></html>")
    assert not parser.errors, parser.errors
    assert not parser.stack, [f"<{tag}> never closed (line {line})" for tag, line in parser.stack]


def test_no_line_welds_two_elements_together(static_html: str) -> None:
    """Each top-level element keeps its own line.

    A ``</p>`` immediately followed by ``<ol>`` on the same line means an
    edit consumed the newline between two siblings; the markup still
    parses, so nothing else would notice.
    """
    welded = [
        (number, line.strip()[:120])
        for number, line in enumerate(static_html.splitlines(), 1)
        if re.search(r"</[a-zA-Z][\w-]*>\s{2,}<", line)
    ]
    assert not welded, f"two elements share a line: {welded}"


def test_parity_test_covers_the_aria_form() -> None:
    """tests/test_i18n_languages.py must check data-i18n-aria keys too.

    Without ``-aria`` in that regex, a typo in any of the aria keys would
    pass CI and be announced as a raw key name. This runs the parity
    test's own regex against one element per marker form, so a narrowing
    of the pattern is caught here rather than in the dictionary.
    """
    parity = (REPO_ROOT / "tests" / "test_i18n_languages.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r"re\.findall\(\s*r'(data-i18n[^']*)'", parity)
    assert match, "the referenced-key regex in the parity test is gone"
    pattern = re.compile(match.group(1))
    samples = {
        "data-i18n": '<span data-i18n="close">Close</span>',
        **{
            marker: f'<div {marker}="someKey">'
            for marker in ATTRIBUTE_FOR_KEY
        },
    }
    for marker, markup in samples.items():
        found = pattern.findall(markup)
        assert found == ["someKey" if marker != "data-i18n" else "close"], (
            f"the parity regex {match.group(1)!r} does not extract the key "
            f"from {marker}: found {found}"
        )


# --- tooltips name the same thing as the heading above them ----------------


CHART_TOOLTIPS = {
    "turnTrendDesc": "turnTrend",
    "cumulativeTurnTrendDesc": "cumulativeTurnTrend",
    "cumulativeTrendByTimeDesc": "cumulativeTrendByTime",
}


def test_chart_tooltips_reuse_their_heading_terminology() -> None:
    """Every locale's tooltip opens with its own heading's wording.

    The three session-chart tooltips sit directly under headings. The ja
    "Turn trend" tooltip sat under "Turn Trend", the ko one said "Turn
    Trend" under "Turn Trend", and the es and pt tooltips kept the
    English heading verbatim. A tooltip that names the chart differently
    from the heading above it is a mismatch the reader has to resolve
    themselves, so each one must start with the heading's own term.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover - node ships with the dev extra
        pytest.skip("node not available")
    source = INDEX_HTML.read_text(encoding="utf-8")
    harness = (
        REPO_ROOT / "_i18n_tooltip_probe.js"
    )
    harness.write_text(
        _i18n_literal(source)
        + "\nconst pairs = "
        + json.dumps(CHART_TOOLTIPS)
        + ";\nconst out = [];\n"
        "for (const lang of Object.keys(I18N)) {\n"
        "  for (const [tip, heading] of Object.entries(pairs)) {\n"
        "    out.push([lang, tip, I18N[lang][tip], I18N[lang][heading],\n"
        "      I18N[lang][tip].startsWith(I18N[lang][heading])]);\n"
        "  }\n"
        "}\n"
        "process.stdout.write(JSON.stringify(out));",
        encoding="utf-8",
    )
    try:
        rows = json.loads(
            subprocess.run(
                [node, str(harness)], capture_output=True, text=True, check=True
            ).stdout
        )
    finally:
        harness.unlink(missing_ok=True)
    mismatches = [
        f"{lang} {tip}: {tip!r} starts {tooltip!r} instead of the heading "
        f"{heading!r}"
        for lang, tip, tooltip, heading, ok in rows
        if not ok
    ]
    assert not mismatches, (
        "a chart tooltip must name the chart the way the heading above it "
        f"does: {mismatches}"
    )
