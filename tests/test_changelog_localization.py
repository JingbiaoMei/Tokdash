"""Guards for the two-language changelog.

English is canonical: `docs/development/CHANGELOG.md` is the full record and
`src/tokdash/static/release-notes.json` the in-app What's new view. Chinese is the only
translation shipped today, and it has two halves that both have to line up with English:
the shipped sidecar the dashboard overlays, and the docs page a GitHub Release links.

Every test here is a drift detector, not a behaviour test: the app renders English when a
translation is missing, so a stale Chinese file never breaks a user. It does make the
Chinese view lie about the newest release, which is what these catch.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "tokdash" / "static"
INDEX_HTML = STATIC_DIR / "index.html"
RELEASE_NOTES_EN = STATIC_DIR / "release-notes.json"
RELEASE_NOTES_ZH = STATIC_DIR / "release-notes.zh.json"
CHANGELOG_EN = REPO_ROOT / "docs" / "development" / "CHANGELOG.md"
CHANGELOG_CN = REPO_ROOT / "docs" / "development" / "CHANGELOG_CN.md"

# Translating a language is a standing release obligation, so it stays deliberate: widen
# this set only together with the release checklist in docs/development/RELEASING.md.
SUPPORTED_SIDECAR_LANGS = {"zh"}
MIN_TRANSLATED_RELEASES = 10

CJK = re.compile(r"[\u4e00-\u9fff]")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def english_payload() -> dict:
    return read_json(RELEASE_NOTES_EN)


def chinese_entries() -> dict:
    return read_json(RELEASE_NOTES_ZH)["releases"]


def load_script(name: str):
    """Import a file from scripts/ the way the release checklist runs it."""
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *argv],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        # The release body carries CJK once a version is translated; the Windows
        # locale codec would mangle (or refuse) the child's UTF-8 output.
        encoding="utf-8",
    )


def test_chinese_sidecar_aligns_with_the_english_list_item_by_item() -> None:
    releases = {release["version"]: release for release in english_payload()["releases"]}
    translations = chinese_entries()

    for version, sections in translations.items():
        assert version in releases, f"{version} is translated but not in the English notes"
        english = releases[version]
        english_types = [section["type"] for section in english["sections"]]
        assert list(sections) == english_types, f"{version}: section types/order differ"

        for section in english["sections"]:
            translated = sections[section["type"]]
            assert len(translated) == len(section["items"]), (
                f"{version}/{section['type']}: the renderer pairs entries by index, so a "
                "different item count would drop an entry into the wrong bullet"
            )
            for item in translated:
                assert isinstance(item, str) and item.strip(), f"{version}: empty entry"
                # An entry that is still English is an untranslated entry, not a Chinese one.
                assert CJK.search(item), f"{version}: entry carries no Chinese text: {item[:60]}"


def test_newest_release_is_always_translated() -> None:
    # The in-app What's new opens on the current release. An English entry there is the
    # Chinese view failing at the one moment everyone looks at it.
    payload = english_payload()
    assert payload["current"] in chinese_entries(), (
        f"{payload['current']} is missing from release-notes.zh.json; add its Chinese "
        "entries as part of the release"
    )


def test_translation_window_stays_deep_enough_to_read() -> None:
    translated = len(chinese_entries())
    assert translated >= MIN_TRANSLATED_RELEASES, (
        f"only {translated} releases translated; backfill so the Chinese view does not "
        "turn English after a couple of entries"
    )


def test_only_deliberate_locales_get_a_sidecar() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    mapping = re.search(r"const RELEASE_NOTES_SIDECARS = \{(.*?)\};", source, re.DOTALL)
    assert mapping, "RELEASE_NOTES_SIDECARS is missing from the dashboard"

    entries = dict(re.findall(r"(\w+):\s*'([^']+)'", mapping.group(1)))
    assert set(entries) == SUPPORTED_SIDECAR_LANGS

    for lang, path in entries.items():
        assert (STATIC_DIR / path.rsplit("/static/", 1)[-1]).exists(), f"{lang} sidecar missing"
        # Every sidecar language must be a dashboard locale, or nothing can select it.
        assert f"\n      {lang}: {{\n" in source, (
            f"{lang} has a changelog sidecar but no dashboard locale"
        )


def test_untranslated_entries_fall_back_to_english_in_the_dashboard() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    renderer = source[source.index("function renderReleaseNotes(payload) {"):]
    renderer = renderer[:renderer.index("\n    async function loadReleaseNotes")]

    # English is the default and the repair path, never a blank row.
    assert "englishItem" in renderer
    assert "typeof translated === 'string' && translated.trim()" in renderer
    assert ": englishItem;" in renderer
    assert "itemElement.textContent = item;" in renderer

    loader = source[source.index("function loadReleaseNotesTranslation(lang = currentLang) {"):]
    loader = loader[:loader.index("\n    async function openReleaseNotes")]
    assert "cache: 'no-store'" in loader
    # A failed sidecar fetch must not surface an error: the English payload already shows.
    assert "return null;" in loader
    # ...and must not be cached as an empty map, which would read as "translated" and skip
    # every later retry, stranding the locale in English until a page reload.
    assert "releaseNotesTranslationsByLang.set(lang, {})" not in loader
    assert loader.count("releaseNotesTranslationsByLang.set(lang,") == 1

    # The English payload paints as soon as it lands, so a sidecar that arrives second has
    # to repaint the drawer that is already open. Awaiting both is not enough on its own.
    opener = source[source.index("async function openReleaseNotes() {"):]
    opener = opener[:opener.index("\n    function closeReleaseNotes")]
    assert "Promise.all([loadReleaseNotes(), loadReleaseNotesTranslation()])" in opener
    assert "if (releaseNotesPayload) renderReleaseNotes(releaseNotesPayload);" in opener

    # The footer's full-changelog link follows the same language rule as the entries.
    assert "CHANGELOG_DOCS_URLS" in source
    assert "CHANGELOG_CN.md" in source
    assert "CHANGELOG_DOCS_URLS[currentLang] || CHANGELOG_DOCS_URLS.en" in source


def test_chinese_docs_page_is_generated_and_current() -> None:
    result = run_script(["scripts/changelog_cn.py", "--check"])
    assert result.returncode == 0, result.stderr.strip()


def test_chinese_docs_page_covers_exactly_the_translated_versions() -> None:
    page = CHANGELOG_CN.read_text(encoding="utf-8")
    headings = re.findall(r"^## (\d+\.\d+\.\d+) - (\d{4}-\d{2}-\d{2})$", page, re.MULTILINE)
    documented = {version for version, _ in headings}
    assert documented == set(chinese_entries()), "page and sidecar cover different releases"

    dates = {entry["version"]: entry["date"] for entry in english_payload()["releases"]}
    for version, date in headings:
        assert dates.get(version) == date, f"{version}: date differs from the English changelog"


def test_release_body_carries_one_release_section_only() -> None:
    # A body that pasted the whole history shipped once: the scan stopped at the next
    # `## ` heading but the slice did not, so every release under the current one came
    # along. The heading count is the cheapest check that catches the whole class.
    section_for = load_script("release_body").section_for
    changelog = CHANGELOG_EN.read_text(encoding="utf-8")
    releases = english_payload()["releases"]

    for entry in releases[:3]:
        version = entry["version"]
        older = next(r["version"] for r in releases if r["version"] != version)
        body = section_for(version, changelog)
        headings = [line for line in body.splitlines() if line.startswith("## ")]
        assert len(headings) == 1, f"{version}: body carries {len(headings)} release sections"
        assert headings[0].startswith(f"## {version} - ")
        assert f"## {older} - " not in body, f"{version}: body leaked the {older} section"


def test_pr_refs_names_prs_and_not_issues() -> None:
    # `closes #41` is an issue and a `#42` in prose is not a ref; labeling either
    # 相关 PR on the Chinese page is a small lie that outlives the release.
    pr_refs = load_script("changelog_cn").pr_refs
    assert pr_refs("- Cline falls back to the session record. (#40)") == ["40"]
    assert pr_refs("- Opt-in Z.ai tracking. (#48, thanks @Werkaninchen)") == ["48"]
    assert pr_refs("- Retirement keyed on the directory. (#42, closes #41, thanks @handle)") == ["42"]
    assert pr_refs("- Two commits did it. (#98, #99)") == ["98", "99"]
    assert pr_refs("- Noted in #12 upstream, no trailing group") == []
    assert pr_refs("- Plain prose about the quota card") == []


def test_release_body_links_the_chinese_section_for_the_current_version() -> None:
    version = english_payload()["current"]
    result = run_script(["scripts/release_body.py", "--version", version, "--stdout"])
    assert result.returncode == 0, result.stderr.strip()

    body = result.stdout
    assert body.startswith("**简体中文：** [查看本版本的中文更新日志]("), body[:120]
    assert [line for line in body.splitlines() if line.startswith("## ")] == [
        next(line for line in body.splitlines() if line.startswith("## "))
    ], "release body must hold exactly one section"
    link = re.search(r"\]\(([^)]+)\)", body.splitlines()[0]).group(1)
    page_url, _, anchor = link.partition("#")
    assert page_url.endswith("docs/development/CHANGELOG_CN.md")

    # The anchor must resolve to the section that actually exists on the Chinese page.
    heading = next(
        line
        for line in CHANGELOG_CN.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"## {version} - ")
    )
    slug = re.sub(r"[^a-z0-9 -]", "", heading[len("## "):].lower()).replace(" ", "-")
    assert anchor == slug, f"{anchor} does not resolve to the {version} section"
