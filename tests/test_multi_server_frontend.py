from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash


INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

# loadQuota declares `options = {}`, which this file's brace scan stops on; the sibling
# module already has an extractor that skips the parameter list.
from test_usage_db_schema_frontend import _extract_js_function as _extract_js_function_with_params

# localStorage key the dashboard persists per-host provider visibility under.
QUOTA_VISIBILITY_STORAGE_KEY = "tokdash-quota-visible-by-host"


def _extract_js_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start >= 0, f"{signature} not found"
    depth = 0
    for index in range(src.find("{", start), len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


def _js_binding(source: str, name: str) -> str:
    """The shipped `const`/`let` line for a top-level binding, so harnesses never
    drift from the key names and provider list the dashboard actually uses."""
    match = re.search(rf"^\s*(?:const|let) {re.escape(name)} = .*?;$", source, re.MULTILINE)
    assert match, f"{name} binding not found"
    return match.group(0).strip()


def _run(tmp_path: Path, name: str, functions: list[str], expression: str, value):
    harness = tmp_path / f"{name}.js"
    harness.write_text(
        "\n".join(functions)
        + "\nconst input = JSON.parse(process.argv[2]);\n"
        + f"process.stdout.write(JSON.stringify({expression}));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(value)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_usage_merger_identity_sum_and_missing_previous(tmp_path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    function = _extract_js_function(source, "function combineUsagePayloads(list) {")
    first = {
        "total_tokens": 10,
        "total_cost": 2,
        "total_messages": 3,
        "timestamp": "2026-08-10T12:00:00Z",
        "by_tool": {"codex": {"tokens": 10, "cost": 2, "messages": 3}},
        "top_models": [{"name": "gpt", "tokens": 10, "cost": 2}],
        "comparison": {"cost_prev": 1, "tokens_prev": 5, "messages_prev": 2},
    }
    assert _run(tmp_path, "identity", [function], "combineUsagePayloads(input)", [first]) == first

    second = {
        "total_tokens": 20,
        "total_cost": 4,
        "total_messages": 5,
        "timestamp": "2026-08-10T11:00:00Z",
        "by_tool": {"codex": {"tokens": 20, "cost": 4, "messages": 5}},
        "top_models": [{"name": "gpt", "tokens": 20, "cost": 4}],
        "comparison": {"cost_prev": None, "tokens_prev": 10, "messages_prev": 3},
    }
    merged = _run(tmp_path, "sum", [function], "combineUsagePayloads(input)", [first, second])
    assert (merged["total_tokens"], merged["total_cost"], merged["total_messages"]) == (30, 6, 8)
    assert merged["top_models"] == [{"name": "gpt", "tokens": 30, "cost": 6}]
    assert merged["timestamp"] == "2026-08-10T11:00:00Z"
    assert merged["comparison"]["cost_pct"] is None
    assert merged["comparison"]["tokens_prev"] == 15

    apps = _run(
        tmp_path, "apps", [function], "combineUsagePayloads(input).apps.editor.models", [
            {"total_tokens": 1, "total_cost": 1, "total_messages": 1, "apps": {"editor": {"tokens": 1, "cost": 1, "models": [{"name": "gpt", "tokens": 1, "cost": 1}]}}},
            {"total_tokens": 2, "total_cost": 2, "total_messages": 2, "apps": {"editor": {"tokens": 2, "cost": 2, "models": [{"name": "gpt", "tokens": 2, "cost": 2}]}}},
        ],
    )
    assert apps == [{"name": "gpt", "tokens": 3, "cost": 3}]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_overview_deltas_use_one_decimal_place(tmp_path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    function = _extract_js_function(source, "function renderDelta(elementId, pctChange) {")
    harness = [
        "const element = { textContent: '', style: {} };",
        "const document = { getElementById: () => element };",
        "function t() { return 'vs prev period'; }",
        function,
    ]

    assert _run(
        tmp_path,
        "delta_down",
        harness,
        "(renderDelta('delta', input), element.textContent)",
        -37.7355087610252,
    ) == "↓ 37.7% vs prev period"
    assert _run(
        tmp_path,
        "delta_up",
        harness,
        "(renderDelta('delta', input), element.textContent)",
        12,
    ) == "↑ 12.0% vs prev period"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_stats_and_session_mergers(tmp_path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    stats_fn = _extract_js_function(source, "function combineStatsPayloads(list) {")
    sessions_fn = _extract_js_function(source, "function mergeSessionLists(list) {")
    stats = _run(
        tmp_path,
        "stats",
        [stats_fn],
        "combineStatsPayloads(input)",
        [
            {"contributions": [{"date": "2026-08-10", "totals": {"tokens": 4, "cost": 1}, "tokenBreakdown": {"input": 3}}], "summary": {"totalCost": 1}, "stats": {}},
            {"contributions": [{"date": "2026-08-10", "totals": {"tokens": 6, "cost": 2}, "tokenBreakdown": {"input": 5}}], "summary": {"totalCost": 2}, "stats": {}},
        ],
    )
    assert stats["contributions"][0]["totals"] == {"tokens": 10, "cost": 3}
    assert stats["summary"]["totalCost"] == 3

    stale = _run(
        tmp_path, "stale-streak", [stats_fn], "combineStatsPayloads(input).stats.current_streak", [
            {"contributions": [{"date": "2020-01-01", "totals": {"tokens": 1}}], "summary": {}, "stats": {}},
            {"contributions": [{"date": "2020-01-02", "totals": {"tokens": 1}}], "summary": {}, "stats": {}},
        ],
    )
    assert stale == 0

    sessions = _run(
        tmp_path,
        "sessions",
        [sessions_fn],
        "mergeSessionLists(input)",
        [
            {
                "server": {"id": "a", "label": "A"},
                "payload": {
                    # Concurrent agents: agent time exceeds this session's clock time.
                    "sessions": [{"session_id": "1", "last_seen_at": "2026-08-10T10:00:00Z", "tokens": 2, "cost": 1, "active_ms": 60000, "active_ms_sum": 100000, "span_ms": 600000}],
                    "summary": {"active_ms": 60000, "active_ms_sum": 100000, "active_gap_cap_ms": 300000, "active_time_estimated": True, "active_time_method": "capped-inter-event-gap"},
                },
            },
            {
                "server": {"id": "b", "label": "B"},
                "payload": {
                    "sessions": [{"session_id": "2", "last_seen_at": "2026-08-10T11:00:00Z", "tokens": 3, "cost": 2, "active_ms": 120000, "active_ms_sum": 150000, "span_ms": 900000}],
                    "summary": {"active_ms": 120000, "active_ms_sum": 150000, "active_gap_cap_ms": 300000, "active_time_estimated": True, "active_time_method": "capped-inter-event-gap"},
                },
            },
        ],
    )
    assert [row["session_id"] for row in sessions["sessions"]] == ["2", "1"]
    assert sessions["sessions"][0]["_server"]["label"] == "B"
    # Servers are separate machines: their deduplicated active times add up. Agent
    # time must come from each server's own total — re-summing per-session
    # active_ms here would silently drop every session's concurrent agents.
    assert sessions["summary"] == {
        "session_count": 2,
        "tokens": 5,
        "cost": 3,
        "active_ms": 180000,
        "active_ms_sum": 250000,
        "span_ms": 1500000,
        "active_gap_cap_ms": 300000,
        "active_time_estimated": True,
        "active_time_method": "capped-inter-event-gap",
    }

    legacy = _run(
        tmp_path,
        "sessions-legacy",
        [sessions_fn],
        "mergeSessionLists(input)",
        [
            {
                "server": {"id": "a", "label": "A"},
                # A server predating the active-time fields: recover what we can
                # from its session rows instead of reporting nothing.
                "payload": {"sessions": [{"session_id": "1", "last_seen_at": "2026-08-10T10:00:00Z", "tokens": 2, "cost": 1}], "summary": {}},
            },
            {
                "server": {"id": "b", "label": "B"},
                "payload": {
                    "sessions": [{"session_id": "2", "last_seen_at": "2026-08-10T11:00:00Z", "tokens": 3, "cost": 2, "active_ms": 120000, "active_ms_sum": 150000}],
                    "summary": {},
                },
            },
        ],
    )
    assert legacy["summary"]["active_ms"] == 120000
    assert legacy["summary"]["active_ms_sum"] == 150000


def test_multi_server_contract_is_client_only_and_service_worker_is_same_origin():
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert "tokdash-servers" in source
    assert "tokdash-server-selection" in source
    assert "Promise.allSettled" in source
    assert "Math.min(2, queue.length)" in source
    assert "await Promise.all(Array.from" in source
    assert "currentStartDate = today;" in source
    assert "date_from=${encodeURIComponent(dateFrom)}&date_to=${encodeURIComponent(dateTo)}" in source
    assert "Add anyway" not in source and "仍然添加" not in source
    assert "await probeServer(baseUrl)" in source
    assert "fetchJson(candidate, '/health'" in source
    assert "addServerBtn')?.addEventListener('click', storeServerFromForm)" in source
    assert "server-setting-row" in source
    assert "server-form-status" in source
    assert 'placeholder="Name (optional)"' in source
    assert "label.placeholder = defaultServerLabel" not in source
    sw = (INDEX_HTML.parent / "sw.js").read_text(encoding="utf-8")
    assert "url.origin !== self.location.origin" in sw
    assert source.index('id="quotaGlobalControls"') < source.index('id="quota-content"') + 500
    assert "if (!multi) {\n          renderQuotaSettings(payload);" in source
    assert "csrfTokensByServer.delete(server.id)" in source
    assert "quotaReadOnlyRemote" in source
    # A language switch re-renders whichever quota layout is on screen, and the recorded
    # single-scope owner (null = the per-host blocks) is what says so -- not the selection.
    assert "if (!quotaSingleScopeOwner && lastQuotaServerRows.length) {" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_multi_server_quota_hides_single_server_sections(tmp_path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    quota_start = source.index('id="quota-content"')
    quota_end = source.index('id="pricing-content"')
    assert quota_start < source.index('id="quotaSingleServerPanel"') < quota_end
    assert quota_start < source.index('id="quotaSingleServerCharts"') < quota_end
    function = _extract_js_function(source, "function setQuotaSingleServerLayout(multi) {")
    expression = """(() => {
      const sections = {
        quotaSingleServerPanel: { hidden: false, style: { display: '' } },
        quotaSingleServerCharts: { hidden: false, style: { display: '' } },
      };
      global.document = { getElementById: (id) => sections[id] };
      setQuotaSingleServerLayout(input);
      return sections;
    })()"""

    hidden = _run(tmp_path, "quota-multi", [function], expression, True)
    assert all(section == {"hidden": True, "style": {"display": "none"}} for section in hidden.values())

    shown = _run(tmp_path, "quota-single", [function], expression, False)
    assert all(section == {"hidden": False, "style": {"display": ""}} for section in shown.values())


def test_companion_v2_schema_and_loose_test_rule_are_present():
    root = INDEX_HTML.parents[3]
    windows = (root / "companion/windows/TokdashCompanion/BindableBase.cs").read_text(encoding="utf-8")
    macos = (root / "companion/macos/TokdashCompanion/CompanionStore.swift").read_text(encoding="utf-8")
    mac_settings = (root / "companion/macos/TokdashCompanion/SettingsView.swift").read_text(encoding="utf-8")
    win_settings = (root / "companion/windows/TokdashCompanion/SettingsWindow.xaml.cs").read_text(encoding="utf-8")
    assert 'JsonPropertyName("version")' in windows
    assert 'JsonPropertyName("servers")' in windows
    assert "TryGetProperty(\"BaseURL\"" in windows
    assert "var version: Int = 3" in macos  # schema v3 (components); v2 files migrate up
    assert 'case baseURL = "baseUrl"' in macos
    assert "decodeIfPresent(String.self, forKey: .baseURL)" in macos
    # Test remains optional: Save validates URLs but never checks a probe result.
    mac_save = mac_settings[mac_settings.index("private func saveSettings()") : mac_settings.index("private func removeServer")]
    assert "validServers" in mac_save and "testResults" not in mac_save
    assert "entries.Any(entry => !CompanionStore.IsValidBaseURL" in win_settings
    assert (root / "companion/windows/TokdashCompanion/MultiServerTokdashClient.cs").exists()
    assert "runMultiServerRefresh" in macos
    assert "combineUsage" in macos
    assert "serverFailureCounts[server.id, default: 0]" in macos
    assert "if Task.isCancelled { return }" in macos
    multi_client = (root / "companion/windows/TokdashCompanion/MultiServerTokdashClient.cs").read_text(encoding="utf-8")
    win_store = (root / "companion/windows/TokdashCompanion/CompanionStore.cs").read_text(encoding="utf-8")
    assert "FailedServerIds" in multi_client
    assert "OperationCanceledException" in multi_client
    assert "_serverFailureCounts.GetValueOrDefault(s.Id)" in win_store
    assert "ServerRegistriesEqual(s.Servers, entries)" in win_settings
    fixture_name = 'ContractFile("expected", "multi-server.json")'
    assert fixture_name in (root / "companion/windows/TokdashCompanion.Tests/MultiServerContractTests.cs").read_text(encoding="utf-8")
    assert 'contractURL("expected/multi-server.json")' in (root / "companion/macos/TokdashCompanionTests/SnapshotTests.swift").read_text(encoding="utf-8")


def test_quota_visibility_dropdown_is_scoped_per_server():
    source = INDEX_HTML.read_text(encoding="utf-8")
    # The old global dropdown (its option list was driven by the last-rendered
    # server's payload, i.e. not the per-server harness set) is gone.
    for gone in ('id="quotaVisibilityWrap"', 'id="quotaVisibilityBtn"', 'id="quotaVisibilityPanel"', 'id="quotaVisibilityLabel"'):
        assert gone not in source
    # One dropdown slot per server block, plus the single-server slot in the settings row.
    assert 'data-role="visibility"' in source
    assert 'id="quotaVisibilityHost"' in source
    # Presence is computed per payload and threaded into cards and charts.
    assert "const present = quotaPresentProviders(payload);" in source
    # Every backend quota provider must be in the shipped visibility list and the
    # card render order — a provider missing here is filtered out of presence and
    # can never render, no matter what /api/quota returns.
    shipped = re.search(r"const QUOTA_PROVIDERS = \[(.*?)\];", source)
    assert shipped and "'opencode_go'" in shipped.group(1)
    assert "'commandcode'" in shipped.group(1)
    assert "'opencode_go'" in re.search(r"const order = \[(.*?)\];", source).group(1)
    assert "'commandcode'" in re.search(r"const order = \[(.*?)\];", source).group(1)
    assert "const present = quotaPresentProviders(row.payload);" in source
    assert "present: quotaPresentProviders(payload)" in source
    assert "syncQuotaServerVisibilityControl(block, payload, server);" in source
    assert "syncQuotaSingleVisibility(payload);" in source
    # Every dropdown is stamped with the host it belongs to, and a toggle syncs that
    # host's dropdown in place.
    assert "createQuotaVisibilityControl(present, server.id)" in source
    assert "syncQuotaVisibilityControlInPlace(block.querySelector" in source
    # Issue #93: the choice is stored per host, and a toggle repaints only the host that
    # owns the dropdown. The old shared-pref path must stay gone: one key for every host
    # (QUOTA_VISIBILITY_KEY below is the per-host key; the legacy name moved to
    # QUOTA_VISIBILITY_GLOBAL_KEY and is read-only) and one toggle repainting every block.
    assert "const QUOTA_VISIBILITY_KEY = 'tokdash-quota-visible-by-host';" in source
    assert "const QUOTA_VISIBILITY_GLOBAL_KEY = 'tokdash-quota-visible';" in source
    assert "applyQuotaVisibilityChange" not in source
    # The toggle resolves its scope by host key: the single-server scope owns the host it
    # is showing, everything else is a server block. That owner follows the payload on
    # screen, so a cached payload never repaints through another host's map.
    assert "function refreshQuotaScopeForHost(hostKey) {\n      const key = quotaVisibilityHostKey(hostKey);" in source
    assert "if (quotaSingleScopeOwner && key === quotaVisibilityHostKey(quotaSingleScopeOwner.id)) {" in source
    assert "quotaSingleScopeOwner = multi ? null : rows[0].server;" in source
    # A failed reload repaints the layout the last good load rendered. Deriving that from the
    # cached-row count misreads a partial multi load: its one surviving block shares the host
    # key with the single scope, so its dropdown would repaint hidden UI and go dead.
    assert "quotaSingleScopeOwner = (lastQuotaServerRows[0]" not in source
    assert "if (lastQuotaServerRows.length <= 1) {" not in source
    assert "if (quotaSingleScopeOwner) {" in source
    # The static HTML checkboxes are gone; rows are built per scope.
    assert "data-quota-provider=" not in source


def _quota_visibility_pure_functions(source: str) -> list[str]:
    """The visibility model without any DOM: storage, per-host maps, presence, labels."""
    return [
        _js_binding(source, "QUOTA_PROVIDERS"),
        _js_binding(source, "QUOTA_VISIBILITY_KEY"),
        _js_binding(source, "QUOTA_VISIBILITY_GLOBAL_KEY"),
        _js_binding(source, "quotaVisibilityByHost"),
        _js_binding(source, "quotaVisibilityGlobalDefaults"),
        _extract_js_function(source, "function quotaVisibilityHostKey(hostKey) {"),
        _extract_js_function(source, "function allQuotaProvidersVisible() {"),
        _extract_js_function(source, "function quotaPresentProviders(payload) {"),
        _extract_js_function(source, "function readQuotaVisibilityStorage(key) {"),
        _extract_js_function(source, "function loadQuotaVisibilityByHost() {"),
        _extract_js_function(source, "function loadQuotaVisibility(hostKey) {"),
        _extract_js_function(source, "function quotaProviderLabel(provider) {"),
        _extract_js_function(source, "function quotaVisibilityLabelText(present, hostKey) {"),
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_quota_presence_and_label_are_per_payload(tmp_path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = _quota_visibility_pure_functions(source)
    expression = """(() => {
      global.localStorage = { getItem: () => null, setItem: () => {} };
      global.t = (key) => key;
      const serverA = { providers: { codex: { detected: true }, claude: { detected: true } } };
      const serverB = { providers: { codex: { detected: true }, kimi: { network_enabled: true } } };
      const pa = quotaPresentProviders(serverA);
      const pb = quotaPresentProviders(serverB);
      return {
        a: Array.from(pa).sort(),
        b: Array.from(pb).sort(),
        labelA: quotaVisibilityLabelText(pa, 'A'),
        labelB: quotaVisibilityLabelText(pb, 'B'),
        hiddenClaudeA: (() => { loadQuotaVisibility('A').claude = false; return quotaVisibilityLabelText(pa, 'A'); })(),
        labelBAfterA: quotaVisibilityLabelText(pb, 'B'),
        labelBOwnChoice: (() => { loadQuotaVisibility('B').codex = false; return quotaVisibilityLabelText(pb, 'B'); })(),
        labelAUnaffected: quotaVisibilityLabelText(pa, 'A'),
      };
    })()"""
    out = _run(tmp_path, "quota-visibility-scopes", functions, expression, {})
    # Each scope sees only its own server's harnesses — never the other's.
    assert out["a"] == ["claude", "codex"]
    assert out["b"] == ["codex", "kimi"]
    assert out["labelA"] == "quotaVisibilityAll"
    assert out["labelB"] == "quotaVisibilityAll"
    # Hiding a provider only changes the label over the providers present in the scope.
    assert out["hiddenClaudeA"] == "quotaVisibilityPrefix Codex"
    # ...and only on the host it was hidden on (issue #93).
    assert out["labelBAfterA"] == "quotaVisibilityAll"
    assert out["labelBOwnChoice"] == "quotaVisibilityPrefix Kimi Code"
    assert out["labelAUnaffected"] == "quotaVisibilityPrefix Codex"


# --- Quota visibility: checkbox event path (DOM-level) -------------------------
# Each host owns its show/hide map, so a toggle re-renders (cards, charts) and syncs
# the dropdown of the one host under the cursor, and leaves every other host's cards,
# charts, labels and checkboxes alone. That isolation is what issue #93 asked for.

QUOTA_VISIBILITY_DOM_STUBS = r"""
// --- minimal DOM stub: just enough for the quota visibility controls ---------
class El {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.parent = null;
    this.className = '';
    this.attrs = {};
    this.dataset = {};
    this.textContent = '';
    this.checked = false;
    this.style = {};
    this.listeners = {};
  }
  get classes() { return new Set(this.className.split(/\s+/).filter(Boolean)); }
  get classList() {
    const el = this;
    return {
      contains: (c) => el.classes.has(c),
      add: (c) => { const s = new Set(el.classes); s.add(c); el.className = [...s].join(' '); },
      remove: (c) => { el.className = [...el.classes].filter((x) => x !== c).join(' '); },
      toggle: (c) => {
        const s = new Set(el.classes);
        if (s.has(c)) { s.delete(c); el.className = [...s].join(' '); return false; }
        s.add(c); el.className = [...s].join(' '); return true;
      },
    };
  }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  replaceChildren() { this.children.length = 0; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type) { (this.listeners[type] || []).forEach((fn) => fn({ target: this, stopPropagation() {} })); }
  matches(part) {
    const tag = part.match(/^[a-zA-Z][\w-]*/);
    if (tag && this.tag !== tag[0]) return false;
    for (const cls of part.match(/\.[\w-]+/g) || []) if (!this.classes.has(cls.slice(1))) return false;
    const id = part.match(/#[\w-]+/);
    if (id && this.attrs.id !== id[0].slice(1)) return false;
    for (const a of part.match(/\[[\w-]+(?:="[^"]*")?\]/g) || []) {
      const m = a.match(/\[([\w-]+)(?:="([^"]*)")?\]/);
      if (this.attrs[m[1]] === undefined || (m[2] !== undefined && this.attrs[m[1]] !== m[2])) return false;
    }
    return true;
  }
  find(sel) {
    let current = [this];
    for (const part of sel.trim().split(/\s+/)) {
      const next = [];
      const walk = (node) => node.children.forEach((child) => { if (child.matches(part)) next.push(child); walk(child); });
      current.forEach(walk);
      current = next;
    }
    return current;
  }
  querySelectorAll(sel) { return this.find(sel); }
  querySelector(sel) { return this.find(sel)[0] || null; }
}
const document = {
  createElement: (tag) => new El(tag),
  getElementById: () => null,
  querySelector: () => null,
};
function t(key) { return key; }
global.CSS = { escape: (s) => String(s) };
// A real key/value store, so a scenario can read back what the dashboard persisted.
const quotaStore = new Map();
global.localStorage = {
  getItem: (key) => (quotaStore.has(key) ? quotaStore.get(key) : null),
  setItem: (key, value) => { quotaStore.set(key, String(value)); },
};
const renderLog = [];
function renderQuotaProviderCards(payload, container, ownerServer) {
  renderLog.push({ kind: 'cards', server: ownerServer ? ownerServer.id : 'local', present: [...quotaPresentProviders(payload)].sort() });
}
function renderQuotaCharts(history, targets) {
  renderLog.push({ kind: 'charts', server: targets.serverId || 'local', hostKey: targets.hostKey || null, present: [...(targets.present || [])].sort() });
}
function inputFor(control, provider) {
  return control.querySelectorAll('.quota-visibility-item input').find((i) => i.dataset.provider === provider);
}
"""

QUOTA_VISIBILITY_MULTI_SCENARIO = r"""
function selectedServers() { return [{ id: 'A' }, { id: 'B' }]; }

const root = new El('body');
const blocks = new El('div');
blocks.setAttribute('id', 'quotaServerBlocks');
root.appendChild(blocks);
const singleHost = new El('span');
singleHost.setAttribute('id', 'quotaVisibilityHost');
root.appendChild(singleHost);
document.getElementById = (id) => (id === 'quotaVisibilityHost' ? singleHost : null);
document.querySelector = (sel) => root.find(sel)[0] || null;

const serverA = { id: 'A', label: 'A', payload: { providers: { codex: { detected: true }, claude: { detected: true } } }, history: {} };
const serverB = { id: 'B', label: 'B', payload: { providers: { codex: { detected: true }, kimi: { detected: true } } }, history: {} };
lastQuotaServerRows = [
  { server: serverA, payload: serverA.payload, history: serverA.history },
  { server: serverB, payload: serverB.payload, history: serverB.history },
];

function makeBlock(id) {
  const block = new El('section');
  block.setAttribute('data-server-id', id);
  ['cards', 'visibility', 'utilization', 'consumption', 'estimated'].forEach((role) => {
    const el = new El('div');
    el.setAttribute('data-role', role);
    block.appendChild(el);
  });
  blocks.appendChild(block);
  return block;
}
const blockA = makeBlock('A');
const blockB = makeBlock('B');
const controlA = createQuotaVisibilityControl(quotaPresentProviders(serverA.payload), serverA.id);
blockA.querySelector('[data-role="visibility"]').appendChild(controlA);
const controlB = createQuotaVisibilityControl(quotaPresentProviders(serverB.payload), serverB.id);
blockB.querySelector('[data-role="visibility"]').appendChild(controlB);

// Open A's dropdown and hide Codex there.
const panelA = controlA.querySelector('.quota-visibility-panel');
controlA.querySelector('.quota-visibility-btn').dispatch('click');
const codexA = inputFor(controlA, 'codex');
codexA.checked = false;
codexA.dispatch('change');

// Then hide Kimi in B's dropdown: A must not move, and B's own choice must stick.
const bAfterAToggle = {
  label: controlB.querySelector('.quota-visibility-label').textContent,
  codexChecked: inputFor(controlB, 'codex').checked,
  renderCount: renderLog.length,
};
const kimiB = inputFor(controlB, 'kimi');
kimiB.checked = false;
kimiB.dispatch('change');

process.stdout.write(JSON.stringify({
  labelA: controlA.querySelector('.quota-visibility-label').textContent,
  labelB: controlB.querySelector('.quota-visibility-label').textContent,
  codexBChecked: inputFor(controlB, 'codex').checked,
  kimiBChecked: inputFor(controlB, 'kimi').checked,
  panelAOpen: !panelA.classList.contains('hidden'),
  rowKept: inputFor(controlA, 'codex') === codexA,
  renders: renderLog,
  bAfterAToggle: bAfterAToggle,
  labelAAfterB: controlA.querySelector('.quota-visibility-label').textContent,
  codexAChecked: inputFor(controlA, 'codex').checked,
  stored: JSON.parse(quotaStore.get(QUOTA_VISIBILITY_KEY) || 'null'),
  storedKeys: [...quotaStore.keys()],
}));
"""

QUOTA_VISIBILITY_SINGLE_SCENARIO = r"""
function selectedServers() { return [{ id: 'local' }]; }

const root = new El('body');
const singleHost = new El('span');
singleHost.setAttribute('id', 'quotaVisibilityHost');
root.appendChild(singleHost);
document.getElementById = (id) => (id === 'quotaVisibilityHost' ? singleHost : null);
document.querySelector = (sel) => root.find(sel)[0] || null;

lastQuotaPayload = { providers: { codex: { detected: true }, claude: { detected: true } } };
lastQuotaHistory = {};
// What a successful single-server load records: the shown server owns the scope, and the
// rows hold that same server. Re-pointing selectedServers() re-points the whole scope.
const shown = selectedServers();
quotaSingleScopeOwner = shown.length === 1 ? shown[0] : null;
lastQuotaServerRows = quotaSingleScopeOwner ? [{ server: quotaSingleScopeOwner, payload: lastQuotaPayload, history: lastQuotaHistory }] : [];
syncQuotaSingleVisibility(lastQuotaPayload);

const control = singleHost.querySelector('.quota-visibility-wrap');

const claude = inputFor(control, 'claude');
claude.checked = false;
claude.dispatch('change');

process.stdout.write(JSON.stringify({
  label: control.querySelector('.quota-visibility-label').textContent,
  codexChecked: inputFor(control, 'codex').checked,
  renders: renderLog,
  stored: JSON.parse(quotaStore.get(QUOTA_VISIBILITY_KEY) || 'null'),
}));
"""


QUOTA_VISIBILITY_PARTIAL_SCENARIO = r"""
// Two servers selected, only A responded: multi layout with a single block.
function selectedServers() { return [{ id: 'A' }, { id: 'B' }]; }

const root = new El('body');
const blocks = new El('div');
blocks.setAttribute('id', 'quotaServerBlocks');
root.appendChild(blocks);
const singleHost = new El('span');
singleHost.setAttribute('id', 'quotaVisibilityHost');
root.appendChild(singleHost);
document.getElementById = (id) => (id === 'quotaVisibilityHost' ? singleHost : null);
document.querySelector = (sel) => root.find(sel)[0] || null;

const serverA = { id: 'A', label: 'A', payload: { providers: { codex: { detected: true }, claude: { detected: true } } }, history: {} };
lastQuotaServerRows = [ { server: serverA, payload: serverA.payload, history: serverA.history } ];
// Stale single-server cache: if the single path were taken, these would be rendered.
lastQuotaPayload = { providers: { grok: { detected: true } } };
lastQuotaHistory = {};

function makeBlock(id) {
  const block = new El('section');
  block.setAttribute('data-server-id', id);
  ['cards', 'visibility', 'utilization', 'consumption', 'estimated'].forEach((role) => {
    const el = new El('div');
    el.setAttribute('data-role', role);
    block.appendChild(el);
  });
  blocks.appendChild(block);
  return block;
}
const blockA = makeBlock('A');
const controlA = createQuotaVisibilityControl(quotaPresentProviders(serverA.payload), 'A');
blockA.querySelector('[data-role="visibility"]').appendChild(controlA);

const panelA = controlA.querySelector('.quota-visibility-panel');
controlA.querySelector('.quota-visibility-btn').dispatch('click');
const codexA = inputFor(controlA, 'codex');
codexA.checked = false;
codexA.dispatch('change');

process.stdout.write(JSON.stringify({
  labelA: controlA.querySelector('.quota-visibility-label').textContent,
  singleHostChildren: singleHost.children.length,
  renders: renderLog,
}));
"""



QUOTA_VISIBILITY_STALE_CACHE_SCENARIO = r"""
// Selected: host B only. B's quota request just failed, so the single-server UI still shows
// A's payload from the last good load -- the state loadQuota()'s failure path leaves behind,
// including the scope owner it records from the cached row.
function selectedServers() { return [{ id: 'B', baseUrl: 'http://other:55423' }]; }

const root = new El('body');
const singleHost = new El('span');
singleHost.setAttribute('id', 'quotaVisibilityHost');
root.appendChild(singleHost);
document.getElementById = (id) => (id === 'quotaVisibilityHost' ? singleHost : null);
document.querySelector = (sel) => root.find(sel)[0] || null;

const serverA = { id: 'A', label: 'A' };
lastQuotaServerRows = [{ server: serverA, payload: { providers: { codex: { detected: true }, claude: { detected: true } } }, history: {} }];
lastQuotaPayload = lastQuotaServerRows[0].payload;
lastQuotaHistory = lastQuotaServerRows[0].history;
quotaSingleScopeOwner = serverA;
syncQuotaSingleVisibility(lastQuotaPayload);

const control = singleHost.querySelector('.quota-visibility-wrap');
const codex = inputFor(control, 'codex');
codex.checked = false;
codex.dispatch('change');

process.stdout.write(JSON.stringify({
  hostKey: control.dataset.hostKey,
  label: control.querySelector('.quota-visibility-label').textContent,
  renders: renderLog,
  stored: JSON.parse(quotaStore.get(QUOTA_VISIBILITY_KEY) || 'null'),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_quota_visibility_toggle_on_partial_multi_load_updates_the_block(tmp_path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    body = QUOTA_VISIBILITY_DOM_STUBS + "\n" + _quota_visibility_dom_functions(source) + "\n" + QUOTA_VISIBILITY_PARTIAL_SCENARIO
    out = _run_quota_dom(tmp_path, "quota-visibility-partial", body)
    # One of two selected servers responded: the toggle must update the rendered
    # server block (multi layout), not the hidden single-server UI.
    assert out["labelA"] == "quotaVisibilityPrefix Claude Code"
    assert out["singleHostChildren"] == 0
    assert out["renders"] == [
        {"kind": "cards", "server": "A", "present": ["claude", "codex"]},
        {"kind": "charts", "server": "A", "hostKey": "A", "present": ["claude", "codex"]},
    ]


def _quota_visibility_dom_functions(source: str) -> str:
    return "\n".join(
        [
            _js_binding(source, "QUOTA_PROVIDERS"),
            _js_binding(source, "QUOTA_VISIBILITY_KEY"),
            _js_binding(source, "QUOTA_VISIBILITY_GLOBAL_KEY"),
            _js_binding(source, "quotaVisibilityByHost"),
            _js_binding(source, "quotaVisibilityGlobalDefaults"),
            "const LOCAL_SERVER = { id: 'local', label: 'Local', baseUrl: '' };",
            _js_binding(source, "quotaSingleScopeOwner"),
            "let lastQuotaServerRows = [];",
            "let lastQuotaPayload = null;",
            "let lastQuotaHistory = null;",
            _extract_js_function(source, "function quotaVisibilityHostKey(hostKey) {"),
            _extract_js_function(source, "function allQuotaProvidersVisible() {"),
            _extract_js_function(source, "function quotaPresentProviders(payload) {"),
            _extract_js_function(source, "function readQuotaVisibilityStorage(key) {"),
            _extract_js_function(source, "function loadQuotaVisibilityByHost() {"),
            _extract_js_function(source, "function loadQuotaVisibility(hostKey) {"),
            _extract_js_function(source, "function persistQuotaVisibility() {"),
            _extract_js_function(source, "function quotaProviderLabel(provider) {"),
            _extract_js_function(source, "function quotaVisibilityLabelText(present, hostKey) {"),
            _extract_js_function(source, "function quotaSingleScopeServer() {"),
            _extract_js_function(source, "function quotaSingleScopeHostKey() {"),
            _extract_js_function(source, "function buildQuotaVisibilityRow(provider, hostKey) {"),
            _extract_js_function(source, "function createQuotaVisibilityControl(present, hostKey) {"),
            _extract_js_function(source, "function syncQuotaVisibilityControlInPlace(control, present) {"),
            _extract_js_function(source, "function refreshQuotaServerScope(server) {"),
            _extract_js_function(source, "function refreshQuotaSingleScope() {"),
            _extract_js_function(source, "function syncQuotaSingleVisibility(payload) {"),
            _extract_js_function(source, "function refreshQuotaScopeForHost(hostKey) {"),
        ]
    )


def _run_quota_dom(tmp_path: Path, name: str, body: str) -> dict:
    harness = tmp_path / f"{name}.js"
    harness.write_text(body, encoding="utf-8")
    result = subprocess.run(["node", str(harness)], check=True, capture_output=True, encoding="utf-8")
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_quota_visibility_toggle_touches_only_its_own_host(tmp_path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    body = QUOTA_VISIBILITY_DOM_STUBS + "\n" + _quota_visibility_dom_functions(source) + "\n" + QUOTA_VISIBILITY_MULTI_SCENARIO
    out = _run_quota_dom(tmp_path, "quota-visibility-multi", body)
    # Issue #93: hiding Codex in A's dropdown repaints A alone. B keeps showing Codex,
    # and hiding Kimi in B afterwards does not move A's own choice either.
    assert out["labelA"] == "quotaVisibilityPrefix Claude Code"
    assert out["labelB"] == "quotaVisibilityPrefix Codex"
    assert out["codexBChecked"] is True
    assert out["kimiBChecked"] is False
    assert out["labelAAfterB"] == "quotaVisibilityPrefix Claude Code"
    assert out["codexAChecked"] is False
    # Snapshot taken between the two toggles: A's toggle left B's dropdown reading "All"
    # with Codex still ticked, which is exactly the propagation issue #93 reported.
    assert out["bAfterAToggle"] == {"label": "quotaVisibilityAll", "codexChecked": True, "renderCount": 2}
    # The label follows the toggle in place and the open panel is not rebuilt.
    assert out["panelAOpen"] is True
    assert out["rowKept"] is True
    # Exactly one cards+charts pair per toggle, always for the host under the cursor.
    assert out["renders"] == [
        {"kind": "cards", "server": "A", "present": ["claude", "codex"]},
        {"kind": "charts", "server": "A", "hostKey": "A", "present": ["claude", "codex"]},
        {"kind": "cards", "server": "B", "present": ["codex", "kimi"]},
        {"kind": "charts", "server": "B", "hostKey": "B", "present": ["codex", "kimi"]},
    ]
    # Both choices are persisted, each under its own host key.
    assert out["stored"]["A"]["codex"] is False
    assert out["stored"]["A"]["kimi"] is True
    assert out["stored"]["B"]["codex"] is True
    assert out["stored"]["B"]["kimi"] is False
    assert out["storedKeys"] == [QUOTA_VISIBILITY_STORAGE_KEY]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_quota_visibility_toggle_updates_single_server_scope(tmp_path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    body = QUOTA_VISIBILITY_DOM_STUBS + "\n" + _quota_visibility_dom_functions(source) + "\n" + QUOTA_VISIBILITY_SINGLE_SCENARIO
    out = _run_quota_dom(tmp_path, "quota-visibility-single", body)
    assert out["label"] == "quotaVisibilityPrefix Codex"
    assert out["codexChecked"] is True
    assert out["renders"] == [
        {"kind": "cards", "server": "local", "present": ["claude", "codex"]},
        {"kind": "charts", "server": "local", "hostKey": "local", "present": ["claude", "codex"]},
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_quota_visibility_single_scope_follows_the_selected_server(tmp_path):
    """Selecting one remote host instead of several shows that host's own choices, so the
    settings-row dropdown, its cards and its label all read the same host key."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    body = (
        QUOTA_VISIBILITY_DOM_STUBS + "\n"
        + _quota_visibility_dom_functions(source) + "\n"
        + QUOTA_VISIBILITY_SINGLE_SCENARIO.replace("[{ id: 'local' }]", "[{ id: 'B', baseUrl: 'http://other:55423' }]")
    )
    out = _run_quota_dom(tmp_path, "quota-visibility-single-remote", body)
    assert out["renders"] == [
        {"kind": "cards", "server": "B", "present": ["claude", "codex"]},
        {"kind": "charts", "server": "local", "hostKey": "B", "present": ["claude", "codex"]},
    ]
    # The hidden choice is filed under B, the host actually on screen.
    assert out["stored"]["B"]["claude"] is False
    assert out["label"] == "quotaVisibilityPrefix Codex"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_quota_visibility_seeds_untouched_hosts_from_the_legacy_global_choice(tmp_path):
    """Pre-per-host builds stored one shared choice. After the upgrade that value seeds
    every host, and a host's own toggle wins over it."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = _quota_visibility_pure_functions(source)
    expression = """(() => {
      const store = new Map([
        [QUOTA_VISIBILITY_GLOBAL_KEY, JSON.stringify({ codex: false })],
        [QUOTA_VISIBILITY_KEY, JSON.stringify({ B: { claude: false } })],
      ]);
      global.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: () => {} };
      global.t = (key) => key;
      const present = new Set(['codex', 'claude']);
      return {
        untouched: quotaVisibilityLabelText(present, 'A'),
        ownChoice: quotaVisibilityLabelText(present, 'B'),
        local: quotaVisibilityLabelText(present, undefined),
      };
    })()"""
    out = _run(tmp_path, "quota-visibility-legacy", functions, expression, {})
    # A never toggled: it keeps the old global "hide Codex".
    assert out["untouched"] == "quotaVisibilityPrefix Claude Code"
    # B has its own map, so the shared value no longer applies to it.
    assert out["ownChoice"] == "quotaVisibilityPrefix Codex"
    # An omitted host key is the local host, and it inherits too.
    assert out["local"] == "quotaVisibilityPrefix Claude Code"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_quota_visibility_cached_payload_keeps_its_own_host(tmp_path):
    """Load A, then select B and fail to load B: the single-server UI still shows A's cached
    payload, so A's dropdown must repaint A's cards and charts through A's map, not B's."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    body = (
        QUOTA_VISIBILITY_DOM_STUBS + "\n"
        + _quota_visibility_dom_functions(source) + "\n"
        + QUOTA_VISIBILITY_STALE_CACHE_SCENARIO
    )
    out = _run_quota_dom(tmp_path, "quota-visibility-stale-cache", body)
    # The dropdown stayed on the host it was built for, not the newly selected one.
    assert out["hostKey"] == "A"
    assert out["label"] == "quotaVisibilityPrefix Claude Code"
    # And the repaint followed it: cards and charts both resolved host A's visibility map.
    assert out["renders"] == [
        {"kind": "cards", "server": "A", "present": ["claude", "codex"]},
        {"kind": "charts", "server": "local", "hostKey": "A", "present": ["claude", "codex"]},
    ]
    assert out["stored"]["A"]["codex"] is False
    # B has no entry: nothing about the failed host's preference was written either.
    assert "B" not in out["stored"]


# --- the real loader, driven through a partial load and a failed reload ---------

QUOTA_VISIBILITY_LOAD_STUBS = r"""
// --- what loadQuota() needs besides the visibility controls --------------------
const serverRuntimeStatus = new Map();
const quotaUtilizationCharts = new Map();
const quotaConsumptionCharts = new Map();
let quotaLoaded = false;
const statusLog = [];
function currentQuotaRange() { return '24h'; }
function quotaRangeSeconds() { return 86400; }
function positionQuotaGlobalControls() {}
function setQuotaSingleServerLayout() {}
function renderQuotaSettings() {}
function renderServerSettings() {}
function updateServerIndicator() {}
function setQuotaFetchStatus(error) { statusLog.push(error ? 'error' : 'clear'); }
"""


def _quota_visibility_load_functions(source: str) -> str:
    """The visibility controls plus the real loader, so one harness can run two rounds."""
    return "\n".join(
        [
            _quota_visibility_dom_functions(source),
            _extract_js_function(source, "function collectServerResults("),
            _extract_js_function_with_params(source, "async function loadQuota("),
        ]
    )


QUOTA_VISIBILITY_FAILED_RELOAD_SCENARIO = r"""
// Two hosts selected on both rounds. Round 1: only A answers, so the multi layout shows one
// block. Round 2: both fail, and A's cached block stays on screen -- the state that used to
// read as single-host mode because exactly one row was cached.
const rounds = [
  { A: { providers: { codex: { detected: true }, claude: { detected: true } } } },
  {},
];
let round = 0;
function selectedServers() { return [{ id: 'A', label: 'A' }, { id: 'B', label: 'B' }]; }

const root = new El('body');
const blocks = new El('div');
blocks.setAttribute('id', 'quotaServerBlocks');
root.appendChild(blocks);
const singleHost = new El('span');
singleHost.setAttribute('id', 'quotaVisibilityHost');
root.appendChild(singleHost);
document.getElementById = (id) => (id === 'quotaVisibilityHost' ? singleHost : null);
document.querySelector = (sel) => root.find(sel)[0] || null;

function ensureQuotaServerBlocks() { return blocks; }

function makeBlock(id) {
  const block = new El('section');
  block.setAttribute('data-server-id', id);
  ['cards', 'visibility', 'utilization', 'consumption', 'estimated'].forEach((role) => {
    const el = new El('div');
    el.setAttribute('data-role', role);
    block.appendChild(el);
  });
  blocks.appendChild(block);
  return block;
}

// Stands in for the real block renderer, which builds its DOM out of innerHTML: enough of it
// to give the surviving host a working dropdown stamped with its own key.
function renderQuotaServerBlock(server, payload) {
  let block = blocks.querySelector('[data-server-id="' + server.id + '"]');
  if (!block) {
    block = makeBlock(server.id);
    block.querySelector('[data-role="visibility"]').appendChild(
      createQuotaVisibilityControl(quotaPresentProviders(payload), server.id));
  }
  renderLog.push({ kind: 'block', server: server.id });
}

async function fetchJsonWithRetry(server, path) {
  const spec = rounds[round][server.id];
  if (!spec) throw new Error(server.id + ' is down');
  return path.includes('/history') ? { series: [] } : { enabled: true, providers: spec.providers };
}

(async () => {
  const outcomes = [];
  for (round = 0; round < rounds.length; round += 1) {
    const ok = await loadQuota();
    outcomes.push({ ok: ok, rows: lastQuotaServerRows.map((row) => row.server.id) });
  }

  // Toggle the surviving block's own dropdown.
  const controlA = blocks.querySelector('[data-server-id="A"] .quota-visibility-wrap');
  const codexA = inputFor(controlA, 'codex');
  codexA.checked = false;
  codexA.dispatch('change');

  process.stdout.write(JSON.stringify({
    outcomes: outcomes,
    statusLog: statusLog,
    singleScopeOwner: quotaSingleScopeOwner ? quotaSingleScopeOwner.id : null,
    hostKey: controlA.dataset.hostKey,
    labelA: controlA.querySelector('.quota-visibility-label').textContent,
    singleHostChildren: singleHost.children.length,
    renders: renderLog,
    stored: JSON.parse(quotaStore.get(QUOTA_VISIBILITY_KEY) || 'null'),
  }));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_quota_visibility_failed_reload_after_partial_multi_load_keeps_the_block(tmp_path):
    """Multi selection, one host answering, then a refresh that fails completely: the surviving
    server block is what the user sees, so its dropdown must still drive its own block."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    body = (
        QUOTA_VISIBILITY_DOM_STUBS + "\n"
        + QUOTA_VISIBILITY_LOAD_STUBS + "\n"
        + _quota_visibility_load_functions(source) + "\n"
        + QUOTA_VISIBILITY_FAILED_RELOAD_SCENARIO
    )
    out = _run_quota_dom(tmp_path, "quota-visibility-failed-reload", body)
    # Round 1 kept the one answering host; round 2 failed and left that cached row alone.
    assert out["outcomes"] == [{"ok": True, "rows": ["A"]}, {"ok": False, "rows": ["A"]}]
    assert out["statusLog"] == ["clear", "error"]
    # The blocks layout survived the failure: the single-server scope claims no host.
    assert out["singleScopeOwner"] is None
    assert out["singleHostChildren"] == 0
    # And the visible block answered its own toggle: label re-read from host A's map, cards and
    # charts repainted through host A's key, nothing else touched.
    assert out["hostKey"] == "A"
    assert out["labelA"] == "quotaVisibilityPrefix Claude Code"
    assert out["renders"] == [
        {"kind": "block", "server": "A"},
        {"kind": "cards", "server": "A", "present": ["claude", "codex"]},
        {"kind": "charts", "server": "A", "hostKey": "A", "present": ["claude", "codex"]},
    ]
    assert out["stored"]["A"]["codex"] is False
    assert "B" not in out["stored"]
