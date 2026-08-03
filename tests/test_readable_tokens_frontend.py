from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

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


def test_readable_token_switch_markup_and_tooltip_contract() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = "".join(source.split())
    assert 'id="readableTokensToggle"' in source
    assert 'role="switch"' in source
    assert 'aria-checked="true"' in source
    assert 'id="totalTokensWrap"' in source
    assert 'id="totalTokensExact"' in source
    assert 'role="tooltip"' in source
    assert source.index('id="settingsPanel"') < source.index(
        'id="readableTokensToggle"'
    ) < source.index('id="overview-content"')
    assert "#totalTokens:hover+.overview-token-exact-tooltip" in compact
    assert "#totalTokens:focus-visible+.overview-token-exact-tooltip" in compact
    assert source.count("readableTokens: '") == 2


def test_readable_token_render_and_toggle_do_not_refetch() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    renderer = _extract_js_function(
        source,
        "function renderOverviewTokenTotal(value = overviewTotalTokensRaw) {",
    )
    setter = _extract_js_function(
        source,
        "function setOverviewReadableTokens(enabled) {",
    )
    overview = _extract_js_function(source, "function renderOverviewTab(data) {")
    i18n = _extract_js_function(source, "function applyI18n() {")

    assert "overviewTotalTokensRaw = normalizeOverviewTokenCount(value);" in renderer
    assert "formatReadableTokenCount(overviewTotalTokensRaw)" in renderer
    assert "formatNumber(overviewTotalTokensRaw)" in renderer
    assert "totalTokensExact" in renderer
    assert "aria-describedby" in renderer
    assert "saveOverviewReadableTokensPreference" in setter
    assert "renderOverviewTokenTotal();" in setter
    assert "fetch(" not in setter
    assert "updateDashboard" not in setter
    assert "renderOverviewTokenTotal(data.total_tokens);" in overview
    assert "renderOverviewTokenTotal();" in i18n


def test_readable_token_scope_preserves_other_token_views() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert source.count("formatReadableTokenCount(") == 2
    assert (
        "document.getElementById('totalTokens').textContent = "
        "formatNumber(data.total_tokens);"
    ) not in source
    for existing in (
        "document.getElementById('statTotalTokens').textContent = formatNumber",
        "document.getElementById('monthTotalTokens').textContent = formatNumber",
        'document.getElementById("sessionModalTotal").textContent = formatNumber',
        "formatProfileMetricNumber(summary.recordedTokens",
    ):
        assert existing in source


def test_settings_panel_groups_display_and_install_controls() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    panel_start = source.index('id="settingsPanel"')
    overview_start = source.index('id="overview-content"')

    assert source.index('id="settingsToggle"') < panel_start
    assert 'aria-expanded="false"' in source
    assert 'aria-controls="settingsPanel"' in source
    assert 'aria-labelledby="settingsPanelTitle"' in source
    for control_id in (
        "langToggle",
        "themeToggle",
        "styleThemeSelect",
        "readableTokensToggle",
        "installBtn",
    ):
        assert panel_start < source.index(f'id="{control_id}"') < overview_start

    assert "function setSettingsPanelOpen(open, returnFocus = false)" in source
    assert "panel.getBoundingClientRect()" in source
    assert "window.innerWidth - gutter" in source
    assert "event.key === 'Escape'" in source
    assert "!settingsMenu.contains(event.target)" in source
    assert source.count("settings: '") == 2
    assert source.count("colorMode: '") == 2
