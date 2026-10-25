"""Tests for the tool brand frontend.

Covers the packaged brand marks and README pills, the brand registry, and the
identity (mark plus name) the dashboard renders. The identity is checked by
running the real builders in Node: in the Overview breakdowns, the usage
chart legend, the combined sessions table and the quota card headings.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import tokdash  # type: ignore[import-untyped]

from support.frontend import extract_js_function, index_source, needs_node, run_node


STATIC_DIR = Path(tokdash.__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
ICON_DIR = STATIC_DIR / "icons" / "agents"
PROJECT_ROOT = STATIC_DIR.parents[2]
DOCS_PILL_DIR = PROJECT_ROOT / "docs" / "assets" / "agents" / "pills"


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
        "freebuff.svg",
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
        "Freebuff": "freebuff.png",
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


def test_readme_pills_keep_alpha_transparent_corners() -> None:
    """A pill is a rounded shape, so its corner pixels must stay transparent.

    Flattening an export onto white (an RGB PNG instead of RGBA) turns the pill
    into a slab on GitHub's dark theme, which is the only place the shape shows.
    PNG colour type 6 is truecolour with alpha.
    """
    offenders = []
    for pill in sorted(DOCS_PILL_DIR.glob("*.png")):
        header = pill.read_bytes()[:26]
        if header[:8] != b"\x89PNG\r\n\x1a\n" or header[25] != 6:
            offenders.append(pill.name)
    assert not offenders, f"pills without an alpha channel: {offenders}"


def test_tool_brand_registry_points_only_at_packaged_marks() -> None:
    """Every mark is a file in the package, so the dashboard makes no request
    off the machine and no brand silently loses its mark to a typo."""
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
        "freebuff",
    ):
        assert re.search(rf"\b{tool}:\s*\{{", body)
    assert "https://" not in body
    icons = re.findall(r"\bicon:\s*([^,}]+)", body)
    assert icons, "no brand entry declares a mark"
    for value in icons:
        value = value.strip()
        if value == "null":
            continue  # no packaged mark: the identity shows its letter tile
        local = re.fullmatch(r"'(/static/icons/agents/[^']+)'", value)
        assert local, f"a brand mark is not one of the packaged icons: {value}"
        assert (STATIC_DIR / local.group(1).removeprefix("/static/")).is_file(), value
    assert "codex-transparent.png" in body
    assert "grok-transparent.png" in body
    for name in ("codex-transparent.png", "grok-transparent.png"):
        data = (ICON_DIR / name).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert data[25] in {4, 6}, f"{name} must carry an alpha channel"


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

    # Every card the tab can draw has to be in that table. A provider missing from it
    # does not fail -- it resolves on its own key and quietly renders the letter tile --
    # so the coverage is what the assertion is really for: `zai` maps to `zcode` because
    # the poll reads its key from the ZCode CLI's config, not because a tile is ugly.
    providers = re.search(r"const QUOTA_PROVIDERS = \[(?P<items>[^\]]*)\];", source)
    assert providers, "the quota provider list is missing"
    listed = set(re.findall(r"'([^']+)'", providers.group("items")))
    assert listed == set(pairs), (
        "quota providers render without a brand entry and land on the letter tile: "
        f"{sorted(listed - set(pairs))}"
    )

    # The card heading goes through the helper. What the helper builds is run in
    # test_the_quota_card_heading_keeps_its_name_readable.
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
const unbranded = createQuotaProviderIdentity('future_cli', 'Future CLI');
process.stdout.write(JSON.stringify({
  name: name ? name.textContent : null,
  hiddenChain,
  unbrandedMarkHidden: find(unbranded, (node) => node.className === 'tool-brand-label').attrs['aria-hidden'] || null,
  iconHidden: find(identity, (node) => node.className === 'tool-brand-icon').attrs['aria-hidden'] || null,
  darkInvert: identity.dataset.darkInvert,
}));
"""


@needs_node
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
        extract_js_function(source, sig) for sig in QUOTA_IDENTITY_SIGNATURES
    )
    harness = tmp_path / "quota-identity.js"
    harness.write_text(
        QUOTA_IDENTITY_FIXTURE.replace("__TABLE__", table.group(0)).replace(
            "__FUNCTIONS__", functions
        ),
        encoding="utf-8",
    )
    report = json.loads(run_node(harness))

    assert report["name"] == "Claude Code (work-laptop-01)", "the card must render its label in full"
    assert report["hiddenChain"] == [], f"the provider name is hidden from assistive tech: {report['hiddenChain']}"
    assert report["unbrandedMarkHidden"] is None, "the letter-tile card keeps its name too"
    assert report["iconHidden"] == "true", "the mark itself stays decorative"
    assert report["darkInvert"] == "true", "the card inherits the brand's dark-mode inversion"


