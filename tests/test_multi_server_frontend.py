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
    # A trailing line comment stays part of the emitted line, so ROUTE_PROBE_TIMEOUT_MS --
    # the one binding that ships annotated -- is read verbatim instead of being restated.
    match = re.search(rf"^\s*(?:const|let) {re.escape(name)} = .*?;(\s*//.*)?$", source, re.MULTILINE)
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
    # The probe is per route and it is a plain browser fetch: nothing proxies one daemon
    # through another, which is what keeps this feature client-only.
    assert "fetch(routePath(route, '/health')" in source
    assert "addEventListener('click', () => { storeServerFromForm(); })" in source
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
    win_save = win_settings[win_settings.index("private async void Save_Click") : win_settings.index("private static string ServerSignature")]
    # Every address is validated, including additional routes; an offline probe does
    # not prevent saving an explicit URL. The old single-address expression is gone.
    assert "new[] { entry.BaseUrl }.Concat(entry.Routes)" in win_save
    assert "Any(a => !CompanionStore.IsValidBaseURL(a))" in win_save
    assert "CheckRoutesAsync" not in win_save and "RouteStatus" not in win_save
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
            "const LOCAL_SERVER = { id: 'local', label: 'Local', baseUrl: '' };\nfunction localHost() { return LOCAL_SERVER; }",
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


# ---- Issue #108: one host, N routes ----------------------------------------
# The pure half of the route model, extracted and run the same way as the mergers above.
# None of it touches a browser, which is the point: the merge rule, the anti-flap band and
# the blocked-vs-unreachable split are the parts that can be wrong quietly.

def _route_source(source: str) -> list[str]:
    """The route-model bindings and pure functions, in dependency order."""
    return [
        _js_binding(source, "LOCAL_HOST_ID"),
        _js_binding(source, "ORIGIN_ROUTE_ID"),
        _js_binding(source, "SERVERS_STORAGE_KEY"),
        _js_binding(source, "ROUTE_FLAP_MARGIN_MS"),
        _js_binding(source, "ROUTE_FLAP_MARGIN_RATIO"),
        # The harness has no location, so the page route is a stub; every call below passes
        # its own page address explicitly.
        "const PAGE_ROUTE_URL = '';",
        _extract_js_function(source, "function newId(prefix) {"),
        _extract_js_function(source, "function normalizeRouteUrl(value) {"),
        _extract_js_function(source, "function routeUrlParts(value) {"),
        _js_binding(source, "LOOPBACK_HOSTNAMES"),
        _extract_js_function(source, "function isLoopbackHostname(hostname) {"),
        _extract_js_function(source, "function tailnetSuffix(hostname) {"),
        _extract_js_function(source, "function predictBlocked(pageUrl, routeUrl) {"),
        _extract_js_function(source, "function routeMedianMs(state) {"),
        _extract_js_function(source, "function beatsIncumbent(challengerMs, incumbentMs) {"),
        _extract_js_function_with_params(source, "function rankRoutes("),
        _extract_js_function(source, "function hostRoutes(host) {"),
        _extract_js_function(source, "function defaultServerLabel(value) {"),
        _extract_js_function_with_params(source, "function migrateRegistry("),
        _extract_js_function_with_params(source, "function localHostFrom("),
        _extract_js_function_with_params(source, "function loadServerRegistry("),
        _extract_js_function_with_params(source, "function mergeHostsByIdentity("),
        _extract_js_function(source, "function tokdashShaped(payload) {"),
        _extract_js_function(source, "function routeLevelError(error) {"),
    ]


