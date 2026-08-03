from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"


def _extract_js_function(source: str, signature: str) -> str:
    start = source.find(signature)
    assert start != -1, f"{signature} not found in index.html"
    depth = 0
    body_start = (
        start + len(signature) - 1
        if signature.endswith("{")
        else source.find("{", start)
    )
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


def _run_readable_token_js(
    tmp_path: Path, expression: str, payload: object
) -> object:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, signature)
        for signature in (
            "function normalizeOverviewTokenCount(value) {",
            "function formatReadableTokenCount(value) {",
            "function loadOverviewReadableTokensPreference(storage = null) {",
            "function saveOverviewReadableTokensPreference(enabled, storage = null) {",
        )
    )
    key_match = (
        "const OVERVIEW_READABLE_TOKENS_STORAGE_KEY = "
        "'tokdash-overview-readable-tokens';"
    )
    assert key_match in source
    harness = tmp_path / "readable-tokens.js"
    harness.write_text(
        "let currentLang = 'en';\n"
        "const LABELS = { tokensUnit: 'tokens' };\n"
        "function t(key) { return LABELS[key] || key; }\n"
        "function formatNumber(value) { return Number(value || 0).toLocaleString('en-US'); }\n"
        + key_match
        + "\n"
        + functions
        + "\nconst payload = JSON.parse(process.argv[2]);\n"
        + f"const result = {expression};\n"
        + "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(payload)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_readable_token_formatter_boundaries(tmp_path: Path) -> None:
    cases = {
        0: "0 tokens",
        -1: "0 tokens",
        842_315: "842,315 tokens",
        999_999: "999,999 tokens",
        1_000_000: "1M tokens",
        1_049_999: "1M tokens",
        1_050_000: "1.1M tokens",
        482_563_219: "482.6M tokens",
        999_999_999: "1,000M tokens",
        1_000_000_000: "1B tokens",
        1_249_000_000: "1.2B tokens",
    }
    result = _run_readable_token_js(
        tmp_path,
        "Object.fromEntries(payload.map(value => "
        "[String(value), formatReadableTokenCount(value)]))",
        list(cases),
    )
    assert result == {str(key): value for key, value in cases.items()}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_readable_token_preference_defaults_and_fails_soft(tmp_path: Path) -> None:
    expression = """
(() => {
  const writes = [];
  const missing = {
    getItem: () => null,
    setItem: (key, value) => writes.push([key, value]),
  };
  const disabled = {
    getItem: () => '0',
    setItem: (key, value) => writes.push([key, value]),
  };
  const broken = {
    getItem: () => { throw new Error('blocked'); },
    setItem: () => { throw new Error('blocked'); },
  };
  saveOverviewReadableTokensPreference(false, missing);
  saveOverviewReadableTokensPreference(true, missing);
  saveOverviewReadableTokensPreference(true, broken);
  return {
    missing: loadOverviewReadableTokensPreference(missing),
    disabled: loadOverviewReadableTokensPreference(disabled),
    broken: loadOverviewReadableTokensPreference(broken),
    writes,
  };
})()
"""
    assert _run_readable_token_js(tmp_path, expression, None) == {
        "missing": True,
        "disabled": False,
        "broken": True,
        "writes": [
            ["tokdash-overview-readable-tokens", "0"],
            ["tokdash-overview-readable-tokens", "1"],
        ],
    }
