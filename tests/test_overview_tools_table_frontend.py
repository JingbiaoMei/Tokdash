"""Tests for the Tools Breakdown table on the Overview tab.

The table ranks the range by tool. Three of its columns -- sessions, active time,
messages -- are not in the usage payload that ranks it: they arrive later, from
/api/active-time and from the tools facet of /api/insights. That is where the quiet
failures live: a tool whose activity never arrived printing a confident zero, the
cells drifting out of the order the headers promise, or a sort on a column whose
field the row never carried.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from support.frontend import extract_js_function, index_source, needs_node, run_node


pytestmark = needs_node

# The header contract: field the click ranks by, and the shared term it prints.
HEADERS = (
    ("tool", "tool"),
    ("tokens", "total"),
    ("cache_hit_rate", "hitPct"),
    ("sessions", "sessions"),
    ("activeMs", "active"),
    ("cost", "cost"),
    ("messages", "msgs"),
)


def _source() -> str:
    return index_source()


def _declaration(source: str, prefix: str) -> str:
    return next(line.strip() for line in source.splitlines() if line.strip().startswith(prefix))


def _run_node(tmp_path: Path, name: str, body: str) -> dict:
    harness = tmp_path / name
    harness.write_text(body, encoding="utf-8")
    return json.loads(run_node(harness))


# --- markup -----------------------------------------------------------------


def test_the_tools_table_offers_every_new_column_a_sort_and_a_shared_term() -> None:
    """Sessions, active time and messages are columns now, so they sort."""
    source = _source()
    start = source.index('id="toolsTable"')
    table = source[start - 1600 : start]
    shipped = source[start : source.index("</tbody>", start)]
    fields = re.findall(r'data-sortable="([^"]+)"', table)
    labels = re.findall(r'data-i18n="([^"]+)" data-sortable="[^"]+"', table)
    assert fields == [field for field, _ in HEADERS], fields
    assert labels == [label for _, label in HEADERS], labels

    # The estimate caveat the tile used to print now rides on the column header.
    assert 'data-i18n="active" data-sortable="activeMs" data-i18n-title="activeTimeHint"' in table

    # Seven columns, and the shipped placeholder spans them. The painted no-data
    # row, and the columns sorting as numbers, are checked where the table runs.
    assert 'colspan="7"' in shipped, "the shipped placeholder does not span the table"


# --- the table itself, executed for real under node -------------------------

TABLE_DOM_FIXTURE = """
function makeNode(tag) {
  const node = {
    tag, children: [], className: '', textContent: '', html: '', style: {}, attrs: {},
    append(...kids) { kids.forEach((kid) => this.children.push(kid)); },
    appendChild(kid) { this.children.push(kid); return kid; },
    replaceChildren(...kids) { this.children = kids; },
    insertAdjacentHTML(_position, markup) { node.html += markup; },
    closest: () => table,
    querySelectorAll: () => [],
    setAttribute(name, value) { node.attrs[name] = value; },
  };
  return node;
}
const ids = { toolsTable: makeNode('tbody') };
const table = makeNode('table');
const document = {
  createElement: makeNode,
  createDocumentFragment: () => makeNode('fragment'),
  getElementById: (id) => ids[id],
};
let sortSetups = 0;
function setupBreakdownSort() { sortSetups += 1; }
function createToolIdentity(tool) { return makeNode(`identity:${tool}`); }
const t = (key) => key;
const formatTokenCount = (v) => `t${v}`;
const formatNumber = (v) => `n${v}`;
const formatCurrency = (v) => `$${v}`;
const formatDurationUnits = (ms) => `d${ms}`;
function formatHitRate(rate) {
  return rate === null || rate === undefined ? 'n/a' : `${(Number(rate) * 100).toFixed(1)}%`;
}
// The figures printed in the table's first row, after the tool's own cell.
const figures = () => (ids.toolsTable.children[0].children[0].html
  .match(/<td[^>]*>([^<]*)<\\/td>/g) || []).map(td => td.replace(/.*>([^<]*)<.*/, '$1'));
