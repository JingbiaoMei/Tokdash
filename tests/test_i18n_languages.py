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

EXPECTED_LANGUAGES = ("en", "zh", "ja", "ko", "es", "pt")

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
