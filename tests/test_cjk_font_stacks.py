"""Han glyphs need a family of their own wherever the dashboard names a font.

Fira Sans and Fira Code carry no CJK glyph, so a stack that stops at `monospace`
leaves Chinese to whatever the browser substitutes. On a Linux box with no CJK
font installed that is a Ming serif, at a weight the face does not have, under
`font-extrabold` -- which is how a section title stops looking like the rest of
the page. These tests keep:

- one definition of each stack, reachable through `--font-sans` / `--font-mono`;
- a CJK family in the tail of both, covering each supported platform's own face;
- the 3D view's labels, which name the current stylesheet stack;
- the locale rules that take translated labels off the mono stack and off tracking;
- the optional vendored face: its discovery rule, the link the served page's head
  script writes for it (run under node), and the fetch script's licence handling.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tokdash  # type: ignore[import-untyped]
import tokdash.api as api
from tokdash import assets

from support.frontend import needs_node, run_node

STATIC = Path(tokdash.__file__).parent / "static"
INDEX_HTML = STATIC / "index.html"
THEMES_CSS = STATIC / "themes.css"
NOTICE = STATIC / "vendor" / "NOTICE.txt"
REPO_ROOT = Path(tokdash.__file__).resolve().parents[2]
VENDOR_SCRIPT = REPO_ROOT / "scripts" / "vendor_cjk_fonts.sh"

# The face each platform already ships, plus the names a Linux distribution uses.
PLATFORM_CJK_FAMILIES = (
    "pingfang sc",          # macOS
    "microsoft yahei",      # Windows, Simplified
    "microsoft jhenghei",   # Windows, Traditional
    "noto sans sc",         # Linux / Android
    "source han sans sc",
    "noto sans cjk sc",
)

# The dashboard ships six languages, three of them CJK.
OTHER_CJK_FAMILIES = (
    "hiragino sans",
    "yu gothic",
    "noto sans jp",
    "apple sd gothic neo",
    "malgun gothic",
    "noto sans kr",
)


def _source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _declaration(name: str) -> str:
    match = re.search(rf"^\s*{name}:([^;]+);", _source(), re.MULTILINE)
    assert match, f"{name} is not declared in index.html"
    return match.group(1).strip()


def _token(name: str, lang: str | None = None) -> str:
    """A custom property with any nested `var(--...)` expanded once."""
    overrides = {}
    if lang:
        block = re.search(rf"html:lang\({lang}\)\s*\{{([^}}]+)\}}", _source())
        if block:
            overrides = dict(re.findall(r"(--font[\w-]+):([^;]+);", block.group(1)))
    value = overrides.get(name, _declaration(name))
    for _ in range(4):
        referenced = re.findall(r"var\((--[^)]+)\)", value)
        if not referenced:
            return value
        for reference in referenced:
            value = value.replace(f"var({reference})", overrides.get(reference, _declaration(reference)))
    raise AssertionError(f"{name} nests more than four custom properties deep")


def _families(stack: str) -> list[str]:
    """A stack as a family list, so quoting style cannot hide a difference."""
    return [part.strip().strip("\"'").lower() for part in stack.split(",") if part.strip()]


def test_no_stack_names_fira_directly():
    """Every rule goes through a custom property, so there is one list to fix."""
    for path in (INDEX_HTML, THEMES_CSS):
        source = path.read_text(encoding="utf-8")
        hardcoded = [
            line.strip()
            for line in source.splitlines()
            if re.search(r"font-family:\s*[\"']Fira", line)
        ]
        assert not hardcoded, f"{path.name} still hardcodes a Fira stack: {hardcoded}"
    assert "font-family: var(--font-mono)" in _source()
    assert "font-family: var(--font-sans)" in _source()


def test_every_font_declaration_terminates():
    """A dropped semicolon after `var(--font-...)` is not a small mistake.

    The value then runs to the next semicolon, so the declaration is invalid and
    both the font and the property that followed it disappear -- a rule that
    looks present in the file and does nothing.
    """
    for path in (INDEX_HTML, THEMES_CSS):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if "font-family: var(--font-" in stripped:
                assert stripped.endswith((";", "}")), (path.name, stripped)


@pytest.mark.parametrize("token", ("--font-sans", "--font-mono"))
def test_stacks_carry_every_platforms_own_cjk_face(token: str):
    families = _families(_token(token))
    for family in PLATFORM_CJK_FAMILIES:
        assert family in families, f"{token} cannot reach {family}"


def test_the_language_overrides_are_outside_the_root_block():
    """`:root` stays one block holding every global token.

    Closing it early to drop a language rule in leaves the rest of the token list
    inside that selector, so the dashboard loses its colours in every other
    language -- and looks fine in the one you happen to be testing.
    """
    source = _source()
    start = source.index("    :root {")
    depth = 0
    cursor = start + len("    :root")
    while source[cursor] != "}" or depth != 1:
        if source[cursor] == "{":
            depth += 1
        elif source[cursor] == "}":
            depth -= 1
        cursor += 1
    block = source[start:cursor]
    assert "--font-sans" in block, "the font tokens left :root"
    assert "--color-label" in block, "the colour tokens left :root"
    assert "html:lang" not in block, "a language override sits inside :root"
    assert source.index("html:lang(ja) {") > cursor


def test_mono_stack_still_ends_in_a_generic_family():
    """The CJK names were inserted ahead of the generic, not in place of it."""
    assert _families(_token("--font-mono"))[-1] == "monospace"
    assert _families(_token("--font-sans"))[0] == "fira sans"


def test_simplified_chinese_is_the_default_for_han():
    """The UI's zh strings are its CJK majority, so SC wins when nothing else does."""
    assert _declaration("--font-cjk") == "var(--font-cjk-sc)"
    families = _families(_token("--font-sans"))
    assert "noto sans sc" in families
    for family in OTHER_CJK_FAMILIES:
        assert family not in families, f"{family} belongs to an interface language only"


