from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import tokdash  # type: ignore[import-untyped]

STATIC_DIR = Path(tokdash.__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
ICON_DIR = STATIC_DIR / "icons" / "agents"
PROJECT_ROOT = STATIC_DIR.parents[2]
DOCS_PILL_DIR = PROJECT_ROOT / "docs" / "assets" / "agents" / "pills"


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
        "cline.png",
        "codex.png",
        "copilot.svg",
        "crush.png",
        "cursor.svg",
        "dsh.svg",
        "gemini.svg",
        "grok.png",
        "hermes.png",
        "kimi.png",
        "kilocode.png",
        "mimo.svg",
        "minimax.png",
        "openclaw.png",
        "opencode.png",
        "omp.png",
        "pi.png",
        "reasonix.svg",
        "qoder.png",
        "qwen_code.svg",
        "workbuddy.png",
        "zcode.png",
        "zed.svg",
    }
    actual = {path.name for path in ICON_DIR.glob("*") if path.is_file()}
    assert expected <= actual
    # Budget for the 25 supported marks; raises the ceiling when a new
    # source lands, not on incidental growth of an existing icon.
    assert sum((ICON_DIR / name).stat().st_size for name in expected) < 110_000


def test_recent_sources_have_readme_pills() -> None:
    expected = {
        "WorkBuddy": "workbuddy.png",
        "Qoder IDE": "qoder-ide.png",
        "Qoder CLI": "qoder-cli.png",
        "omp": "omp.png",
        "Kilo Code": "kilocode.png",
        "Cline": "cline.png",
        "Zed": "zed.png",
        "Qwen Code": "qwen-code.png",
        "Crush": "crush.png",
        "MiniMax Code": "minimax.png",
    }
    for document in (
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "README_CN.md",
        PROJECT_ROOT / "docs" / "reference" / "SUPPORTED_CLIENTS.md",
    ):
        source = document.read_text(encoding="utf-8")
        for label, filename in expected.items():
            assert f'title="{label}"' in source
            assert f'/docs/assets/agents/pills/{filename}' in source
    for filename in expected.values():
        pill = DOCS_PILL_DIR / filename
        assert pill.is_file()
        assert pill.stat().st_size < 20_000


def test_tool_brand_registry_uses_local_lazy_assets_with_a_fallback() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    registry = re.search(
        r"const TOOL_BRAND_META = Object\.freeze\(\{(?P<body>.*?)\n\s*\}\);",
        source,
        re.DOTALL,
    )
    assert registry, "tool brand registry not found"
    body = registry.group("body")
    for tool in (
        "codex",
        "claude",
        "gemini_cli",
        "cursor",
        "amp",
        "mimo",
        "zcode",
        "omp",
        "kilocode",
        "cline",
        "workbuddy",
        "qoder",
        "qoder_cli",
        "zed",
        "qwen_code",
        "crush",
        "muse",
        "minimax",
        "commandcode",
    ):
        assert re.search(rf"\b{tool}:\s*\{{", body)
    assert "https://" not in body
    assert "/static/icons/agents/" in body
    asset_paths = re.findall(r"icon:\s*'(/static/icons/agents/[^']+)'", body)
    assert len(asset_paths) == 28
    assert body.count("/static/icons/agents/qoder.png") == 2
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


