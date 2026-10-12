"""Theme and translation contract for the session detail modal.

Clicking a session row in the Sessions explorer raises a modal with an
Overview & Graphs / View Chat switch, three token charts and the message
transcript. That markup used to carry a fixed slate-and-indigo palette, so on
the light themes the tab labels, graph headings and chat chips read as
dark-on-dark text, and none of those strings were in the six dictionaries, so
every locale showed English. Neither failure raises an error at runtime, so
both are pinned here:

* the modal paints with theme tokens only (no fixed palette utilities, no
  hardcoded chart ink), and its labels clear a readable contrast in both modes;
* every visible string in the modal, and every label the transcript renderer
  writes, has a key in all six language dictionaries.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

EXPECTED_LANGUAGES = ("en", "zh", "ja", "ko", "es", "pt")

MODAL_START = '<div id="sessionModal" class='
MODAL_END = "<!-- Slide-Over Session Inspector Drawer -->"

# The modal scrims are the one deliberate exception: a dark overlay behind a
# dialog is right in both modes, and both are spelled out here so a new fixed
# colour anywhere else in the modal still fails the test.
ALLOWED_FIXED_PALETTE = (
    "absolute inset-0 bg-slate-950/70 backdrop-blur-sm",
    "hidden fixed inset-0 z-[60] bg-slate-950/80 backdrop-blur-md "
    "flex items-center justify-center p-4 sm:p-8",
)

FIXED_PALETTE = re.compile(
    r"(?:bg|text|border|from|to)-(?:slate|gray|zinc|neutral|indigo|violet|purple|white)\b"
)

TRANSLATION_ATTRS = ("data-i18n", "data-i18n-title", "data-i18n-aria", "data-i18n-placeholder")

# Strings only the JavaScript writes: the transcript cards, the fullscreen
# chart's title and subtitles, and the turn axis label. A dictionary entry that
# goes missing here is invisible in the markup, so it is listed on purpose.
RENDERER_KEYS = (
    "turnNumber",
    "graphExpandTitle",
    "graphExpandSubPerTurn",
    "graphExpandSubCumulative",
    "graphExpandSubTimeline",
    "sessionChatMessages",
    "sessionChatToolCalls",
    "sessionChatEmpty",
    "sessionChatEmptyHint",
    "sessionChatRoleUser",
    "sessionChatRoleAssistant",
    "sessionChatRoleSystem",
    "sessionChatMessageIndex",
    "sessionChatChars",
    "sessionChatTokens",
    "sessionChatToolCallChip",
    "sessionChatNoContent",
    "sessionChatReasoning",
)


@pytest.fixture(scope="module")
def source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def modal_region(source: str) -> str:
    start = source.index(MODAL_START)
    return source[start : source.index(MODAL_END, start)]


def _function(name: str, source: str) -> str:
    """The named function, from its signature to its closing brace."""
    signature = "function " + name + "("
    start = source.index(signature)
    # Walk past the parameter list before counting braces: a default such as
    # `options = {}` closes the count early if the scan starts too soon.
    depth = 0
    cursor = start
    while True:
        char = source[cursor]
        cursor += 1
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
    depth = 0
    for index in range(source.index("{", cursor), len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError("unterminated function: " + name)


def _i18n_keys() -> dict[str, list[str]]:
    """Every dictionary's key list, read by evaluating the literal in node."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    start = source.index("const I18N = {")
    end = source.index("\n    };", start)
    # `end + 6` keeps the object's closing brace but drops its semicolon.
    # The literal is too big for an -e argument, so it goes through a file.
    with tempfile.TemporaryDirectory() as scratch:
        program = Path(scratch) / "i18n_keys.js"
        program.write_text(
            source[start : end + 6]
            + ";\nconsole.log(JSON.stringify(Object.fromEntries("
            + "Object.keys(I18N).map((lang) => [lang, Object.keys(I18N[lang])]))));\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["node", str(program)], capture_output=True, text=True, check=True
        )
    return json.loads(result.stdout)


# --- no fixed palette in the modal ------------------------------------------