@needs_node
def test_tool_brand_icon_requests_are_shared_per_asset(tmp_path: Path) -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    function = extract_js_function(source, "function loadToolBrandIcon(iconPath) {")
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
    assert run_node(harness) == (
        '{"fetchCalls":1,"urls":["blob:shared-icon","blob:shared-icon"]}'
    )


def test_tool_icons_are_unboxed() -> None:
    """The marks sit on the page with no tile behind them, and the dark-mode
    inversion is a per-brand opt-in. These are computed styles, which need a
    browser to read, so this stays a stylesheet check."""
    compact = re.sub(r"\s+", "", index_source())

    assert ".tool-brand-icon{" in compact
    assert "border:0;background:transparent;box-shadow:none" in compact
    assert 'data-dark-invert="true"' in compact
    assert ".tool-chart-legend-more{" in compact


# ---------------------------------------------------------------------------
# The identity as the breakdowns, the chart legend and the session rows draw it
# ---------------------------------------------------------------------------

BRAND_SIGNATURES = (
    "function normalizeToolBrandKey(tool) {",
    "function toolBrandMeta(tool) {",
    "function createToolBrandIcon(tool, meta) {",
    "function createToolIdentity(tool, options = {}) {",
    "function formatToolName(tool) {",
    # the usage chart and its legend
    "function renderToolChartLegend(entries, colors) {",
    "function updateToolChart(byTool) {",
    # the Overview apps breakdown
    "function updateAppsBreakdown(apps, openclawModels) {",
    "function hasModelTokenUsage(model) {",
    "function hitRateFrom(tokensIn, tokensCache) {",
    # the session tables
    "function updateSessionPanel(prefix, data, options = {}) {",
    "function getSessionSortComparator(field, direction) {",
    "function groupSessionsByProject(sessions, sortComparator) {",
    "function sortProjectGroups(groups, field, direction) {",
    "function createTableCell(tag, className, text, style = null) {",
    "function toTimestampMs(value) {",
    "function sessionDisplayName(session) {",
    "function sessionShortId(id) {",
)

