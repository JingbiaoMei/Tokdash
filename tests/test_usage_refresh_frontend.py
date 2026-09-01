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
            "function combineUsagePayloads(list) {",
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
    assert "refreshReportComplete: 'Refresh complete'" in html
    assert "refreshReportComplete: '刷新完成'" in html
    assert "usageApiUrl += '&refresh=1';" in updater
    assert updater.count("fetchSelectedServers(usageApiUrl)") == 1
    assert "reconcileUsageRows(rows, windowKey, usageServers)" in updater
    assert "const shouldReportRefresh = (forceRefresh || previousUsageForReport !== null)" in updater
    assert "&& refreshReportDismissedWindowKey !== windowKey;" in updater
    assert updater.count("if (shouldReportRefresh) {") == 2
    assert html.count("hideUsageRefreshReport({ focusButton: true, dismissed: true });") == 2
    assert "localStorage" not in reconcile


def test_refresh_report_calls_a_cached_response_cached_not_complete(tmp_path: Path) -> None:
    """The reported bug: `refresh=1` answered from cache still read "Refresh complete".

    The backend flags it on every cached path (`served_from_cache` is true for both
    `hit` and `stale`), and the timestamp beside the panel already said "cached" —
    only this report ignored it. It also has to keep `age_seconds`, which shipped in
    the same object and was thrown away.
    """
    out = _run(
        tmp_path,
        """
const before = { total_tokens: 10, total_cost: 1, total_messages: 2, by_tool: {} };
const cachedAfter = {
  total_tokens: 10, total_cost: 1, total_messages: 2, by_tool: {},
  response_cache: { status: 'stale', served_from_cache: true, age_seconds: 4300 },
};
const freshAfter = {
  total_tokens: 12, total_cost: 1, total_messages: 3, by_tool: {},
  response_cache: { status: 'recomputed', served_from_cache: false, age_seconds: 0 },
};
const noMetaAfter = { total_tokens: 12, total_cost: 1, total_messages: 3, by_tool: {} };
// The Refresh button. `{}` is the timer tick and is pinned by its own test below.
const forced = (extra = {}) => ({ forced: true, ...extra });
const oneRetainedRow = [{ server: { id: 'local' }, reason: 'source-errors', sources: ['codex'] }];
const cached = buildUsageRefreshReport(before, cachedAfter, forced());
const recomputed = buildUsageRefreshReport(before, freshAfter, forced());
const noMeta = buildUsageRefreshReport(before, noMetaAfter, forced());
const failed = buildUsageRefreshReport(before, cachedAfter, forced({ failed: true, error: 'boom' }));
const retainedCached = buildUsageRefreshReport(before, cachedAfter, forced({ retained: oneRetainedRow }));
const retainedFresh = buildUsageRefreshReport(before, freshAfter, forced({ retained: oneRetainedRow }));
const unavailableCached = buildUsageRefreshReport(before, cachedAfter, forced({ unavailable: oneRetainedRow }));
process.stdout.write(JSON.stringify({
  cached: [cached.status, cached.ageSeconds],
  recomputed: [recomputed.status, recomputed.ageSeconds],
  noMeta: [noMeta.status, noMeta.ageSeconds],
  failed: [failed.status, failed.ageSeconds],
  retainedCached: [retainedCached.status, retainedCached.ageSeconds],
  retainedFresh: retainedFresh.status,
  unavailableCached: [unavailableCached.status, unavailableCached.ageSeconds],
}));
""",
    )

    assert out == {
        # A forced refresh handed a 4300-second-old body must say both things.
        "cached": ["cached", 4300],
        "recomputed": ["complete", None],
        # An older server that ships no response_cache stays on the old verdict.
        "noMeta": ["complete", None],
        # `preserved` is NOT one of these: it reads "Refresh complete · previous
        # data kept", so it claims completion. Cached + retained composes instead,
        # because both facts are true and the user needs both.
        "retainedCached": ["cachedPreserved", 4300],
        "retainedFresh": "preserved",
        # These two say plainly that nothing completed, so they still outrank cached —
        # and the age must follow the verdict out, or the panel appends "cached data
        # 1h 11m old" to a refresh that errored and was never served from cache.
        "failed": ["failed", None],
        "unavailableCached": ["partial", None],
    }


def test_the_cached_status_is_wired_into_the_panel_and_every_locale() -> None:
    """A status with no titleKeys entry falls back to 'Refresh complete' — the bug."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    for status in ("cached", "cachedPreserved"):
        assert f"{status}: 'refreshReport" in html, f"{status} has no title key"
        assert f'.refresh-report[data-status="{status}"]' in html, f"{status} has no styling"
    assert html.count("refreshReportCached:") == 6
    assert html.count("refreshReportCachedAge:") == 6
    assert html.count("refreshReportCachedPreserved:") == 6
    assert "refreshReportCached: 'Nothing refreshed · served from cache'" in html
    # The composed title must not inherit "Refresh complete" from refreshReportPreserved.
    assert "refreshReportCachedPreserved: 'Nothing refreshed · served from cache · previous data kept'" in html
    # The age note must follow the age, not one status name, or the composed status
    # silently loses it. showUsageRefreshReport is DOM-bound, so this is pinned here.
    assert "const ageNote = Number.isFinite(report.ageSeconds) && report.ageSeconds >= 1" in html, (
        "formatDuration renders '—' below half a second, so a sub-second age would "
        "read 'cached data — old'"
    )
    # The verdict is only honest if the builder is told whether work was requested.
    updater = _extract_js_function(
        html,
        "async function updateDashboard(customDays = null, dateFrom = null, dateTo = null, options = {}) {",
    )
    assert updater.count("forced: forceRefresh,") == 2, "both report call sites must pass the flag"
    assert "Refresh complete" not in html.split("refreshReportCachedPreserved:")[1].split("\n")[0]


def test_combined_payloads_merge_response_cache_instead_of_inheriting_row_zero(tmp_path: Path) -> None:
    """Multi-server: the cached verdict must cover every row, not just rows[0].

    combineUsagePayloads deep-copies rows[0] and then re-derives every field that
    matters — totals summed, timestamp taken from the oldest row. response_cache was
    not among them, so a recomputed rows[0] beside a cache-served rows[1] reported
    "complete" while the panel displayed rows[1]'s older timestamp. It merges on the
    same weakest-link rule the timestamp uses.
    """
    out = _run(
        tmp_path,
        """
