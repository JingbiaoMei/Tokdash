"""The usage-DB schema remediation must survive the trip to the dashboard.

The backend fails fast with an actionable message ("run `tokdash update`"), but
that only helps if the UI shows it. Previously `fetchSelectedServers` swallowed
every rejection and returned [], and each caller substituted a generic
`loadFailed` -- so a user saw "不可用：Local / 数据加载失败" and had nothing to act
on. These tests pin the message end to end through the two frontend seams.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from support.frontend import extract_js_function, index_source, needs_node, run_node


# What the backend actually puts in the HTTP 500 body (api.py -> str(exc)).
SCHEMA_DETAIL = (
    "usage database schema 11 at /home/u/.tokdash/usage.sqlite3 is newer than "
    "this Tokdash supports (<= 9); run `tokdash update` and restart any other "
    "Tokdash processes using this data directory"
)


def _functions() -> str:
    src = index_source()
    return "\n".join(
        [
            extract_js_function(src, "function collectServerResults("),
            extract_js_function(src, "async function fetchSelectedServers("),
            extract_js_function(src, "function dashboardFetchStatusText("),
            extract_js_function(src, "function setDashboardFetchStatus("),
        ]
    )


# Stubs for everything the two extracted functions touch. `t()` returns the key
# so an assertion can tell "the i18n key was used" from "the detail was kept".
HARNESS_PREAMBLE = """
const scenario = JSON.parse(process.argv[2]);
const serverRuntimeStatus = new Map();
let renderCalls = 0;
let indicatorCalls = 0;
function renderServerSettings() { renderCalls += 1; }
function updateServerIndicator() { indicatorCalls += 1; }
function t(key) { return key; }
function selectedServers() { return scenario.servers.map((s) => ({ id: s.id, label: s.label })); }
function makeError(spec) {
  const error = new Error(spec.message);
  if (spec.status !== undefined && spec.status !== null) error.status = spec.status;
  return error;
}
async function fetchJsonWithRetry(server) {
  const spec = scenario.servers.find((s) => s.id === server.id);
  if (spec.fails) throw makeError(spec);
  return spec.payload;
}
"""


def _run_node(tmp_path: Path, body: str, scenario: dict):
    harness = tmp_path / "harness.js"
    harness.write_text(_functions() + HARNESS_PREAMBLE + body, encoding="utf-8")
    return json.loads(run_node(harness, json.dumps(scenario)))


FETCH_BODY = """
(async () => {
  const out = {};
  try {
    const rows = await fetchSelectedServers('/api/usage?period=today');
    out.threw = false;
    out.rows = rows.map((row) => ({ id: row.server.id, payload: row.payload }));
  } catch (error) {
    out.threw = true;
    out.message = error.message;
    out.status = error.status === undefined ? null : error.status;
    out.statusText = dashboardFetchStatusText(error);
  }
  out.failedTooltips = [...serverRuntimeStatus.entries()]
    .filter(([, state]) => !state.ok)
    .map(([id, state]) => ({ id, message: state.error ? state.error.message : null }));
  process.stdout.write(JSON.stringify(out));
})();
"""


@needs_node
def test_sole_server_failure_propagates_the_schema_message(tmp_path: Path) -> None:
    out = _run_node(
        tmp_path,
        FETCH_BODY,
        {"servers": [{"id": "local", "label": "Local", "fails": True, "status": 500, "message": SCHEMA_DETAIL}]},
    )
    assert out["threw"] is True
    # The ORIGINAL rejection, not a substituted generic error.
    assert out["message"] == SCHEMA_DETAIL
    assert out["status"] == 500
    # ...and it reaches the status line the user actually reads.
    assert "tokdash update" in out["statusText"]
    assert "loadFailed" in out["statusText"]


@needs_node
def test_all_servers_failing_propagates_the_first_rejection(tmp_path: Path) -> None:
    out = _run_node(
        tmp_path,
        FETCH_BODY,
        {
            "servers": [
                {"id": "local", "label": "Local", "fails": True, "status": 500, "message": SCHEMA_DETAIL},
                {"id": "remote", "label": "Remote", "fails": True, "status": 500, "message": "something else"},
            ]
        },
    )
    assert out["threw"] is True
    assert out["message"] == SCHEMA_DETAIL
    # Every failed server keeps its own reason for the per-server tooltip.
    assert {row["id"] for row in out["failedTooltips"]} == {"local", "remote"}
    assert any("tokdash update" in (row["message"] or "") for row in out["failedTooltips"])


@needs_node
def test_partial_success_still_returns_rows_and_does_not_throw(tmp_path: Path) -> None:
    """One healthy server must keep the dashboard rendering, as before."""
    out = _run_node(
        tmp_path,
        FETCH_BODY,
        {
            "servers": [
                {"id": "local", "label": "Local", "fails": True, "status": 500, "message": SCHEMA_DETAIL},
                {"id": "remote", "label": "Remote", "fails": False, "payload": {"total_cost": 1.5}},
            ]
        },
    )
    assert out["threw"] is False
    assert [row["id"] for row in out["rows"]] == ["remote"]
    assert out["rows"][0]["payload"] == {"total_cost": 1.5}
    # The broken server is still flagged, with its reason retained.
    assert out["failedTooltips"] == [{"id": "local", "message": SCHEMA_DETAIL}]


@needs_node
def test_no_servers_selected_returns_empty_without_throwing(tmp_path: Path) -> None:
    out = _run_node(tmp_path, FETCH_BODY, {"servers": []})
    assert out["threw"] is False and out["rows"] == []


STATUS_BODY = """
process.stdout.write(JSON.stringify(
  scenario.cases.map((spec) => dashboardFetchStatusText(makeError(spec)))
));
"""


@needs_node
def test_status_text_keeps_detail_but_not_for_503(tmp_path: Path) -> None:
    out = _run_node(
        tmp_path,
        STATUS_BODY,
        {
            "cases": [
                {"status": 500, "message": SCHEMA_DETAIL},
                {"status": 503, "message": "Too many cold requests; retry shortly"},
                {"status": None, "message": ""},
            ]
        },
    )
    schema_text, overload_text, empty_text = out
    assert "tokdash update" in schema_text
    # 503 is the transient signal: it keeps its own wording and gains no detail.
    assert overload_text == "temporaryOverload"
    assert "retry shortly" not in overload_text
    # No message to show falls back to the plain key, with no dangling separator.
    assert empty_text == "loadFailed"


LAST_UPDATE_BODY = """
const lastUpdate = { textContent: 'Updated 12:00' };
const document = { getElementById: (id) => (id === 'lastUpdate' ? lastUpdate : null) };
process.stdout.write(JSON.stringify(scenario.cases.map((spec) => {
  setDashboardFetchStatus(makeError(spec));
  return lastUpdate.textContent;
})));
"""


@needs_node
def test_the_status_line_shows_the_remediation(tmp_path: Path) -> None:
    """setDashboardFetchStatus writes the persistent #lastUpdate line, and that line
    is where a dashboard-wide failure is read: it must carry the same detail as the
    rest of the page, and keep the overload wording for a 503."""
    out = _run_node(
        tmp_path,
        LAST_UPDATE_BODY,
        {
            "cases": [
                {"status": 500, "message": SCHEMA_DETAIL},
                {"status": 503, "message": "Too many cold requests; retry shortly"},
            ]
        },
    )
    assert out == [f"loadFailed: {SCHEMA_DETAIL}", "temporaryOverload"]


# --- Quota tab ---------------------------------------------------------------
# The Quota tab runs its own Promise.allSettled instead of fetchSelectedServers,
# so fixing the shared loader did not fix it. These tests drive the REAL loadQuota
# against stubs rather than re-implementing its settlement logic.

QUOTA_PREAMBLE = """
const scenario = JSON.parse(process.argv[2]);
const serverRuntimeStatus = new Map();
const LOCAL_SERVER = { id: 'local', label: 'Local' };
function localHost() { return LOCAL_SERVER; }
let lastQuotaServerRows = [];
let lastQuotaPayload = null;
let lastQuotaHistory = null;
// loadQuota() records which server owns the single-server UI. The renderers it repaints
// are stubbed below, so the scope helpers only have to answer without throwing.
let quotaSingleScopeOwner = null;
let quotaLoaded = false;
let quotaActionStatusResetTimer = null;
const quotaUtilizationCharts = new Map();
const quotaConsumptionCharts = new Map();
let renderCalls = 0;
let indicatorCalls = 0;
const rendered = [];

