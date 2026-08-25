"""The Overview's two deferred breakdowns must never sit under another range's label.

The date picker writes its new label before the fetch starts, and Apps & Models and
Combined Models are painted from an idle callback rather than from the render itself.
So they can lag the range twice over: for the whole /api/usage round trip, and again
for as long as the browser withholds an idle slot after the KPI cards update.

String assertions cannot see any of that. These run the real clearing, staleness and
render functions under node against a stub DOM and a hand-driven idle queue.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _extract_js_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start >= 0, f"{signature} not found"
    depth = 0
    body_start = start + len(signature) - 1 if signature.endswith("{") else src.find("{", start)
    for index in range(body_start, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


HARNESS = """
// --- a DOM just deep enough for the functions under test ---------------------
const nodes = {};
function node(id) {
  if (!nodes[id]) {
    nodes[id] = {
      id, textContent: '', innerHTML: '', style: {}, attrs: {}, classes: new Set(),
      classList: {
        toggle(name, on) { on ? nodes[id].classes.add(name) : nodes[id].classes.delete(name); },
        contains(name) { return nodes[id].classes.has(name); },
      },
      setAttribute(name, value) { nodes[id].attrs[name] = value; },
      removeAttribute(name) { delete nodes[id].attrs[name]; },
    };
  }
  return nodes[id];
}
const document = { getElementById: (id) => node(id) };

// Snapshot of what the two breakdown containers are showing right now.
const breakdowns = () => ({
  apps: node('appsBreakdown').innerHTML,
  models: node('allModelsTable').innerHTML,
  toggle: node('combinedModelsToggle').style.display,
  info: node('combinedModelsInfo').textContent,
});
const overviewDimmed = () => node('overview-content').classes.has('is-stale');
const overviewBusy = () => node('overview-content').attrs['aria-busy'] || null;

// --- stand-ins for the rest of the page -------------------------------------
let updateInFlight = false;
let inFlightDashboardKey = null;
let pendingDashboardRequest = null;
let inFlightResultDiscarded = false;
let updateIsManualRefresh = false;
let refreshUiState = 'idle';
let lastUsageResponse = null;
let lastSessionsResponses = null;
let sessionsLoadedKey = null;
let lastWindowKey = null;
let overviewBreakdownWindowKey = null;
let overviewRenderToken = 0;
let lastCombinedModels = [];
let lastAppsBreakdown = null;
let lastByTool = {};
let includeCodexReviewSessions = null;
let currentStartDate = null;
let currentEndDate = null;
const statsCache = { default: null };
const overviewActiveTimeState = { status: 'idle', data: null, key: null, requestId: 0 };
// Servers-tab stand-ins: the extracted updateDashboard references them; not under test here.
const lastUsageRowsByServer = new Map();
function isServersActive() { return false; }
function renderServersTab() {}


const log = { rendered: [], paints: [], stale: [], errors: [], alerts: [] };
const fetches = [];

// Idle callbacks are queued, never run on their own: the point of these tests is
// what happens in the gap the browser is free to leave open.
const idleQueue = [];
function scheduleIdle(fn) { idleQueue.push(fn); }
function runIdle() { idleQueue.splice(0).forEach((fn) => fn()); }

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
function fetchSelectedServers(url) {
  const entry = { url, ...deferred() };
  fetches.push(entry);
  return entry.promise;
}
function fetchSessionsResponses() { return Promise.resolve({}); }
function combineUsagePayloads(payloads) { return { ...payloads[0] }; }
function selectedServers() { return [{ id: 'local' }]; }
function isSessionsActive() { return false; }
function isOverviewActive() { return true; }
function renderSessionsTab() {}
function renderRefreshButton() {}
function setRefreshUiState(state) { refreshUiState = state; }
function setDashboardFetchStatus(error) { log.errors.push(String(error && error.message || error)); }
function updateTimestamp() {}
function scheduleStatsWarm() {}
function loadActivityInsights() { return Promise.resolve(); }
function loadOverviewActiveTime() { return Promise.resolve(); }
function renderOverviewActiveTime() {}
function sleep() { return Promise.resolve(); }
function alert(message) { log.alerts.push(String(message)); }
function t(key) { return key; }

