from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import tokdash

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"
PANELS = ("codex", "claude", "opencode", "pi_agent", "mimo", "kimi", "dsh", "reasonix", "zcode", "kilocode", "omp", "grok", "hermes", "antigravity_cli", "cline", "workbuddy", "qoder", "qwen_code", "openclaw", "qoder_cli", "combined")


def _extract_js_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start != -1, f"{signature} not found in index.html"
    depth = 0
    for j in range(start + len(signature) - 1, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError(f"unterminated JS function: {signature}")


def _run(fn_src: str, args: list) -> object:
    import json as _json
    program = (
        f"{fn_src}\n"
        "const out = sessionPanelShouldOpen("
        + ", ".join(_json.dumps(a) for a in args)
        + ");\nconsole.log(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return __import__("json").loads(result.stdout.strip())


@pytest.fixture(scope="module")
def fn_src() -> str:
    source = INDEX_HTML.read_text(encoding="utf-8")
    return _extract_js_function(source, "function sessionPanelShouldOpen(override, data) {")


def test_default_is_collapsed(fn_src):
    # Howard, 2026-08-24: every section starts collapsed, even with data.
    assert _run(fn_src, [None, {"sessions": [{"session_id": "1"}], "error": None}]) is False
    assert _run(fn_src, [None, {"sessions": [], "error": None}]) is False
    assert _run(fn_src, [None, None]) is False


def test_fetch_error_without_sessions_stays_open(fn_src):
    # Failures must stay visible: an errored panel with no sessions defaults open.
    assert _run(fn_src, [None, {"sessions": [], "error": "boom"}]) is True


def test_error_with_sessions_is_collapsed(fn_src):
    # Stale-but-present data is the normal degraded path; collapse it like data.
    assert _run(fn_src, [None, {"sessions": [{"session_id": "1"}], "error": "boom"}]) is False


def test_user_override_wins(fn_src):
    # True = user collapsed; false = user expanded. Either beats the default.
    assert _run(fn_src, [True, {"sessions": [{"session_id": "1"}]}]) is False
    assert _run(fn_src, [False, {"sessions": [{"session_id": "1"}]}]) is True
    assert _run(fn_src, [True, {"sessions": []}]) is False
    assert _run(fn_src, [False, {"sessions": []}]) is True


def test_every_panel_has_collapsible_markup():
    source = INDEX_HTML.read_text(encoding="utf-8")
    for panel in PANELS:
        assert f'data-panel-details="{panel}"' in source, panel
        assert f'id="{panel}PanelCount"' in source, panel
        assert f'id="{panel}PanelKpis"' in source, panel
        assert f'id="{panel}PanelTokens"' in source, panel
        assert f'id="{panel}PanelCost"' in source, panel
        assert f'id="{panel}PanelLast"' in source, panel
    # Panels start collapsed: no static `open` attribute on any of them.
    for m in __import__("re").finditer(r"<details class=\"session-panel\" data-panel-details=\"\w+\">", source):
        assert "open" not in source[m.end() : m.end() + 40]
    assert source.count('id="sessionsExpandAll"') == 1
    assert source.count('id="sessionsCollapseAll"') == 1


def test_i18n_keys_in_both_languages():
    source = INDEX_HTML.read_text(encoding="utf-8")
    for key in ("panelTokens", "panelLast", "sessionsExpandAll", "sessionsCollapseAll"):
        assert len(re.findall(rf"^\s+{key}: ", source, re.MULTILINE)) == 6, key


def test_sessions_panels_have_logos_and_hide_when_empty():
    """Panel headers reuse the Overview brand identity (icon-only; combined
    gets none) and zero-session harnesses are hidden, with a tab-level empty
    state as the fallback."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    panels = set(re.findall(r'data-panel-details="(\w+)"', source))
    assert len(panels) >= 15, panels
    # Icon-only identity injected once per panel header, combined excluded.
    assert 'createToolIdentity(panel, { iconOnly: true })' in source
    assert 'function createToolBrandIcon(tool, meta)' in source
    assert '!details.querySelector("summary .tool-identity")' in source
    assert 'panel !== "combined"' in source
    # Empty-range panels are hidden only when the range has no sessions and
    # the fetch did not fail. Assert on the guard pieces, not the formatting.
    hide_line = next(l for l in source.splitlines() if "panelEl.parentElement.style.display" in l)
    assert "!sessions.length" in hide_line, hide_line
    assert "!data?.error" in hide_line, hide_line
    assert '"none"' in hide_line, hide_line
    # Tab-level empty state and the expand/collapse row share the same gate.
    assert 'id="sessionsEmptyState"' in source
    assert 'data-i18n="sessionsEmptyRange"' in source
    assert 'id="sessionsPanelToolbar"' in source
    # `hidden` alone is beaten by Tailwind's display utilities on the flex row;
    # the inline display is what actually hides it, and both must gate on
    # showEmpty.
    toolbar_line = next(l for l in source.splitlines() if "toolbarEl.hidden" in l)
    assert "showEmpty" in toolbar_line, toolbar_line
    display_line = next(l for l in source.splitlines() if "toolbarEl.style.display" in l)
    assert "showEmpty" in display_line, display_line


# ---------------------------------------------------------------------------
# Tool filter pills: harness mark + name, marks only when the row runs out
# ---------------------------------------------------------------------------

def _all_pill_button(source: str) -> str:
    match = re.search(r'<button class="modern-tool-pill active".*?</button>', source, re.S)
    assert match, "the always-present All Tools pill is gone from the Sessions toolbar"
    return match.group(0)


def test_filter_pills_wear_the_harness_marks():
    """The filter row is a brand row, not a text row: every tool pill carries the
    same identity the Overview and the panel headers use, and keeps the harness
    name as title plus aria-label so a mark-only pill is still identifiable."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    pills = _extract_js_function(source, "function buildSessionQuickFilterPills() {")
    assert "btn.appendChild(createToolIdentity(tool));" in pills
    assert "btn.title = name;" in pills
    assert "btn.setAttribute('aria-label', name);" in pills
    assert "btn.textContent = formatToolName(tool);" not in pills, (
        "the pills went back to text-only labels"
    )


def test_all_pill_keeps_its_translated_name_outside_the_button():
    """applyI18n writes `textContent`, which deletes the glyph child, so the
    `data-i18n` hook sits on the inner label span and the button carries only the
    attribute hooks (title + aria) that survive a re-translation."""
    button = _all_pill_button(INDEX_HTML.read_text(encoding="utf-8"))
    start_tag = button[: button.index(">") + 1]
    assert 'data-i18n="allTools"' not in start_tag, (
        "data-i18n on the button wipes the collapsed-state glyph via textContent"
    )
    assert 'data-i18n-title="allTools"' in start_tag
    assert 'data-i18n-aria="allTools"' in start_tag
    assert '<span class="tool-brand-label" data-i18n="allTools">' in button
    assert 'class="modern-tool-pill-icon"' in button, (
        "the collapsed row needs a glyph for All Tools, or the pill goes blank"
    )


def test_collapsed_pill_row_css_drops_only_the_names():
    """The collapse is one class on the row: names and the all-pill glyph swap,
    the row keeps its pills, and the marks keep the pill-sized box."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    start = source.index("/* Modern Quick Tool Filter Pills */")
    css = source[start : source.index("/* Slide-Over Drawer Styles */")]
    assert ".modern-tool-pill .tool-brand-icon { width: 18px; height: 18px; }" in css
    assert "#sessionsQuickFilterBar.is-icon-only .tool-brand-label { display: none; }" in css
    assert ".modern-tool-pill-icon { display: none; }" in css, (
        "the all-pill glyph must be hidden while the names are showing"
    )
    assert "#sessionsQuickFilterBar.is-icon-only .modern-tool-pill-icon" in css


def test_pill_density_collapses_only_when_the_names_do_not_fit():
    """The decision must come from the labelled measurement, never from the class
    the previous pass left behind. A row that already collapsed has to be able to
    say "these fit now" on the next resize, and one that fits must refuse to
    latch, whichever way the viewport moved."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    fn = _extract_js_function(source, "function updateSessionPillDensity() {")
    program = """
const AVAILABLE = 400;
function makeBar(labelledWidth, preCollapsed) {
  const classes = new Set(preCollapsed ? ['is-icon-only'] : []);
  const bar = {
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
    },
    // Marks only is narrower, exactly as the real row is: the labelled width is
    // visible for the measurement because the function unhides the names first.
    get scrollWidth() { return classes.has('is-icon-only') ? 120 : labelledWidth; },
    get clientWidth() { return Math.min(AVAILABLE, this.scrollWidth); },
  };
  return bar;
}
let currentBar = null;
globalThis.document = { getElementById: () => currentBar };
function check(labelledWidth, preCollapsed, rounds) {
  currentBar = makeBar(labelledWidth, preCollapsed);
  for (let i = 0; i < rounds; i += 1) updateSessionPillDensity();
  return currentBar.classList.contains('is-icon-only');
}
%SOURCE%
console.log(JSON.stringify({
  tight: check(520, false, 1),
  tightStaysCollapsed: check(520, true, 3),
  roomy: check(360, false, 1),
  roomyRecovers: check(360, true, 3),
  onePixelRounding: check(401, false, 1),
  twoPixelsOver: check(403, false, 1),
}));
""".replace("%SOURCE%", fn)
    result = __import__("json").loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", program],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )
    assert result == {
        "tight": True,
        "tightStaysCollapsed": True,
        "roomy": False,
        "roomyRecovers": False,
        # Sub-pixel rounding shows up as a one-pixel overflow, so the collapse
        # waits for a difference a reader could actually see.
        "onePixelRounding": False,
        "twoPixelsOver": True,
    }


def test_pill_density_runs_on_load_and_on_resize():
    """A row that only collapses after the webfont lands, or after the sidebar
    steals width, is a row that collapses never: the measurement needs a trigger
    for each of the three ways it can change."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    init = _extract_js_function(source, "function initSessionPillDensity() {")
    assert "new ResizeObserver(scheduleSessionPillDensity).observe(watched)" in init
    assert "window.addEventListener('resize', scheduleSessionPillDensity)" in init
    assert "document.fonts.ready" in init
    # The observer watches the toolbar, not the row: collapsing re-sizes the row,
    # and an observer on the element its own edit resized fires forever.
    assert "getElementById('sessionsPanelToolbar')" in init
    # Every rebuild of the pill row re-measures.
    pills = _extract_js_function(source, "function buildSessionQuickFilterPills() {")
    assert "scheduleSessionPillDensity();" in pills
    assert "initSessionPillDensity();" in source


@pytest.fixture
def pill_browser_page():
    """Exercise the real CSS and identity builder without starting an API server.

    Playwright and Chromium are optional developer tools; the default suite
    still runs without adding a browser dependency to the package.
    """
    import base64
    import json
    import os

    playwright = pytest.importorskip("playwright.sync_api")
    source = INDEX_HTML.read_text(encoding="utf-8")
    css = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    brands = re.search(
        r"const TOOL_BRAND_META = Object\.freeze\(\{.*?\n    \}\);", source, re.S
    ).group(0)
    icons = {}
    for path in re.findall(r"icon: '([^']+)'", brands):
        asset = INDEX_HTML.parent / path.removeprefix("/static/")
        mime = "image/svg+xml" if asset.suffix == ".svg" else "image/png"
        icons[path] = f"data:{mime};base64," + base64.b64encode(asset.read_bytes()).decode()
    functions = "\n".join(_extract_js_function(source, signature) for signature in (
        "function normalizeToolBrandKey(tool) {",
        "function toolBrandMeta(tool) {",
        "function createToolBrandIcon(tool, meta) {",
        "function createToolIdentity(tool, options = {}) {",
        "function formatToolName(tool) {",
        "function buildSessionQuickFilterPills() {",
        "function updateSessionPillDensity() {",
        "function scheduleSessionPillDensity() {",
    ))
    bootstrap = f"""
      {brands}
      const icons = {json.dumps(icons)};
      const loadToolBrandIcon = path => Promise.resolve(icons[path]);
      const SESSION_TOOL_KEYS = ['codex', 'claude', 'opencode', 'mimo', 'dsh'];
      const lastSessionsResponses = Object.fromEntries(SESSION_TOOL_KEYS.map(tool =>
        [tool, {{ sessions: [{{ session_id: 'example' }}] }}]));
      let sessionToolFilter = 'all';
      let sessionPillDensityFrame = 0;
      const filterSessionPanels = () => {{}};
      {functions}
      buildSessionQuickFilterPills();
    """
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
            page = browser.new_page(viewport={"width": 1400, "height": 300})
            page.set_content(f"""<!doctype html><style>{css}
              #sessionsQuickFilterBar {{ display: flex; align-items: center;
                gap: 8px; overflow-x: auto; width: 1000px; padding-block: 4px; }}
              body {{ background: var(--color-bg); }}
              html.dark {{ --color-bg: #0f172a; --color-text: #e2e8f0; }}
            </style><div id="sessionsPanelToolbar"><div id="sessionsQuickFilterBar">
              {_all_pill_button(source)}
            </div></div><script>{bootstrap}</script>""")
            page.wait_for_function("""() => [...document.querySelectorAll(
              '#sessionsQuickFilterBar .tool-brand-icon img')].length === 5 &&
              [...document.querySelectorAll('#sessionsQuickFilterBar img')].every(img => img.complete)
            """)
            yield page
        finally:
            browser.close()


def test_pill_density_stays_collapsed_near_the_fit_boundary(pill_browser_page):
    page = pill_browser_page
    natural_width = page.evaluate("""() => {
      const pills = document.querySelectorAll('#sessionsQuickFilterBar button');
      return pills[pills.length - 1].getBoundingClientRect().right -
        pills[0].getBoundingClientRect().left;
    }""")

    def measure(width):
        page.evaluate("""width => {
          document.getElementById('sessionsQuickFilterBar').style.width = `${width}px`;
          updateSessionPillDensity();
        }""", width)
        # Let a padding transition finish: measuring before this missed the bug.
        page.wait_for_timeout(200)
        return page.evaluate("""() => {
          const bar = document.getElementById('sessionsQuickFilterBar');
          return { collapsed: bar.classList.contains('is-icon-only'),
            overflow: bar.scrollWidth - bar.clientWidth };
        }""")

    assert measure(natural_width - 80)["collapsed"]
    for _ in range(3):
        # Six pills used to retain 36px of narrower padding while the names
        # returned. A 12px deficit must never count as enough room for labels.
        assert measure(natural_width - 12)["collapsed"]
    roomy = measure(natural_width + 24)
    assert not roomy["collapsed"] and roomy["overflow"] <= 1
    assert measure(natural_width - 12)["collapsed"]


def test_collapsed_mimo_logo_has_contrast_in_both_themes(pill_browser_page):
    page = pill_browser_page
    page.evaluate("""() => {
      const bar = document.getElementById('sessionsQuickFilterBar');
      bar.style.width = '300px';
      updateSessionPillDensity();
    }""")
    for dark in (False, True):
        page.evaluate("dark => document.documentElement.classList.toggle('dark', dark)", dark)
        contrast = page.evaluate("""() => {
          const img = document.querySelector('button[data-tool="mimo"] img');
          const canvas = document.createElement('canvas');
          canvas.width = img.naturalWidth;
          canvas.height = img.naturalHeight;
          const ctx = canvas.getContext('2d');
          ctx.filter = getComputedStyle(img).filter;
          ctx.drawImage(img, 0, 0);
          const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
          let ink;
          for (let i = 0; i < pixels.length; i += 4) {
            if (pixels[i + 3] === 255) { ink = [...pixels.slice(i, i + 3)]; break; }
          }
          if (!ink) throw new Error('Mimo logo contains no opaque pixels');
          const bg = getComputedStyle(document.body).backgroundColor.match(/\\d+/g).slice(0, 3).map(Number);
          const luminance = rgb => rgb.map(v => {
            const c = v / 255;
            return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4;
          }).reduce((sum, c, i) => sum + c * [.2126, .7152, .0722][i], 0);
          const a = luminance(ink), b = luminance(bg);
          return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
        }""")
        assert contrast >= 3, f"Mimo logo contrast is {contrast:.2f} in dark={dark}"
