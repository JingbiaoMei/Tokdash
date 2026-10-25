"""The Sessions tab's tool filter and search survive everything that redraws the tab.

The panels are rebuilt on every refresh, and each rebuild re-shows every tool
with sessions and redraws its rows unfiltered. Two ways this has regressed:

- the tool filter lived only as a pill's ``active`` class, so a refresh left the
  pill glowing over a tab showing every tool, and the search box kept its text
  while the fresh rows ignored it;
- "All Tools" blanket-showed and force-opened all 21 panels, empty ones
  included, instead of handing visibility and open state back to the rules a
  fresh render uses.

These run the real renderSessionsTab, buildSessionQuickFilterPills,
filterSessionPanels, filterSessionsBySearch and the panel open-state machine,
sliced out of index.html, over a stub toolbar, panels and table rows.
updateSessionPanel is a stand-in that does the three things the real one does
to a panel: show it when it has sessions or an error, reset its open state, and
rebuild its rows with nothing hidden.
"""
from __future__ import annotations

import json
import re

from support.frontend import extract_js_function, index_source, needs_node, run_node

pytestmark = needs_node

FUNCTIONS = (
    "function formatToolName(",
    "function hasLoadedSessionResponses(",
    "function sessionPanelShouldOpen(",
    "function sessionPanelSetOpen(",
    "function filterSessionPanels(",
    "function filterSessionsBySearch(",
    "function buildSessionQuickFilterPills(",
    "function renderSessionsTab(",
)

# Declarations the functions read, taken from the page so the tool list and the
# filter's starting value are the shipped ones.
STATE = (
    r"const SESSION_TOOL_KEYS = \[[^\]]*\];",
    r"let sessionToolFilter = [^;]*;",
)

HARNESS = r"""
// --- the page's own state -----------------------------------------------------
__STATE__

// --- stub DOM -------------------------------------------------------------------
const window = globalThis;  // no CSS.escape: tool keys need no escaping

function classSet() {
  const names = new Set();
  return {
    add: (...n) => n.forEach((name) => names.add(name)),
    remove: (...n) => n.forEach((name) => names.delete(name)),
    contains: (name) => names.has(name),
  };
}

function makePill() {
  const pill = {
    type: '', className: '', title: '', dataset: {}, attributes: {}, children: [],
    classList: classSet(),
    onClick: null,
    setAttribute(name, value) { pill.attributes[name] = String(value); },
    appendChild(child) { pill.children.push(child); return child; },
    addEventListener(type, handler) { if (type === 'click') pill.onClick = handler; },
    remove() {
      const index = bar.pills.indexOf(pill);
      if (index !== -1) bar.pills.splice(index, 1);
    },
  };
  return pill;
}

// The static "All Tools" pill; its markup click is filterSessionPanels('all', this).
const allPill = makePill();
allPill.dataset.tool = 'all';
allPill.classList.add('active');

// Just the pill selectors buildSessionQuickFilterPills uses.
function pillMatcher(selector) {
  if (selector.includes(':not([data-tool="all"])')) return (pill) => pill.dataset.tool !== 'all';
  const tool = /\[data-tool="([^"]*)"\]/.exec(selector);
  return tool ? (pill) => pill.dataset.tool === tool[1] : () => true;
}
const bar = {
  pills: [allPill],
  querySelectorAll: (selector) => bar.pills.filter(pillMatcher(selector)),
  querySelector: (selector) => bar.pills.find(pillMatcher(selector)) || null,
  appendChild(fragment) { bar.pills.push(...fragment.children); return fragment; },
};

// One <details class="session-panel"> per tool plus Combined, as in the page.
const PANEL_TOOLS = [...SESSION_TOOL_KEYS, 'combined'];
const panels = PANEL_TOOLS.map((tool) => ({
  dataset: { panelDetails: tool },
  parentElement: { style: {} },
  open: false,
  querySelector: () => null,
}));
const panelFor = (tool) => panels.find((panel) => panel.dataset.panelDetails === tool);
const rowsByTool = {};
const rowsOf = (tool) => rowsByTool[tool] || [];

const searchInput = { value: '' };
const byId = {
  sessionsQuickFilterBar: bar,
  sessionSearchInput: searchInput,
  sessionsEmptyState: { hidden: false },
  sessionsPanelToolbar: { hidden: false, style: {} },
};
const document = {
  getElementById: (id) => byId[id] || null,
  createElement: () => makePill(),
  createDocumentFragment: () => {
    const fragment = { children: [], appendChild: (child) => { fragment.children.push(child); return child; } };
    return fragment;
  },
  querySelectorAll(selector) {
    if (selector === '#sessionsQuickFilterBar .modern-tool-pill') return bar.pills;
    if (selector === '#sessions-content details.session-panel') return panels;
    if (selector === '#sessions-content .session-table tbody tr') return PANEL_TOOLS.flatMap(rowsOf);
    // A selector the stub does not know would return nothing and pass vacuously.
    throw new Error('stub DOM has no answer for ' + selector);
  },
};

// --- page stand-ins -------------------------------------------------------------
let lastSessionsResponses = {};
let sessionCollapseOverrides = {};
let codexReviewDefault = false;
let includeCodexReviewSessions = null;
function syncCodexReviewToggle() {}
function observeSessionSpeedRows() {}
function scheduleSessionSpeedList() {}
function scheduleSessionPillDensity() {}
function initSortHeaders() {}
function createToolIdentity(tool) { return { tool }; }
function buildCombinedSessionsData(responses) {
  return { sessions: SESSION_TOOL_KEYS.flatMap((tool) => responses[tool]?.sessions || []) };
}
function updateSessionPanel(tool, data) {
  const panel = panelFor(tool);
  const sessions = data?.sessions || [];
  panel.parentElement.style.display = (!sessions.length && !data?.error) ? 'none' : '';
  sessionPanelSetOpen(panel, sessionPanelShouldOpen(sessionCollapseOverrides[tool], data));
  rowsByTool[tool] = sessions.map((session) => ({
    textContent: `${session.project} ${session.model}`,
    style: { display: '' },
  }));
}

// --- the code under test --------------------------------------------------------
__FUNCTIONS__

// --- driving ----------------------------------------------------------------------
const shown = (panel) => panel.parentElement.style.display !== 'none';
function snapshot() {
  const visible = panels.filter(shown).map((panel) => panel.dataset.panelDetails);
  return {
    filter: sessionToolFilter,
    activePills: bar.pills.filter((pill) => pill.classList.contains('active')).map((pill) => pill.dataset.tool),
    shownPanels: visible,
    openPanels: panels.filter((panel) => panel.open).map((panel) => panel.dataset.panelDetails),
    shownRows: visible.flatMap((tool) => rowsOf(tool)
      .filter((row) => row.style.display !== 'none')
      .map((row) => `${tool}: ${row.textContent}`)),
  };
}
const clickPill = (tool) => bar.querySelector(`[data-tool="${tool}"]`).onClick();
const responses = () => ({
  codex: { sessions: [{ project: 'alpha', model: 'gpt' }, { project: 'beta', model: 'gpt' }] },
  claude: { sessions: [{ project: 'alpha', model: 'sonnet' }] },
  opencode: { sessions: [] },
  hermes: { sessions: [], error: 'offline' },
});

const out = {};
const scenario = process.argv[2];

if (scenario === 'all-after-a-tool-filter') {
  lastSessionsResponses = responses();
  renderSessionsTab();
  clickPill('codex');
  filterSessionPanels('all', allPill);
  Object.assign(out, snapshot());
}

if (scenario === 'filter-and-search-survive-a-refresh') {
  lastSessionsResponses = responses();
  renderSessionsTab();
  clickPill('codex');
  searchInput.value = 'alpha';
  filterSessionsBySearch(searchInput.value);  // the search box's oninput
  out.before = snapshot();
  // An auto-refresh lands: new response objects, every panel and row rebuilt.
  lastSessionsResponses = responses();
  renderSessionsTab();
  out.after = snapshot();
}

process.stdout.write(JSON.stringify(out));
"""