"""

FIXTURE = TABLE_DOM_FIXTURE + """
// Codex has activity and messages; Claude recorded sessions but nothing was read
// for its runtime; OpenClaw has neither, because its source answers neither query.
lastByTool = {
  codex: { tokens: 1000, cache_hit_rate: 0.5, cost: 10 },
  claude: { tokens: 500, cache_hit_rate: null, cost: 0 },
  openclaw: { tokens: 2000, cache_hit_rate: 0.25, cost: 5 },
  idle: { tokens: 0, cache_hit_rate: null, cost: 0 },
};
overviewActiveTimeState.data = {
  by_tool: {
    codex: { session_count: 4, active_ms_sum: 7200000 },
    claude: { session_count: 0, active_ms_sum: 0 },
  },
};
overviewFacetsState.toolMessages = { codex: 40, claude: 0 };

const cells = () => {
  const rows = ids.toolsTable.children[0].children;
  return rows.map((tr) => ({
    name: tr.children[0].children[0].tag,
    figures: (tr.html.match(/<td[^>]*>([^<]*)<\\/td>/g) || []).map((td) => td.replace(/.*>([^<]*)<.*/, '$1')),
  }));
};

const out = {};
updateToolsTable(lastByTool);
out.byTokens = cells();
out.sortSetups = sortSetups;

breakdownSortState.tools = { field: 'sessions', direction: 'desc' };
updateToolsTable(lastByTool);
out.bySessions = cells().map((row) => row.name);

breakdownSortState.tools = { field: 'activeMs', direction: 'asc' };
updateToolsTable(lastByTool);
out.byActive = cells().map((row) => row.name);

breakdownSortState.tools = { field: 'messages', direction: 'desc' };
updateToolsTable(lastByTool);
out.byMessages = cells().map((row) => row.name);

// Nine ranks below forty. The borrowed columns are counts and milliseconds, and
// sorted as text "9" would outrank "40".
overviewActiveTimeState.data = { by_tool: {
  codex: { session_count: 9, active_ms_sum: 9 },
  claude: { session_count: 40, active_ms_sum: 40 },
} };
overviewFacetsState.toolMessages = { codex: 9, claude: 40 };
out.nineBelowForty = {};
for (const field of ['sessions', 'activeMs', 'messages']) {
  breakdownSortState.tools = { field, direction: 'desc' };
  updateToolsTable(lastByTool);
  out.nineBelowForty[field] = cells().map((row) => row.name).slice(0, 2);
}

// Neither facet answered: every borrowed column is a dash, not a zero.
overviewActiveTimeState.data = null;
overviewFacetsState.toolMessages = null;
breakdownSortState.tools = { field: 'tokens', direction: 'desc' };
updateToolsTable(lastByTool);
out.noActivity = cells();

// A range with nothing in it.
updateToolsTable({});
out.empty = ids.toolsTable.children[0].innerHTML;