def test_modal_markup_has_no_fixed_palette_classes(modal_region: str) -> None:
    offenders = []
    for classes in re.findall(r'class="([^"]+)"', modal_region):
        if FIXED_PALETTE.search(classes) and classes.strip() not in ALLOWED_FIXED_PALETTE:
            offenders.append(classes.strip())
    assert not offenders, (
        "the session modal must take its colours from the theme tokens "
        "(--color-text/-muted/-border/-primary/-messages); these fixed palette "
        f"utilities left it unreadable on light themes: {offenders}"
    )


def test_view_switch_selected_state_is_state_driven(modal_region: str, source: str) -> None:
    """aria-pressed styles the switch, so each theme paints its own pill."""
    for element_id in ("sessionModalAnalyticsBtn", "sessionModalChatToggleBtn"):
        button = re.search(r'<button id="' + element_id + r'"[^>]*>', modal_region)
        assert button, element_id + " is gone"
        tag = button.group(0)
        assert "session-view-btn" in tag, element_id + " lost its themed class"
        assert 'aria-pressed="' in tag, element_id + " lost its selected state"
    switcher = _function("setSessionModalView", source)
    assert "setAttribute('aria-pressed'" in switcher
    assert ".className =" not in switcher, (
        "rewriting className re-hardcodes the selected pill outside the theme"
    )


def test_every_modal_class_has_a_stylesheet_rule(modal_region: str, source: str) -> None:
    """Every session-modal class used by the markup has a rule to match."""
    style = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    used = set()
    for classes in re.findall(r'class="([^"]+)"', modal_region):
        used |= {c for c in classes.split() if c.startswith("session-")}
    missing = sorted(c for c in used if "." + c not in style)
    assert not missing, f"no stylesheet rule for {missing}"


# --- chart ink follows the theme --------------------------------------------


def test_session_chart_ink_reads_the_theme_tokens(source: str) -> None:
    ink = _function("sessionChartInk", source)
    assert "getCssVar('--color-muted')" in ink
    assert "getCssVar('--color-text')" in ink
    assert "isDark()" in ink
    options = _function("buildTurnChartOptions", source)
    assert "sessionChartInk()" in options, "the chart options ignore the theme ink"
    assert "#475569" not in options, "hardcoded tick ink returns on dark themes"
    assert "rgba(226,232,240" not in options, "near-white gridlines vanish on light surfaces"
    assert "labels: { color: ink.legend }" in options
    # Without a colour Chart.js draws the axis title in its own #666, which sits
    # under 3.5:1 on every dark theme's card.
    assert "text: xAxisTitle, color: ink.tick" in options, "the axis title ignores the theme ink"


def test_turn_series_colors_split_by_mode(source: str) -> None:
    colors = _function("turnSeriesColors", source)
    assert "isDark()" in colors, "one series palette cannot serve both modes"
    datasets = _function("makeTurnDatasets", source)
    assert "turnSeriesColors()" in datasets
    for hex_color in ("#1E40AF", "#0F766E", "#F59E0B", "#7C3AED", "#111827"):
        assert hex_color not in datasets, (
            f"{hex_color} is the light-only series colour; read it from "
            "turnSeriesColors() instead"
        )
    assert "barColors." in _function("expandSessionChart", source)


def test_an_open_modal_follows_a_language_or_theme_switch(source: str) -> None:
    """Both repaint paths rebuild the modal; a stale one keeps the old colours."""
    # A language switch reaches the modal through applyI18n's token pass.
    assert "rerenderTokenPresentation();" in _function("applyI18n", source)
    i18n_pass = _function("rerenderTokenPresentation", source)
    theme_pass = _function("rerenderPresentation", source)
    for name, block in (("rerenderTokenPresentation", i18n_pass), ("rerenderPresentation", theme_pass)):
        assert "renderSessionCharts(" in block, name + " no longer repaints the modal charts"
        assert "expandSessionChart(graphExpandChartType)" in block, name + " leaves the expanded chart stale"
    assert "renderSessionChat(sessionChatPayload.messages" in i18n_pass


# --- every modal string goes through a dictionary ---------------------------