// renderOverviewTab's own collaborators. The two breakdown painters write a marker
// so a test can tell painted content from the cleared placeholder.
function renderOverviewTokenTotal() {}
function fitOverviewKpis() {}
function formatCurrency(value) { return String(value); }
function formatNumber(value) { return String(value); }
function formatHitRate(value) { return String(value); }
function formatTokenCount(value) { return String(value); }
function updateComparisonDeltas() {}
function updateToolChart() {}
function updateModelChart() {}
function updateToolsTable() {}
function renderOverviewProfilePreview() {}
function reconcileTodayProfileContribution(cached) { return cached; }
function updateAppsBreakdown() {
  log.paints.push('apps');
  node('appsBreakdown').innerHTML = 'PAINTED:' + lastRenderedRange;
}
function updateCombinedModelsTable() {
  log.paints.push('models');
  node('allModelsTable').innerHTML = 'PAINTED:' + lastRenderedRange;
}
let lastRenderedRange = null;

// --- the code under test ----------------------------------------------------
__FUNCTIONS__

// renderOverviewTab is extracted whole; wrap it only to record which range painted.
const realRenderOverviewTab = renderOverviewTab;
renderOverviewTab = function (data) {
  lastRenderedRange = data && data.range;
  log.rendered.push(lastRenderedRange);
  return realRenderOverviewTab(data);
};

// --- driving ---------------------------------------------------------------
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const pick = (range, options) => updateDashboard(null, range, range, options || {});

function fetchFor(range) {
  return fetches.find((entry) => entry.url.includes(`date_from=${range}`) && !entry.done);
}
function resolveRange(range) {
  const entry = fetchFor(range);
  if (!entry) throw new Error(`no in-flight request for ${range}`);
  entry.done = true;
  entry.resolve([{ server: { id: 'local' }, payload: { range } }]);
}
function rejectRange(range, message) {
  const entry = fetchFor(range);
  if (!entry) throw new Error(`no in-flight request for ${range}`);
  entry.done = true;
  entry.reject(new Error(message));
}

// One committed, fully painted range, so every scenario starts from a live page
// rather than from the cold-load state where there is nothing to go stale.
async function commit(range) {
  pick(range);
  await settle();
  resolveRange(range);
  await settle(); await settle();
  runIdle();
}

