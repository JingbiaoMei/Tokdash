"""Parity and mechanism tests for the dashboard i18n dictionaries.

The app ships six UI languages (en, zh, ja, ko, es, pt) in a single
`const I18N = {...}` literal in static/index.html. These tests keep the
dictionaries honest: same key set in every language, no dropped or invented
placeholders, and a language picker that offers every supported language. The
mechanism runs under node: the browser locale and the stored choice pick the
language, and applyI18n relabels what data-i18n cannot express.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

from support.frontend import extract_i18n_literal, needs_node, run_node
from support.i18n_profile import (
    APPLY_I18N_FUNCTIONS,
    APPLY_I18N_STATE,
    page_script,
    run_page,
    slice_statement,
)


INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"
REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_LANGUAGES = ("en", "zh", "ja", "ko", "es", "pt")
README_FILES = (
    "README.md",
    "README_CN.md",
    "README_JA.md",
    "README_KO.md",
    "README_ES.md",
    "README_PT.md",
)
TRANSLATED_READMES = (
    "README_JA.md",
    "README_KO.md",
    "README_ES.md",
    "README_PT.md",
)

# The plural suffix placeholder is language-specific (CJK languages have no
# plural), so translations may omit it; every other placeholder is mandatory.
OPTIONAL_PLACEHOLDERS = {"{s}"}


@needs_node
def test_i18n_language_parity(tmp_path: Path) -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    literal = extract_i18n_literal(source)

    blocks = dict(
        re.findall(r"^      ([a-z]+): \{\n(.*?)^      \},?$", literal, re.M | re.S)
    )
    assert tuple(blocks) == EXPECTED_LANGUAGES
    for lang, block in blocks.items():
        raw_keys = re.findall(r"^        ([a-zA-Z0-9_]+):", block, re.M)
        duplicates = sorted({key for key in raw_keys if raw_keys.count(key) > 1})
        assert not duplicates, f"{lang} has duplicate keys: {duplicates}"

    harness = tmp_path / "i18n_report.js"
    prelude = (
        "function reportI18n(I18N) {\n"
        "  const report = { languages: [], keys: {}, placeholders: {}, empty: [] };\n"
        "  const known = new Set();\n"
        "  for (const [k, v] of Object.entries(I18N.en)) {\n"
        "    for (const p of String(v).match(/\\{[a-z]+\\}/g) || []) known.add(p);\n"
        "  }\n"
        "  report.languages = Object.keys(I18N);\n"
        "  for (const lang of report.languages) {\n"
        "    report.keys[lang] = Object.keys(I18N[lang]).sort();\n"
        "    report.placeholders[lang] = {};\n"
        "    for (const [key, value] of Object.entries(I18N[lang])) {\n"
        "      const ph = String(value).match(/\\{[a-z]+\\}/g) || [];\n"
        "      if (ph.length) report.placeholders[lang][key] = ph.sort();\n"
        "      for (const p of ph) if (!known.has(p)) report.empty.push([lang, key, p]);\n"
        "    }\n"
        "  }\n"
        "  return report;\n"
        "}\n"
    )
    # `literal` is the full `const I18N = {...};` statement.
    harness.write_text(
        prelude + literal + "\nprocess.stdout.write(JSON.stringify(reportI18n(I18N)));\n",
        encoding="utf-8",
    )
    report = json.loads(run_node(harness))

    assert report["languages"] == list(EXPECTED_LANGUAGES)

    reference = report["keys"]["en"]
    assert reference, "en dictionary is empty"
    for lang in EXPECTED_LANGUAGES:
        assert report["keys"][lang] == reference, (
            f"{lang} key set differs from en"
        )

    # Every attribute form the dashboard uses, so a typo in a data-i18n-aria
    # key fails here instead of reaching a screen reader as a raw key.
    referenced = set(
        re.findall(r'data-i18n(?:-placeholder|-title|-aria)?="([^"]+)"', source)
    )
    # t('key') and t("key") alike, with or without placeholder values after the
    # key: the page uses both quote styles, and t('key', {...}) is how every
    # counted or named label is looked up.
    referenced.update(
        key for _, key in re.findall(r"""\bt\((['"])([^'"]+)\1\s*[,)]""", source)
    )
    missing = referenced - set(reference)
    assert not missing, f"referenced i18n keys are undefined: {sorted(missing)}"

    # Unknown placeholder name anywhere: typo in a translation.
    assert not report["empty"], f"unknown placeholders: {report['empty']}"

    # Every mandatory placeholder present in the en value of a key must be
    # present in the same key's translation (the plural suffix is exempt).
    en_ph = report["placeholders"]["en"]
    for lang in EXPECTED_LANGUAGES:
        lang_ph = report["placeholders"][lang]
        for key, placeholders in en_ph.items():
            missing = (
                set(placeholders) - set(lang_ph.get(key, [])) - OPTIONAL_PLACEHOLDERS
            )
            assert not missing, f"{lang} key {key!r} is missing {sorted(missing)}"


def test_language_select_offers_every_language() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    select_start = source.index('<select id="langToggle"')
    select_end = source.index("</select>", select_start)
    select = source[select_start : select_end]
    options = re.findall(r'<option value="([^"]+)"', select)
    assert options == ["system", *EXPECTED_LANGUAGES]


@needs_node
def test_the_browser_language_picks_its_dictionary(tmp_path: Path) -> None:
    """A browser locale opens the dashboard in its own language, an unsupported
    one in English, and choosing "system" in the picker follows the browser
    again rather than storing the word "system" as a language."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    languages = source[
        source.index("const LANG_LOCALES = {") : source.index("let currentLang = (function() {")
    ]
    boot = (
        "function initialLanguage() {\n"
        + slice_statement(source, "let currentLang = (function() {", "})();")
        + "\n"
        + slice_statement(source, "let selectedLang = (function() {", "})();")
        + "\nreturn { currentLang, selectedLang };\n}\n"
    )
    picker = slice_statement(
        source, "const langSelectEl = document.getElementById('langToggle');", "\n    });"
    )
    script = page_script(
        snippets=[extract_i18n_literal(source) + ";", languages, boot, picker],
        prelude=(
            "document.body.append(el('select', { id: 'langToggle' }));\n"
            "let currentLang = 'en';\n"
            "let selectedLang = 'system';\n"
        ),
        body="""
const detected = {};
for (const browser of ['ja-JP', 'pt-BR', 'fr-FR', 'zh-TW', 'ko-KR', 'es-419', 'en-GB']) {
  navigator.language = browser;
  detected[browser] = detectBrowserLang();
}
navigator.language = 'ko-KR';
const booted = {};
for (const stored of [null, 'system', 'ja', 'xx']) {
  if (stored === null) localStorage.removeItem('tokdash-lang');
  else localStorage.setItem('tokdash-lang', stored);
  booted[String(stored)] = initialLanguage();
}
const picker = document.getElementById('langToggle');
const picked = {};
for (const choice of ['system', 'pt']) {
  picker.value = choice;
  fire(picker, 'change');
  picked[choice] = { currentLang, selectedLang, stored: localStorage.getItem('tokdash-lang') };
}
print({ detected, booted, picked, repainted: calls('applyI18n').length, rerendered: calls('rerenderPresentation').length });
""",
    )
    report = run_page(tmp_path, "browser-language", script)

    assert report["detected"] == {
        "ja-JP": "ja",
        "pt-BR": "pt",
        "fr-FR": "en",
        "zh-TW": "zh",
        "ko-KR": "ko",
        "es-419": "es",
        "en-GB": "en",
    }
    assert report["booted"] == {
        "null": {"currentLang": "ko", "selectedLang": "system"},
        "system": {"currentLang": "ko", "selectedLang": "system"},
        "ja": {"currentLang": "ja", "selectedLang": "ja"},
        "xx": {"currentLang": "ko", "selectedLang": "system"},
    }
    assert report["picked"] == {
        "system": {"currentLang": "ko", "selectedLang": "system", "stored": "system"},
        "pt": {"currentLang": "pt", "selectedLang": "pt", "stored": "pt"},
    }
    assert report["repainted"] == 2
    assert report["rerendered"] == 2


def test_hit_rate_headers_carry_translated_hint() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    # Static headers take the hint through data-i18n-title; the Apps & Models
    # sub-tables are rebuilt per render and set it with t() directly.
    headers = re.findall(r'<th\b[^>]*data-sortable="cache_hit_rate"[^>]*>', source)
    assert len(headers) > 20, "Hit % headers not found"
    bare = [h for h in headers if "cacheHitRateHint" not in h]
    assert not bare, f"{len(bare)} Hit % headers lack the translated hint"
    # A hardcoded English title stays English in every language.
    assert 'title="Cache hit rate' not in source


def test_quota_poll_options_are_the_four_intervals() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    select_start = source.index('<select id="quotaIntervalSelect"')
    select = source[select_start : source.index("</select>", select_start)]
    assert re.findall(r'<option value="(\d+)"', select) == ["15", "30", "60", "120"]


@needs_node
def test_a_language_switch_relabels_the_poll_interval_options(tmp_path: Path) -> None:
    """The options pair a number with the minute unit, which data-i18n cannot
    express, so applyI18n writes them itself in the current language."""
    script = page_script(
        APPLY_I18N_FUNCTIONS,
        prelude=APPLY_I18N_STATE
        + """
document.body.append(el('select', { id: 'quotaIntervalSelect' },
  ...['15', '30', '60', '120'].map((value) => {
    const option = el('option', {}, `${value} min`);
    option.value = value;
    return option;
  })));
""",
        i18n=True,
        body="""
const out = {};
for (const lang of Object.keys(I18N)) {
  currentLang = lang;
  applyI18n();
  out[lang] = {
    labels: document.querySelectorAll('#quotaIntervalSelect option').map((option) => option.textContent),
    unit: I18N[lang].minuteShort,
  };
}
print(out);
""",
    )
    report = run_page(tmp_path, "poll-interval-options", script)
    assert tuple(report) == EXPECTED_LANGUAGES
    for lang, row in report.items():
        assert row["labels"] == [f"{value} {row['unit']}" for value in ("15", "30", "60", "120")], lang
    assert report["zh"]["labels"][0] == "15 分钟"


@pytest.mark.parametrize("readme", TRANSLATED_READMES)
def test_translated_readmes_cover_zai_quota(readme: str) -> None:
    # The per-provider quota commands and env keys moved to
    # docs/reference/QUOTA.md; what stays in every README is the Z.ai name
    # (pill alt and quota sentence) and the link to the quota internals doc.
    source = (REPO_ROOT / readme).read_text(encoding="utf-8")
    for marker in ("Z.ai", "docs/reference/QUOTA.md"):
        assert marker in source, f"{readme} is missing {marker}"


@pytest.mark.parametrize("readme", README_FILES)
def test_readmes_link_every_translation(readme: str) -> None:
    source = (REPO_ROOT / readme).read_text(encoding="utf-8")
    for target in README_FILES:
        assert f'href="{target}"' in source, f"{readme} does not link to {target}"


LOCALIZED_READMES = README_FILES[1:]

# --- README structural parity -----------------------------------------------
#
# The six READMEs are one document in six languages. The client support matrix
# and its per-client notes used to live here and had their own parity test; they
# now live in one place (docs/reference/SUPPORTED_CLIENTS.md), so six-way drift
# on them is impossible. What every language still duplicates is the marketing
# structure itself — the four-views lead, the gallery, the folded details blocks
# — and drift there is invisible to the rest of the suite, so this pins it:
# identical heading levels, block counts, links, and image sets, with every
# table-of-contents anchor resolving under GitHub's slug rules.


def _readme_text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _heading_levels(source: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^(#{1,6}) .*$", source, re.M)]


def _block_counts(source: str) -> dict[str, int]:
    return {
        "fences": source.count("```") // 2,
        "details": len(re.findall(r"<details", source)),
        "tables": len(re.findall(r"<table[ >]", source)),
        "pictures": len(re.findall(r"<picture", source)),
        "imgs": len(re.findall(r"<img\b", source)),
        "callouts": len(
            re.findall(r"^> \[!(?:NOTE|WARNING|TIP|IMPORTANT)\]", source, re.M)
        ),
    }


def _github_slug(heading: str) -> str:
    out = []
    for ch in heading.lower():
        if ch in " ":
            out.append("-")
        elif ch in "-_":
            out.append(ch)
        elif unicodedata.category(ch)[0] in ("L", "N", "M"):
            out.append(ch)
    return "".join(out)


# The WebUI gallery swaps to -cn- captures in README_CN.md only; after
# normalising those URLs both sides must carry the identical image set.
CN_GALLERY = re.compile(r"demo-(overview|report|quota|servers)-cn-")

PILL_IMAGE = re.compile(r"/docs/assets/agents/pills/([A-Za-z0-9_-]+\.png)")


def _link_urls(source: str) -> set[str]:
    urls = re.findall(r"https?://[^\s\")<>`]+", source)
    return {
        CN_GALLERY.sub(r"demo-\1-en-", re.sub(r"[^\w/&=#%?~.-]+$", "", u))
        for u in urls
    }


@pytest.mark.parametrize("readme", LOCALIZED_READMES)
def test_readme_structure_matches_english(readme: str) -> None:
    english = _readme_text("README.md")
    theirs = _readme_text(readme)

    assert _heading_levels(theirs) == _heading_levels(english), (
        f"{readme} heading levels differ from README.md"
    )
    mine, ref = _block_counts(theirs), _block_counts(english)
    assert mine == ref, (
        f"{readme} block counts differ: {mine} vs README.md {ref}"
    )

    slugs = {
        _github_slug(m.group(2)) for m in re.finditer(r"^(#{1,6}) (.*)$", theirs, re.M)
    }
    broken = [a for a in re.findall(r"\]\(#([^)]+)\)", theirs) if a not in slugs]
    assert not broken, f"{readme} has anchors that resolve to no heading: {broken}"

    en_urls, their_urls = _link_urls(english), _link_urls(theirs)
    assert their_urls == en_urls, (
        f"{readme} link/image URLs drifted from README.md: "
        f"missing={sorted(en_urls - their_urls)[:5]} "
        f"extra={sorted(their_urls - en_urls)[:5]}"
    )


@pytest.mark.parametrize("readme", LOCALIZED_READMES)
def test_readme_client_pills_match_english(readme: str) -> None:
    source = _readme_text("README.md")
    english = set(PILL_IMAGE.findall(source))
    assert len(english) > 20, "no client pills found in README.md"
    theirs = set(PILL_IMAGE.findall(_readme_text(readme)))
    assert theirs == english, (
        f"{readme} pill row has drifted from README.md: "
        f"missing={sorted(english - theirs)} extra={sorted(theirs - english)}"
    )
