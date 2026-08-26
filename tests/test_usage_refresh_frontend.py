"""Refresh reconciliation and scan-receipt behaviour in the dashboard frontend."""
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


def _run(tmp_path: Path, script: str) -> dict:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, signature)
        for signature in (
            "function usageSourceErrors(payload) {",
            "function reconcileUsageRows(rows, windowKey, servers = selectedServers(), cache = lastUsageRowsByServer) {",
            "function usageToolFingerprint(entry) {",
            "function buildUsageRefreshReport(before, after, details = {}) {",
        )
    )
    harness = tmp_path / "refresh.js"
    harness.write_text(f"{functions}\n{script}", encoding="utf-8")
    result = subprocess.run(["node", str(harness)], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_refresh_keeps_same_range_snapshots_for_incomplete_servers(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
const servers = [{ id: 'local', label: 'Local' }, { id: 'remote', label: 'Studio' }];
const cache = new Map([
  ['local', { windowKey: 'today', at: 1, payload: { total_tokens: 100, by_tool: { codex: { tokens: 100 } } } }],
  ['remote', { windowKey: 'today', at: 1, payload: { total_tokens: 50, by_tool: { claude: { tokens: 50 } } } }],
]);
const rows = [{
  server: servers[0],
  payload: { total_tokens: 4, source_errors: ['codex', 'codex'], by_tool: {} },
}];
const result = reconcileUsageRows(rows, 'today', servers, cache);
process.stdout.write(JSON.stringify({
  totals: result.rows.map((row) => row.payload.total_tokens),
  retained: result.retained.map((item) => [item.server.id, item.reason, item.sources]),
  unavailable: result.unavailable.length,
  retainedFlags: result.rows.map((row) => row._retained),
}));
""",
    )

    assert out == {
        "totals": [100, 50],
        "retained": [
            ["local", "source-errors", ["codex"]],
            ["remote", "unreachable", []],
        ],
        "unavailable": 0,
        "retainedFlags": [True, True],
    }


def test_refresh_never_reuses_a_snapshot_from_another_range(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
const server = { id: 'local', label: 'Local' };
const previous = { total_tokens: 100 };
const partial = { total_tokens: 4, source_errors: ['codex'] };
const cache = new Map([['local', { windowKey: 'yesterday', at: 1, payload: previous }]]);
const result = reconcileUsageRows([{ server, payload: partial }], 'today', [server], cache);
process.stdout.write(JSON.stringify({
  total: result.rows[0].payload.total_tokens,
  partial: result.rows[0]._partial,
  retained: result.retained.length,
  unavailable: result.unavailable.map((item) => item.server.id),
  cachedWindow: cache.get('local').windowKey,
}));
""",
    )

    assert out == {
        "total": 4,
        "partial": True,
        "retained": 0,
        "unavailable": ["local"],
        "cachedWindow": "yesterday",
    }


def test_clean_refresh_replaces_the_snapshot_and_reports_exact_deltas(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
const server = { id: 'local', label: 'Local' };
const cache = new Map();
const before = {
  total_tokens: 100, total_cost: 1.25, total_messages: 4,
  by_tool: { codex: { tokens: 100, cost: 1.25, messages: 4 } },
};
const after = {
  total_tokens: 145, total_cost: 1.75, total_messages: 6,
  by_tool: {
    codex: { tokens: 140, cost: 1.7, messages: 5 },
    claude: { tokens: 5, cost: 0.05, messages: 1 },
  },
};
const reconciled = reconcileUsageRows([{ server, payload: after }], 'today', [server], cache);
const report = buildUsageRefreshReport(before, after, {
  windowKey: 'today', retained: reconciled.retained, unavailable: reconciled.unavailable,
});
process.stdout.write(JSON.stringify({
  cached: cache.get('local').payload.total_tokens,
  cachedWindow: cache.get('local').windowKey,
  status: report.status,
  tokens: report.tokens,
  cost: report.cost,
  messages: report.messages,
  toolsChanged: report.toolsChanged,
}));
""",
    )

    assert out["cached"] == 145
    assert out["cachedWindow"] == "today"
    assert out["status"] == "complete"
    assert out["tokens"] == 45
    assert out["cost"] == pytest.approx(0.5)
    assert out["messages"] == 2
    assert out["toolsChanged"] == 2


def test_refresh_report_is_accessible_and_the_scan_stays_incremental() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    updater = _extract_js_function(
        html,
        "async function updateDashboard(customDays = null, dateFrom = null, dateTo = null, options = {}) {",
    )
    reconcile = _extract_js_function(
        html,
        "function reconcileUsageRows(rows, windowKey, servers = selectedServers(), cache = lastUsageRowsByServer) {",
    )

    assert 'id="refreshReport"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    assert 'id="refreshReportClose"' in html
    assert "refreshReportComplete: 'Scan complete'" in html
    assert "refreshReportComplete: '扫描完成'" in html
    assert "usageApiUrl += '&refresh=1';" in updater
    assert updater.count("fetchSelectedServers(usageApiUrl)") == 1
    assert "reconcileUsageRows(rows, windowKey, usageServers)" in updater
    assert "localStorage" not in reconcile