# The real brand table and builders, and the real renderers that use them, over a
# DOM just deep enough to walk what they built. Icon requests resolve at once to a
# fake blob URL; formatting, sorting handlers and the speed cell are stubbed,
# since this judges which identity each place draws, not its figures.
BRAND_HARNESS = """
function makeNode(tag) {
  return {
    tag, children: [], attrs: {}, dataset: {}, className: '', parent: null, isConnected: true,
    style: { setProperty(name, value) { this[name] = value; } },
    _text: '', _html: '',
    get textContent() { return this._text + this.children.map((kid) => kid.textContent).join(''); },
    set textContent(value) { this._text = String(value); this.children = []; },
    get innerHTML() { return this._html; },
    set innerHTML(value) { this._html = String(value); this.children = []; },
    get firstElementChild() { return this.children[0] || null; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
    append(...kids) {
      for (const kid of kids) {
        // A fragment hands over its children, as in the DOM.
        if (kid.tag === '#fragment') { this.append(...kid.children); kid.children = []; continue; }
        kid.parent = this;
        this.children.push(kid);
      }
    },
    appendChild(kid) { this.append(kid); return kid; },
    replaceChildren(...kids) { this.children = []; this._text = ''; this.append(...kids); },
    addEventListener() {},
    // updateAppsBreakdown writes its table as markup and then asks for the body.
    querySelector(selector) {
      if (selector !== 'tbody') return null;
      if (!this._tbody) this._tbody = makeNode('tbody');
      return this._tbody;
    },
  };
}
const elements = {};
const document = {
  createElement: (tag) => makeNode(tag),
  createDocumentFragment: () => makeNode('#fragment'),
  getElementById: (id) => elements[id] || null,
  querySelector: () => null,
};
const window = {};

const LABELS = { moreTools: '+{n} more' };
function t(key) { return LABELS[key] || key; }
function formatTokenCount(value) { return String(value || 0); }
function formatCurrency(value) { return `$${Number(value || 0)}`; }
function formatNumber(value) { return String(value || 0); }
function formatHitRate() { return '-'; }
function formatDuration() { return '-'; }
function formatSessionTime() { return '-'; }
function formatSessionDateTime() { return '-'; }
function escapeHtml(value) { return String(value); }
function loadToolBrandIcon(path) { return Promise.resolve(`blob:${path}`); }

// updateToolChart
let toolChart = null;
const charts = [];
function createChart(context, config) { charts.push(config); return { data: config.data, update() {} }; }
const PALETTE = ['#c0', '#c1', '#c2', '#c3', '#c4', '#c5', '#c6', '#c7', '#c8', '#c9'];
function getChartPalette() { return PALETTE; }

// updateAppsBreakdown
const breakdownSortState = { apps: { field: 'cost', direction: 'desc' } };
let lastAppsBreakdown = null;
function getBreakdownComparator() { return () => 0; }
function setupBreakdownSort() {}

// updateSessionPanel
const DEFAULT_SORT = { field: 'last_seen_at', direction: 'desc' };
const sessionSortState = {};
const sessionCollapseOverrides = {};
const SESSION_COLS_8 = '';
const SESSION_COLS_9 = '';
function selectedServers() { return [{ id: 'local' }]; }
function sessionPanelShouldOpen() { return false; }
function sessionPanelSetOpen() {}
function updateSortArrows() {}
function createSessionSpeedCell() { return makeNode('td'); }
function openSessionModal() {}

__BRAND_META__
__FUNCTIONS__

function findAll(node, wanted, found = []) {
  if (wanted(node)) found.push(node);
  for (const kid of node.children) findAll(kid, wanted, found);
  return found;
}
const byClass = (name) => (node) => node.className === name;
function readIdentity(identity) {
  const img = findAll(identity, (node) => node.tag === 'img')[0];
  const fallback = findAll(identity, byClass('tool-brand-fallback'))[0];
  return {
    name: identity.getAttribute('aria-label'),
    compact: identity.dataset.compact,
    darkInvert: identity.dataset.darkInvert,
    image: img ? { src: img.src, loading: img.loading, decoding: img.decoding } : null,
    fallback: fallback ? fallback.textContent : null,
  };
}
const identitiesIn = (node) => findAll(node, byClass('tool-identity')).map(readIdentity);
// The marks arrive on a resolved promise; one macrotask lets them land.
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

async function main() {
  const scenario = process.argv[2];
  const out = {};

  if (scenario === 'apps') {
    elements.appsBreakdown = makeNode('div');
    updateAppsBreakdown({
      claude: { cost: 3, models: [{ name: 'opus', tokens: 300, cost: 3 }] },
      codex: { cost: 5, models: [{ name: 'gpt', tokens: 500, cost: 5 }] },
      cursor: { cost: 0, models: [{ name: 'idle', tokens: 0, cost: 0 }] },
    }, [{ name: 'kimi-k2', tokens: 100, cost: 1 }]);
    await settle();
    out.headings = elements.appsBreakdown.children.map(
      (section) => identitiesIn(findAll(section, (node) => node.tag === 'h4')[0]),
    );
  }

  if (scenario === 'sessions') {
    elements.combinedSessionsTable = makeNode('tbody');
    elements.codexSessionsTable = makeNode('tbody');
    const sessions = [
      { tool: 'kimi', session_id: 'b', project: 'web', last_seen_at: '2026-10-01T10:00:00Z', tokens: 3 },
      { tool: 'qoder_cli', session_id: 'a', project: 'web', last_seen_at: '2026-10-02T10:00:00Z', tokens: 5 },
    ];
    updateSessionPanel('combined', { sessions, latest_session: sessions[1], summary: {} }, { showTool: true });
    updateSessionPanel('codex', {
      sessions: [{ session_id: 'c', project: 'web', last_seen_at: '2026-10-01T10:00:00Z', tokens: 2 }],
      summary: {},
    });
    await settle();
    out.combined = identitiesIn(elements.combinedSessionsTable);
    out.codex = identitiesIn(elements.codexSessionsTable);
  }

  if (scenario === 'legend') {
    elements.toolChart = { getContext: () => ({}) };
    elements.toolChartLegend = makeNode('div');
    // Out of order, with one tool that used nothing: the chart sorts and drops it.
    const byTool = {
      dsh: { tokens: 100 }, claude: { tokens: 700 }, cursor: { tokens: 0 }, kimi: { tokens: 500 },
      codex: { tokens: 800 }, qoder: { tokens: 200 }, zcode: { tokens: 300 }, muse: { tokens: 600 },
      cline: { tokens: 400 },
    };
    updateToolChart(byTool);
    await settle();
    const legendOf = (legend) => ({
      items: legend.children.filter(byClass('tool-chart-legend-item')).map((item) => ({
        swatch: findAll(item, byClass('tool-chart-legend-swatch'))[0].style.backgroundColor,
        identities: identitiesIn(item),
      })),
      more: legend.children.filter(byClass('tool-chart-legend-more')).map((node) => node.textContent),
    });
    Object.assign(out, legendOf(elements.toolChartLegend));
    const chart = charts[0];
    out.chart = {
      labels: chart.data.labels,
      colors: chart.data.datasets[0].backgroundColor,
      legend: chart.options.plugins.legend,
    };
    // Exactly six fit, so there is nothing to count.
    elements.toolChartLegend = makeNode('div');
    renderToolChartLegend(
      ['codex', 'claude', 'kimi', 'cline', 'zcode', 'dsh'].map((tool) => [tool, { tokens: 1 }]),
      PALETTE,
    );
    out.exactlySix = legendOf(elements.toolChartLegend);
  }

  process.stdout.write(JSON.stringify(out));
}
main();
"""