def test_every_shipped_cjk_language_has_its_own_faces_named():
    """Each language group covers the faces its platforms ship."""
    expected = {
        "--font-cjk-sc": ("pingfang sc", "microsoft yahei", "microsoft jhenghei", "noto sans sc"),
        "--font-cjk-ja": ("hiragino sans", "yu gothic", "noto sans jp", "noto sans cjk jp"),
        "--font-cjk-ko": ("apple sd gothic neo", "malgun gothic", "noto sans kr", "noto sans cjk kr"),
    }
    for name, families in expected.items():
        declared = _families(_declaration(name))
        for family in families:
            assert family in declared, f"{name} cannot reach {family}"


@pytest.mark.parametrize(("lang", "first_cjk"), (("ja", "hiragino sans"), ("ko", "apple sd gothic neo")))
def test_each_cjk_language_gets_its_own_glyph_variants(lang, first_cjk):
    """Japanese and Korean share Han characters with Chinese, but not their shapes.

    直, 关 and 恶 are drawn differently, so the interface language picks the face
    and Simplified Chinese stays behind it for characters the other faces lack.
    """
    source = _source()
    override = re.search(rf"html:lang\({lang}\)\s*\{{\s*--font-cjk:([^;]+);", source)
    assert override, f"no --font-cjk override for {lang}"
    tail = _families(override.group(1))
    assert tail[0] == f"var(--font-cjk-{lang})", override.group(1)
    assert tail[-1] == "var(--font-cjk-sc)", "Simplified Chinese must stay behind it"
    assert first_cjk in _families(_declaration(f"--font-cjk-{lang}")), lang


@pytest.mark.parametrize(("lang", "region"), (("ja", "jp"), ("ko", "kr")))
def test_locale_fonts_precede_chinese_in_sans_and_mono(lang, region):
    for token in ("--font-sans", "--font-mono"):
        families = _families(_token(token, lang))
        assert families.index(f"noto sans cjk {region}") < families.index("noto sans sc")
    mono = _families(_token("--font-mono", lang))
    assert f"noto sans mono cjk {region}" in mono
    assert "sarasa mono sc" not in mono
    assert "noto sans mono cjk sc" not in mono


