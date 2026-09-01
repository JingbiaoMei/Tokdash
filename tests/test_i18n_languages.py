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


@pytest.mark.parametrize("readme", TRANSLATED_READMES)
def test_translated_readmes_cover_zai_quota(readme: str) -> None:
    source = (REPO_ROOT / readme).read_text(encoding="utf-8")
    for marker in ("Z.ai", "--zai-api on", "ZAI_API_KEY", "Z_AI_API_KEY"):
        assert marker in source, f"{readme} is missing {marker}"


@pytest.mark.parametrize("readme", README_FILES)
def test_readmes_link_every_translation(readme: str) -> None:
    source = (REPO_ROOT / readme).read_text(encoding="utf-8")
    for target in README_FILES:
        assert f'href="{target}"' in source, f"{readme} does not link to {target}"


# --- README client-support parity -------------------------------------------
#
# The client support matrix, its pill row and its per-client notes are the same
# content in all six READMEs, and drift between them is invisible to the rest of
# the suite: test_recent_sources_have_readme_pills reads only README.md,
# README_CN.md and SUPPORTED_CLIENTS.md. Two changes in a row drifted anyway —
# one added three clients to the English and 中文 READMEs alone, and one flipped
# two matrix rows to a Sessions tick in every language while updating the prose
# in four of the six, leaving ES and PT contradicting their own table.

# `| Name | ✅ | — |`, bolded or not. Requiring both cells to be a tick or a dash
# is what keeps the unrelated platform/download table out of the match.
CLIENT_ROW = re.compile(r"^\| \*{0,2}([^|*]+?)\*{0,2} \| (✅|—) \| (✅|—) \|$", re.M)

PILL_IMAGE = re.compile(r"/docs/assets/agents/pills/([A-Za-z0-9_-]+\.png)")

# Each language's wording of "this client has no Sessions tab". These are
# counted, not located: the count has to equal English's. That is what catches a
# translation whose matrix row gained a Sessions tick while its prose still said
# the client had none. A reworded translation quietly stops being checked rather
# than failing for the wrong reason, which is the right way for this to age.
NO_SESSIONS_NOTE = {
    "README.md": "does not appear in the Sessions tab",
    "README_CN.md": "不出现在 Sessions 标签页",
    "README_JA.md": "Sessions タブには登場しません",
    "README_KO.md": "Sessions 탭에 나타나지 않습니다",
    "README_ES.md": "no aparece en la pestaña Sesiones",
    "README_PT.md": "não aparece na aba Sessões",
}

LOCALIZED_READMES = README_FILES[1:]


def _readme_text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _client_matrix(source: str) -> dict[str, tuple[str, str]]:
    return {
        m.group(1).strip(): (m.group(2), m.group(3))
        for m in CLIENT_ROW.finditer(source)
    }


@pytest.mark.parametrize("readme", LOCALIZED_READMES)
def test_readme_client_matrix_matches_english(readme: str) -> None:
    english = _client_matrix(_readme_text("README.md"))
    assert len(english) > 20, "client matrix not parsed out of README.md"
    theirs = _client_matrix(_readme_text(readme))
    missing = {k: v for k, v in english.items() if k not in theirs}
    extra = {k: v for k, v in theirs.items() if k not in english}
    differing = {
        k: (v, theirs[k]) for k, v in english.items() if k in theirs and theirs[k] != v
    }
    assert theirs == english, (
        f"{readme} client matrix has drifted from README.md: "
        f"missing={missing} extra={extra} differing_marks={differing}"
    )


@pytest.mark.parametrize("readme", LOCALIZED_READMES)
def test_readme_client_pills_match_english(readme: str) -> None:
    english = set(PILL_IMAGE.findall(_readme_text("README.md")))
    assert len(english) > 20, "no client pills found in README.md"
    theirs = set(PILL_IMAGE.findall(_readme_text(readme)))
    assert theirs == english, (
        f"{readme} pill row has drifted from README.md: "
        f"missing={sorted(english - theirs)} extra={sorted(theirs - english)}"
    )


@pytest.mark.parametrize("readme", LOCALIZED_READMES)
def test_readme_no_sessions_notes_match_english(readme: str) -> None:
    expected = _readme_text("README.md").count(NO_SESSIONS_NOTE["README.md"])
    assert expected, "English 'no Sessions tab' note not found"
    found = _readme_text(readme).count(NO_SESSIONS_NOTE[readme])
    assert found == expected, (
        f"{readme} says {found} clients have no Sessions tab, README.md says "
        f"{expected}. A matrix row and its prose have most likely disagreed."
    )
