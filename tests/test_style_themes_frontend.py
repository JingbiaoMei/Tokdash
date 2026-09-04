"""Contract tests for the dashboard style themes.

Adding a theme means touching five places at once: the `validStyleThemes`
list in static/theme-config.js, the three per-theme maps in the same file
(`heatColorsMap`, `chartPaletteMap`, `themeMetaColors`), a theme section in
static/themes.css, an `<option>` in the settings picker, and a `styleXxx`
label in all six i18n dictionaries. The runtime falls back to `elevated` for
missing map entries, so a forgotten map fails silently — these tests make the
whole checklist fail loudly instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

STATIC = Path(tokdash.__file__).parent / "static"
THEME_CONFIG_JS = STATIC / "theme-config.js"
THEMES_CSS = STATIC / "themes.css"
INDEX_HTML = STATIC / "index.html"

EXPECTED_LANGUAGES = ("en", "zh", "ja", "ko", "es", "pt")


def _valid_style_themes() -> list[str]:
    source = THEME_CONFIG_JS.read_text(encoding="utf-8")
    block = re.search(r"validStyleThemes: \[(.*?)\]", source, re.S)
    assert block, "validStyleThemes not found in theme-config.js"
    themes = re.findall(r'"([^"]+)"', block.group(1))
    assert themes, "validStyleThemes is empty"
    return themes


VALID_STYLE_THEMES = _valid_style_themes()

# Elevated is the base stylesheet: the unselected `html` rules already are the
# elevated look, so themes.css carries no `data-ui-theme="elevated"` override
# section for it. Every other theme needs one.
BASE_STYLESHEET_THEMES = ("elevated",)


def _config_map_span(source: str, name: str, next_marker: str | None) -> str:
    start = source.find(f"    {name}: {{")
    assert start != -1, f"{name} not found in theme-config.js"
    end = source.find(next_marker, start) if next_marker else source.find("\n  });", start)
    assert end != -1, f"unterminated {name} in theme-config.js"
    return source[start:end]


@pytest.mark.parametrize("theme", VALID_STYLE_THEMES)
def test_theme_has_css_section(theme: str) -> None:
    if theme in BASE_STYLESHEET_THEMES:
        pytest.skip(f"{theme!r} is the base stylesheet, no override section by design")
    source = THEMES_CSS.read_text(encoding="utf-8")
    assert f'html[data-ui-theme="{theme}"]' in source, (
        f"themes.css has no section for {theme!r}"
    )


@pytest.mark.parametrize("theme", VALID_STYLE_THEMES)
def test_theme_has_heat_colors(theme: str) -> None:
    source = THEME_CONFIG_JS.read_text(encoding="utf-8")
    span = _config_map_span(source, "heatColorsMap", "    chartPaletteMap: {")
    entry = re.search(
        rf'{re.escape(theme)}: \{{\s*light: \[(.*?)\],\s*dark: \[(.*?)\],?\s*\}}', span
    )
    assert entry, f"heatColorsMap is missing {theme!r}"
    for mode in (1, 2):
        colors = re.findall(r'"#[0-9A-Fa-f]{6}"', entry.group(mode))
        assert len(colors) == 8, (
            f"heatColorsMap[{theme}] must have 8 colors per mode, "
            f"found {len(colors)} in group {mode}"
        )


@pytest.mark.parametrize("theme", VALID_STYLE_THEMES)
def test_theme_has_chart_palette(theme: str) -> None:
    source = THEME_CONFIG_JS.read_text(encoding="utf-8")
    span = _config_map_span(source, "chartPaletteMap", "    themeMetaColors: {")
    entry = re.search(
        rf'{re.escape(theme)}: \{{\s*light: \[(.*?)\],\s*dark: \[(.*?)\],?\s*\}}', span
    )
    assert entry, f"chartPaletteMap is missing {theme!r}"
    for mode in (1, 2):
        colors = re.findall(r'"#[0-9A-Fa-f]{6}"', entry.group(mode))
        assert len(colors) == 6, (
            f"chartPaletteMap[{theme}] must have 6 colors per mode, "
            f"found {len(colors)} in group {mode}"
        )


@pytest.mark.parametrize("theme", VALID_STYLE_THEMES)
def test_theme_has_meta_colors(theme: str) -> None:
    source = THEME_CONFIG_JS.read_text(encoding="utf-8")
    span = _config_map_span(source, "themeMetaColors", None)
    entry = re.search(
        rf'{re.escape(theme)}: \{{\s*light: "#[0-9A-Fa-f]{{6}}",\s*dark: "#[0-9A-Fa-f]{{6}}"\s*\}}',
        span,
    )
    assert entry, f"themeMetaColors is missing a light/dark pair for {theme!r}"


def _picker_options(source: str) -> list[tuple[str, str]]:
    select_start = source.index('<select id="styleThemeSelect"')
    select_end = source.index("</select>", select_start)
    select = source[select_start:select_end]
    return re.findall(r'<option value="([^"]+)" data-i18n="([^"]+)"', select)


def test_picker_lists_every_theme_in_order() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    options = _picker_options(source)
    assert [value for value, _ in options] == VALID_STYLE_THEMES, (
        "styleThemeSelect options drifted from validStyleThemes"
    )
    for value, i18n_key in options:
        expected_key = "style" + value[:1].upper() + value[1:]
        assert i18n_key == expected_key, (
            f"picker option {value!r} uses {i18n_key!r}, expected {expected_key!r}"
        )


@pytest.mark.parametrize("theme", VALID_STYLE_THEMES)
def test_theme_label_in_every_language(theme: str) -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    key = "style" + theme[:1].upper() + theme[1:]
    start = source.find("const I18N = {")
    assert start != -1, "const I18N = { not found in index.html"
    end = source.find("\n    };", start)
    assert end != -1, "unterminated I18N object in index.html"
    literal = source[start : end + 6]
    blocks = dict(
        re.findall(r"^      ([a-z]+): \{\n(.*?)^      \},?$", literal, re.M | re.S)
    )
    assert tuple(blocks) == EXPECTED_LANGUAGES
    for lang, block in blocks.items():
        assert re.search(rf"^        {key}: ", block, re.M), (
            f"i18n dictionary {lang!r} is missing {key!r}"
        )


# --- README theme-count parity ------------------------------------------------
#
# Each README's feature list quotes the theme count in its own words, and no
# other test reads those lines — the count once shipped stale in five of the
# six READMEs after a theme batch bumped only the English one. The phrases are
# per-language on purpose: a reworded README stops matching and fails loudly
# instead of passing unchecked.

REPO_ROOT = Path(__file__).resolve().parents[1]

README_THEME_COUNT_PHRASE = {
    "README.md": "style themes",
    "README_CN.md": "款样式主题",
    "README_JA.md": "種類のスタイルテーマ",
    "README_KO.md": "가지 스타일 테마",
    "README_ES.md": "temas de estilo",
    "README_PT.md": "temas de estilo",
}


@pytest.mark.parametrize("readme", sorted(README_THEME_COUNT_PHRASE))
def test_readme_theme_count_matches_config(readme: str) -> None:
    source = (REPO_ROOT / readme).read_text(encoding="utf-8")
    phrase = README_THEME_COUNT_PHRASE[readme]
    match = re.search(rf"(\d+)\s*{re.escape(phrase)}", source)
    assert match, f"{readme} theme-count line not found (expected '<N> {phrase}')"
    assert int(match.group(1)) == len(VALID_STYLE_THEMES), (
        f"{readme} says {match.group(1)} themes, "
        f"validStyleThemes has {len(VALID_STYLE_THEMES)}"
    )


# --- Rendering invariant guards -----------------------------------------------
#
# The checks below exist because each new theme used to be authored by copying
# the previous theme's block, so a single authoring mistake replicated across
# every new theme: flat's `--shadow-*: none` broke every composed box-shadow,
# the `.ui-input { background: ... }` shorthand erased the select caret in
# eleven themes, and seven new `--color-label` tokens failed WCAG AA. All three
# are mechanically checkable, so they are checked here.


@pytest.mark.parametrize("theme", VALID_STYLE_THEMES)
def test_theme_has_dark_section(theme: str) -> None:
    if theme in BASE_STYLESHEET_THEMES:
        pytest.skip(f"{theme!r} is the base stylesheet, no override section by design")
    source = THEMES_CSS.read_text(encoding="utf-8")
    assert f'html.dark[data-ui-theme="{theme}"]' in source, (
        f"themes.css has no dark-mode section for {theme!r}"
    )


def test_shadow_tokens_are_composable() -> None:
    # Base rules compose `box-shadow: var(--shadow-md), <second layer>`. A bare
    # `none` token is only legal as the sole value, so it invalidates the whole
    # declaration (second layer included) at computed-value time. Use `0 0 #0000`.
    source = THEMES_CSS.read_text(encoding="utf-8")
    offenders = re.findall(r"--shadow-(?:sm|md|lg):\s*none\s*;", source)
    assert not offenders, (
        f"--shadow-* tokens set to none break composed box-shadows: {offenders}"
    )


def test_theme_inputs_keep_background_shorthand_out() -> None:
    # `.style-select` paints the dropdown caret with background-image at lower
    # specificity. A theme rule on the control itself (`.ui-input`, optionally
    # with :hover/:focus) using the `background:` shorthand resets
    # background-image to none and the caret vanishes. Descendant rules such as
    # `.ui-input option` are unaffected — options carry no caret image.
    source = THEMES_CSS.read_text(encoding="utf-8")
    offenders = []
    for match in re.finditer(
        r"(html(?:\.dark)?\[data-ui-theme=\"[^\"]+\"\] \.ui-input(?::\w+)?)\s*\{(.*?)\}",
        source,
        re.S,
    ):
        selector, body = match.group(1), match.group(2)
        if re.search(r"(?<![\w-])background:\s", body):
            offenders.append(selector)
    assert not offenders, (
        f".ui-input rules using the background: shorthand hide the select caret: "
        f"{offenders}"
    )


def _hex_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return (
        int(value[0:2], 16) / 255,
        int(value[2:4], 16) / 255,
        int(value[4:6], 16) / 255,
    )


def _parse_rgba(value: str) -> tuple[tuple[float, float, float], float]:
    match = re.match(r"rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\s*\)", value)
    assert match, f"not an rgba() color: {value!r}"
    rgb = tuple(int(match.group(i)) / 255 for i in (1, 2, 3))
    return rgb, float(match.group(4))


def _composite(fg: tuple[float, float, float], alpha: float, bg: tuple[float, float, float]):
    return tuple(f * alpha + b * (1 - alpha) for f, b in zip(fg, bg))


def _relative_luminance(rgb) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg, bg) -> float:
    lighter, darker = sorted(
        (_relative_luminance(fg), _relative_luminance(bg)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def _theme_token(body: str, name: str) -> str | None:
    match = re.search(rf"--{name}:\s*([^;]+);", body)
    return match.group(1).strip() if match else None


def _global_tokens(dark: bool) -> dict[str, str]:
    """The :root / html.dark base tokens in index.html that theme sections
    inherit from when they do not declare an override."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    selector = r"html\.dark" if dark else r":root"
    block = re.search(rf"{selector} \{{(.*?)\}}", source, re.S)
    assert block, f"base {selector} block not found in index.html"
    return {
        name: _theme_token(block.group(1), name)
        for name in ("color-label", "color-bg", "color-surface-glass")
    }


