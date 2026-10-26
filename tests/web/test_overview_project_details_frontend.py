"""Tests for the Project Details table on the Overview tab.

The section ranks a range by the project each session ran in, and it is the one
Overview breakdown that does not come from the usage payload: it reads the
projects facet of ``/api/insights`` for the tab's own window and merges the answer
across the selected servers. That split is where the quiet failures live -- a
stale range's rows left standing under a new label, a server that never served the
facet reported as a project that happened to be empty, a share column whose header
sorts nothing because the field only ever existed inside the row painter.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from support.frontend import extract_js_function, index_source, needs_node, run_node


pytestmark = needs_node


def _source() -> str:
    return index_source()


def _declaration(source: str, prefix: str) -> str:
    line = next(line for line in source.splitlines() if line.strip().startswith(prefix))
    return line.strip()


def _run_node(tmp_path: Path, name: str, body: str) -> dict:
    harness = tmp_path / name
    harness.write_text(body, encoding="utf-8")
    return json.loads(run_node(harness))


# --- markup -----------------------------------------------------------------


def test_the_section_sits_under_combined_models_and_sorts_every_column() -> None:
    """The section the request asked for, in the place the request asked for it."""
    source = _source()
    overview = source[: source.index('id="sessions-content"')]
    combined = overview.index('data-i18n="combinedModels"')
    projects = overview.index('data-i18n="projectDetails"')
    assert combined < projects, "Project Details moved above the table it follows"

    table = source[source.index('id="projectDetailsTable"') - 1400 : source.index('id="projectDetailsTable"')]
    fields = re.findall(r'data-sortable="([^"]+)"', table)
    assert fields == ["project", "tokens", "share", "cost", "messages"], fields
    # A sortable header needs both halves: the field the click ranks by, and the
    # label that survives a language switch.
    headers = re.findall(r"<th\b[^>]*data-sortable=\"[^\"]+\"[^>]*>", table)
    assert len(headers) == len(fields)
    for th in headers:
        assert 'data-i18n="' in th, th

    # The cold markup is the same placeholder the other breakdowns ship with, and
    # its colspan matches the five columns above it.
    shipped = source[source.index('id="projectDetailsTable"') : source.index("</tbody>", source.index("id=\"projectDetailsTable\""))]
    assert 'colspan="5"' in shipped, "the loading row does not span the table"
    assert 'data-i18n="loading"' in shipped, "the placeholder is not translatable"


# --- merging, executed for real under node ----------------------------------

MERGE_FIXTURE = """
const SERVER_A = {
  totals: { tokens: 600 },
  projects: {
    projects: [
      { project: 'alpha', tokens: 400, messages: 40, cost: 4 },
      { project: 'shared', tokens: 200, messages: 20, cost: 2 },
      { project: 'empty', tokens: 0, messages: 0, cost: 0 },
      { tokens: 99, messages: 9, cost: 1 },
    ],
    unattributed: { tokens: 60 },
  },
};
const SERVER_B = {
  totals: { tokens: 400 },
  projects: {
    projects: [{ project: 'shared', tokens: 300, messages: 30, cost: 3 }],
    unattributed: { tokens: 40 },
  },
  tools: { ranked: [{ tool: 'codex', tokens: 700, messages: 70, cost: 7 }, { tool: 'idle', tokens: 0, messages: 0, cost: 0 }] },
};
const TOOLS_ONLY = { tools: { ranked: [{ tool: 'codex', messages: 5 }] } };
const shape = (merged) => merged && {
  names: merged.rows.map((row) => row.project),
  tokens: merged.rows.map((row) => row.tokens),
  shares: merged.rows.map((row) => row.share),
  facetTotal: merged.facetTotal,
  unattributedTokens: merged.unattributedTokens,
};
process.stdout.write(JSON.stringify({
  one: shape(combineOverviewFacets([SERVER_A])),
  two: shape(combineOverviewFacets([SERVER_A, SERVER_B])),
  tools: shape(combineOverviewFacets([SERVER_B])) && combineOverviewFacets([SERVER_B]).toolMessages,
  // A server that serves one facet and not the other still has a table to fill.
  toolsOnly: combineOverviewFacets([TOOLS_ONLY]),
  // An older server that never served either facet is not a range with no projects.
  none: combineOverviewFacets([{ totals: { tokens: 10 } }]),
  empty: combineOverviewFacets([]),
}));
"""


def _merge_source() -> str:
    source = _source()
    return extract_js_function(source, "function combineOverviewFacets(payloads) {")


@needs_node
def test_the_facet_is_merged_across_servers_rather_than_replaced_by_one(tmp_path: Path) -> None:
    """The Overview reads every selected server, so the ranks have to be added.

    Run because a merge that keeps only the last payload still renders a full,
    plausible-looking table -- it is just somebody else's machine.
    """
    out = _run_node(tmp_path, "project-facet-merge.js", _merge_source() + MERGE_FIXTURE)

    assert out["one"]["names"] == ["alpha", "shared"], "a project with no tokens gets no row"
    assert out["one"]["tokens"] == [400, 200]
    assert out["one"]["shares"] == [pytest.approx(400 / 600), pytest.approx(200 / 600)]
    assert out["one"]["unattributedTokens"] == 60

    assert out["two"]["names"] == ["alpha", "shared"], "the same project on two servers is one row"
    assert out["two"]["tokens"] == [400, 500], out["two"]["tokens"]
    assert out["two"]["facetTotal"] == 1000, "the share divides by the merged total, not one server's"
    assert out["two"]["unattributedTokens"] == 100

    # The map keeps every tool the facet named, including one with nothing in it:
    # the tools table decides which rows exist, from the usage figures it owns.
    assert out["tools"] == {"codex": 70, "idle": 0}, "message counts add up per tool"

    # A server that served the tools facet and not the projects one has a message
    # column to fill and no project table to show. Each half says for itself.
    assert out["toolsOnly"]["rows"] is None
    assert out["toolsOnly"]["toolMessages"] == {"codex": 5}

    assert out["none"] is None, "a server that never served either facet is reported, not shown as empty"
    assert out["empty"] is None


# --- the table itself, executed for real under node -------------------------

TABLE_DOM = """
function makeNode(tag) {
  const node = {
    tag, children: [], className: '', textContent: '', title: '', colSpan: null,
    style: {}, attrs: {}, dataset: {},
    append(...kids) { kids.forEach((kid) => this.children.push(kid)); },
    appendChild(kid) { this.children.push(kid); return kid; },
    replaceChildren(...kids) { this.children = kids; },
    closest: () => table,
    querySelectorAll: () => [],
    setAttribute(name, value) { node.attrs[name] = value; },
    getAttribute(name) { return node.attrs[name] || null; },
  };
  return node;
}
const ids = {
  projectDetailsTable: makeNode('tbody'),
  projectDetailsInfo: makeNode('div'),
  projectDetailsToggle: makeNode('button'),
  projectDetailsNote: makeNode('p'),
};
const table = makeNode('table');
const document = {
  createElement: makeNode,
  createDocumentFragment: () => makeNode('fragment'),
  getElementById: (id) => ids[id],
};
let sortSetups = 0;
function setupBreakdownSort() { sortSetups += 1; }
const LABELS = {
  loading: 'Loading', na: 'n/a', noData: 'No data', loadFailed: 'Failed to load',
  nProjects: '{n} projects', showingTopN: 'Top {top} of {total}',
  showAll: 'Show all', showTop: 'Show top',
  projectDetailsUnattributed: '{pct} of tokens carry no project',
};
function t(key) { return LABELS[key] || key; }
const formatNumber = (v) => `n${v}`;
const formatTokenCount = (v) => `t${v}`;
const formatCurrency = (v) => `$${v}`;
const formatPercent = (v) => `${(Number(v || 0) * 100).toFixed(1)}%`;

