from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import tokdash  # type: ignore[import-untyped]

STATIC_DIR = Path(tokdash.__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
ICON_DIR = STATIC_DIR / "icons" / "agents"


def _extract_js_function(source: str, signature: str) -> str:
    start = source.find(signature)
    assert start != -1, f"{signature} not found in index.html"
    body_start = source.find("{", start)
    depth = 0
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


def test_supported_tool_brand_icons_are_local_and_small() -> None:
    expected = {
        "amp.svg",
        "antigravity.png",
        "claude.svg",
        "codex.png",
        "copilot.svg",
        "cursor.svg",
        "dsh.svg",
        "gemini.svg",
        "grok.png",
        "hermes.png",
        "kimi.png",
        "mimo.svg",
        "openclaw.png",
        "opencode.png",
        "pi.png",
    }
    actual = {path.name for path in ICON_DIR.glob("*") if path.is_file()}
    assert expected <= actual
    assert sum((ICON_DIR / name).stat().st_size for name in expected) < 100_000


def test_tool_brand_registry_uses_local_lazy_assets_with_a_fallback() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    registry = re.search(
        r"const TOOL_BRAND_META = Object\.freeze\(\{(?P<body>.*?)\n\s*\}\);",
        source,
        re.DOTALL,
    )
    assert registry, "tool brand registry not found"
    body = registry.group("body")
    for tool in ("codex", "claude", "gemini_cli", "cursor", "amp", "mimo"):
        assert re.search(rf"\b{tool}:\s*\{{", body)
    assert "https://" not in body
    assert "/static/icons/agents/" in body
    asset_paths = re.findall(r"icon:\s*'(/static/icons/agents/[^']+)'", body)
    assert len(asset_paths) == 15
    for asset_path in asset_paths:
        assert (STATIC_DIR / asset_path.removeprefix("/static/")).is_file()
    assert "function createToolIdentity(tool, options = {}) {" in source
    assert "identity.setAttribute('aria-label', formatToolName(tool));" in source
    assert "image.loading = 'lazy';" in source
    assert "image.decoding = 'async';" in source
    assert "fallback.textContent = meta.fallback;" in source
    assert "const toolBrandIconPromises = new Map();" in source
    assert "function loadToolBrandIcon(iconPath) {" in source
    assert "toolBrandIconPromises.set(iconUrl, request);" in source
    assert "loadToolBrandIcon(meta.icon).then((iconUrl) => {" in source
    assert "codex-transparent.png" in body
    assert "grok-transparent.png" in body
    for name in ("codex-transparent.png", "grok-transparent.png"):
        data = (ICON_DIR / name).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert data[25] in {4, 6}, f"{name} must carry an alpha channel"


def test_tool_identity_is_used_in_primary_tool_breakdowns() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert "toolCell.appendChild(createToolIdentity(row.tool));" in source
    assert "title.appendChild(createToolIdentity(appName, { compact: true }));" in source
    assert "toolCell.appendChild(createToolIdentity(session.tool, { compact: true }));" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_tool_brand_icon_requests_are_shared_per_asset(tmp_path: Path) -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    function = _extract_js_function(source, "function loadToolBrandIcon(iconPath) {")
    harness = tmp_path / "tool-brand-icon-cache.js"
    harness.write_text(
        """
let fetchCalls = 0;
const toolBrandIconPromises = new Map();
function appAssetWithBase(path) { return `/tokdash${path}`; }
async function fetch() {
  fetchCalls += 1;
  return { ok: true, status: 200, blob: async () => ({}) };
}
const URL = { createObjectURL: () => 'blob:shared-icon' };
"""
        + function
        + """
Promise.all([
  loadToolBrandIcon('/static/icons/agents/codex-transparent.png'),
  loadToolBrandIcon('/static/icons/agents/codex-transparent.png'),
]).then((urls) => {
  process.stdout.write(JSON.stringify({ fetchCalls, urls }));
});
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.stdout == (
        '{"fetchCalls":1,"urls":["blob:shared-icon","blob:shared-icon"]}'
    )


def test_tool_icons_are_unboxed_and_usage_chart_has_an_icon_legend() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)

    assert ".tool-brand-icon{" in compact
    assert "border:0;background:transparent;box-shadow:none" in compact
    assert 'data-dark-invert="true"' in compact
    assert "identity.dataset.darkInvert = String(Boolean(meta.darkInvert));" in source
    assert 'id="toolChartLegend"' in source
    assert "function renderToolChartLegend(entries, colors) {" in source
    assert "const visibleEntries = entries.slice(0, 6);" in source
    assert "const hiddenCount = entries.length - visibleEntries.length;" in source
    assert "`+${hiddenCount} more tools`" in source
    assert "`另有 ${hiddenCount} 个工具`" in source
    assert ".tool-chart-legend-more{" in compact
    assert "createToolIdentity(tool, { compact: true })" in source
    assert "renderToolChartLegend(entries, colors);" in source
    assert "legend: { display: false }" in source
