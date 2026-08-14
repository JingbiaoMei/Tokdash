"""updateDashboard's queue, driven for real under node with deferred fetches.

One dashboard load runs at a time. A request arriving mid-load is queued so a
range change is never dropped while its label is already on screen — which means
the running load must not commit its results once it has been superseded, and the
queue must not outlive the selection that filled it. String assertions cannot see
any of that, so these run the extracted function against controllable promises.
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
// --- stand-ins for the page the function lives in ---------------------------
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
let includeCodexReviewSessions = null;
const overviewActiveTimeState = { status: 'idle', data: null, key: null, requestId: 0 };

const log = { rendered: [], errors: [], alerts: [], refreshStates: [], activeCards: [], activeLoads: [] };
const fetches = [];

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
function renderOverviewTab(data) { log.rendered.push(data && data.range); }
function renderSessionsTab() {}
function renderRefreshButton() {}
function setRefreshUiState(state) { refreshUiState = state; log.refreshStates.push(state); }
function setDashboardFetchStatus(error) { log.errors.push(String(error && error.message || error)); }
function updateTimestamp() {}
function scheduleStatsWarm() {}
function loadActivityInsights() { return Promise.resolve(); }
function loadOverviewActiveTime(customDays, dateFrom, dateTo) {
  log.activeLoads.push(dateFrom);
  return Promise.resolve();
}
function renderOverviewActiveTime() {
  log.activeCards.push(overviewActiveTimeState.status);
}
// The forced-refresh cooldown. Held open on demand so a test can act while the
// finished load is still in flight, which is where the queue is at its most
// fragile: it has thrown its result away but has not released the flag yet.
let heldSleep = null;
function sleep() {
  if (!holdSleep) return Promise.resolve();
  heldSleep = deferred();
  return heldSleep.promise;
}
let holdSleep = false;
function releaseSleep() { heldSleep.resolve(); }
function alert(message) { log.alerts.push(String(message)); }
function t(key) { return key; }

// --- the code under test ----------------------------------------------------
__FUNCTIONS__

// --- driving ---------------------------------------------------------------
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function fetchFor(range) {
  return fetches.find((entry) => entry.url.includes(`date_from=${range}`) && !entry.done);
}

function resolveRange(range, payload) {
  const entry = fetchFor(range);
  if (!entry) throw new Error(`no in-flight request for ${range}`);
  entry.done = true;
  entry.resolve([{ server: { id: 'local' }, payload: { range, ...payload } }]);
}

function rejectRange(range, message) {
  const entry = fetchFor(range);
  if (!entry) throw new Error(`no in-flight request for ${range}`);
  entry.done = true;
  entry.reject(new Error(message));
}

const pick = (range, options) => updateDashboard(null, range, range, options || {});

async function main() {
  const scenario = process.argv[2];
  const out = {};

  if (scenario === 'return-to-in-flight-range') {
    pick('X');
    await settle();
    pick('Y');            // queued
    await settle();
    pick('X');            // back to the one already loading
    await settle();
    out.queuedAfterReturn = pendingDashboardRequest;
    resolveRange('X');
    await settle(); await settle();
    out.urls = fetches.map((entry) => entry.url);
    out.rendered = log.rendered;
    out.lastWindowKey = lastWindowKey;
    out.activeLoads = log.activeLoads;
  }

  if (scenario === 'superseded-result-is-not-committed') {
    pick('X');
    await settle();
    pick('Y');
    await settle();
    resolveRange('X');    // the superseded load finishes first
    await settle(); await settle();
    out.afterStaleResolve = { rendered: [...log.rendered], lastWindowKey, usage: lastUsageResponse };
    resolveRange('Y');
    await settle(); await settle();
    out.rendered = log.rendered;
    out.lastWindowKey = lastWindowKey;
    out.usage = lastUsageResponse && lastUsageResponse.range;
    out.urls = fetches.map((entry) => entry.url);
  }

  if (scenario === 'superseded-failure-is-not-shown') {
    pick('X');
    await settle();
    pick('Y');
    await settle();
    rejectRange('X', 'stale boom');
    await settle(); await settle();
    out.afterStaleFailure = { errors: [...log.errors], alerts: [...log.alerts] };
    rejectRange('Y', 'current boom');
    await settle(); await settle();
    out.errors = log.errors;
    out.alerts = log.alerts;
    out.updateInFlight = updateInFlight;
    out.pending = pendingDashboardRequest;
  }

  if (scenario === 'return-to-x-after-its-result-was-discarded') {
    holdSleep = true;
    pick('X', { forceRefresh: true });
    await settle();
    pick('Y');            // queued
    await settle();
    resolveRange('X');    // X finishes superseded and throws its result away
    await settle(); await settle();
    // Still "in flight": the refresh cooldown has not released the flag yet.
    out.stillInFlight = updateInFlight;
    pick('X');            // and the user comes back to X inside that window
    await settle();
    out.pendingDuringCooldown = pendingDashboardRequest && pendingDashboardRequest.dateFrom;
    releaseSleep();
    await settle(); await settle();
    out.urls = fetches.map((entry) => entry.url);
    out.renderedBeforeRefetch = [...log.rendered];
    if (fetchFor('X')) {
      resolveRange('X');  // the re-issued request
      await settle(); await settle();
    }
    out.rendered = log.rendered;
    out.lastWindowKey = lastWindowKey;
    out.updateInFlight = updateInFlight;
  }

  if (scenario === 'error-does-not-leak-into-the-next-range') {
    overviewActiveTimeState.status = 'error';
    overviewActiveTimeState.key = 'old';
    overviewActiveTimeState.data = null;
    invalidateOverviewActiveTime(null, 'Z', 'Z');
    out.statusesRendered = log.activeCards;
    out.status = overviewActiveTimeState.status;
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
            "async function updateDashboard(customDays = null, dateFrom = null, dateTo = null, options = {}) {",
        )
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


def test_returning_to_the_in_flight_range_cancels_the_queue(tmp_path):
    """X running, Y picked, X picked again: Y must not run afterwards."""
    out = _run(tmp_path, "return-to-in-flight-range")

    assert out["queuedAfterReturn"] is None, "the intermediate pick is stale once X is chosen again"
    assert [url for url in out["urls"] if "date_from=Y" in url] == [], "Y must never be fetched"
    assert out["rendered"] == ["X"]
    assert out["lastWindowKey"] == "X_X"
    # The deferred active-time fetch follows the range actually on screen.
    assert out["activeLoads"] == ["X"]


def test_a_superseded_load_commits_nothing(tmp_path):
    """X finishing after Y was queued must not render under Y's label."""
    out = _run(tmp_path, "superseded-result-is-not-committed")

    stale = out["afterStaleResolve"]
    assert stale["rendered"] == [], "the superseded range must not paint"
    assert stale["lastWindowKey"] is None
    assert stale["usage"] is None
    # Only the queued range commits, and only after its own request lands.
    assert out["rendered"] == ["Y"]
    assert out["usage"] == "Y"
    assert out["lastWindowKey"] == "Y_Y"
    assert sum("date_from=Y" in url for url in out["urls"]) == 1