def test_quota_provider_cards_wear_the_shared_brand_marks() -> None:
    """The Quota tab names vendors the Overview already carries marks for, under
    its own subscription labels -- "Kimi Code" where the source is `kimi`, "MiniMax
    (China)" where a region decides the heading. The card keeps its label and
    borrows the mark, so a rebrand is still one table to edit.
    """
    source = INDEX_HTML.read_text(encoding="utf-8")
    registry = re.search(
        r"const TOOL_BRAND_META = Object\.freeze\(\{(?P<body>.*?)\n\s*\}\);",
        source,
        re.DOTALL,
    )
    assert registry, "tool brand registry not found"
    table = re.search(
        r"const QUOTA_PROVIDER_BRAND_KEYS = Object\.freeze\(\{(?P<body>.*?)\n\s*\}\);",
        source,
        re.DOTALL,
    )
    assert table, "the quota provider -> brand key table is missing"
    pairs = dict(re.findall(r"(\w+):\s*'([^']+)'", table.group("body")))
    assert pairs, "the quota provider -> brand key table is empty"
    for provider, brand_key in pairs.items():
        # The entry has to carry an actual asset. `muse` and `devin` exist in the
        # registry with `icon: null`, so a key that only proves existence could
        # point a card at a letter tile and still call it a mark.
        entry = re.search(
            rf"\b{brand_key}:\s*\{{[^}}]*\}}",
            registry.group("body"),
        )
        assert entry, f"quota provider {provider} points at a brand key that does not exist: {brand_key}"
        assert "icon: '/static/icons/agents/" in entry.group(0), (
            f"quota provider {provider} points at {brand_key}, which has no local mark: {entry.group(0)}"
        )

    helper = _extract_js_function(source, "function createQuotaProviderIdentity(providerKey, label) {")
    # A provider the table does not mention resolves on its own key, which is how
    # the two without a local mark land on the shared letter tile.
    assert "QUOTA_PROVIDER_BRAND_KEYS[providerKey] || providerKey" in helper
    assert "createToolBrandIcon(brandKey, meta)" in helper
    assert "identity.className = 'tool-identity';" in helper, "the card must reuse the shared identity shell"
    assert "name.textContent = label;" in helper, "the card's own provider label wins over the tool-name map"
    assert "title.append(createQuotaProviderIdentity(" in source

    # The shared label is built to ellipsize inside a table cell. A card heading has
    # no room left to ellipsize TO -- three cards per row, and an install name like
    # `Claude Code (work-laptop-01)` -- so the heading opts out and wraps.
    wrap = re.search(
        r"\.quota-card-title \.tool-brand-label \{(?P<body>[^}]*)\}",
        source,
    )
    assert wrap and "white-space: normal;" in wrap.group("body"), (
        "a long quota card title clips instead of wrapping"
    )

    # A wrapping heading also needs the plan suffix to travel as one word. A flex
    # title row shrinks the identity to fill the line and parks the muted plan out
    # at the card edge, so the title flows as running text and the suffix keeps its
    # leading separator.
    plan = re.search(
        r"\.quota-card-title \.quota-card-plan \{(?P<body>[^}]*)\}",
        source,
    )
    assert plan and "white-space: nowrap;" in plan.group("body"), (
        "the plan suffix can break away from its separator on a wrapping title"
    )
    assert (
        "planSuffix.className = 'text-xs font-semibold ml-2 quota-card-plan'" in source
    ), "the plan suffix is not the element the nowrap rule targets"
    assert (
        "title.className = 'text-base font-extrabold mono quota-card-title';" in source
    ), "a flex title row strands the plan suffix at the card edge"


QUOTA_IDENTITY_SIGNATURES = (
    "function createToolBrandIcon(tool, meta) {",
    "function createQuotaProviderIdentity(providerKey, label) {",
)

