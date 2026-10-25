"""Tests for the page background and the release-notes drawer.

Covers the full-page themed background, the shipped notes matching the package
version, the drawer's markup, and the drawer itself run under node on the stub
DOM in ``tests/support/i18n_profile.py``: when it reads the notes, what it does
when the read fails, how it renders them and the three ways it closes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import tokdash  # type: ignore[import-untyped]

from support.frontend import needs_node
from support.i18n_profile import release_notes_script, run_page


STATIC_DIR = Path(tokdash.__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
RELEASE_NOTES = STATIC_DIR / "release-notes.json"
THEMES_CSS = STATIC_DIR / "themes.css"


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


def test_release_notes_drawer_markup() -> None:
    """The trigger names the dialog it opens, the dialog is named by its
    heading, and the drawer paints on the theme's page colour. PR #110 moved the
    trigger from the header rail into the sidebar footer and added a phone-width
    mirror, because the sidebar itself is display:none below 768px."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    toggle = re.search(r'<button id="releaseNotesToggle"[^>]*>', source).group(0)
    assert 'aria-controls="releaseNotesDialog"' in toggle
    dialog_start = source.index('<dialog id="releaseNotesDialog"')
    dialog = source[dialog_start : source.index("</dialog>", dialog_start)]
    assert 'aria-labelledby="releaseNotesTitle"' in dialog.split(">", 1)[0]
    assert 'id="releaseNotesTitle"' in dialog
    assert 'id="releaseNotesClose"' in dialog
    assert re.search(
        r"\.release-notes-dialog\s*\{[^}]*background:\s*var\(--color-bg\)",
        source,
        re.DOTALL,
    )
    mobile = re.search(r'<button id="mobileReleaseNotesBtn"[^>]*>', source).group(0)
    assert 'onclick="openReleaseNotesModal()"' in mobile
    assert source.index('id="appSidebar"') < source.index('id="releaseNotesToggle"')
    assert source.index('id="releaseNotesToggle"') < source.index('id="sidebarSettingsBtn"')
    assert 'id="settingsMenu"' not in source  # the duplicate header pair is gone for good


def _text_item(value: str) -> dict:
    """A bullet as entries() reports it: its text, and no child elements."""
    return {"text": value, "children": 0}


@needs_node
def test_the_release_notes_drawer_loads_on_open_and_closes_three_ways(tmp_path: Path) -> None:
    """The notes are read on the first open, not at start-up, through
    tokdashPath and past the browser cache; a failed read says so and the next
    open tries again; a loaded payload is reused. Each entry is written as text.
    Escape, a click on the backdrop and the close button all close the drawer,
    which hands focus back to its trigger and marks it collapsed. The version
    label shows the running version when the server reports one, and the
    packaged one otherwise."""
    script = release_notes_script(
        body="""
const NOTES = '/base/static/release-notes.json';
const hostile = '<img src=x onerror=alert(1)>';
const payload = { current: '2.6.0', releases: [
  { version: '2.6.0', date: '2026-10-20', sections: [
    { type: 'added', items: [hostile, 'A drawer'] },
    { type: 'fixed', items: ['A fix'] },
  ] },
  { version: '2.5.0', date: 'not a date', sections: [{ type: 'changed', items: ['A change'] }] },
] };
const state = () => ({ open: dialog.open, expanded: toggle.getAttribute('aria-expanded'),
  focused: document.activeElement === toggle });
const versions = () => [text(document.getElementById('releaseNotesVersion')), text(document.querySelector('[data-app-version]'))];

initReleaseNotes();
const atStart = requests.map((request) => request.url);
answer('/app/api/version', { current: '2.6.0' });
await settle();
const packaged = versions();

toggle.click();
const opening = { ...state(), list: text(list), reads: requested(NOTES) };
fail(NOTES);
await settle();
const failed = text(list);
closeButton.focus();
fire(list, 'keydown', { key: 'Enter' });
const afterEnter = dialog.open;
const escape = fire(list, 'keydown', { key: 'Escape' });
const byEscape = { ...state(), prevented: escape.defaultPrevented };

toggle.click();
toggle.click();
const retrying = requested(NOTES).length;
answer(NOTES, payload);
await settle();
const rendered = entries();
const dates = list.querySelectorAll('time').map((node) => [node.dateTime, node.textContent]);
closeButton.focus();
fire(panel, 'click');
const afterPanelClick = dialog.open;
fire(dialog, 'click');
const byBackdrop = state();

toggle.click();
await settle();
const reopened = { entries: entries().length, reads: requested(NOTES).length };
closeButton.focus();
closeButton.click();
const byButton = state();

runtimeVersionPromise = null;
initReleaseNotes();
answer('/app/api/version', { runtime_version: '2.6.1', current: '2.6.0' });
await settle();
print({
  atStart, packaged, opening, failed, afterEnter, byEscape, retrying, rendered, dates,
  afterPanelClick, byBackdrop, reopened, byButton, running: versions(),
  urls: requests.map((request) => request.url), hostile,
  innerHTML: __innerHTMLWrites.length,
  date: new Date('2026-10-20T12:00:00').toLocaleDateString(langLocale(), { year: 'numeric', month: 'short', day: 'numeric' }),
  words: Object.fromEntries(['loading', 'releaseNotesUnavailable', 'added', 'fixed', 'changed'].map((key) => [key, t(key)])),
  // Section labels come from t(section.type), which the parity test cannot see.
  unlabelled: Object.keys(I18N).filter((lang) => ['added', 'changed', 'fixed']
    .some((key) => !Object.prototype.hasOwnProperty.call(I18N[lang], key))),
});
""",
    )
    report = run_page(tmp_path, "release-notes-drawer", script)
    words = report["words"]
    closed = {"open": False, "expanded": "false", "focused": True}

    assert report["atStart"] == ["/app/api/version"], "the notes are read before anyone opens them"
    assert report["packaged"] == ["v2.6.0", "v2.6.0"]
    assert report["opening"] == {
        "open": True,
        "expanded": "true",
        "focused": False,
        "list": words["loading"],
        "reads": [{"cache": "no-store"}],
    }
    assert report["failed"] == words["releaseNotesUnavailable"]
    assert report["afterEnter"] is True
    assert report["byEscape"] == {**closed, "prevented": True}

    assert report["retrying"] == 2, "a failed read is not retried, or a double click reads twice"
    assert report["rendered"] == [
        {
            "version": "v2.6.0",
            "sections": [
                {"label": words["added"], "items": [_text_item(report["hostile"]), _text_item("A drawer")]},
                {"label": words["fixed"], "items": [_text_item("A fix")]},
            ],
        },
        {"version": "v2.5.0", "sections": [{"label": words["changed"], "items": [_text_item("A change")]}]},
    ]
    assert report["unlabelled"] == [], "a language falls back to English section labels"
    assert report["dates"] == [["2026-10-20", report["date"]], ["not a date", "not a date"]]
    assert report["innerHTML"] == 0
    assert report["afterPanelClick"] is True, "a click inside the panel closed the drawer"
    assert report["byBackdrop"] == closed
    assert report["reopened"] == {"entries": 2, "reads": 2}
    assert report["byButton"] == closed

    assert report["running"] == ["v2.6.1", "v2.6.1"]
    assert report["urls"] == [
        "/app/api/version",
        "/base/static/release-notes.json",
        "/base/static/release-notes.json",
        "/app/api/version",
    ]