@pytest.mark.parametrize("theme", VALID_STYLE_THEMES)
@pytest.mark.parametrize("dark", [False, True])
def test_theme_label_color_meets_wcag_aa(theme: str, dark: bool) -> None:
    # Tokens resolve through the same cascade as the browser: theme section
    # first, then the global :root / html.dark default. A theme that declares
    # its own background but no --color-label is still checked — skipping on a
    # missing token would let a future theme fall back silently onto a
    # combination nobody measured.
    source = THEMES_CSS.read_text(encoding="utf-8")
    prefix = "html.dark" if dark else "html"
    section = re.search(
        rf'{prefix}\[data-ui-theme="{re.escape(theme)}"\] \{{(.*?)\n\}}', source, re.S
    )
    section_body = section.group(1) if section else ""
    globals_ = _global_tokens(dark)

    def resolve(name: str) -> str:
        value = _theme_token(section_body, name) or globals_.get(name)
        assert value, f"no {name} in {theme!r} section or the global defaults"
        return value

    label = resolve("color-label")
    bg = resolve("color-bg")
    surface_glass = resolve("color-surface-glass")

    bg_rgb = _hex_rgb(bg)
    if surface_glass.startswith("#"):
        surface_rgb = _hex_rgb(surface_glass)
    else:
        s_rgb, s_alpha = _parse_rgba(surface_glass)
        surface_rgb = _composite(s_rgb, s_alpha, bg_rgb)

    l_rgb, l_alpha = _parse_rgba(label)
    mode = "dark" if dark else "light"
    for surface_name, surface in (("bg", bg_rgb), ("surface", surface_rgb)):
        ratio = _contrast(_composite(l_rgb, l_alpha, surface), surface)
        assert ratio >= 4.5, (
            f"--color-label in {theme} {mode} is {ratio:.2f}:1 over {surface_name}, "
            f"below the WCAG AA 4.5:1 floor for small text"
        )


def test_refresh_icon_stroke_follows_button_color() -> None:
    # The refresh glyph ships twice (static markup and the renderRefreshButton
    # JS template); a hardcoded stroke="white" is invisible on light-background
    # primary buttons. Both copies must inherit the button text color.
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert 'stroke="white"' not in source, (
        'an SVG icon hardcodes stroke="white"; use stroke="currentColor"'
    )