def test_modal_markup_translates_every_hook_key(modal_region: str, _dictionary_keys) -> None:
    keys = set()
    for attribute in TRANSLATION_ATTRS:
        keys |= set(re.findall(attribute + r'="([^"]+)"', modal_region))
    assert keys, "the modal lost every translation hook"
    known = set(_dictionary_keys["en"])
    unknown = sorted(k for k in keys if k not in known)
    assert not unknown, f"data-i18n hooks with no dictionary entry: {unknown}"


def test_expanded_chart_wording_is_translated(source: str) -> None:
    expand = _function("expandSessionChart", source)
    assert "t('graphExpandTitle'" in expand
    assert "(Full Screen)`" not in expand, "the full-screen suffix is hardcoded again"
    assert "t('turnNumber', { n:" in expand, "the turn axis labels are hardcoded English"
    assert "t('sessionSpeedColTurn')" in expand, "the turn axis title is hardcoded English"
    literals = re.findall(r"subEl\.textContent = (['\"])(.*?)\1", expand)
    assert not literals, f"expanded-chart subtitles bypass t(): {literals}"


def test_turn_chart_axis_titles_come_from_the_dictionary(source: str) -> None:
    """Axis titles are drawn on the canvas, so a literal there stays English
    however the rest of the dashboard is translated."""
    sites = [
        match.group(1)
        for match in re.finditer(r"(?<!function )buildTurnChartOptions\(\s*([^,)\n]+)", source)
    ]
    assert len(sites) >= 4, f"turn chart lost its axis-title call sites: {sites}"
    literal = [site for site in sites if not site.startswith("t(")]
    assert not literal, f"axis titles bypass t(): {literal}"


def test_chat_renderer_writes_no_hardcoded_labels(source: str) -> None:
    chat = _function("renderSessionChat", source)
    literals = re.findall(r"textContent = (['\"])(.*?)\1", chat)
    assert not literals, f"chat labels bypass t(): {literals}"
    for key in RENDERER_KEYS:
        where = chat if key.startswith("sessionChat") else _function("expandSessionChart", source)
        if key in ROLE_KEYS:
            # The renderer picks one of the three role names, then translates
            # the choice, so the key reaches t() through a variable.
            assert "'" + key + "'" in where, key + " is no longer a role label"
            assert "t(roleKey)" in where, "the role label stopped going through t()"
            continue
        # `t('key'` only: several of these take placeholders, so the closing
        # paren is not part of the call.
        assert "t('" + key + "'" in where, key + " no longer goes through t()"


ROLE_KEYS = ("sessionChatRoleUser", "sessionChatRoleAssistant", "sessionChatRoleSystem")


@pytest.fixture(scope="module")
def _dictionary_keys() -> dict[str, list[str]]:
    if shutil.which("node") is None:
        pytest.skip("node not available")
    return _i18n_keys()


def test_modal_keys_exist_in_all_six_languages(_dictionary_keys) -> None:
    assert tuple(_dictionary_keys) == EXPECTED_LANGUAGES, "dictionary languages drifted"
    for lang in EXPECTED_LANGUAGES:
        missing = sorted(set(RENDERER_KEYS) - set(_dictionary_keys[lang]))
        assert not missing, f"{lang} dictionary is missing {missing}"


# --- the labels are actually readable in both modes -------------------------

# renderSessionChat() builds the transcript cards; the markup below reproduces
# them statically so the stylesheet can be measured without booting the
# dashboard. test_chat_fixture_classes_match_the_renderer keeps it honest.
CHAT_FIXTURE = """
<div class="session-chat-summary flex items-center justify-between gap-2 p-3.5 rounded-xl">
  <span id="sessionChatMsgCount" class="session-chat-chip text-xs font-mono font-bold px-2 py-0.5 rounded">3 messages</span>
</div>
<div class="session-chat-card session-chat-card--user p-4 rounded-2xl shadow-md">
  <div class="session-chat-card-head flex items-center justify-between mb-3 pb-2">
    <span class="session-chat-role px-2.5 py-0.5 rounded-full text-[11px] font-mono font-bold uppercase tracking-wider border" data-i18n-dynamic>User #1</span>
    <span class="session-chat-time text-[11px] font-mono">2026-10-11 09:14:22</span>
  </div>
  <div class="session-chat-meta flex items-center gap-2 text-xs">
    <span class="session-chat-meta-chip text-[10px] px-2 py-0.5 rounded font-mono" data-i18n-dynamic>1,840 chars</span>
  </div>
</div>
<div class="session-chat-card session-chat-card--assistant p-4 rounded-2xl shadow-md">
  <div class="session-chat-reasoning rounded-xl px-3.5 py-2.5 text-xs font-semibold flex items-center gap-2">Reasoning recorded</div>
</div>
<div class="session-chat-empty p-8 text-center rounded-2xl">No conversational transcript stored for this session.</div>
"""