function makeEl() {
  return {
    style: {}, textContent: '', dataset: {}, children: [],
    removeAttribute() {}, setAttribute() {}, appendChild() {}, insertBefore() {}, remove() {},
    querySelector() { return makeEl(); }, querySelectorAll() { return []; },
  };
}
const elements = {};
const document = { getElementById: (id) => (elements[id] = elements[id] || makeEl()) };

function t(key) { return key; }
function renderServerSettings() { renderCalls += 1; }
function updateServerIndicator() { indicatorCalls += 1; }
function selectedServers() { return scenario.servers.map((s) => ({ id: s.id, label: s.label })); }
function currentQuotaRange() { return '24h'; }
function quotaRangeSeconds() { return 86400; }
function positionQuotaGlobalControls() {}
function setQuotaSingleServerLayout() {}
function ensureQuotaServerBlocks() { return makeEl(); }
function renderQuotaServerBlock(server) { rendered.push(server.id); }
function renderQuotaSettings() {}
function renderQuotaProviderCards() {}
function renderQuotaCharts() {}
function syncQuotaSingleVisibility() {}
function quotaSingleScopeServer() { return LOCAL_SERVER; }
function quotaSingleScopeHostKey() { return 'local'; }
function quotaPresentProviders() { return new Set(); }
function makeError(spec) {
  const error = new Error(spec.message);
  if (spec.status !== undefined && spec.status !== null) error.status = spec.status;
  return error;
}
async function fetchJsonWithRetry(server) {
  const spec = scenario.servers.find((s) => s.id === server.id);
  if (spec.fails) throw makeError(spec);
  return spec.payload;
}