process.stdout.write(JSON.stringify(out));
"""


def _table_body(source: str) -> str:
    parts = [
        extract_js_function(source, sig)
        for sig in (
            "function getBreakdownComparator(field, direction) {",
            "function updateToolsTable(byTool) {",
        )
    ]
    parts.append(_declaration(source, "const BREAKDOWN_NUMERIC_FIELDS"))
    parts.append(_declaration(source, "const OVERVIEW_TABLE_DASH"))
    parts.append(extract_js_function(source, "const breakdownSortState = {"))
    parts.append("let lastByTool = {};")
    parts.append("const overviewActiveTimeState = { status: 'idle', data: null, key: null, requestId: 0 };")
    parts.append("const overviewFacetsState = { key: null, rows: [], toolMessages: null, status: 'idle', requestId: 0 };")
    return "\n".join(parts)


@needs_node
def test_the_table_prints_the_shared_column_order_and_says_what_it_lacks(tmp_path: Path) -> None:
    source = _source()
    out = _run_node(tmp_path, "tools-table.js", _table_body(source) + FIXTURE)

    assert [row["name"] for row in out["byTokens"]] == [
        "identity:openclaw",
        "identity:codex",
        "identity:claude",
    ], "a tool with no tokens gets no row"
    assert [row["figures"] for row in out["byTokens"]] == [
        ["t2000", "25.0%", "\u2014", "\u2014", "$5", "\u2014"],
        ["t1000", "50.0%", "n4", "d7200000", "$10", "n40"],
        ["t500", "n/a", "n0", "d0", "$0", "n0"],
    ], out["byTokens"]
    # Claude's zero sessions is Claude's own zero; OpenClaw's dash is the page
    # having never been told. The two must not print the same thing.
    assert out["sortSetups"] >= 1, "the headers never got their click handlers"

    # A tool nobody measured for ranks with the zeros rather than inventing a
    # third tier, which is how the hit-rate column has always behaved.
    assert out["bySessions"] == ["identity:codex", "identity:claude", "identity:openclaw"]
    assert out["byActive"] == ["identity:claude", "identity:openclaw", "identity:codex"]
    assert out["byMessages"] == ["identity:codex", "identity:claude", "identity:openclaw"]
    for field, order in out["nineBelowForty"].items():
        assert order == ["identity:claude", "identity:codex"], f"{field} sorted as text"

    assert [row["figures"] for row in out["noActivity"]][0] == ["t2000", "25.0%", "\u2014", "\u2014", "$5", "\u2014"]
    assert 'colspan="7"' in out["empty"] and "noData" in out["empty"]


def _active_time_body() -> str:
    source = _source()
    functions = "\n".join(
        extract_js_function(source, signature)
        for signature in (
            "function windowKeyFor(customDays, dateFrom, dateTo) {",
            "function activeTimeRequestKey(customDays, dateFrom, dateTo) {",
            "function pctChange(current, previous) {",
            "function combineActiveTimePayloads(payloads) {",
            "function invalidateOverviewActiveTime(customDays, dateFrom, dateTo) {",
            "async function loadOverviewActiveTime(customDays, dateFrom, dateTo, options = {}) {",
            "function paintToolsTableIfLoaded() {",
        )
    )
    return _table_body(source) + functions + TABLE_DOM_FIXTURE + """
let includeCodexReviewSessions = null;
function selectedServers() { return [{ id: 'local' }]; }
function renderOverviewActiveTime() {}
const fetches = [];
function fetchSelectedServers(url) {
  return new Promise((resolve, reject) => fetches.push({ url, resolve, reject }));
}
const reply = [{ server: { id: 'local' }, payload: {
  active_ms: 7200000, active_ms_sum: 7200000, unavailable_tools: [],
  by_tool: { codex: { session_count: 4, active_ms_sum: 7200000 } },
} }];
"""


def test_the_runtime_columns_are_repainted_when_active_time_lands(tmp_path: Path) -> None:
    """Usage paints the table first, so the active-time reply has to come back for it.

    And only once usage has painted it: a reply that lands first must leave the
    loading row alone rather than print "no data" over a table still on its way.
    The facets reply does the same for the message column, which
    test_a_new_server_load_clears_the_previous_message_cell runs.
    """
    body = _active_time_body() + """
(async () => {
  const out = {};
  ids.toolsTable.children = ['LOADING'];
  const early = loadOverviewActiveTime(null, 'X', 'X');
  fetches[0].resolve(reply);
  await early;
  out.beforeUsage = ids.toolsTable.children;

  lastByTool = { codex: { tokens: 1000, cache_hit_rate: 0.5, cost: 10 } };
  overviewActiveTimeState.data = null;
  updateToolsTable(lastByTool);
  out.usageOnly = figures();
  const late = loadOverviewActiveTime(null, 'X', 'X');
  fetches[1].resolve(reply);
  await late;
  out.afterActiveTime = figures();
  process.stdout.write(JSON.stringify(out));
})();
"""
    out = _run_node(tmp_path, "active-time-repaint.js", body)

    assert out["beforeUsage"] == ["LOADING"], "an empty range painted over the loading table"
    assert out["usageOnly"] == ["t1000", "50.0%", "—", "—", "$10", "—"]
    assert out["afterActiveTime"] == ["t1000", "50.0%", "n4", "d7200000", "$10", "—"], (
        "the sessions and runtime columns never reached the table"
    )


def _loader_body() -> str:
    source = _source()
    functions = "\n".join(
        extract_js_function(source, signature)
        for signature in (
            "function windowKeyFor(customDays, dateFrom, dateTo) {",
            "function usageServerKeyFor(servers = selectedServers()) {",
            "function combineOverviewFacets(payloads) {",
            "function clearOverviewFacets() {",
            "async function loadOverviewFacets(customDays, dateFrom, dateTo, options = {}) {",
            "function paintToolsTableIfLoaded() {",
        )
    )
    return _table_body(source) + functions + TABLE_DOM_FIXTURE + """