CONTRAST_SELECTORS = {
    "idle tab label": "#sessionModalChatToggleBtn span",
    "selected tab label": "#sessionModalAnalyticsBtn span",
    "modal title": "#sessionModalTitle",
    "message count chip": "#sessionChatMsgCount",
    "chat view note": "#sessionModalChatView .session-chat-note",
    "role badge": ".session-chat-role",
    "meta chip": ".session-chat-meta-chip",
    "message timestamp": ".session-chat-time",
    "empty transcript note": ".session-chat-empty",
    "chart heading": ".session-graph-title",
    "chart subtitle": ".session-modal-sub",
}

# Foreground colour plus the background composited over every translucent
# ancestor, so the caller can take a WCAG ratio.
PROBE_JS = """
(selector) => {
  const scratch = document.createElement('canvas').getContext('2d');
  const parse = (value, depth) => {
    let m = value.match(/rgba?\\(([^)]+)\\)/);
    if (m) {
      const p = m[1].split(/[\\s,/]+/).filter(Boolean).map(Number);
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    }
    // A computed color-mix() arrives as color(srgb r g b / a), channels on 0..1.
    m = value.match(/^color\\(\\s*\\S+\\s+([^)]+)\\)/);
    if (m) {
      const p = m[1].split(/[\\s,/]+/).filter(Boolean).map(Number);
      return { r: p[0] * 255, g: p[1] * 255, b: p[2] * 255, a: p.length > 3 ? p[3] : 1 };
    }
    m = value.match(/^#([0-9a-f]{6})$/i);
    if (m) {
      const n = parseInt(m[1], 16);
      return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 };
    }
    if (depth) return null;
    // Wide-gamut forms: let the 2D canvas resolve them down to sRGB.
    scratch.fillStyle = '#000';
    scratch.fillStyle = value;
    const flat = String(scratch.fillStyle);
    return flat === value ? null : parse(flat, 1);
  };
  const over = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a),
    a: 1,
  });
  const el = document.querySelector(selector);
  if (!el) return null;
  const layers = [];
  for (let node = el; node; node = node.parentElement) {
    const c = parse(getComputedStyle(node).backgroundColor);
    if (c && c.a > 0) {
      layers.push(c);
      if (c.a >= 1) break;
    }
    if (!node.parentElement) break;
  }
  const root = parse(getComputedStyle(document.documentElement).backgroundColor);
  let bg = root && root.a >= 1 ? root : { r: 255, g: 255, b: 255, a: 1 };
  for (const layer of layers.reverse()) bg = over(layer, bg);
  return { fg: parse(getComputedStyle(el).color), bg };
}
"""


def _channel(value: float) -> float:
    scaled = value / 255
    return scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4


def _contrast(fg: dict, bg: dict) -> float:
    def luminance(color):
        return (
            0.2126 * _channel(color["r"])
            + 0.7152 * _channel(color["g"])
            + 0.0722 * _channel(color["b"])
        )

    light = max(luminance(fg), luminance(bg))
    dark = min(luminance(fg), luminance(bg))
    return (light + 0.05) / (dark + 0.05)


