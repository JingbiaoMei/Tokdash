from __future__ import annotations

import json
import re
from pathlib import Path

import tokdash  # type: ignore[import-untyped]

STATIC_DIR = Path(tokdash.__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
RELEASE_NOTES = STATIC_DIR / "release-notes.json"
THEMES_CSS = STATIC_DIR / "themes.css"


def _extract_js_function(source: str, signature: str) -> str:
    start = source.find(signature)
    assert start != -1, f"{signature} not found in index.html"
    body_start = start + len(signature) - 1
    assert source[body_start] == "{", f"{signature} must end at the function body"
    depth = 0
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


def test_document_background_covers_the_full_page() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    themes = THEMES_CSS.read_text(encoding="utf-8")
    assert "html, body { height: 100%; }" not in source
    assert re.search(r"html\s*\{[^}]*min-height:\s*100%", source, re.DOTALL)
    assert re.search(r"body\s*\{[^}]*min-height:\s*100%", source, re.DOTALL)
    themed_body = re.search(
        r"html\[data-ui-theme\]:not\(\[data-ui-theme=\"brutalist\"\]\)\s+body\s*\{(?P<body>[^}]*)\}",
        themes,
        re.DOTALL,
    )
    assert themed_body, "themes.css must define one shared full-page background rule"
    body = themed_body.group("body")
    assert "background-repeat: no-repeat" in body
    assert "background-size: 100% 100%" in body
    assert "background-color: var(--color-bg)" in body


def test_loading_placeholders_share_the_profile_activity_shimmer() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert ".tokdash-loading-placeholder" in source
    assert ".tokdash-loading-placeholder:has(> .tokdash-loading-label)" in source
    assert "@keyframes tokdash-loading-sweep" in source
    assert "function renderLoadingPlaceholder(element, label = t('loading')) {" in source
    assert "new MutationObserver" not in source
    assert "querySelectorAll('*')" not in source
    assert source.count('class="tokdash-loading-label"') >= 30
    assert source.count("animation:tokdash-loading-sweep 1.4s ease-in-out infinite") >= 3
    assert "prefers-reduced-motion:reduce" in source


def test_release_notes_ship_with_the_current_package_version() -> None:
    payload = json.loads(RELEASE_NOTES.read_text(encoding="utf-8"))
    assert payload["current"] == tokdash.__version__
    assert payload["releases"][0]["version"] == tokdash.__version__
    assert len(payload["releases"]) >= 3
    for release in payload["releases"]:
        assert release["version"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", release["date"])
        assert release["sections"]
        for section in release["sections"]:
            assert section["type"] in {"added", "changed", "fixed"}
            assert section["items"] and all(section["items"])


def test_release_notes_accessible_drawer_contract() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="releaseNotesToggle"' in source
    assert 'id="releaseNotesDialog"' in source
    assert 'aria-controls="releaseNotesDialog"' in source
    assert 'aria-labelledby="releaseNotesTitle"' in source
    assert 'id="releaseNotesClose"' in source
    assert re.search(
        r"\.release-notes-dialog\s*\{[^}]*background:\s*var\(--color-bg\)",
        source,
        re.DOTALL,
    )
    assert "event.key === 'Escape'" in source
    assert "version?.runtime_version || version?.current" in source
    assert source.count("whatsNew: '") == 2
    assert source.count("releaseNotesIntro: '") == 2


def test_release_notes_render_with_text_content_and_lazy_local_fetch() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    renderer = _extract_js_function(source, "function renderReleaseNotes(payload) {")
    loader = _extract_js_function(source, "async function loadReleaseNotes() {")
    opener = _extract_js_function(source, "async function openReleaseNotes() {")

    assert "itemElement.textContent = item;" in renderer
    assert "replaceChildren" in renderer
    assert "tokdashPath('/static/release-notes.json')" in loader
    assert "cache: 'no-store'" in loader
    assert "showModal()" in opener
    assert "loadReleaseNotes()" in opener