def _run(tmp_path, scenario: str) -> dict:
    source = index_source()
    state = []
    for pattern in STATE:
        match = re.search(pattern, source)
        assert match, f"{pattern} not found in index.html"
        state.append(match.group(0))
    functions = "\n\n".join(extract_js_function(source, signature) for signature in FUNCTIONS)
    script = tmp_path / "sessions_tab.js"
    script.write_text(
        HARNESS.replace("__STATE__", "\n".join(state)).replace("__FUNCTIONS__", functions),
        encoding="utf-8",
    )
    return json.loads(run_node(script, scenario))


def test_all_tools_keeps_empty_panels_hidden_and_errors_visible(tmp_path):
    """Back to "All Tools" from Codex: the panels a fresh render would show, opened as it would.

    OpenCode answered with no sessions and most tools sent nothing, so they stay
    hidden; Hermes failed, so it shows, and opens so its error row is seen.
    Codex was opened by its own filter and closes again: open state is the
    state machine's, not a blanket ``open = true`` and not whatever the filter
    left behind.
    """
    out = _run(tmp_path, "all-after-a-tool-filter")

    assert out["filter"] == "all"
    assert out["activePills"] == ["all"]
    assert out["shownPanels"] == ["codex", "claude", "hermes", "combined"]
    assert out["openPanels"] == ["hermes"]


def test_tool_filter_and_search_survive_a_refresh(tmp_path):
    """Pick Codex, search "alpha", then let an auto-refresh rebuild the tab.

    The refresh re-shows Claude, Hermes and Combined and redraws every row, so only
    state that is re-applied after the rebuild survives it: the filter, the
    pill that shows it, and the search over the new rows.
    """
    out = _run(tmp_path, "filter-and-search-survive-a-refresh")

    expected = {
        "filter": "codex",
        "activePills": ["codex"],
        "shownPanels": ["codex"],
        "shownRows": ["codex: alpha gpt"],
    }
    for moment in ("before", "after"):
        seen = {key: out[moment][key] for key in expected}
        assert seen == expected, f"{moment} the refresh: {seen}"
        assert "codex" in out[moment]["openPanels"], f"{moment} the refresh: the filtered panel is shut"