@pytest.fixture(scope="module")
def chromium():
    """One headless Chromium for the module's measured pages.

    Playwright and Chromium stay optional developer tools, so the default suite
    runs without adding a browser dependency to the package. The fixture is
    module-scoped because the suite redirects HOME per test, and the browser
    lives under ~/.cache; it is shared because a second sync Playwright cannot
    start while the first is running.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=os.environ.get("TOKDASH_TEST_CHROMIUM"),
                args=["--disable-gpu", "--disable-features=Vulkan,VizDisplayCompositor"],
            )
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Playwright Chromium is not installed")
            raise
        try:
            yield browser
        finally:
            browser.close()


def _measured_markup(modal_region: str) -> str:
    return (
        modal_region.replace(MODAL_END, "")
        # Show the modal and both views: the measurement is closer to the
        # rendered page when nothing sits at display:none.
        .replace('class="hidden fixed inset-0 z-50"', 'class="fixed inset-0 z-50"')
        .replace('style="display:none;"', "")
    )


@pytest.fixture(scope="module")
def modal_pages(source: str, modal_region: str, chromium):
    """One page per mode, holding the real stylesheet and the real modal markup.

    Each page is built already in its mode. Toggling the class on a page that
    has been measured once answers with the previous mode's colours, because a
    style change is only re-resolved for elements that get laid out.
    """
    style = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    markup = _measured_markup(modal_region)
    pages = {}
    for mode in ("light", "dark"):
        page = chromium.new_page(viewport={"width": 1600, "height": 1000})
        page.set_content(
            '<!doctype html><html class="' + ("dark" if mode == "dark" else "") + '">'
            "<head><style>" + style + "</style></head><body>"
            + markup + CHAT_FIXTURE + "</body></html>"
        )
        page.wait_for_timeout(100)
        pages[mode] = page
    yield pages
    for page in pages.values():
        page.close()


def test_chat_fixture_classes_match_the_renderer(source: str, modal_region: str) -> None:
    """The measured markup cannot drift away from the modal and its renderer."""
    owned = _function("renderSessionChat", source) + modal_region
    used = {
        name
        for group in re.findall(r'class="([^"]+)"', CHAT_FIXTURE)
        for name in group.split()
        if name.startswith("session-chat")
    }
    missing = sorted(name for name in used if name not in owned)
    assert not missing, f"neither the markup nor renderSessionChat emits {missing}"


@pytest.mark.parametrize("mode", ("light", "dark"))
def test_modal_labels_clear_aa_contrast(modal_pages, mode: str) -> None:
    """Both modes keep every modal label above the WCAG AA small-text ratio."""
    page = modal_pages[mode]
    thin = []
    for label, selector in CONTRAST_SELECTORS.items():
        colors = page.evaluate(PROBE_JS, selector)
        assert colors and colors["fg"], f"{label} not found in the modal markup"
        ratio = _contrast(colors["fg"], colors["bg"])
        if ratio < 4.5:
            thin.append(f"{label} = {ratio:.2f}:1")
    assert not thin, f"{mode} mode leaves these modal labels under the 4.5:1 AA ratio: {thin}"


# --- the #217 follow-ups -----------------------------------------------------


def test_session_speed_chart_reads_the_theme_ink(source: str) -> None:
    """The speed chart drew Chart.js's own #666 ticks and black-tinted grid on
    every theme, which all but vanish on a dark card."""
    speed = _function("renderSessionSpeedTimeline", source)
    assert "const ink = sessionChartInk();" in speed
    assert speed.count("color: ink.tick") >= 4, "an axis title or tick list ignores the theme ink"
    assert speed.count("grid: { color: ink.grid }") == 2, "a gridline ignores the theme ink"
    assert "labels: { color: ink.legend }" in speed, "the stream legend ignores the theme ink"


def test_an_open_modal_repaints_its_speed_chart_on_either_switch(source: str) -> None:
    """The expanded speed chart clones the small one's options, so the small
    one must repaint first or the expanded copy keeps the old ink."""
    for name in ("rerenderPresentation", "rerenderTokenPresentation"):
        block = _function(name, source)
        repaint = "renderSessionSpeedTimeline(sessionSpeedModalState.payload)"
        assert repaint in block, name + " leaves the speed chart in the old theme"
        assert block.index(repaint) < block.index("expandSessionChart(graphExpandChartType)"), (
            name + " expands the speed chart before repainting it"
        )


def test_session_load_failure_is_translated(source: str, _dictionary_keys) -> None:
    opener = _function("openSessionModal", source)
    assert '"Failed to load session detail"' not in opener, "the failure line is hardcoded English"
    assert 't("sessionDetailLoadFailed")' in opener
    # A language switch with the failure showing goes through applyI18n's
    # data-i18n pass; the next open must take that hook off, or the pass would
    # overwrite the next session's meta line with the failure text.
    assert 'setAttribute("data-i18n", "sessionDetailLoadFailed")' in opener
    assert opener.index('removeAttribute("data-i18n")') < opener.index("fetchFromHost(")
    for lang in EXPECTED_LANGUAGES:
        assert "sessionDetailLoadFailed" in _dictionary_keys[lang], lang


def test_an_open_modal_retranslates_its_meta_line_but_not_a_failure(source: str) -> None:
    """The meta line is written from t(), so a language switch must write it
    again; the failure line has its own data-i18n hook and must survive."""
    helper = _function("renderSessionModalMeta", source)
    assert 'hasAttribute("data-i18n")' in helper, "a rewrite would replace the failure line"
    assert 't("events")' in helper and 't("activeTime")' in helper
    assert "renderSessionModalMeta(activeSessionDetail.tool, session)" in _function("rerenderTokenPresentation", source)
    opener = _function("openSessionModal", source)
    assert "renderSessionModalMeta(tool, session)" in opener
    assert 't("events")' not in opener, "the meta line is built in one place"


def test_a_new_open_clears_the_last_sessions_bars_and_charts(source: str) -> None:
    opener = _function("openSessionModal", source)
    reset = opener.index("renderSessionCharts([])")
    assert reset < opener.index("fetchFromHost("), "the old charts stay up while the next session loads"
    assert '"sessionShareInput", "sessionShareCache", "sessionShareOutput", "sessionShareReasoning"' in opener
    assert 'bar.style.width = "0%"' in opener


def test_script_written_modal_labels_are_marked_for_the_cjk_rules(source: str, modal_region: str) -> None:
    """#215's CJK rules key off data-i18n, which these labels cannot carry."""
    for element_id in ("graphExpandTitle", "sessionChatMsgCount", "sessionChatToolCount", "sessionSpeedCoverage"):
        tag = re.search(r'<[a-z0-9]+ [^>]*id="' + element_id + r'"[^>]*>', modal_region)
        assert tag and "data-i18n-dynamic" in tag.group(0), element_id + " misses the CJK rules"
    chat = _function("renderSessionChat", source)
    role = re.search(r'<span class="session-chat-role[^"]*"[^>]*>', chat).group(0)
    assert "data-i18n-dynamic" in role, "the role badge misses the CJK rules"
    assert '"session-chat-meta-chip text-[10px] px-2 py-0.5 rounded font-mono" data-i18n-dynamic>${escapeHtml(c)}' in chat
    # The model chip shows a model id, not a translation: it keeps the mono face.
    assert "font-mono\">${escapeHtml(m.model)}" in chat
    for lang in ("zh-CN", "ja", "ko"):
        assert f"html:lang({lang}) [data-i18n-dynamic]:is(.mono, .font-mono)" in source, lang
        assert f"html:lang({lang}) [data-i18n-dynamic]," in source or f"html:lang({lang}) [data-i18n-dynamic] {{" in source, lang