let selected = [{ id: 'local' }];
function selectedServers() { return selected; }
function updateProjectDetailsTable() {}
const fetches = [];
function fetchSelectedServers() {
  return new Promise((resolve, reject) => fetches.push({ resolve, reject }));
}
const payload = (project, messages) => [{ payload: {
  totals: { tokens: 1000 },
  tools: { ranked: [{ tool: 'codex', messages }] },
  projects: { projects: [{ project, tokens: 1000 }] },
} }];
lastByTool = { codex: { tokens: 1000, cost: 10 } };
"""


def test_a_failed_refresh_repaints_the_visible_message_cell(tmp_path: Path) -> None:
    body = _loader_body() + """
(async () => {
  overviewFacetsState.key = 'X_X|local';
  overviewFacetsState.toolMessages = { codex: 40 };
  updateToolsTable(lastByTool);
  const before = figures();
  const loading = loadOverviewFacets(null, 'X', 'X');
  fetches[0].reject(new Error('offline'));
  await loading;
  process.stdout.write(JSON.stringify({ before, after: figures(), status: overviewFacetsState.status }));
})();
"""
    out = _run_node(tmp_path, "failed-facet-refresh.js", body)
    assert out["before"][-1] == "n40"
    assert out["after"][-1] == "\u2014"
    assert out["status"] == "error"


def test_a_new_server_load_clears_the_previous_message_cell(tmp_path: Path) -> None:
    body = _loader_body() + """
(async () => {
  overviewFacetsState.key = 'X_X|local';
  overviewFacetsState.toolMessages = { codex: 40 };
  updateToolsTable(lastByTool);
  selected = [{ id: 'remote' }];
  const loading = loadOverviewFacets(null, 'X', 'X');
  const pending = figures();
  fetches[0].resolve(payload('remote-project', 90));
  await loading;
  process.stdout.write(JSON.stringify({ pending, after: figures(), projects: overviewFacetsState.rows }));
})();
"""
    out = _run_node(tmp_path, "new-server-facets.js", body)
    assert out["pending"][-1] == "\u2014"
    assert out["after"][-1] == "n90"
    assert out["projects"][0]["project"] == "remote-project"


@pytest.mark.parametrize("failed", [False, True])
def test_an_obsolete_server_reply_cannot_commit_or_report_an_error(
    tmp_path: Path, failed: bool,
) -> None:
    body = _loader_body() + """
(async () => {
  const loading = loadOverviewFacets(null, 'X', 'X');
  selected = [{ id: 'remote' }];
  __SETTLE__;
  await loading;
  process.stdout.write(JSON.stringify({
    status: overviewFacetsState.status,
    projects: overviewFacetsState.rows,
    messages: overviewFacetsState.toolMessages,
    figures: figures(),
  }));
})();
""".replace(
        "__SETTLE__",
        "fetches[0].reject(new Error('offline'))" if failed
        else "fetches[0].resolve(payload('obsolete-project', 40))",
    )
    out = _run_node(tmp_path, "obsolete-server-facets.js", body)
    assert out["status"] == "loading"
    assert out["projects"] == []
    assert out["messages"] is None
    assert out["figures"][-1] == "\u2014"
