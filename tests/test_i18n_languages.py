"""Parity and mechanism tests for the dashboard i18n dictionaries.

The app ships six UI languages (en, zh, ja, ko, es, pt) in a single
`const I18N = {...}` literal in static/index.html. These tests keep the
dictionaries honest: same key set in every language, no dropped or invented
placeholders, and a language picker that offers every supported language.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

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


def _extract_i18n_literal(source: str) -> str:
    start = source.find("const I18N = {")
    assert start != -1, "const I18N = { not found in index.html"
    end = source.find("\n    };", start)
    assert end != -1, "unterminated I18N object in index.html"
    return source[start : end + 6]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_i18n_language_parity(tmp_path: Path) -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    literal = _extract_i18n_literal(source)

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
    output = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, check=True
    ).stdout
    report = json.loads(output)

    assert report["languages"] == list(EXPECTED_LANGUAGES)

    reference = report["keys"]["en"]
    assert reference, "en dictionary is empty"
    for lang in EXPECTED_LANGUAGES:
        assert report["keys"][lang] == reference, (
            f"{lang} key set differs from en"
        )

    referenced = set(
        re.findall(r'data-i18n(?:-placeholder|-title)?="([^"]+)"', source)
    )
    referenced.update(re.findall(r"\bt\('([^']+)'\)", source))
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


def test_language_mechanism_supports_all_languages() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    locales = re.search(r"const LANG_LOCALES = \{([^}]*)\};", source)
    assert locales, "LANG_LOCALES map not found"
    for lang in EXPECTED_LANGUAGES:
        assert f"{lang}: '" in locales.group(1), f"LANG_LOCALES missing {lang}"

    assert "function detectBrowserLang()" in source
    # Storing 'system' resolves to the detected browser language.
    assert "stored === 'system'" in source
    assert "selectedLang === 'system' ? detectBrowserLang()" in source


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


def test_quota_poll_options_use_translated_minute_unit() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    select_start = source.index('<select id="quotaIntervalSelect"')
    select = source[select_start : source.index("</select>", select_start)]
    assert re.findall(r'<option value="(\d+)"', select) == ["15", "30", "60", "120"]
    apply_start = source.index("function applyI18n()")
    apply_body = source[apply_start : source.index("\n    }\n", apply_start)]
    assert "#quotaIntervalSelect option" in apply_body
    assert "${option.value} ${t('minuteShort')}" in apply_body


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