def test_share_bars_use_the_series_colours_of_their_mode(source: str, modal_region: str) -> None:
    """Inline light hexes kept the bars light on dark themes while the charts
    beside them switched to the brighter set."""
    for name in ("Input", "Cache", "Output", "Reasoning"):
        bar = re.search(r'<div id="sessionShare' + name + r'Bar"[^>]*>', modal_region).group(0)
        assert "background" not in bar, name + " bar hardcodes its colour inline"
        assert "session-share-fill--" + name.lower() in bar
    # turnSeriesColors() returns `isDark() ? { dark set } : { light set }`.
    dark, light = re.findall(r"\{ (input: [^}]+)\}", _function("turnSeriesColors", source))
    style = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    for mode, palette, prefix in (("light", light, "    ."), ("dark", dark, "    html.dark .")):
        for series in ("input", "cache", "output", "reasoning"):
            hex_color = re.search(series + r': "(#[0-9A-F]{6})"', palette).group(1)
            rule = prefix + "session-share-fill--" + series + " { background: " + hex_color + "; }"
            assert rule in style, f"{mode} {series} bar does not match turnSeriesColors ({hex_color})"


# Tailwind's play build injects its utilities after this stylesheet, so at equal
# specificity a utility wins. These stand-ins load in the same order, carrying
# the values the real utilities set.
TAILWIND_AFTER = (
    ".text-xs{font-size:.75rem;line-height:1rem}"
    ".font-mono{font-family:ui-monospace,SFMono-Regular,monospace}"
    ".font-bold{font-weight:700}.font-extrabold{font-weight:800}"
    ".tracking-wider{letter-spacing:.05em}.uppercase{text-transform:uppercase}"
)