async function main() {
  const scenario = process.argv[2];
  const out = {};

  if (scenario === 'range-change-clears-before-the-fetch-lands') {
    await commit('X');
    out.afterX = breakdowns();
    out.keyAfterX = overviewBreakdownWindowKey;

    pick('Y');                       // label has already moved
    await settle();
    out.duringY = breakdowns();
    out.dimmedDuringY = overviewDimmed();
    out.busyDuringY = overviewBusy();
    out.keyDuringY = overviewBreakdownWindowKey;

    resolveRange('Y');
    await settle(); await settle();
    out.dimmedAfterY = overviewDimmed();
    out.beforeIdle = breakdowns();   // cards are Y's; tables still say loading
    runIdle();
    out.afterIdle = breakdowns();
    out.keyAfterY = overviewBreakdownWindowKey;
  }

  if (scenario === 'same-range-refresh-keeps-its-content') {
    await commit('X');
    log.stale.length = 0;

    pick('X', { forceRefresh: true });
    await settle();
    out.duringRefresh = breakdowns();
    out.dimmed = overviewDimmed();
    out.stale = [...log.stale];
    resolveRange('X');
    await settle(); await settle();
    runIdle();
    out.after = breakdowns();
  }

  if (scenario === 'a-superseded-idle-callback-paints-nothing') {
    await commit('X');
    log.paints.length = 0;

    // X reloads and commits, but the browser withholds its idle slot...
    pick('X2');
    await settle();
    resolveRange('X2');
    await settle(); await settle();
    // ...and the user moves on before it arrives.
    pick('Y');
    await settle();
    resolveRange('Y');
    await settle(); await settle();

    runIdle();                       // four callbacks queued, only Y's may paint
    out.paints = log.paints;
    out.breakdowns = breakdowns();
    out.key = overviewBreakdownWindowKey;
  }

  if (scenario === 'queued-callbacks-die-when-the-range-changes') {
    // X commits but the browser has not handed out an idle slot yet...
    pick('X');
    await settle();
    resolveRange('X');
    await settle(); await settle();
    log.paints.length = 0;

    // ...and the user moves on while X's callbacks are still queued. Y has not
    // even come back yet, so nothing has bumped the token on Y's behalf.
    pick('Y');
    await settle();
    out.duringY = breakdowns();
    runIdle();
    out.afterStaleIdle = breakdowns();
    out.paints = [...log.paints];
    out.key = overviewBreakdownWindowKey;

    resolveRange('Y');
    await settle(); await settle();
    runIdle();
    out.afterY = breakdowns();
    out.keyAfterY = overviewBreakdownWindowKey;
  }

  if (scenario === 'back-to-the-in-flight-range') {
    await commit('T');
    pick('T2');                      // in flight
    await settle();
    pick('Y');                       // queued
    await settle();
    pick('T2');                      // back again; cancels the queue
    await settle();
    resolveRange('T2');
    await settle(); await settle();
    runIdle();
    out.breakdowns = breakdowns();
    out.key = overviewBreakdownWindowKey;
    out.dimmed = overviewDimmed();
    out.busy = overviewBusy();
    out.fetchedY = fetches.some((entry) => entry.url.includes('date_from=Y'));
  }

  if (scenario === 'a-failed-range-change-stays-marked') {
    await commit('X');
    pick('Y');
    await settle();
    out.dimmedWhileLoading = overviewDimmed();
    rejectRange('Y', 'boom');
    await settle(); await settle();
    runIdle();
    out.dimmedAfterFailure = overviewDimmed();
    out.busyAfterFailure = overviewBusy();
    out.breakdownsAfterFailure = breakdowns();
    out.errors = log.errors;
    out.updateInFlight = updateInFlight;

    // Recovering onto a range that does load must clear the marking again.
    pick('Z');
    await settle();
    resolveRange('Z');
    await settle(); await settle();
    runIdle();
    out.dimmedAfterRecovery = overviewDimmed();
    out.breakdownsAfterRecovery = breakdowns();
  }

  if (scenario === 'a-failed-same-range-refresh-is-not-marked') {
    await commit('X');
    pick('X', { forceRefresh: true });
    await settle();
    out.dimmedWhileRefreshing = overviewDimmed();
    rejectRange('X', 'boom');
    await settle(); await settle();
    runIdle();
    out.dimmedAfterFailure = overviewDimmed();
    out.breakdowns = breakdowns();
    out.errors = log.errors;
  }

  if (scenario === 're-render-of-the-same-range-does-not-clear') {
    await commit('X');
    const before = breakdowns();
    renderOverviewTab({ range: 'X' });   // what a tab switch or language change does
    out.beforeIdle = breakdowns();
    runIdle();
    out.afterIdle = breakdowns();
    out.unchangedWhileRerendering = JSON.stringify(before) === JSON.stringify(out.beforeIdle);
    out.key = overviewBreakdownWindowKey;
    out.dimmed = overviewDimmed();
  }

  process.stdout.write(JSON.stringify(out));
}

