"""The usage-DB schema remediation must survive the trip to the dashboard.

The backend fails fast with an actionable message ("run `tokdash update`"), but
that only helps if the UI shows it. Previously `fetchSelectedServers` swallowed
every rejection and returned [], and each caller substituted a generic
`loadFailed` -- so a user saw "不可用：Local / 数据加载失败" and had nothing to act
on. These tests pin the message end to end through the two frontend seams.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

# What the backend actually puts in the HTTP 500 body (api.py -> str(exc)).
SCHEMA_DETAIL = (
    "usage database schema 11 at /home/u/.tokdash/usage.sqlite3 is newer than "
    "this Tokdash supports (<= 9); run `tokdash update` and restart any other "
    "Tokdash processes using this data directory"
)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _extract_js_function(src: str, signature: str) -> str:
    """Slice one function out of index.html, parameter defaults included.

    The naive "count braces from the first `{`" approach used elsewhere in the
    suite stops at the first default-value object literal -- `fetchSelectedServers`
    declares `options = {}`, so the scan would open and close on the parameter
    list and return a stub. The parameter list is therefore skipped by matching
    parentheses first, and only then is the body brace-matched.
    """
    start = src.find(signature)
    assert start >= 0, f"{signature} not found"

    index = src.index("(", start)
    depth = 0
    while index < len(src):
        if src[index] == "(":
            depth += 1
        elif src[index] == ")":
            depth -= 1
            if depth == 0:
                break
        index += 1
    else:  # pragma: no cover - malformed source
        raise AssertionError(f"unterminated parameter list: {signature}")

    depth = 0
    for index in range(src.index("{", index), len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


def _functions() -> str:
    src = INDEX_HTML.read_text(encoding="utf-8")
    return "\n".join(
        [
            _extract_js_function(src, "function collectServerResults("),
            _extract_js_function(src, "async function fetchSelectedServers("),
            _extract_js_function(src, "function dashboardFetchStatusText("),
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
    result = subprocess.run(
        ["node", str(harness), json.dumps(scenario)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


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


@needs_node
def test_status_line_is_rendered_from_the_shared_helper() -> None:
    """setDashboardFetchStatus must not keep its own copy of the message policy."""
    src = INDEX_HTML.read_text(encoding="utf-8")
    setter = _extract_js_function(src, "function setDashboardFetchStatus(")
    assert "dashboardFetchStatusText(error)" in setter
    assert "temporaryOverload" not in setter


# --- Quota tab ---------------------------------------------------------------
# The Quota tab runs its own Promise.allSettled instead of fetchSelectedServers,
# so fixing the shared loader did not fix it. These tests drive the REAL loadQuota
# against stubs rather than re-implementing its settlement logic.

QUOTA_PREAMBLE = """
const scenario = JSON.parse(process.argv[2]);
const serverRuntimeStatus = new Map();
let lastQuotaServerRows = [];
let lastQuotaPayload = null;
let lastQuotaHistory = null;
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
    src = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        [
            _extract_js_function(src, "function collectServerResults("),
            _extract_js_function(src, "function dashboardFetchStatusText("),
            _extract_js_function(src, "function quotaStatusElement("),
            _extract_js_function(src, "function setQuotaFetchStatus("),
            _extract_js_function(src, "async function loadQuota("),
        ]
    )
    harness = tmp_path / "quota.js"
    harness.write_text(functions + QUOTA_PREAMBLE, encoding="utf-8")
    result = subprocess.run(
        ["node", str(harness), json.dumps(scenario)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


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


def test_quota_path_has_no_private_settlement_logic() -> None:
    """loadQuota must not grow a second copy of the settlement policy."""
    src = INDEX_HTML.read_text(encoding="utf-8")
    load_quota = _extract_js_function(src, "async function loadQuota(")
    assert "collectServerResults(servers, settled)" in load_quota
    assert "setQuotaFetchStatus" in load_quota
    # The old private bookkeeping is gone: no direct status writes, no bare
    # loadFailed substituted for a real rejection.
    assert "serverRuntimeStatus.set(" not in load_quota
    assert "throw new Error(t('loadFailed'))" not in load_quota


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
    src = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        [
            _extract_js_function(src, "function dashboardFetchStatusText("),
            _extract_js_function(src, "function quotaStatusElement("),
            _extract_js_function(src, "function setQuotaFetchStatus("),
            _extract_js_function(src, "async function refreshQuotaViaGlobalButton("),
        ]
    )
    harness = tmp_path / "refresh.js"
    harness.write_text(functions + REFRESH_PREAMBLE, encoding="utf-8")
    result = subprocess.run(
        ["node", str(harness), json.dumps(scenario)],
        check=True, capture_output=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


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
let lastQuotaServerRows = [];
let lastQuotaPayload = null;
let lastQuotaHistory = null;
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
  await setQuotaEnabled(true);
  process.stdout.write(JSON.stringify({
    statusText: quotaStatusElement().textContent,
    statusColor: quotaStatusElement().style.color || null,
    postCalls,
    quotaLoaded,
  }));
  process.exit(0);
})();
"""


def _run_write(tmp_path: Path, scenario: dict):
    src = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        [
            _extract_js_function(src, "function collectServerResults("),
            _extract_js_function(src, "function dashboardFetchStatusText("),
            _extract_js_function(src, "function quotaStatusElement("),
            _extract_js_function(src, "function setQuotaFetchStatus("),
            _extract_js_function(src, "function setQuotaActionStatus("),
            _extract_js_function(src, "async function loadQuota("),
            _extract_js_function(src, "async function setQuotaEnabled("),
        ]
    )
    harness = tmp_path / "write.js"
    harness.write_text(functions + WRITE_PREAMBLE, encoding="utf-8")
    result = subprocess.run(
        ["node", str(harness), json.dumps(scenario)],
        check=True, capture_output=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


@needs_node
def test_successful_write_with_failed_reload_keeps_the_fetch_error(tmp_path: Path) -> None:
    out = _run_write(tmp_path, {"reloadFails": True, "message": SCHEMA_DETAIL})
    assert out["postCalls"] == 1, "the settings write itself must still be sent"
    # The remediation survives instead of being replaced by a write ack.
    assert "tokdash update" in out["statusText"]
    assert out["statusColor"] == "#DC2626"
    assert "quotaActionSaved" not in out["statusText"]
    assert out["quotaLoaded"] is False


@needs_node
def test_successful_write_with_successful_reload_still_acknowledges(tmp_path: Path) -> None:
    """The fix must not cost users their normal "Saved" confirmation."""
    out = _run_write(tmp_path, {"reloadFails": False, "message": ""})
    assert out["postCalls"] == 1
    assert out["statusText"] == "quotaActionSaved"
    assert out["quotaLoaded"] is True


def test_all_quota_write_paths_gate_their_acknowledgment() -> None:
    """All four settings writers must gate 'saved' on the reload result."""
    src = INDEX_HTML.read_text(encoding="utf-8")
    writers = [
        "async function enableQuotaProvider(",
        "async function setQuotaCredentialScan(",
        "async function setQuotaEnabled(",
        "async function setQuotaInterval(",
    ]
    for signature in writers:
        body = _extract_js_function(src, signature)
        assert "if (await loadQuota({ force: true })) setQuotaActionStatus('saved');" in body, signature
        # The ungated sequence must not come back.
        assert "await loadQuota({ force: true });\n        setQuotaActionStatus('saved');" not in body


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