const textOf = (node) => node.textContent || (node.children || []).map(textOf).join('');
const rows = () => ids.projectDetailsTable.children[0] && ids.projectDetailsTable.children[0].tag === 'fragment'
  ? ids.projectDetailsTable.children[0].children
  : ids.projectDetailsTable.children;
const read = () => ({
  printed: rows().map((tr) => tr.children.map(textOf)),
  info: ids.projectDetailsInfo.textContent,
  note: ids.projectDetailsNote.textContent,
  toggle: ids.projectDetailsToggle.style.display,
  toggleLabel: ids.projectDetailsToggle.textContent,
  sortSetups,
});
"""

TABLE_FIXTURE = TABLE_DOM + """
const ROWS = [
  { project: 'alpha', tokens: 400, messages: 40, cost: 4, share: 0.4 },
  { project: 'beta', tokens: 500, messages: 5, cost: 30, share: 0.5 },
  { project: 'gamma', tokens: 100, messages: 90, cost: 0, share: 0.1 },
];
overviewFacetsState.rows = ROWS;
overviewFacetsState.facetTotal = 1000;
overviewFacetsState.unattributedTokens = 120;
overviewFacetsState.status = 'ready';
const out = {};

updateProjectDetailsTable();
out.byTokens = read();

// The reader clicked the cost header: the dear project leads, and the share
// column keeps the figure the row was ranked on.
breakdownSortState.projects = { field: 'cost', direction: 'desc' };
updateProjectDetailsTable();
out.byCost = read().printed.map((cells) => cells[0]);