(async () => {
  await loadQuota();
  const statusEl = quotaStatusElement();
  process.stdout.write(JSON.stringify({
    statusText: statusEl.textContent,
    statusColor: statusEl.style.color || null,
    indicatorCalls,
    renderCalls,
    rows: lastQuotaServerRows.map((row) => row.server.id),
    quotaLoaded,
    failed: [...serverRuntimeStatus.entries()]
      .filter(([, state]) => !state.ok)
      .map(([id, state]) => ({ id, message: state.error ? state.error.message : null })),
  }));
})();
"""


def _run_quota(tmp_path: Path, scenario: dict):
    src = index_source()
    functions = "\n".join(
        [
            extract_js_function(src, "function collectServerResults("),
            extract_js_function(src, "function dashboardFetchStatusText("),
            extract_js_function(src, "function quotaStatusElement("),
            extract_js_function(src, "function setQuotaFetchStatus("),
            extract_js_function(src, "async function loadQuota("),
        ]
    )
    harness = tmp_path / "quota.js"
    harness.write_text(functions + QUOTA_PREAMBLE, encoding="utf-8")
    return json.loads(run_node(harness, json.dumps(scenario)))


@needs_node
def test_quota_total_failure_shows_the_schema_remediation(tmp_path: Path) -> None:
    out = _run_quota(
        tmp_path,
        {"servers": [{"id": "local", "label": "Local", "fails": True, "status": 500, "message": SCHEMA_DETAIL}]},
    )
    # Previously this path threw a generic loadFailed and only console.error'd it,
    # leaving the Quota tab blank with no remediation on screen.
    assert "tokdash update" in out["statusText"]
    assert out["statusColor"] == "#DC2626"
    # The indicator must be refreshed after serverRuntimeStatus is updated.
    assert out["indicatorCalls"] >= 1 and out["renderCalls"] >= 1
    assert out["failed"] == [{"id": "local", "message": SCHEMA_DETAIL}]
    assert out["quotaLoaded"] is False


@needs_node
def test_quota_total_failure_keeps_the_first_rejection(tmp_path: Path) -> None:
    out = _run_quota(
        tmp_path,
        {
            "servers": [
                {"id": "local", "label": "Local", "fails": True, "status": 500, "message": SCHEMA_DETAIL},
                {"id": "remote", "label": "Remote", "fails": True, "status": 500, "message": "unrelated failure"},
            ]
        },
    )
    assert "tokdash update" in out["statusText"]
    assert {row["id"] for row in out["failed"]} == {"local", "remote"}


@needs_node
def test_quota_partial_success_still_renders_and_clears_status(tmp_path: Path) -> None:
    out = _run_quota(
        tmp_path,
        {
            "servers": [
                {"id": "local", "label": "Local", "fails": True, "status": 500, "message": SCHEMA_DETAIL},
                {"id": "remote", "label": "Remote", "fails": False, "payload": {"enabled": True}},
            ]
        },
    )
    assert out["rows"] == ["remote"]
    assert out["quotaLoaded"] is True
    # A healthy server means no blocking error banner...
    assert out["statusText"] == ""
    # ...but the broken one is still flagged with its reason for the tooltip.
    assert out["failed"] == [{"id": "local", "message": SCHEMA_DETAIL}]


@needs_node
def test_quota_success_clears_a_stale_error(tmp_path: Path) -> None:
    out = _run_quota(
        tmp_path,
        {"servers": [{"id": "local", "label": "Local", "fails": False, "payload": {"enabled": True}}]},
    )
    assert out["rows"] == ["local"] and out["quotaLoaded"] is True
    assert out["statusText"] == "" and out["failed"] == []


REFRESH_PREAMBLE = """
const scenario = JSON.parse(process.argv[2]);
let updateInFlight = false;
let refreshUiState = 'idle';
let quotaActionStatusResetTimer = null;
const refreshStates = [];
function setRefreshUiState(state) { refreshStates.push(state); }
function startRefreshCooldown() { refreshStates.push('cooldown'); }
function parseCooldownSeconds() { return 1; }
function makeEl() { return { style: {}, textContent: '' }; }
const elements = {};
const document = { getElementById: (id) => (elements[id] = elements[id] || makeEl()) };
function t(key) { return key; }
function selectedServers() { return scenario.servers.map((s) => ({ id: s.id, label: s.label })); }
function makeError(spec) {
  const error = new Error(spec.message);
  if (spec.status !== undefined && spec.status !== null) error.status = spec.status;
  return error;
}
async function fetchJsonWithRetry(server) {
  const spec = scenario.servers.find((s) => s.id === server.id);
  if (spec.fails) throw makeError(spec);
  return { ok: true };
}
let loadQuotaCalls = 0;
async function loadQuota() { loadQuotaCalls += 1; return scenario.reloadOk !== false; }