const fresh = (n, ts) => ({
  total_tokens: n, total_cost: 0, total_messages: 0, by_tool: {}, timestamp: ts,
  response_cache: { status: 'recomputed', served_from_cache: false, age_seconds: 0 },
});
const stale = (n, ts, age) => ({
  total_tokens: n, total_cost: 0, total_messages: 0, by_tool: {}, timestamp: ts,
  response_cache: { status: 'stale', served_from_cache: true, age_seconds: age },
});
const bare = (n, ts) => ({ total_tokens: n, total_cost: 0, total_messages: 0, by_tool: {}, timestamp: ts });
const meta = (payload) => {
  const cache = payload.response_cache;
  return cache ? [!!cache.served_from_cache, cache.age_seconds] : null;
};
const before = { total_tokens: 0, total_cost: 0, total_messages: 0, by_tool: {} };

// rows[0] recomputed, rows[1] served cache: the old copy inherited rows[0] and lied.
const mixed = combineUsagePayloads([fresh(5, '2026-09-01T10:00:00Z'), stale(7, '2026-09-01T09:00:00Z', 4300)]);
// Both cached: the age is the oldest body in the mix, not the first one.
const bothCached = combineUsagePayloads([stale(5, '2026-09-01T10:00:00Z', 60), stale(7, '2026-09-01T09:00:00Z', 4300)]);
const bothFresh = combineUsagePayloads([fresh(5, '2026-09-01T10:00:00Z'), fresh(7, '2026-09-01T09:00:00Z')]);
// An older server sends no response_cache at all; unknown must not read as cached.
const noMeta = combineUsagePayloads([bare(5, '2026-09-01T10:00:00Z'), bare(7, '2026-09-01T09:00:00Z')]);
const single = combineUsagePayloads([stale(5, '2026-09-01T10:00:00Z', 4300)]);

process.stdout.write(JSON.stringify({
  mixed: meta(mixed),
  mixedTotal: mixed.total_tokens,
  mixedTimestamp: mixed.timestamp,
  mixedStatus: buildUsageRefreshReport(before, mixed, { forced: true }).status,
  bothCached: meta(bothCached),
  bothFresh: meta(bothFresh),
  bothFreshStatus: buildUsageRefreshReport(before, bothFresh, { forced: true }).status,
  noMeta: meta(noMeta),
  single: meta(single),
}));
""",
    )

    assert out == {
        # Any server served from cache makes the combined body a cached one.
        "mixed": [True, 4300],
        # The rest of the merge is untouched: totals still sum, timestamp still oldest.
        "mixedTotal": 12,
        "mixedTimestamp": "2026-09-01T09:00:00Z",
        "mixedStatus": "cached",
        "bothCached": [True, 4300],
        "bothFresh": [False, 0],
        "bothFreshStatus": "complete",
        # No row claims a cache, so the combined body claims none either.
        "noMeta": None,
        # The single-row fast path returns the row verbatim, cache metadata included.
        "single": [True, 4300],
    }


def test_an_automatic_refresh_tick_served_from_cache_stays_complete(tmp_path: Path) -> None:
    """The cached verdict is about refused work, not about the cache being used.

    shouldReportRefresh only needs a previous payload, so the panel reopens on every
    five-minute tick, and after the first successful load essentially every tick is
    answered from cache — both `hit` and `stale` set served_from_cache. Deriving the
    verdict from that flag alone therefore repainted the panel amber, reading "nothing
    refreshed", while the TTL was simply doing its job. Only a forced refresh can be
    "you asked for work and got none".
    """
    out = _run(
        tmp_path,
        """
const before = { total_tokens: 10, total_cost: 1, total_messages: 2, by_tool: {} };
const body = (age) => ({
  total_tokens: 10, total_cost: 1, total_messages: 2, by_tool: {},
  response_cache: { status: 'hit', served_from_cache: true, age_seconds: age },
});
const retainedRow = [{ server: { id: 'local' }, reason: 'source-errors', sources: ['codex'] }];
const report = (details) => buildUsageRefreshReport(before, body(120), details);
process.stdout.write(JSON.stringify({
  // A timer tick: same cached body, no forced flag.
  tick: [report({}).status, report({}).ageSeconds],
  tickRetained: report({ retained: retainedRow }).status,
  // Identical body, but the user pressed Refresh.
  clicked: [report({ forced: true }).status, report({ forced: true }).ageSeconds],
  clickedRetained: report({ forced: true, retained: retainedRow }).status,
  // An explicit false must behave like an absent flag, not like a forced refresh.
  explicitFalse: report({ forced: false }).status,
}));
""",
    )

    assert out == {
        # The cache doing its job is not a warning, and carries no age note.
        "tick": ["complete", None],
        "tickRetained": "preserved",
        # The same body the user actually asked to have refreshed is.
        "clicked": ["cached", 120],
        "clickedRetained": "cachedPreserved",
        "explicitFalse": "complete",
    }