breakdownSortState.projects = { field: 'share', direction: 'asc' };
updateProjectDetailsTable();
out.byShare = read().printed.map((cells) => cells[0]);

// Share is a number, and must rank as one. Fractions written out in decimals sort
// the same either way, but a project small enough prints its share as "1e-7",
// which a text sort ranks above every other share.
overviewFacetsState.rows = ROWS.concat([{ project: 'delta', tokens: 1, messages: 1, cost: 0, share: 1e-7 }]);
updateProjectDetailsTable();
out.byShareWithATinyProject = read().printed.map((cells) => cells[0]);
overviewFacetsState.rows = ROWS;

// A long tail is capped, said so, and the toggle is the way past the cap.
breakdownSortState.projects = { field: 'tokens', direction: 'desc' };
overviewFacetsState.rows = Array.from({ length: MAX_PROJECT_DETAIL_ROWS + 2 }, (_, i) => ({
  project: `p-${i}`, tokens: 1000 - i, messages: i, cost: i, share: (1000 - i) / 1000,
}));
updateProjectDetailsTable();
out.capped = { rows: rows().length, info: ids.projectDetailsInfo.textContent, toggle: ids.projectDetailsToggle.style.display, toggleLabel: ids.projectDetailsToggle.textContent };
projectDetailsShowAll = true;
updateProjectDetailsTable();
out.shownAll = { rows: rows().length, toggleLabel: ids.projectDetailsToggle.textContent };
projectDetailsShowAll = false;

// A failed read keeps a row that says so; an empty range says that instead.
overviewFacetsState.rows = [];
overviewFacetsState.status = 'error';
updateProjectDetailsTable();
out.failed = read();
overviewFacetsState.status = 'ready';
overviewFacetsState.facetTotal = 0;
overviewFacetsState.unattributedTokens = 0;
updateProjectDetailsTable();
out.empty = read();

// Usage can be entirely unattributed without the range being empty.
overviewFacetsState.facetTotal = 1000;
overviewFacetsState.unattributedTokens = 1000;
updateProjectDetailsTable();
out.unattributed = read();

// What a range change leaves behind: the placeholder, and nothing that reads as data.
clearOverviewFacets();
out.cleared = {
  status: overviewFacetsState.status,
  printed: rows().map((tr) => tr.children.map(textOf)),
  info: ids.projectDetailsInfo.textContent,
  note: ids.projectDetailsNote.textContent,
  toggle: ids.projectDetailsToggle.style.display,
};