(async () => {
  await refreshQuotaViaGlobalButton();
  const el = quotaStatusElement();
  process.stdout.write(JSON.stringify({
    statusText: el.textContent,
    refreshStates,
    loadQuotaCalls,
  }));
})();
"""


def _run_refresh(tmp_path: Path, scenario: dict):
    src = index_source()
    functions = "\n".join(
        [
            extract_js_function(src, "function dashboardFetchStatusText("),
            extract_js_function(src, "function quotaStatusElement("),
            extract_js_function(src, "function setQuotaFetchStatus("),
            extract_js_function(src, "async function refreshQuotaViaGlobalButton("),
        ]
    )
    harness = tmp_path / "refresh.js"
    harness.write_text(functions + REFRESH_PREAMBLE, encoding="utf-8")
    return json.loads(run_node(harness, json.dumps(scenario)))


@needs_node
def test_quota_refresh_button_surfaces_the_schema_remediation(tmp_path: Path) -> None:
    """A failed explicit refresh must leave the remediation on screen.

    The button state alone says only "failed" and resets after 2.6s.
    """
    out = _run_refresh(
        tmp_path,
        {"servers": [{"id": "local", "label": "Local", "fails": True, "status": 500, "message": SCHEMA_DETAIL}]},
    )
    assert "tokdash update" in out["statusText"]
    assert "failed" in out["refreshStates"]
    assert out["loadQuotaCalls"] == 0


@needs_node
def test_quota_refresh_button_leaves_no_banner_for_503(tmp_path: Path) -> None:
    """503 is transient and already has its own 'busy' button state."""
    out = _run_refresh(
        tmp_path,
        {"servers": [{"id": "local", "label": "Local", "fails": True, "status": 503, "message": "Too many cold requests"}]},
    )
    assert out["statusText"] == ""
    assert "busy" in out["refreshStates"]


# --- settings write + reload -------------------------------------------------
# A settings POST is followed by a forced reload. When the POST succeeds but the
# reload fails, acknowledging "saved" both overwrote the red fetch error and
# scheduled its removal -- so the remediation flashed and vanished.

WRITE_PREAMBLE = """
const scenario = JSON.parse(process.argv[2]);
const serverRuntimeStatus = new Map();
const LOCAL_SERVER = { id: 'local', label: 'Local' };
function localHost() { return LOCAL_SERVER; }
let lastQuotaServerRows = [];
let lastQuotaPayload = null;
let lastQuotaHistory = null;
let quotaSingleScopeOwner = null;
let quotaLoaded = false;
let quotaActionStatusResetTimer = null;
const quotaUtilizationCharts = new Map();
const quotaConsumptionCharts = new Map();
let postCalls = 0;

function makeEl() {
  return {
    style: {}, textContent: '', dataset: {}, children: [],
    removeAttribute() {}, setAttribute() {}, appendChild() {}, insertBefore() {}, remove() {},
    querySelector() { return makeEl(); }, querySelectorAll() { return []; },
  };
}
const elements = {};
const document = { getElementById: (id) => (elements[id] = elements[id] || makeEl()) };