main();
"""


def _run(tmp_path: Path, scenario: str) -> dict:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, signature)
        for signature in (
            "function windowKeyFor(customDays, dateFrom, dateTo) {",
            "function activeTimeRequestKey(customDays, dateFrom, dateTo) {",
            "function invalidateOverviewActiveTime(customDays, dateFrom, dateTo) {",
            "function clearOverviewBreakdowns() {",
            "function setOverviewState({ pending = false, stale = false } = {}) {",
            "function renderOverviewTab(data) {",
            "async function updateDashboard(customDays = null, dateFrom = null, dateTo = null, options = {}) {",
        )
    )
    # `function` declarations cannot be reassigned under the wrapper above.
    functions = functions.replace(
        "function renderOverviewTab(data) {", "var renderOverviewTab = function (data) {", 1
    )
    harness = tmp_path / f"{scenario}.js"
    harness.write_text(HARNESS.replace("__FUNCTIONS__", functions), encoding="utf-8")
    result = subprocess.run(
        ["node", str(harness), scenario],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def _is_loading(markup: str) -> bool:
    return "tokdash-loading-placeholder" in markup


def test_a_range_change_clears_the_breakdowns_before_its_data_arrives(tmp_path):
    """The label moves first, so the old rows must go with it, not with the response."""
    out = _run(tmp_path, "range-change-clears-before-the-fetch-lands")

    assert out["afterX"]["apps"] == "PAINTED:X"
    assert out["keyAfterX"] == "X_X"

    during = out["duringY"]
    assert _is_loading(during["apps"]), "Apps & Models must not survive into Y's label"
    assert _is_loading(during["models"]), "Combined Models must not survive into Y's label"
    assert during["info"] == "", "the row count belonged to X"
    assert during["toggle"] == "none", "so did the show-all toggle"
    assert out["keyDuringY"] is None
    assert out["dimmedDuringY"] is True
    assert out["busyDuringY"] == "true", "a fetch really is in progress here"

    # The cards commit on the response; the tables wait for their idle slot. That gap
    # is the bug's original window — it must show loading, never X's rows.
    assert out["dimmedAfterY"] is False
    assert _is_loading(out["beforeIdle"]["apps"])
    assert out["afterIdle"]["apps"] == "PAINTED:Y"
    assert out["afterIdle"]["models"] == "PAINTED:Y"
    assert out["keyAfterY"] == "Y_Y"


def test_a_same_range_refresh_keeps_its_content(tmp_path):
    """Refresh re-reads the range already on screen; blanking it would just flicker."""
    out = _run(tmp_path, "same-range-refresh-keeps-its-content")

    assert out["duringRefresh"]["apps"] == "PAINTED:X", "a same-range refresh must not clear"
    assert out["duringRefresh"]["models"] == "PAINTED:X"
    assert out["dimmed"] is False, "the refresh button reports this load itself"
    assert out["stale"] == []
    assert out["after"]["apps"] == "PAINTED:X"


def test_a_superseded_idle_callback_paints_nothing(tmp_path):
    """A callback queued for a range the user has left must not fill the cleared tables."""
    out = _run(tmp_path, "a-superseded-idle-callback-paints-nothing")

    assert out["paints"] == ["apps", "models"], "only the current range may paint"
    assert out["breakdowns"]["apps"] == "PAINTED:Y"
    assert out["breakdowns"]["models"] == "PAINTED:Y"
    assert out["key"] == "Y_Y"


def test_returning_to_the_in_flight_range_still_paints_it(tmp_path):
    """T → Y → T cancels Y's request; the clearing must not strand T's tables empty."""
    out = _run(tmp_path, "back-to-the-in-flight-range")

    assert out["fetchedY"] is False, "Y was superseded before it ran"
    assert out["breakdowns"]["apps"] == "PAINTED:T2"
    assert out["breakdowns"]["models"] == "PAINTED:T2"
    assert out["key"] == "T2_T2"
    assert out["dimmed"] is False
    assert out["busy"] is None


def test_idle_callbacks_queued_before_the_range_moved_paint_nothing(tmp_path):
    """The gap the clear cannot see: X committed, its tables not yet painted, then Y.

    Nothing has rendered for Y at this point, so nothing has bumped the token on Y's
    behalf. Only the clear itself can invalidate X's queued callbacks.
    """
    out = _run(tmp_path, "queued-callbacks-die-when-the-range-changes")

    assert _is_loading(out["duringY"]["apps"]), "the clear runs when the range moves"
    assert out["paints"] == [], "X's queued callbacks must not paint under Y's label"
    assert _is_loading(out["afterStaleIdle"]["apps"]), "and must leave the loading state alone"
    assert _is_loading(out["afterStaleIdle"]["models"])
    assert out["key"] is None
    # Y still paints normally once its own response lands.
    assert out["afterY"]["apps"] == "PAINTED:Y"
    assert out["keyAfterY"] == "Y_Y"


def test_a_failed_range_change_stays_marked(tmp_path):
    """A failed range change leaves another range's numbers under the new label.

    The only other signal is #lastUpdate turning into "Load failed" — one small line
    in the header. Restoring full contrast would present the previous range as this
    one's answer, so the marking stays until a load actually lands.
    """
    out = _run(tmp_path, "a-failed-range-change-stays-marked")

    assert out["dimmedWhileLoading"] is True
    assert out["errors"] == ["boom"]
    assert out["updateInFlight"] is False
    assert out["dimmedAfterFailure"] is True, "what is on screen is still X's"
    # aria-busy means an update is in progress. This one is over and failed, so the
    # region must stop claiming to be busy even though it stays visually marked —
    # otherwise assistive tech waits for an update that is never coming.
    assert out["busyAfterFailure"] is None
    # Restoring the previous range's rows is deliberate: a permanent loading shimmer
    # for a load that already failed would be worse. They stay marked instead.
    assert out["breakdownsAfterFailure"]["apps"] == "PAINTED:X"
    assert out["dimmedAfterRecovery"] is False, "a load that lands clears the marking"
    assert out["breakdownsAfterRecovery"]["apps"] == "PAINTED:Z"


def test_a_failed_same_range_refresh_is_not_marked(tmp_path):
    """The content still belongs to the range on the label, so nothing is misleading."""
    out = _run(tmp_path, "a-failed-same-range-refresh-is-not-marked")

    assert out["dimmedWhileRefreshing"] is False
    assert out["dimmedAfterFailure"] is False
    assert out["breakdowns"]["apps"] == "PAINTED:X"
    assert out["errors"] == ["boom"]


def test_re_rendering_the_same_range_does_not_clear(tmp_path):
    """Tab switches and language changes re-render; they are not range changes."""
    out = _run(tmp_path, "re-render-of-the-same-range-does-not-clear")

    assert out["unchangedWhileRerendering"] is True, "a re-render must not blank the tables"
    assert out["afterIdle"]["apps"] == "PAINTED:X"
    assert out["key"] == "X_X"
    assert out["dimmed"] is False


def test_the_cleared_markup_matches_the_placeholder_the_page_ships(tmp_path):
    """Clearing reuses the initial markup, so the two cannot drift apart."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    cleared = _extract_js_function(source, "function clearOverviewBreakdowns() {")

    for shipped in (
        '<p class="tokdash-loading-placeholder text-center py-10" style="color: var(--color-muted);">'
        '<span class="tokdash-loading-label" data-i18n="loading">Loading…</span></p>',
        '<td colspan="8" class="tokdash-loading-placeholder text-center py-10" style="color: var(--color-muted);">'
        '<span class="tokdash-loading-label" data-i18n="loading">Loading…</span></td>',
    ):
        assert shipped in source, "the initial markup moved; update clearOverviewBreakdowns"

    # Same classes, same colspan, and still translatable after being rewritten.
    assert cleared.count("tokdash-loading-placeholder") == 2
    assert 'data-i18n="loading"' in cleared
    assert 'colspan="8"' in cleared
    assert "style=\"color: var(--color-muted);\"" in cleared