def test_a_superseded_failure_is_not_shown(tmp_path):
    """The user is waiting on another range; the old one's error is not theirs."""
    out = _run(tmp_path, "superseded-failure-is-not-shown")

    stale = out["afterStaleFailure"]
    assert stale["errors"] == [], "a superseded failure must not reach the status line"
    assert stale["alerts"] == []
    # The queued request reports its own outcome, and the queue drains.
    assert out["errors"] == ["current boom"]
    assert out["alerts"] == ["Failed to fetch data. Check console for details."]
    assert out["updateInFlight"] is False
    assert out["pending"] is None


def test_returning_to_a_range_whose_result_was_discarded_refetches(tmp_path):
    """The in-flight load has nothing left to commit, so waiting on it shows nothing.

    A forced refresh keeps updateInFlight true through its cooldown, well after
    it has been superseded and dropped its own result. Coming back to that range
    during the cooldown must re-request it, not just cancel the queue.
    """
    out = _run(tmp_path, "return-to-x-after-its-result-was-discarded")

    assert out["stillInFlight"] is True, "the scenario must act inside the cooldown"
    assert out["pendingDuringCooldown"] == "X", "returning to X must queue X, not clear the queue"
    assert out["renderedBeforeRefetch"] == [], "the discarded result must stay discarded"
    assert sum("date_from=X" in url for url in out["urls"]) == 2, "X must be fetched again"
    assert sum("date_from=Y" in url for url in out["urls"]) == 0, "Y was superseded before it ran"
    # And the re-issued request is what finally lands on screen.
    assert out["rendered"] == ["X"]
    assert out["lastWindowKey"] == "X_X"
    assert out["updateInFlight"] is False


def test_a_new_range_does_not_open_on_the_old_range_error(tmp_path):
    """Clearing the card must clear its error state, not just its value."""
    out = _run(tmp_path, "error-does-not-leak-into-the-next-range")

    assert out["statusesRendered"] == ["loading"], "the card must not render as failed"
    assert out["status"] != "error"