function t(key) { return key; }
function renderServerSettings() {}
function updateServerIndicator() {}
function selectedServers() { return [LOCAL_SERVER]; }
function currentQuotaRange() { return '24h'; }
function quotaRangeSeconds() { return 86400; }
function positionQuotaGlobalControls() {}
function setQuotaSingleServerLayout() {}
function ensureQuotaServerBlocks() { return makeEl(); }
function renderQuotaServerBlock() {}
function renderQuotaSettings() {}
function renderQuotaProviderCards() {}
function renderQuotaCharts() {}
function syncQuotaSingleVisibility() {}
function quotaSingleScopeServer() { return LOCAL_SERVER; }
function quotaSingleScopeHostKey() { return 'local'; }
function quotaPresentProviders() { return new Set(); }
async function postJsonWithCsrf() { postCalls += 1; return { ok: true }; }
async function fetchJsonWithRetry() {
  if (scenario.reloadFails) {
    const error = new Error(scenario.message);
    error.status = 500;
    throw error;
  }
  return { enabled: true };
}

(async () => {
  await __WRITE__;
  process.stdout.write(JSON.stringify({
    statusText: quotaStatusElement().textContent,
    statusColor: quotaStatusElement().style.color || null,
    postCalls,
    quotaLoaded,
  }));
  process.exit(0);
})();
"""


# Every settings writer the Quota tab has, and a call that sends one write.
QUOTA_WRITERS = [
    pytest.param("async function enableQuotaProvider(", "enableQuotaProvider('codex_api')", id="enableQuotaProvider"),
    pytest.param("async function setQuotaCredentialScan(", "setQuotaCredentialScan(true)", id="setQuotaCredentialScan"),
    pytest.param("async function setQuotaEnabled(", "setQuotaEnabled(true)", id="setQuotaEnabled"),
    pytest.param("async function setQuotaInterval(", "setQuotaInterval(15)", id="setQuotaInterval"),
]


def _run_write(tmp_path: Path, scenario: dict, writer: str, call: str):
    src = index_source()
    functions = "\n".join(
        [
            extract_js_function(src, "function collectServerResults("),
            extract_js_function(src, "function dashboardFetchStatusText("),
            extract_js_function(src, "function quotaStatusElement("),
            extract_js_function(src, "function setQuotaFetchStatus("),
            extract_js_function(src, "function setQuotaActionStatus("),
            extract_js_function(src, "async function loadQuota("),
            extract_js_function(src, writer),
        ]
    )
    harness = tmp_path / "write.js"
    harness.write_text(functions + WRITE_PREAMBLE.replace("__WRITE__", call), encoding="utf-8")
    return json.loads(run_node(harness, json.dumps(scenario)))


@needs_node
@pytest.mark.parametrize("writer, call", QUOTA_WRITERS)
def test_successful_write_with_failed_reload_keeps_the_fetch_error(tmp_path: Path, writer: str, call: str) -> None:
    out = _run_write(tmp_path, {"reloadFails": True, "message": SCHEMA_DETAIL}, writer, call)
    assert out["postCalls"] == 1, "the settings write itself must still be sent"
    # The remediation survives instead of being replaced by a write ack.
    assert "tokdash update" in out["statusText"]
    assert out["statusColor"] == "#DC2626"
    assert "quotaActionSaved" not in out["statusText"]
    assert out["quotaLoaded"] is False


@needs_node
@pytest.mark.parametrize("writer, call", QUOTA_WRITERS)
def test_successful_write_with_successful_reload_still_acknowledges(tmp_path: Path, writer: str, call: str) -> None:
    """The fix must not cost users their normal "Saved" confirmation."""
    out = _run_write(tmp_path, {"reloadFails": False, "message": ""}, writer, call)
    assert out["postCalls"] == 1
    assert out["statusText"] == "quotaActionSaved"
    assert out["quotaLoaded"] is True


@needs_node
def test_quota_refresh_does_not_claim_updated_when_the_reload_failed(tmp_path: Path) -> None:
    """The provider refresh can succeed while the reload that follows it fails.

    Reporting 'updated' there would contradict the error banner loadQuota just set.
    """
    out = _run_refresh(
        tmp_path,
        {
            "reloadOk": False,
            "servers": [{"id": "local", "label": "Local", "fails": False}],
        },
    )
    assert out["loadQuotaCalls"] == 1
    assert "failed" in out["refreshStates"] and "updated" not in out["refreshStates"]


@needs_node
def test_quota_refresh_reports_updated_when_the_reload_succeeds(tmp_path: Path) -> None:
    out = _run_refresh(
        tmp_path,
        {"servers": [{"id": "local", "label": "Local", "fails": False}]},
    )
    assert out["loadQuotaCalls"] == 1
    assert "updated" in out["refreshStates"] and "failed" not in out["refreshStates"]
