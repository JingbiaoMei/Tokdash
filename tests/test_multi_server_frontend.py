from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash


INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"


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
            {"server": {"id": "a", "label": "A"}, "payload": {"sessions": [{"session_id": "1", "last_seen_at": "2026-08-10T10:00:00Z", "tokens": 2, "cost": 1}]}},
            {"server": {"id": "b", "label": "B"}, "payload": {"sessions": [{"session_id": "2", "last_seen_at": "2026-08-10T11:00:00Z", "tokens": 3, "cost": 2}]}},
        ],
    )
    assert [row["session_id"] for row in sessions["sessions"]] == ["2", "1"]
    assert sessions["sessions"][0]["_server"]["label"] == "B"
    assert sessions["summary"] == {"session_count": 2, "tokens": 5, "cost": 3}


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
    assert "selectedServers().length > 1 && lastQuotaServerRows.length" in source


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
    assert "var version: Int = 2" in macos
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