process.stdout.write(JSON.stringify(out));
"""


def _table_body() -> str:
    source = _source()
    parts = [
        extract_js_function(source, sig)
        for sig in (
            "function getBreakdownComparator(field, direction) {",
            "function projectDetailsLoadingRow() {",
            "function createNoDataRow(colSpan) {",
            "function createErrorRow(colSpan, message) {",
            "function updateProjectDetailsTable() {",
            "function clearOverviewFacets() {",
        )
    ]
    parts.append(_declaration(source, "const BREAKDOWN_NUMERIC_FIELDS"))
    parts.append(_declaration(source, "const MAX_PROJECT_DETAIL_ROWS"))
    parts.append(_declaration(source, "let projectDetailsShowAll"))
    parts.append(extract_js_function(source, "const breakdownSortState = {"))
    parts.append(
        "const overviewFacetsState = { key: null, rows: [], toolMessages: null, unattributedTokens: 0, facetTotal: 0, status: 'idle', requestId: 0 };"
    )
    return "\n".join(parts)


@needs_node
def test_the_table_prints_the_rank_the_header_promises(tmp_path: Path) -> None:
    source = _table_body()
    out = _run_node(tmp_path, "project-details-table.js", source + TABLE_FIXTURE)

    assert [row[0] for row in out["byTokens"]["printed"]] == ["beta", "alpha", "gamma"]
    # Cost then messages, the way every other table on the page ends.
    assert out["byTokens"]["printed"][0] == ["beta", "t500", "50.0%", "$30", "n5"], out["byTokens"]["printed"][0]
    assert out["byTokens"]["printed"][2] == ["gamma", "t100", "10.0%", "$0", "n90"]
    assert out["byCost"] == ["beta", "alpha", "gamma"]
    assert out["byShare"] == ["gamma", "alpha", "beta"], "the share column ranks by the share it shows"
    assert out["byShareWithATinyProject"] == ["delta", "gamma", "alpha", "beta"], (
        "the share column sorted as text"
    )
    # The number goes through the same thousands formatter the table's rows use.
    assert out["byTokens"]["info"] == "n3 projects"
    assert out["byTokens"]["note"] == "12.0% of tokens carry no project"
    assert out["byTokens"]["toggle"] == "none", "three projects need no disclosure"
    assert out["byTokens"]["sortSetups"] >= 1, "the headers never got their click handlers"

    assert out["capped"]["rows"] == 300
    assert out["capped"]["info"] == "Top n300 of n302"
    assert out["capped"]["toggle"] == ""
    assert out["capped"]["toggleLabel"] == "Show all"
    assert out["shownAll"]["rows"] == 302
    assert out["shownAll"]["toggleLabel"] == "Show top"

    assert len(out["failed"]["printed"]) == 1, "a failed read leaves one row, and it says so"
    assert out["failed"]["printed"][0][0] == "Failed to load"
    assert out["failed"]["note"] == ""
    assert out["empty"]["printed"][0][0] == "No data"
    assert out["empty"]["note"] == ""
    assert out["unattributed"]["note"] == "100.0% of tokens carry no project"

    assert out["cleared"]["status"] == "loading", "an idle clear lets a stale repaint back in"
    assert out["cleared"]["printed"] == [["Loading"]]
    assert out["cleared"]["info"] == "" and out["cleared"]["note"] == ""
    assert out["cleared"]["toggle"] == "none"


# --- the loader, executed for real under node --------------------------------

LOADER_CASES = """
const requests = [];
function selectedServers() { return [{ id: 'local' }]; }
function fetchSelectedServers(url) {
  return new Promise((resolve, reject) => requests.push({ url, resolve, reject }));
}
function paintToolsTableIfLoaded() {}