def _brand_report(tmp_path: Path, scenario: str) -> dict:
    source = index_source()
    brands = re.search(
        r"const TOOL_BRAND_META = Object\.freeze\(\{.*?\n\s*\}\);", source, re.DOTALL
    )
    assert brands, "tool brand registry not found"
    functions = "\n".join(extract_js_function(source, sig) for sig in BRAND_SIGNATURES)
    harness = tmp_path / "tool-identity.js"
    harness.write_text(
        BRAND_HARNESS.replace("__BRAND_META__", brands.group(0)).replace(
            "__FUNCTIONS__", functions
        ),
        encoding="utf-8",
    )
    return json.loads(run_node(harness, scenario))


@needs_node
def test_the_breakdowns_name_each_tool_with_its_brand_identity(tmp_path: Path) -> None:
    """The Overview apps breakdown and the combined sessions table name a tool
    with the shared identity (mark plus name), not bare text, the same as the
    Overview tools table. That third place is run by
    test_overview_tools_table_frontend.py::test_the_table_prints_the_shared_column_order_and_says_what_it_lacks."""
    apps = _brand_report(tmp_path, "apps")
    # Highest cost first. OpenClaw comes in through its own model list, and an
    # app whose models used nothing gets no heading.
    assert [[identity["name"] for identity in heading] for heading in apps["headings"]] == [
        ["Codex"],
        ["Claude Code"],
        ["OpenClaw"],
    ]
    assert {identity["compact"] for heading in apps["headings"] for identity in heading} == {"true"}

    sessions = _brand_report(tmp_path, "sessions")
    # Most recent first, one identity per row, in the Tool column only the
    # combined table has.
    assert [identity["name"] for identity in sessions["combined"]] == ["Qoder CLI", "Kimi CLI"]
    assert {identity["compact"] for identity in sessions["combined"]} == {"true"}
    assert sessions["codex"] == [], "a per-tool table has no Tool column to put a mark in"


@needs_node
def test_the_usage_chart_legend_names_six_tools_and_counts_the_rest(tmp_path: Path) -> None:
    """The doughnut's own legend is off and the page draws one: a mark and
    name for each of the six biggest tools, in the chart's colours, and a
    count for the rest."""
    out = _brand_report(tmp_path, "legend")

    assert out["chart"]["legend"] == {"display": False}, "Chart.js would draw a second legend"
    assert out["chart"]["labels"] == [
        "Codex", "Claude Code", "Muse Code", "Kimi CLI", "Cline", "ZCode", "Qoder IDE", "DeepSeek Harness",
    ]
    assert [[identity["name"] for identity in item["identities"]] for item in out["items"]] == [
        ["Codex"], ["Claude Code"], ["Muse Code"], ["Kimi CLI"], ["Cline"], ["ZCode"],
    ]
    assert [item["swatch"] for item in out["items"]] == out["chart"]["colors"][:6]
    assert out["more"] == ["+2 more"]
    assert len(out["exactlySix"]["items"]) == 6
    assert out["exactlySix"]["more"] == [], "six tools fit, so there is no one left to count"

    codex = out["items"][0]["identities"][0]
    assert codex["compact"] == "true"
    assert codex["darkInvert"] == "true"
    assert codex["image"] == {
        "src": "blob:/static/icons/agents/codex-transparent.png",
        "loading": "lazy",
        "decoding": "async",
    }
    # Muse has no packaged mark: its identity keeps the letter tile.
    muse = out["items"][2]["identities"][0]
    assert muse["image"] is None
    assert muse["fallback"] == "M"
    assert muse["darkInvert"] == "false"