FOLLOWUP_PROBE_JS = """
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const style = getComputedStyle(el);
  return { family: style.fontFamily, weight: style.fontWeight, spacing: style.letterSpacing,
           lineHeight: style.lineHeight, fontSize: style.fontSize, background: style.backgroundColor };
}
"""


@pytest.fixture(scope="module")
def followup_pages(source: str, modal_region: str, chromium):
    """Pages per language and mode with the real stylesheet, the modal markup,
    the transcript fixture, and Tailwind-order utilities after them."""
    style = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    markup = _measured_markup(modal_region)
    pages = {}
    for lang, mode in (("en", "light"), ("zh-CN", "light"), ("ja", "light"), ("ko", "light"), ("en", "dark")):
        page = chromium.new_page(viewport={"width": 1600, "height": 1000})
        page.set_content(
            f'<!doctype html><html lang="{lang}" class="{"dark" if mode == "dark" else ""}">'
            "<head><style>" + style + "</style><style>" + TAILWIND_AFTER + "</style></head><body>"
            + markup + CHAT_FIXTURE + "</body></html>"
        )
        page.wait_for_timeout(100)
        pages[(lang, mode)] = page
    yield pages
    for page in pages.values():
        page.close()


def _probe(page, selector: str) -> dict:
    found = page.evaluate(FOLLOWUP_PROBE_JS, selector)
    assert found, selector + " not found"
    return found


@pytest.mark.parametrize("lang", ("zh-CN", "ja", "ko"))
def test_script_written_labels_take_the_cjk_rules(followup_pages, lang: str) -> None:
    page = followup_pages[(lang, "light")]
    title = _probe(page, "#graphExpandTitle")
    assert title["family"].startswith('"Fira Sans"'), title["family"]
    assert title["weight"] == "700", "an 800 heading is past the heaviest CJK weight"
    role = _probe(page, ".session-chat-role")
    assert role["spacing"] == "normal", "tracking splits Han characters apart"
    assert role["family"].startswith('"Fira Sans"'), role["family"]
    chip = _probe(page, ".session-chat-meta-chip")
    assert chip["weight"] == "400", "a regular chip must not turn bold"
    count = _probe(page, "#sessionChatMsgCount")
    assert count["family"].startswith('"Fira Sans"'), count["family"]


def test_latin_labels_keep_their_mono_face_and_tracking(followup_pages) -> None:
    page = followup_pages[("en", "light")]
    title = _probe(page, "#graphExpandTitle")
    assert title["family"].startswith('"Fira Code"'), title["family"]
    assert title["weight"] == "800"
    role = _probe(page, ".session-chat-role")
    assert role["spacing"] != "normal", "English keeps the badge's tracking"


def test_share_bars_switch_to_the_bright_set_in_dark_mode(followup_pages) -> None:
    light = _probe(followup_pages[("en", "light")], "#sessionShareInputBar")["background"]
    dark = _probe(followup_pages[("en", "dark")], "#sessionShareInputBar")["background"]
    assert light == "rgb(30, 64, 175)", light
    assert dark == "rgb(96, 165, 250)", dark


def test_empty_transcript_note_gets_its_own_leading(followup_pages, source: str) -> None:
    empty = re.search(r'<div class="(session-chat-empty[^"]*)"', _function("renderSessionChat", source)).group(1)
    assert "text-xs" not in empty.split(), "a text-xs utility brings back its 1rem leading"
    note = _probe(followup_pages[("en", "light")], ".session-chat-empty")
    assert note["fontSize"] == "12px"
    assert note["lineHeight"] == "20.4px", "a utility's 1rem leading overrides the note's 1.7"