(async () => {
  const out = {};
  const failing = loadOverviewFacets(null, '2026-01-05', '2026-01-05');
  out.whileLoading = read().printed;
  requests[0].reject(new Error('offline'));
  await failing;
  out.failed = { status: overviewFacetsState.status, printed: read().printed };

  const landing = loadOverviewFacets(null, '2026-01-06', '2026-01-06');
  requests[1].resolve([{ server: { id: 'local' }, payload: {
    totals: { tokens: 1000 },
    projects: { projects: [{ project: 'alpha', tokens: 1000, messages: 3, cost: 1 }] },
  } }]);
  await landing;
  out.landed = { status: overviewFacetsState.status, names: read().printed.map((cells) => cells[0]) };
  out.urls = requests.map((request) => request.url);
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_a_failed_read_says_so_instead_of_leaving_the_spinner_up(tmp_path: Path) -> None:
    """A range whose facet read fails must not sit under a loading row forever.

    One request answers for both facet tables: the tools facet is where the tools
    breakdown borrows its message column from, so the request asks for both.
    """
    source = _source()
    loader = "\n".join(
        extract_js_function(source, signature)
        for signature in (
            "function windowKeyFor(customDays, dateFrom, dateTo) {",
            "function usageServerKeyFor(servers = selectedServers()) {",
            "function combineOverviewFacets(payloads) {",
            "async function loadOverviewFacets(customDays, dateFrom, dateTo, options = {}) {",
        )
    )
    out = _run_node(tmp_path, "project-details-loader.js", _table_body() + "\n" + loader + TABLE_DOM + LOADER_CASES)

    assert out["whileLoading"] == [["Loading"]]
    assert out["failed"]["status"] == "error"
    assert out["failed"]["printed"] == [["Failed to load"]], "the failed read left the spinner up"
    assert out["landed"] == {"status": "ready", "names": ["alpha"]}
    assert out["urls"][0] == "/api/insights?facets=tools,projects&date_from=2026-01-05&date_to=2026-01-05"


# --- a language switch, executed for real under node -------------------------

ARROWS_HARNESS = """
function makeArrow() {
  const arrow = { className: '', textContent: '', classes: new Set() };
  arrow.classList = { add: (name) => arrow.classes.add(name), remove: (name) => arrow.classes.delete(name) };
  return arrow;
}
// A header row as applyI18n leaves it: retranslating a header rewrites its text,
// which takes the sort arrow span with it.
function makeTable(fields) {
  const headers = fields.map((field) => ({
    field, children: [],
    getAttribute: (name) => (name === 'data-sortable' ? field : null),
    querySelector(selector) {
      return selector === '.sort-arrow' ? this.children.find((kid) => kid.className === 'sort-arrow') || null : null;
    },
    appendChild(kid) { this.children.push(kid); return kid; },
  }));
  return { headers, querySelectorAll: () => headers };
}
const tables = {
  toolsTable: makeTable(['tool', 'tokens', 'cost']),
  allModelsTable: makeTable(['model', 'tokens', 'cost']),
  projectDetailsTable: makeTable(['project', 'tokens', 'share']),
};
const document = {
  getElementById: (id) => (tables[id] ? { closest: () => tables[id] } : null),
  createElement: makeArrow,
};

__FUNCTIONS__

// The reader had ranked Project Details by share, low to high.
breakdownSortState.projects = { field: 'share', direction: 'asc' };
__RESTORE__

const arrows = (id) => Object.fromEntries(tables[id].headers.map((th) => {
  const arrow = th.children.find((kid) => kid.className === 'sort-arrow');
  return [th.field, arrow ? arrow.textContent : null];
}));
process.stdout.write(JSON.stringify({
  tools: arrows('toolsTable'),
  combined: arrows('allModelsTable'),
  projects: arrows('projectDetailsTable'),
}));
"""


def test_a_language_switch_puts_every_sort_arrow_back(tmp_path: Path) -> None:
    """applyI18n rewrites the header text, so it has to restore each table's arrow.

    Only that step of applyI18n runs here, sliced out of the function: the rest of
    it repaints half the page and has no harness yet. A table missing from its
    list keeps arrowless headers until the next click on one.
    """
    source = _source()
    i18n = extract_js_function(source, "function applyI18n() {")
    restore = extract_js_function(i18n, "if (typeof breakdownSortState !== 'undefined') {")
    functions = "\n".join([
        extract_js_function(source, "const breakdownSortState = {"),
        extract_js_function(source, "function renderBreakdownSortArrows(table, state) {"),
    ])
    out = _run_node(
        tmp_path,
        "sort-arrows.js",
        ARROWS_HARNESS.replace("__FUNCTIONS__", functions).replace("__RESTORE__", restore),
    )

    assert out["tools"] == {"tool": "", "tokens": " ▼", "cost": ""}
    assert out["combined"] == {"model": "", "tokens": "", "cost": " ▼"}
    assert out["projects"] == {"project": "", "tokens": "", "share": " ▲"}