def test_3d_view_labels_use_the_stylesheet_stack():
    """The 3D calendar's DOM labels name the token, not a font of their own.

    The Report cards resolve the same stacks at paint time, and
    test_usage_report_frontend.py::test_share_card_fonts_follow_language_style_changes
    runs that; nothing runs the 3D view, so its one line is pinned here.
    """
    assert "labelsContainer.style.fontFamily = 'var(--font-sans)'" in _source()


@pytest.mark.parametrize("lang", ("zh-CN", "ja", "ko"))
def test_translated_labels_leave_the_mono_stack_under_cjk_locales(lang: str):
    source = _source()
    assert f"html:lang({lang}) .mono[data-i18n]" in source, lang
    assert f"html:lang({lang}) [data-i18n]" in source, lang
    block = source.split(f"html:lang({lang}) .mono[data-i18n]", 1)[1][:300]
    assert "font-family: var(--font-sans)" in block, lang


def test_cjk_locales_drop_the_uppercase_tracking():
    """0.08em between Han characters reads as a gap, not as emphasis."""
    source = _source()
    tracking_rule = source.split("html:lang(zh-CN) [data-i18n],", 1)[1].split("}", 1)[0]
    assert "letter-spacing: normal" in tracking_rule


def test_font_request_names_only_families_the_css_uses():
    link = re.search(r'href="(https://fonts\.googleapis\.com/css2[^"]+)"', _source())
    assert link, "no Google Fonts request found"
    requested = {
        match.replace("+", " ").split(":")[0]
        for match in re.findall(r"family=([^&]+)", link.group(1))
    }
    assert requested == {"Fira Code", "Fira Sans", "Noto Sans SC"}, requested
    named = set(_families(_token("--font-sans"))) | set(_families(_token("--font-mono")))
    for family in requested:
        assert family.lower() in named, f"{family} is fetched but named by no stack"


def test_tailwind_utilities_keep_their_order_and_gain_only_the_cjk_tail():
    """Tailwind's preflight sets `body`, and its sheet is injected after the
    dashboard's own, so these lists are what most of the page actually renders.

    The families stay in Tailwind's order on purpose: replacing them would move
    every Latin label from the system UI face to Fira, which is a design decision
    rather than a CJK fix. Only the CJK tail is added.
    """
    config = re.search(r"if \(window\.tailwind\) tailwind\.config = (\{.*?\})</script>", _source(), re.S)
    assert config, "tailwind.config not found"
    block = config.group(1)
    sans = re.search(r"sans: \[([^\]]+)\]", block).group(1)
    mono = re.search(r"mono: \[([^\]]+)\]", block).group(1)
    cjk = _families(_token("--font-cjk"))
    for stack, first, generic in ((sans, "ui-sans-serif", "sans-serif"), (mono, "ui-monospace", "monospace")):
        families = _families(stack)
        assert families[0] == first, stack
        assert "var(--font-cjk)" in families, f"{first} stack has no CJK tail"
        expanded = _families(stack.replace("var(--font-cjk)", ", ".join(cjk)))
        assert generic in expanded, stack
        assert expanded.index(cjk[0]) < expanded.index(generic), "the generic would win first"


def _clear_font_cache():
    assets.local_cjk_font_css.cache_clear()


@pytest.fixture(autouse=True)
def _reset_vendored_face_cache():
    """local_cjk_font_css() is cached for the process; never leak a stub through it."""
    _clear_font_cache()
    yield
    _clear_font_cache()


def test_no_vendored_face_means_no_font_link(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "STATIC_DIR", tmp_path)
    assert assets.local_cjk_font_css() == ""

    client = TestClient(api.app)
    page = client.get("/").text
    assert assets.CJK_FONT_CSS_PLACEHOLDER not in page
    assert "vendor/fonts" not in page


