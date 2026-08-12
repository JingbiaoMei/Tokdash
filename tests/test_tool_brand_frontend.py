from __future__ import annotations

import re
from pathlib import Path

import tokdash  # type: ignore[import-untyped]

STATIC_DIR = Path(tokdash.__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
ICON_DIR = STATIC_DIR / "icons" / "agents"


def test_supported_tool_brand_icons_are_local_and_small() -> None:
    expected = {
        "amp.svg",
        "antigravity.png",
        "claude.svg",
        "codex.png",
        "copilot.svg",
        "cursor.svg",
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
    assert len(asset_paths) == 14
    for asset_path in asset_paths:
        assert (STATIC_DIR / asset_path.removeprefix("/static/")).is_file()
    assert "function createToolIdentity(tool, options = {}) {" in source
    assert "image.loading = 'lazy';" in source
    assert "image.decoding = 'async';" in source
    assert "fallback.textContent = meta.fallback;" in source


def test_tool_identity_is_used_in_primary_tool_breakdowns() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert "toolCell.appendChild(createToolIdentity(row.tool));" in source
    assert "title.appendChild(createToolIdentity(appName, { compact: true }));" in source
    assert "toolCell.appendChild(createToolIdentity(session.tool, { compact: true }));" in source