def _run_routes(tmp_path: Path, name: str, expression: str, value, extra: list[str] | None = None):
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = _route_source(source) + (extra or [])
    harness = tmp_path / f"{name}.js"
    harness.write_text(
        "\n".join(functions)
        # Each harness names its fixture pieces directly (urls, cases, hosts...), so the
        # parsed input is spread onto the global object rather than reached through `input`.
        + "\nconst input = JSON.parse(process.argv[2]);\nObject.assign(globalThis, input);\n"
        + f"process.stdout.write(JSON.stringify({expression}));\n",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(harness), json.dumps(value)], check=True, capture_output=True, encoding="utf-8")
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_route_url_normalisation_is_the_dedupe_key(tmp_path):
    urls = [
        "HTTP://127.0.0.1:55423/",
        "http://127.0.0.1:55423",
        "https://wsl.tail76535.ts.net:443/tokdash/",
        "https://wsl.tail76535.ts.net/tokdash",
        "http://192.168.1.30:55423/base//",
        "http://[::1]:55423",
        "not a url",
        "ftp://host/x",
        "/relative/only",
    ]
    out = _run_routes(tmp_path, "normalise", "urls.map(normalizeRouteUrl)", {"urls": urls})
    assert out[0] == "http://127.0.0.1:55423" == out[1]
    assert out[2] == "https://wsl.tail76535.ts.net/tokdash" == out[3], "default port and trailing slash drop, prefix stays"
    assert out[4] == "http://192.168.1.30:55423/base"
    assert out[5] == "http://[::1]:55423"
    assert out[6:] == ["", "", ""], "anything a browser could not fetch is not a route"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_blocked_is_predicted_from_the_two_addresses_not_probed(tmp_path):
    cases = [
        # https page, LAN plain-http route: the page itself refuses it.
        ("https://wsl.tail76535.ts.net/tokdash", "http://192.168.1.30:55423"),
        # https Serve page, foreign tailnet: no stock rule admits that origin.
        ("https://wsl.other.tail76535.ts.net/tokdash", "https://mac.tail76535.ts.net"),
        # https Serve page, its own machine's loopback: still admitted by nothing.
        ("https://wsl.tail76535.ts.net/tokdash", "http://127.0.0.1:55423"),
        # https page on a tunnel host, any cross-origin route.
        ("https://dash.example.com", "https://other.example.com"),
        # the loopback page, which is where the feature lives: all of those read fine.
        ("http://127.0.0.1:55423", "https://wsl.tail76535.ts.net/tokdash"),
        ("http://127.0.0.1:55423", "http://192.168.1.30:55423"),
        ("http://127.0.0.1:55423", "http://127.0.0.1:55424"),
        # same tailnet, https to https: admitted.
        ("https://wsl.tail76535.ts.net/tokdash", "https://mac.tail76535.ts.net"),
        # same origin is never blocked, prefix differences included.
        ("https://wsl.tail76535.ts.net/tokdash", "https://wsl.tail76535.ts.net/tokdash/api"),
        # a Serve page cannot read a plain-HTTP same-tailnet address (mixed content first).
        ("https://wsl.tail76535.ts.net/tokdash", "http://matebook.tail76535.ts.net:55423"),
    ]
    out = _run_routes(tmp_path, "blocked", "pairs.map(([p, r]) => predictBlocked(p, r))", {"pairs": cases})
    assert out[0] == "mixedContent"
    assert out[1] == "originNotAdmitted"
    assert out[2] == "originNotAdmitted", "a Serve page reading loopback is blocked, its own machine included"
    assert out[3] == "originNotAdmitted"
    assert out[4:7] == ["", "", ""], "the loopback page is admitted by every daemon"
    assert out[7] == ""
    assert out[8] == ""
    assert out[9] == "mixedContent"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_registry_migration_accepts_both_shapes(tmp_path):
    stored = [
        {"id": "old", "label": "Old", "baseUrl": "HTTP://Mac:55423/"},
        {"id": "routed", "label": "Routed", "routes": [
            {"id": "r1", "url": "http://192.168.1.30:55423"},
            {"url": "http://matebook.tail76535.ts.net:55423"},
            {"id": "dup", "url": "http://192.168.1.30:55423/"},
        ]},
        {"id": "local", "label": "WSL box", "choice": "r1",
         "routes": [{"id": "w", "url": "https://wsl.tail76535.ts.net/tokdash"}]},
        {"id": "junk", "label": "no url"},
        {"id": "routed", "label": "duplicate id"},
        None,
        "not an object",
    ]
    out = _run_routes(
        tmp_path, "migrate", "migrateRegistry(stored, 'http://127.0.0.1:55423')", {"stored": stored}
    )
    ids = [host["id"] for host in out]
    assert ids == ["old", "routed", "local"], "junk and a repeated id drop, a route-less host drops, local does not"
    old, routed, local = out
    assert old["routes"][0]["url"] == "http://mac:55423", "v1 baseUrl becomes the single route, canonicalised"
    assert [r["url"] for r in routed["routes"]] == [
        "http://192.168.1.30:55423", "http://matebook.tail76535.ts.net:55423"]
    assert all(r["id"] for r in routed["routes"]), "a route without an id gets one"
    assert routed["baseUrl"] == "http://192.168.1.30:55423", "baseUrl mirrors the first route for downgrade"
    assert local["label"] == "WSL box"
    assert local["choice"] == "auto", "a choice naming a route this host does not own resets"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_local_entry_survives_a_reload_with_its_routes_and_choice(tmp_path):
    stored = [{"id": "local", "label": "WSL box", "choice": "w",
               "routes": [{"id": "w", "url": "https://wsl.tail76535.ts.net/tokdash", "kind": "added"}]},
              {"id": "mate", "label": "matebook", "baseUrl": "http://192.168.1.30:55423"}]
    out = _run_routes(tmp_path, "localload",
                      "loadServerRegistry({ getItem: () => JSON.stringify(stored) }, 'http://127.0.0.1:55423')",
                      {"stored": stored})
    hosts = out
    assert [h["id"] for h in hosts] == ["local", "mate"], "local stays first, as the old constant did"
    local = hosts[0]
    assert local["label"] == "WSL box", "the rename comes back"
    assert local["choice"] == "w", "the pinned route comes back"
    assert [r["url"] for r in local["routes"]] == [
        "http://127.0.0.1:55423", "https://wsl.tail76535.ts.net/tokdash"], "the origin route is prepended, not stored"
    assert local["routes"][0]["kind"] == "origin"
    assert local["instanceId"] == "", "local's identity is re-read each load, never trusted from storage"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_active_route_picks_fastest_then_holds_inside_the_band(tmp_path):
    routes = [{"id": "a", "url": "http://a"}, {"id": "b", "url": "http://b"}]
    fast_then_held = {
        "routes": routes,
        "runtime": {"a": {"state": "ok", "samples": [40], "reportedInstanceId": "H"},
                      "b": {"state": "ok", "samples": [34], "reportedInstanceId": "H"}},
        "choice": "auto", "hostInstanceId": "H", "incumbentRouteId": "a",
    }
    out = _run_routes(tmp_path, "band",
                      "[rankRoutes(scenario), rankRoutes(Object.assign({}, scenario, { runtime: { a: { state: 'ok', samples: [40], reportedInstanceId: 'H' }, b: { state: 'ok', samples: [4], reportedInstanceId: 'H' } } }))]",
                      {"scenario": fast_then_held})
    held, taken = out
    assert held["activeRouteId"] == "a" and held["activeReason"] == "held", "6 ms of jitter on 40 ms is not a reason to move"
    assert held["fastestRouteId"] == "b", "the ranker still knows which is fastest; only the active route is held"
    assert taken["activeRouteId"] == "b" and taken["activeReason"] == "fastest"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_route_ranking_rules_pinned_unhealthy_and_all_failed(tmp_path):
    routes = [{"id": "a", "url": "http://a"}, {"id": "b", "url": "http://b"}]
    cases = {
        "pinnedDead": {"routes": routes, "runtime": {"a": {"state": "ok", "samples": [10], "reportedInstanceId": "H"},
                                                      "b": {"state": "fail", "samples": []}},
                       "choice": "b", "hostInstanceId": "H"},
        "pinnedLive": {"routes": routes, "runtime": {"a": {"state": "ok", "samples": [10], "reportedInstanceId": "H"},
                                                      "b": {"state": "ok", "samples": [900], "reportedInstanceId": "H"}},
                       "choice": "b", "hostInstanceId": "H"},
        "allFailed": {"routes": routes, "runtime": {"a": {"state": "fail"}, "b": {"state": "blocked"}},
                      "choice": "auto", "hostInstanceId": "H"},
        # A route answering as a different daemon never ranks, and a host with no identity
        # still ranks on the fingerprint alone.
        "wrongDaemon": {"routes": routes, "runtime": {"a": {"state": "ok", "samples": [5], "reportedInstanceId": "OTHER"},
                                                      "b": {"state": "ok", "samples": [500], "reportedInstanceId": "H"}},
                        "choice": "auto", "hostInstanceId": "H"},
        "noHostId": {"routes": routes, "runtime": {"a": {"state": "ok", "samples": [5], "reportedInstanceId": "OTHER"}},
                     "choice": "auto", "hostInstanceId": ""},
        # A route the probe marked not-this-daemon is unusable even with good samples.
        "marked": {"routes": routes, "runtime": {"a": {"state": "not-this-daemon", "samples": [5]},
                                                 "b": {"state": "ok", "samples": [500], "reportedInstanceId": "H"}},
                   "choice": "auto", "hostInstanceId": "H"},
    }
    out = _run_routes(tmp_path, "rules",
                      "Object.fromEntries(Object.entries(cases).map(([k, v]) => [k, rankRoutes(v)]))", {"cases": cases})
    assert out["pinnedDead"]["activeRouteId"] == "a", "a pinned route that died falls back rather than going dark"
    assert out["pinnedDead"]["usableRouteIds"] == ["a"]
    assert out["pinnedLive"]["activeRouteId"] == "b" and out["pinnedLive"]["activeReason"] == "pinned"
    assert out["allFailed"]["usableRouteIds"] == []
    assert out["allFailed"]["activeRouteId"] == "a" and out["allFailed"]["activeReason"] == "unverified", \
        "the host keeps a placeholder so it stays in the combined view"
    assert out["wrongDaemon"]["usableRouteIds"] == ["b"], "a route for another daemon does not count"
    assert out["noHostId"]["usableRouteIds"] == ["a"], "identity-less hosts rank on the fingerprint"
    assert out["marked"]["usableRouteIds"] == ["b"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_identity_merge_survivor_and_carried_state(tmp_path):
    hosts = [
        {"id": "ws", "label": "workstation", "instanceId": "SAME", "routes": [{"id": "r1", "url": "http://ws"}]},
        {"id": "local", "label": "Local", "instanceId": "SAME", "routes": [{"id": "r2", "url": "http://127.0.0.1:55423"}]},
        {"id": "other", "label": "other", "instanceId": "OTHER", "routes": [{"id": "r3", "url": "http://other"}]},
    ]
    side = {"runtime": {"ws": {"ok": True}, "local": {"ok": False}}, "csrf": {"ws": "token"}}
    out, side_after = _run_routes(tmp_path, "merge",
                                  "[mergeHostsByIdentity(hosts, side), side]", {"hosts": hosts, "side": side})
    assert [h["id"] for h in out["hosts"]] == ["local", "other"], "local always survives a merge"
    local = out["hosts"][0]
    assert local["label"] == "Local", "the survivor keeps its label"
    assert [r["url"] for r in local["routes"]] == ["http://127.0.0.1:55423", "http://ws"]
    assert out["lostHostIds"] == ["ws"]
    assert out["notices"] == [{"survivorId": "local", "survivorLabel": "Local", "loserId": "ws", "loserLabel": "workstation"}]
    assert "local" in side_after["runtime"] and "ws" not in side_after["runtime"], "health moves with the host"
    assert side_after["csrf"]["local"] == "token", "the cached CSRF token moves too"

    # Without local in the group the earliest row wins, and a host with no id never merges.
    pair = [
        {"id": "a", "label": "A", "instanceId": "SAME", "routes": [{"id": "x", "url": "http://a"}]},
        {"id": "b", "label": "B", "instanceId": "SAME", "routes": [{"id": "y", "url": "http://b"}]},
        {"id": "d", "label": "D", "instanceId": "", "routes": [{"id": "z", "url": "http://d"}]},
        {"id": "e", "label": "E", "instanceId": "", "routes": [{"id": "q", "url": "http://e"}]},
    ]
    out2 = _run_routes(tmp_path, "merge2", "mergeHostsByIdentity(pair, {})", {"pair": pair})
    assert [h["id"] for h in out2["hosts"]] == ["a", "d", "e"], "earliest row survives; identity-less hosts stay apart"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_503_with_a_tokdash_detail_is_not_a_route_failure(tmp_path):
    cases = {
        "corsOrDeadSocket": {"typeName": "TypeError"},
        "abort": {"name": "AbortError"},
        "proxy502": {"status": 502, "payload": None},
        "proxyJsonNoDetail": {"status": 504, "payload": {"error": "gateway timeout"}},
        "proxy503Plain": {"status": 503, "payload": None},
        "backpressure503": {"status": 503, "payload": {"detail": "Too many cold requests"}},
        "notFound": {"status": 404, "payload": {"detail": "Not Found"}},
        "nothing": None,
    }
    out = _run_routes(tmp_path, "routelevel",
                      "Object.fromEntries(Object.entries(cases).map(([k, v]) => [k, routeLevelError(v)]))",
                      {"cases": cases})
    assert out["abort"] is True and out["nothing"] is False
    assert out["proxy502"] is True and out["proxy503Plain"] is True and out["proxyJsonNoDetail"] is True
    assert out["backpressure503"] is False, "Tokdash backpressure is retry, not a dead route"
    assert out["notFound"] is False

    # The TypeError half needs a real TypeError, which is what fetch throws on a CORS refusal.
    type_case = _run_routes(tmp_path, "routelevel2", "routeLevelError(new TypeError('Failed to fetch'))", {"x": 1})
    assert type_case is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_two_routes_one_host_still_counts_once(tmp_path):
    """The regression this whole feature exists for.

    combineUsagePayloads merges per host, so as long as a two-route daemon is one entry in
    the fan-out the double count is structurally impossible. This pins that the host shape
    still feeds the merger exactly one row per host.
    """
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = _route_source(source) + [
        _extract_js_function_with_params(source, "function loadServerRegistry("),
        _extract_js_function(source, "function combineUsagePayloads(list) {"),
    ]
    harness = tmp_path / "onecount.js"
    harness.write_text(
        "\n".join(functions)
        + "\nconst input = JSON.parse(process.argv[2]);\n"
        + "const hosts = loadServerRegistry({ getItem: () => JSON.stringify(input.stored) }, input.page);\n"
        + "const perHost = hosts.map((host) => JSON.parse(JSON.stringify(input.usage)));\n"
        + "process.stdout.write(JSON.stringify({ hostCount: hosts.length, routeCount: hosts[0].routes.length,"
        + " merged: combineUsagePayloads(perHost) }));\n",
        encoding="utf-8",
    )
    usage = {"total_tokens": 100, "total_cost": 1.5, "daily": [{"date": "2026-09-25", "tokens": 100, "cost": 1.5}]}
    stored = [{"id": "local", "label": "Local",
               "routes": [{"id": "w", "url": "https://wsl.tail76535.ts.net/tokdash"}]}]
    result = json.loads(subprocess.run(
        ["node", str(harness), json.dumps({"stored": stored, "page": "http://127.0.0.1:55423", "usage": usage})],
        check=True, capture_output=True, encoding="utf-8").stdout)
    assert result["hostCount"] == 1, "the tailnet URL joined the page's own host instead of becoming a second row"
    assert result["routeCount"] == 2, "and both addresses are still there"
    assert result["merged"]["total_tokens"] == 100, "two routes for one daemon must not double the tokens"


def _probing_source(source: str) -> list[str]:
    """The live probing code, with the few globals it reaches for stubbed.

    `fetch` is left to each test, so a route can be made to answer, go quiet mid-round, or
    refuse. `hosts` is a `var` because the fixture overwrites it through `globalThis`.
    """
    return [
        _js_binding(source, "LOCAL_HOST_ID"),
        _js_binding(source, "ORIGIN_ROUTE_ID"),
        _js_binding(source, "ROUTE_PROBE_SAMPLES"),
        _js_binding(source, "ROUTE_PROBE_TIMEOUT_MS"),
        _js_binding(source, "ROUTE_PROBE_BACKOFF_CAP_MS"),
        "const PAGE_ROUTE_URL = '';",
        "const t = (key) => key;",
        "var hosts = [];",
        "const routeRuntime = {};",
        _extract_js_function(source, "function newId(prefix) {"),
        _extract_js_function(source, "function normalizeRouteUrl(value) {"),
        _extract_js_function(source, "function routeUrlParts(value) {"),
        _js_binding(source, "LOOPBACK_HOSTNAMES"),
        _extract_js_function(source, "function isLoopbackHostname(hostname) {"),
        _extract_js_function(source, "function tailnetSuffix(hostname) {"),
        _extract_js_function(source, "function predictBlocked(pageUrl, routeUrl) {"),
        _extract_js_function(source, "function routeMedianMs(state) {"),
        _extract_js_function(source, "function routePath(route, path) {"),
        _extract_js_function(source, "function tokdashShaped(payload) {"),
        _extract_js_function(source, "function routeLevelError(error) {"),
        _extract_js_function(source, "function newRouteRuntime() {"),
        _extract_js_function(source, "function setRouteState(routeId, patch) {"),
        _extract_js_function(source, "function routeBackoffReady(routeId, now = Date.now()) {"),
        _extract_js_function(source, "function markRouteBlocked(routeId, url, error) {"),
        _extract_js_function(source, "function markRouteFailure(routeId, url, error) {"),
        _extract_js_function(source, "async function probeOnce(route) {"),
        _extract_js_function_with_params(source, "async function probeRoute("),
        _extract_js_function(source, "function hostRoutes(host) {"),
        _extract_js_function(source, "function applyProbeIdentity(host, route, probe) {"),
        _extract_js_function_with_params(source, "function blockedRuleText(rule) {"),
        _extract_js_function_with_params(source, "async function validateRouteAddress("),
    ]


def _fetching_source(source: str) -> list[str]:
    """The probe code plus the read path, so failover can be driven end to end."""
    return _probing_source(source) + [
        _js_binding(source, "ROUTE_FLAP_MARGIN_MS"),
        _js_binding(source, "ROUTE_FLAP_MARGIN_RATIO"),
        # The harness has no location, so same-origin paths are passed through.
        "const appPath = (path) => path;",
        "const hostActiveRouteId = new Map();",
        _extract_js_function(source, "function beatsIncumbent(challengerMs, incumbentMs) {"),
        _extract_js_function_with_params(source, "function rankRoutes("),
        _extract_js_function(source, "function hostRouteById(server, routeId) {"),
        _extract_js_function(source, "function activeRouteIdFor(host, runtime = routeRuntime) {"),
        _extract_js_function(source, "function activeRouteFor(host) {"),
        _extract_js_function(source, "function serverPath(server, path) {"),
        _extract_js_function(source, "function viaRoute(server, route) {"),
        _extract_js_function_with_params(source, "async function fetchJson("),
        _extract_js_function_with_params(source, "function pickRoute("),
        _extract_js_function_with_params(source, "async function fetchFromHost("),
    ]


def _run_fetching(tmp_path: Path, name: str, body: str, value=None):
    source = INDEX_HTML.read_text(encoding="utf-8")
    harness = tmp_path / f"{name}.js"
    harness.write_text(
        "\n".join(_fetching_source(source))
        + "\nconst input = JSON.parse(process.argv[2]);\nObject.assign(globalThis, input);\n"
        + "const main = async () => " + body + ";\n"
        + "main().then((result) => process.stdout.write(JSON.stringify(result)));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(value if value is not None else {})],
        check=True, capture_output=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def _run_probing(tmp_path: Path, name: str, body: str, value=None):
    """Run an async body against the real probe code and read the JSON it returns."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    harness = tmp_path / f"{name}.js"
    harness.write_text(
        "\n".join(_probing_source(source))
        + "\nconst input = JSON.parse(process.argv[2]);\nObject.assign(globalThis, input);\n"
        + "const main = async () => " + body + ";\n"
        + "main().then((result) => process.stdout.write(JSON.stringify(result)));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(value if value is not None else {})],
        check=True, capture_output=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_placeholder_never_lands_on_a_route_from_another_daemon(tmp_path):
    """The all-failed placeholder exists to keep an unverified host in the combined view.

    Once a route has been caught answering as some other Tokdash it is no longer unverified,
    and reading it would file that machine's tokens under this host -- which is what happens
    on failover when the only remaining address is the stale one.
    """
    routes = [{"id": "stale", "url": "http://stale"}, {"id": "other", "url": "http://other"}]
    barred = {"state": "not-this-daemon", "samples": [5]}
    cases = {
        "allBarred": {"routes": routes, "runtime": {"stale": barred, "other": dict(barred)},
                      "choice": "auto", "hostInstanceId": "H"},
        # A route that only failed says nothing about who answers it, so it keeps standing in
        # for the host the way it always has; the bar is on identity, not on health.
        "barredThenFailed": {"routes": routes,
                             "runtime": {"stale": barred, "other": {"state": "fail", "samples": []}},
                             "choice": "auto", "hostInstanceId": "H"},
        "barredThenUnprobed": {"routes": routes, "runtime": {"stale": barred},
                               "choice": "auto", "hostInstanceId": "H"},
        "nothingProbedYet": {"routes": routes, "runtime": {}, "choice": "auto", "hostInstanceId": "H"},
    }
    out = _run_routes(tmp_path, "barred",
                      "Object.fromEntries(Object.entries(cases).map(([k, v]) => [k, rankRoutes(v)]))",
                      {"cases": cases})
    assert out["allBarred"]["activeRouteId"] == "", "no route left may carry the host's figures"
    assert out["allBarred"]["activeReason"] == ""
    assert out["barredThenFailed"]["activeRouteId"] == "other", "a failed address still stands in"
    assert out["barredThenUnprobed"]["activeRouteId"] == "other", "and so does an unprobed one"
    assert out["nothingProbedYet"]["activeRouteId"] == "stale", "before any probe the first route still stands in"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_route_in_backoff_is_not_dialled_between_probes(tmp_path):
    """The backoff has to cover ordinary reads, not just the probe loop.

    A route that keeps failing backs off so the browser stops knocking on it -- every blocked
    probe is a red console error and a wasted round trip. If a refresh could still pick that
    route as the host's placeholder, the backoff would only hide it from the prober. `now` is
    passed in so the case is about the rule rather than about the clock.
    """
    routes = [{"id": "dead", "url": "http://dead"}, {"id": "quiet", "url": "http://quiet"}]
    in_backoff = {"state": "fail", "samples": [], "nextTryAt": 5_000}
    cases = {
        # Every address just failed and none is due yet: the host sits this refresh out.
        "allInBackoff": {"routes": routes, "runtime": {"dead": dict(in_backoff), "quiet": dict(in_backoff)},
                         "choice": "auto", "hostInstanceId": "H", "now": 1_000},
        # Due again, so it is fair game -- otherwise a route stays benched for good.
        "backoffExpired": {"routes": routes, "runtime": {"dead": dict(in_backoff), "quiet": dict(in_backoff)},
                           "choice": "auto", "hostInstanceId": "H", "now": 6_000},
        # blocked is permanent from this page, not a bad moment, so it never earns a request.
        "allBlocked": {"routes": routes, "runtime": {"dead": {"state": "blocked"}, "quiet": {"state": "blocked"}},
                       "choice": "auto", "hostInstanceId": "H", "now": 1_000},
        "blockedThenBackedOff": {"routes": routes, "runtime": {"dead": {"state": "blocked"}, "quiet": dict(in_backoff)},
                                 "choice": "auto", "hostInstanceId": "H", "now": 1_000},
        # A route nothing has probed still stands in, so a host never vanishes before its
        # first round of probes has run.
        "blockedThenIdle": {"routes": routes, "runtime": {"dead": {"state": "blocked"}},
                            "choice": "auto", "hostInstanceId": "H", "now": 1_000},
    }
    out = _run_routes(tmp_path, "backoff",
                      "Object.fromEntries(Object.entries(cases).map(([k, v]) => [k, rankRoutes(v)]))",
                      {"cases": cases})
    assert out["allInBackoff"]["activeRouteId"] == "", "a refresh does not contact a route that is backing off"
    assert out["allInBackoff"]["activeReason"] == ""
    assert out["backoffExpired"]["activeRouteId"] == "dead", "once the backoff lapses the route is back"
    assert out["allBlocked"]["activeRouteId"] == "", "an address this page can never read is never worth a try"
    assert out["blockedThenBackedOff"]["activeRouteId"] == ""
    assert out["blockedThenIdle"]["activeRouteId"] == "quiet", "the unprobed address carries the host instead"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_backed_off_route_receives_no_request_at_all(tmp_path):
    """Ranking is the only door a read goes through, so the count is the proof.

    The assertion that matters is that nothing was fetched: a placeholder that merely looked
    worse in the list would still put a request on a route the probes just gave up on.
    """
    body = """{
      const hits = [];
      globalThis.fetch = async (url) => { hits.push(String(url)); throw new TypeError('Failed to fetch'); };
      const host = { id: 'ws', instanceId: 'WS', choice: 'auto',
        routes: [{ id: 'a', url: 'http://a', kind: 'added' }, { id: 'b', url: 'http://b', kind: 'added' }] };
      markRouteFailure('a', 'http://a', new TypeError('Failed to fetch'));
      markRouteFailure('b', 'http://b', new TypeError('Failed to fetch'));
      let threw = null;
      try { await fetchFromHost(host, '/api/usage'); } catch (error) { threw = String(error); }
      const whileBackingOff = hits.length;
      // Then the backoff lapses, and the very same read goes out again.
      Object.values(routeRuntime).forEach((state) => { state.nextTryAt = 0; });
      const recovered = await fetchFromHost(host, '/api/usage').catch((error) => `threw:${error}`);
      return { whileBackingOff, total: hits.length, threw, recovered: String(recovered),
               a: routeRuntime.a.state, b: routeRuntime.b.state };
    }"""
    out = _run_fetching(tmp_path, "backoff_reads", body)
    assert out["whileBackingOff"] == 0, "neither address is dialled while both are backing off"
    assert "serverRouteUnreachable" in out["threw"], "the host fails the refresh with its own reason"
    assert out["total"] > 0 and out["recovered"].startswith("threw:"), \
        "once they are due again the read goes out and reports the real failure"
    assert out["a"] == "fail" and out["b"] == "fail"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_locals_identity_comes_only_from_the_page_route(tmp_path):
    """Local is whatever daemon served this page, so only that address may say which one."""
    body = """{
      const local = { id: LOCAL_HOST_ID, instanceId: '',
        routes: [{ id: 'origin', url: 'http://127.0.0.1:55423', kind: 'origin' },
                 { id: 'added', url: 'http://127.0.0.1:55499', kind: 'added' }] };
      // The added address is a stale forward answering as a different machine.
      applyProbeIdentity(local, local.routes[1], { ok: true, instanceId: 'OTHER' });
      const staleMarked = routeRuntime.added && routeRuntime.added.state;
      applyProbeIdentity(local, local.routes[0], { ok: true, instanceId: 'MINE' });
      // Identity known, the stale route is checked against it rather than rewriting it.
      applyProbeIdentity(local, local.routes[1], { ok: true, instanceId: 'OTHER' });
      const late = { id: LOCAL_HOST_ID, instanceId: '', routes: [local.routes[1]] };
      applyProbeIdentity(late, local.routes[1], { ok: true, instanceId: 'OTHER' });
      return { afterStaleFirst: staleMarked, identity: local.instanceId,
               staleState: routeRuntime.added.state, lateIdentity: late.instanceId };
    }"""
    out = _run_probing(tmp_path, "localidentity", body)
    assert out["identity"] == "MINE", "only the page's own address identifies Local"
    assert out["staleState"] == "not-this-daemon", "the added address is judged against it"
    assert out["lateIdentity"] == "", "and an added address cannot name Local before the page route does"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_route_that_goes_quiet_mid_probe_is_not_left_healthy(tmp_path):
    """Stopping the round is only half the rule; the round has to end in a failure."""
    body = """{
      const healthy = { id: 'healthy', url: 'http://healthy', kind: 'added' };
      const quiet = { id: 'quiet', url: 'http://quiet', kind: 'added' };
      let calls = { healthy: 0, quiet: 0 };
      globalThis.fetch = async (url) => {
        const id = String(url).includes('healthy') ? 'healthy' : 'quiet';
        calls[id] += 1;
        if (id === 'quiet' && calls.quiet > 1) throw new TypeError('Failed to fetch');
        return { ok: true, status: 200,
          json: async () => ({ service: 'tokdash', instance_id: 'MINE', version: '2.6.4' }) };
      };
      const goodProbe = await probeRoute(healthy, { bypassBackoff: true });
      const quietProbe = await probeRoute(quiet, { bypassBackoff: true });
      return { good: { ok: goodProbe.ok, state: routeRuntime.healthy.state, samples: routeRuntime.healthy.samples.length },
               quiet: { ok: quietProbe.ok, kind: quietProbe.kind, state: routeRuntime.quiet.state,
                        samples: routeRuntime.quiet.samples.length },
               quietCalls: calls.quiet };
    }"""
    out = _run_probing(tmp_path, "quietroute", body)
    assert out["good"]["ok"] is True and out["good"]["samples"] == 3, "a live route still takes three samples"
    assert out["quiet"]["ok"] is False and out["quiet"]["kind"] == "fail", "a route that goes quiet fails"
    assert out["quiet"]["state"] == "fail", "and is not left green on the sample it managed"
    assert out["quiet"]["samples"] == 0 and out["quietCalls"] == 2, "the round stops at the first failure"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_an_address_is_judged_before_it_is_stored(tmp_path):
    """An address has to prove it belongs before it can sit on a host's row.

    An unknown id cannot prove two addresses are the same machine, so a host that reports no
    identity of its own takes no new addresses at all. Once a host DOES have an identity, an
    address that reports none is refused on an edit just as it is on an add: ranking bars a
    blank report from carrying that host's figures, so accepting one here would only park a
    route that can never be used. A host still waiting on its identity edits loosely, because
    repointing that host's only address defines it rather than merging anything.
    """
    body = """{
      globalThis.fetch = async () => (typeof reply === 'function' ? reply()
        : { ok: true, status: 200, json: async () => ({ service: 'tokdash', instance_id: reply, version: '2.6.4' }) });
      const host = { id: 'ws', instanceId: 'WS', routes: [{ id: 'r', url: 'http://ws', kind: 'added' }] };
      const unidentified = { id: 'old', instanceId: '', routes: [{ id: 'o', url: 'http://old', kind: 'added' }] };
      const results = {};
      const add = (h, url) => validateRouteAddress(h, url, { requireProof: true });
      const edit = (h, url) => validateRouteAddress(h, url);

      reply = () => { throw new TypeError('Failed to fetch'); };
      results.unreachableOnAdd = (await add(host, 'http://c')).error;
      results.unreachableOnEdit = (await edit(host, 'http://c')).error;

      reply = 'WS';
      results.ownDaemonOnAdd = (await add(host, 'http://c')).error;

      reply = 'LAPTOP';
      results.strangerOnAdd = (await add(host, 'http://c')).error;
      results.strangerOnEdit = (await edit(host, 'http://c')).error;
      hosts = [host, { id: 'lap', label: 'laptop', instanceId: 'LAPTOP', routes: [{ id: 'q', url: 'http://lap' }] }];
      results.listedOwnerOnAdd = (await add(host, 'http://c')).error;

      hosts = [host];
      reply = '';
      results.noIdentityOnAdd = (await add(host, 'http://c')).error;
      results.noIdentityOnEdit = (await edit(host, 'http://c')).error;
      // Same blank report, but this host has no identity of its own: repointing its only
      // address defines it, so there is nothing for the silence to contradict.
      results.noIdentitySoleRouteEdit = (await edit(unidentified, 'http://c')).error;

      reply = 'WHOEVER';
      results.hostWithoutIdentityOnAdd = (await add(unidentified, 'http://c')).error;
      results.hostWithoutIdentityOnEdit = (await edit(unidentified, 'http://c')).error;
      return results;
    }"""
    out = _run_probing(tmp_path, "validate", body, {"hosts": [], "reply": ""})
    assert out["unreachableOnAdd"] == out["unreachableOnEdit"] == "serverTestNetworkError"
    assert out["ownDaemonOnAdd"] is None, "an address that answers as this host's daemon is fine"
    assert str(out["strangerOnAdd"]).startswith("serverRouteWrongDaemon"), "an unlisted daemon is refused"
    assert str(out["strangerOnEdit"]).startswith("serverRouteWrongDaemon"), "on an edit too"
    assert out["listedOwnerOnAdd"] == "serverRouteAlreadyOn", "a listed daemon is refused by name"
    assert out["noIdentityOnAdd"] == "serverIdentityUnavailable", "an address with no id proves nothing"
    assert out["noIdentityOnEdit"] == "serverIdentityUnavailable", \
        "an edit is refused too, because ranking would never let that route carry the host"
    assert out["noIdentitySoleRouteEdit"] is None, "a host with no identity can still be repointed"
    assert out["hostWithoutIdentityOnAdd"] == "serverIdentityUnknownNote", "an unidentified host takes no address"
    assert out["hostWithoutIdentityOnEdit"] is None, "an old daemon can still be repointed"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_rejected_address_leaves_no_trace_in_the_live_state(tmp_path):
    """Validation probes under a probe-only id.

    Probing under a stored route's id would leave that route holding the candidate's latency
    or verdict, and Escape mid-probe could not give any of it back.
    """
    body = """{
      const seen = [];
      globalThis.fetch = async (url) => { seen.push(String(url)); throw new TypeError('Failed to fetch'); };
      const host = { id: 'ws', instanceId: 'WS', routes: [{ id: 'stored', url: 'http://ws', kind: 'added' }] };
      const { error } = await validateRouteAddress(host, 'http://candidate', { requireProof: true });
      return { error, keys: Object.keys(routeRuntime),
               storedInFile: seen.some((u) => u.includes('stored')) };
    }"""
    out = _run_probing(tmp_path, "no_trace", body)
    assert out["error"] == "serverTestNetworkError"
    assert out["keys"] == [], "a rejected probe leaves no runtime behind"
    assert out["storedInFile"] is False, "the stored route's id never reaches the network"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_failover_target_that_also_fails_is_demoted_too(tmp_path):
    """The retry has to record its own failure.

    Both addresses are down here, and a route left `ok` after a request that failed on it
    would be chosen again by every later read, with no backoff and no reason shown.
    """
    body = """{
      globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
      const host = { id: 'ws', instanceId: 'WS', choice: 'auto',
        routes: [{ id: 'a', url: 'http://a', kind: 'added' }, { id: 'b', url: 'http://b', kind: 'added' }] };
      setRouteState('a', { state: 'ok', samples: [10], reportedInstanceId: 'WS' });
      setRouteState('b', { state: 'ok', samples: [20], reportedInstanceId: 'WS' });
      let threw = null;
      try { await fetchFromHost(host, '/api/usage'); } catch (error) { threw = String(error); }
      return { threw, a: routeRuntime.a.state, b: routeRuntime.b.state,
               bBackoff: routeRuntime.b.backoffMs > 0 };
    }"""
    out = _run_fetching(tmp_path, "failover_both", body)
    assert "Failed to fetch" in out["threw"], "the caller still sees the failure"
    assert out["a"] == "fail" and out["b"] == "fail", "both addresses are demoted"
    assert out["bBackoff"], "and the second one is off the rotation for a while"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_read_moves_to_the_next_route_within_the_same_call(tmp_path):
    body = """{
      let hits = [];
      globalThis.fetch = async (url) => {
        hits.push(String(url));
        if (String(url).includes('http://a')) throw new TypeError('Failed to fetch');
        return { ok: true, status: 200, json: async () => ({ total_tokens: 7 }) };
      };
      const host = { id: 'ws', instanceId: 'WS', choice: 'auto',
        routes: [{ id: 'a', url: 'http://a', kind: 'added' }, { id: 'b', url: 'http://b', kind: 'added' }] };
      setRouteState('a', { state: 'ok', samples: [10], reportedInstanceId: 'WS' });
      setRouteState('b', { state: 'ok', samples: [20], reportedInstanceId: 'WS' });
      const payload = await fetchFromHost(host, '/api/usage');
      return { tokens: payload.total_tokens, a: routeRuntime.a.state, b: routeRuntime.b.state, hits };
    }"""
    out = _run_fetching(tmp_path, "failover_ok", body)
    assert out["tokens"] == 7, "the host reads through its next address"
    assert out["a"] == "fail" and out["b"] == "ok", "only the address that failed is demoted"
    assert len(out["hits"]) == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_an_identified_host_ranks_only_routes_that_named_themselves(tmp_path):
    """A route that answers without an id has proven reachability, not belonging.

    A current daemon can do that while its first lookup runs or during the backoff after a
    failed one, so such a route stays in the probe rotation -- but it cannot become the
    fastest route and supply another machine's figures under this host's name.
    """
    routes = [{"id": "quiet", "url": "http://quiet"}, {"id": "match", "url": "http://match"}]
    cases = {
        # Fastest by a mile, and still not usable: it never said it was this host.
        "blankVsMatch": {"routes": routes,
                         "runtime": {"quiet": {"state": "ok", "samples": [4], "reportedInstanceId": ""},
                                     "match": {"state": "ok", "samples": [900], "reportedInstanceId": "H"}},
                         "choice": "auto", "hostInstanceId": "H"},
        "blankOnly": {"routes": [routes[0]],
                      "runtime": {"quiet": {"state": "ok", "samples": [4], "reportedInstanceId": ""}},
                      "choice": "auto", "hostInstanceId": "H"},
        # An unidentified host is the old-daemon case, where the fingerprint is all there is.
        "hostWithoutIdentity": {"routes": [routes[0]],
                                "runtime": {"quiet": {"state": "ok", "samples": [4], "reportedInstanceId": ""}},
                                "choice": "auto", "hostInstanceId": ""},
        "notProbedYet": {"routes": routes, "runtime": {"match": {"state": "ok", "samples": [900], "reportedInstanceId": "H"}},
                         "choice": "auto", "hostInstanceId": "H"},
    }
    out = _run_routes(tmp_path, "blankidentity",
                      "Object.fromEntries(Object.entries(cases).map(([k, v]) => [k, rankRoutes(v)]))",
                      {"cases": cases})
    assert out["blankVsMatch"]["usableRouteIds"] == ["match"], "a blank report is not a verified match"
    assert out["blankVsMatch"]["activeRouteId"] == "match"
    assert out["blankOnly"]["usableRouteIds"] == []
    assert out["blankOnly"]["activeRouteId"] == "", "and it cannot stand in as the placeholder either"
    assert out["hostWithoutIdentity"]["usableRouteIds"] == ["quiet"], "an unidentified host ranks on the fingerprint"
    assert out["notProbedYet"]["activeRouteId"] == "match", "a route with no probe at all still stands in"


def test_session_detail_reads_through_the_route_system() -> None:
    """The drill-down used to fetch one fixed address.

    Every other read goes through the host's routes, so a session opened the moment its
    address died showed a failed panel where the next address would have shown the session.
    A browser check covers the happy path; this is what keeps the call from going back to a
    bare fetch.
    """
    source = INDEX_HTML.read_text(encoding="utf-8")
    body = _extract_js_function(source, "async function openSessionModal(tool, sessionId, ownerServer")
    assert "fetchFromHost(" in body, "the modal has to read through the route system"
    assert "await fetch(" not in body, "a fixed-address fetch cannot fail over to the next route"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("address,available,expected,hits", [
    ("box.ts.net/tokdash/", ["https://box.ts.net/tokdash"], "https://box.ts.net/tokdash", 1),
    ("127.0.0.1:55423", ["http://127.0.0.1:55423"], "http://127.0.0.1:55423", 2),
    ("[::1]:55423/base", ["http://[::1]:55423/base"], "http://[::1]:55423/base", 2),
    ("http://offline:55423", [], "http://offline:55423", 0),
    ("https://offline/base", [], "https://offline/base", 0),
    ("box:55423", [], None, 2),
    ("ftp://box", [], None, 0),
    ("not a host", [], None, 0),
    ("javascript:alert(1)", [], None, 0),
])
def test_user_address_protocol_discovery(tmp_path, address, available, expected, hits):
    source = INDEX_HTML.read_text(encoding="utf-8")
    harness = tmp_path / "protocol.js"
    harness.write_text("\n".join([
        _extract_js_function(source, "function normalizeRouteUrl(value) {"),
        _extract_js_function(source, "async function resolveRouteInput(value) {"),
        "const t = key => key; const calls = [];",
        f"const available = {json.dumps(available)};",
        "async function probeServer(url) { calls.push(url); if (!available.includes(url)) throw new Error('unreachable'); }",
        f"resolveRouteInput({json.dumps(address)}).then(result => console.log(JSON.stringify({{result,calls}})));",
    ]), encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(harness)], capture_output=True, text=True, check=True).stdout)
    assert out["result"].get("url") == expected
    assert len(out["calls"]) == hits
    if expected is None:
        assert out["result"]["error"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("confirm", [False, True])
def test_last_route_removal_preserves_host_when_cancelled(tmp_path, confirm):
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = [
        "const LOCAL_HOST_ID = 'local'; const t = key => key;",
        "const route = {id: 'route', kind: 'added', url: 'http://box'};",
        "const host = {id: 'box', label: 'Box', routes: [route], choice: 'route'}; let hosts = [host];",
        "const hostRoutes = host => host.routes; const routeRuntime = {route: {state: 'ok'}};",
        "const serverRuntimeStatus = new Map(); const csrfTokensByServer = new Map(); const hostActiveRouteId = new Map();",
        "let serverSelection = {mode: 'custom', custom: ['box']}; let saves = 0;",
        "const saveServerRegistry = () => saves++; const saveServerSelection = () => {};",
        "const renderServerSettings = () => {}; const refreshCurrentView = () => {};",
        f"const window = {{confirm: () => {str(confirm).lower()}}};",
        _extract_js_function(source, "function removeHost(host) {"),
        _extract_js_function(source, "function removeRoute(host, route) {"),
    ]
    out = _run(tmp_path, "remove_last_route", functions,
               "(() => { removeRoute(host, route); return {count: hosts.length, routes: host.routes.length, state: !!routeRuntime.route, saves, selection: serverSelection.mode}; })()", None)
    assert out == {"count": 0 if confirm else 1, "routes": 1, "state": not confirm,
                   "saves": 1 if confirm else 0, "selection": "all" if confirm else "custom"}