# The real provider -> brand table and the real builders over just enough DOM to walk
# what a card heading is made of. The asset fetch is refused and the brand palette is
# stubbed: this judges the tree, not the network and not the colours.
QUOTA_IDENTITY_FIXTURE = """
__TABLE__
function makeNode(tag) {
  return {
    tag, children: [], attrs: {}, dataset: {}, className: '', textContent: '',
    parent: null, isConnected: true,
    style: { setProperty() {} },
    setAttribute(key, value) { this.attrs[key] = String(value); },
    append(...kids) { kids.forEach((kid) => { kid.parent = this; this.children.push(kid); }); },
    appendChild(kid) { kid.parent = this; this.children.push(kid); return kid; },
    replaceChildren(...kids) { this.children = kids; kids.forEach((kid) => { kid.parent = this; }); },
    addEventListener() {},
  };
}
const document = { createElement: makeNode };
function loadToolBrandIcon() { return Promise.reject(new Error('no icon assets in the harness')); }
function toolBrandMeta(key) {
  return {
    icon: Object.prototype.hasOwnProperty.call(QUOTA_PROVIDER_BRAND_KEYS, key)
      ? '/static/icons/agents/codex-transparent.png'
      : null,
    color: '#111827',
    fallback: key.charAt(0).toUpperCase(),
    logoKind: 'mark',
    darkInvert: true,
  };
}
__FUNCTIONS__
function find(node, wanted) {
  if (wanted(node)) return node;
  for (const kid of node.children) { const hit = find(kid, wanted); if (hit) return hit; }
  return null;
}
const identity = createQuotaProviderIdentity('claude', 'Claude Code (work-laptop-01)');
const name = find(identity, (node) => node.className === 'tool-brand-label');
const hiddenChain = [];
for (let node = name; node; node = node.parent) {
  if (node.attrs['aria-hidden'] === 'true') hiddenChain.push(node.className || node.tag);
}
const unmapped = createQuotaProviderIdentity('zai', 'Z.ai');
process.stdout.write(JSON.stringify({
  name: name ? name.textContent : null,
  hiddenChain,
  unmappedMarkHidden: find(unmapped, (node) => node.className === 'tool-brand-label').attrs['aria-hidden'] || null,
  iconHidden: find(identity, (node) => node.className === 'tool-brand-icon').attrs['aria-hidden'] || null,
  darkInvert: identity.dataset.darkInvert,
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_quota_card_heading_keeps_its_name_readable(tmp_path: Path) -> None:
    """The one accessible name a quota card heading has is the text inside the
    identity wrapper, so hiding that wrapper to keep the mark out of the a11y tree
    hides the provider name with it: heading navigation reads the plan suffix and
    nothing else. `createToolIdentity` hides its wrapper only in its icon-only
    branch, the one branch with no label.

    Executed over the real table and the real builders, so the claim is about the
    tree that ships rather than a source line that happens to mention aria.
    """
    source = INDEX_HTML.read_text(encoding="utf-8")
    table = re.search(
        r"const QUOTA_PROVIDER_BRAND_KEYS = Object\.freeze\(\{.*?\n\s*\}\);",
        source,
        re.DOTALL,
    )
    assert table, "the quota provider -> brand key table is missing"
    functions = "\n".join(
        _extract_js_function(source, sig) for sig in QUOTA_IDENTITY_SIGNATURES
    )
    harness = tmp_path / "quota-identity.js"
    harness.write_text(
        QUOTA_IDENTITY_FIXTURE.replace("__TABLE__", table.group(0)).replace(
            "__FUNCTIONS__", functions
        ),
        encoding="utf-8",
    )
    report = json.loads(
        subprocess.run(
            ["node", str(harness)], check=True, capture_output=True, encoding="utf-8"
        ).stdout
    )

    assert report["name"] == "Claude Code (work-laptop-01)", "the card must render its label in full"
    assert report["hiddenChain"] == [], f"the provider name is hidden from assistive tech: {report['hiddenChain']}"
    assert report["unmappedMarkHidden"] is None, "the letter-tile card keeps its name too"
    assert report["iconHidden"] == "true", "the mark itself stays decorative"
    assert report["darkInvert"] == "true", "the card inherits the brand's dark-mode inversion"


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
    # The overflow legend text is i18n-driven (one key per language).
    assert "more.textContent = t('moreTools').replace('{n}', hiddenCount);" in source
    assert source.count("moreTools: '") == 6
    assert ".tool-chart-legend-more{" in compact
    assert "createToolIdentity(tool, { compact: true })" in source
    assert "renderToolChartLegend(entries, colors);" in source
    assert "legend: { display: false }" in source

def test_zcode_session_panel_is_wired_into_the_sessions_tab() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert re.search(r"const SESSION_TOOL_KEYS = \[[^\]]*'zcode'[^\]]*\];", source)
    assert "zcode: null" in source
    assert 'updateSessionPanel("zcode", lastSessionsResponses.zcode);' in source
    assert 'initSortHeaders("zcode", renderSessionsTab);' in source
    # One heading key per i18n dictionary (all six languages).
    assert len(re.findall(r"zcodeSessions:", source)) == 6
    # The panel follows the shared session-panel element contract.
    assert 'data-panel="zcode"' in source
    assert '<tbody id="zcodeSessionsTable">' in source
    assert 'id="zcodeActiveLabel"' in source
    assert 'id="zcodeLatestSession"' in source