def test_vendored_face_is_discovered_by_upstream_version(tmp_path, monkeypatch):
    """The newest vendored version wins, and v9 sorts before v41."""
    for version in ("v9", "v41"):
        css = tmp_path / "vendor" / "fonts" / f"noto-sans-sc-{version}" / "noto-sans-sc.css"
        css.parent.mkdir(parents=True)
        css.write_text("/* stub */", encoding="utf-8")
    monkeypatch.setattr(assets, "STATIC_DIR", tmp_path)
    assert assets.local_cjk_font_css() == "/static/vendor/fonts/noto-sans-sc-v41/noto-sans-sc.css"


def _font_head_script(page: str) -> str:
    """The head script that decides whether to link the vendored face."""
    start = page.rindex("<script>", 0, page.index("const cjkFontCss")) + len("<script>")
    return page[start : page.index("</script>", start)]


def _served_page(static_dir: Path, monkeypatch) -> str:
    _clear_font_cache()
    monkeypatch.setattr(assets, "STATIC_DIR", static_dir)
    monkeypatch.delenv("TOKDASH_PUBLIC_BASE_PATH", raising=False)
    page = TestClient(api.app).get("/").text
    assert assets.CJK_FONT_CSS_PLACEHOLDER not in page
    return page


# Runs each head script with a window at the given path and reports the
# stylesheet links it writes.
HEAD_LINKS_JS = """
const cases = %s;
const out = {};
for (const [name, { script, pathname }] of Object.entries(cases)) {
  const written = [];
  const window = { location: { pathname } };
  const document = { write: (html) => { written.push(html); } };
  new Function('window', 'document', script)(window, document);
  out[name] = written.filter((html) => html.includes('rel="stylesheet"'));
}
process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_the_page_links_a_vendored_face_only_when_the_server_offers_one(tmp_path, monkeypatch):
    """The client cannot know the file exists, so the server says so through the
    placeholder: the page links the face it names, under the public prefix too,
    and links nothing when it names none. A placeholder the server never
    substituted must not load as a relative path either."""
    face = "vendor/fonts/noto-sans-sc-v41/noto-sans-sc.css"
    (tmp_path / "with" / face).parent.mkdir(parents=True)
    (tmp_path / "with" / face).write_text("/* stub */", encoding="utf-8")
    (tmp_path / "without").mkdir()
    vendored = _font_head_script(_served_page(tmp_path / "with", monkeypatch))
    bare = _font_head_script(_served_page(tmp_path / "without", monkeypatch))
    cases = {
        "vendored": {"script": vendored, "pathname": "/"},
        "vendoredUnderPrefix": {"script": vendored, "pathname": "/tokdash/"},
        "noFace": {"script": bare, "pathname": "/"},
        "unsubstituted": {"script": _font_head_script(_source()), "pathname": "/"},
    }
    harness = tmp_path / "head-links.js"
    harness.write_text(HEAD_LINKS_JS % json.dumps(cases), encoding="utf-8")
    links = json.loads(run_node(harness))
    assert links == {
        "vendored": ['<link rel="stylesheet" href="/static/' + face + '" />'],
        "vendoredUnderPrefix": ['<link rel="stylesheet" href="/tokdash/static/' + face + '" />'],
        "noFace": [],
        "unsubstituted": [],
    }


@pytest.mark.skipif(not VENDOR_SCRIPT.is_file(), reason="scripts/ is not installed")
def test_vendoring_script_keeps_the_font_license_with_the_font():
    """OFL 1.1 lets the face be embedded, provided the license travels with it."""
    script = VENDOR_SCRIPT.read_text(encoding="utf-8")
    assert "OFL.txt" in script, "the fetch must include the license"
    assert "--fail" in script, "HTTP errors must fail the fetch"
    assert "noto-sans-sc-$version" in script, "versioned directory: /static/vendor/ caches for a year"
    assert "fonts.gstatic.com" in script
    assert "OFL" in NOTICE.read_text(encoding="utf-8"), "NOTICE.txt must name the face"


def test_vendored_fonts_are_not_committed():
    """The repository ships no font binaries; the script is what brings one in."""
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "static/vendor/fonts/" in ignore
