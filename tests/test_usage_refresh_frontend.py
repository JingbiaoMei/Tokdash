"""Refresh reconciliation, scan receipts and the refresh report panel.

The reconciliation and report builders run under node over plain payloads; the
panel is run over a stub DOM with the real dictionary, in every language. When
a refresh reports at all is run with updateDashboard's queue in
tests/test_dashboard_queue_frontend.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from support.frontend import (
    INDEX_HTML,
    extract_i18n_literal,
    extract_js_function,
    index_source,
    needs_node,
    run_node,
)

LANGUAGES = ("en", "zh", "ja", "ko", "es", "pt")
STATUSES = ("complete", "preserved", "partial", "failed", "cached", "cachedPreserved")


pytestmark = needs_node


def _run(tmp_path: Path, script: str) -> dict:
    source = index_source()
    functions = "\n".join(
        extract_js_function(source, signature)
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
    return json.loads(run_node(harness))


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
    """The markup half: the panel announces itself politely, and both ways out
    of it (the close button and Escape) dismiss it for the range. When it opens,
    and the forced request it reports on, are run in
    tests/test_dashboard_queue_frontend.py::test_every_same_range_refresh_reports_but_initial_and_range_loads_do_not.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index('<section id="refreshReport"')
    panel = html[start : html.index(">", start)]
    assert 'role="status"' in panel and 'aria-live="polite"' in panel, panel
    assert html.count("hideUsageRefreshReport({ focusButton: true, dismissed: true });") == 2
    # The snapshots live in memory for the session; nothing is persisted.
    reconcile = extract_js_function(
        html,
        "function reconcileUsageRows(rows, windowKey, servers = selectedServers(), cache = lastUsageRowsByServer) {",
    )
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


PANEL_PRELUDE = """
function makeNode(id) {
  return {
    id, hidden: true, dataset: {}, attrs: {}, textContent: '', focused: 0,
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
    focus() { this.focused += 1; },
    querySelector(selector) { return selector === '.refresh-report-deltas' ? deltas : null; },
  };
}
const nodes = {};
const deltas = makeNode('deltas');
const document = { getElementById: (id) => nodes[id] || (nodes[id] = makeNode(id)) };
const node = (id) => document.getElementById(id);
let currentLang = 'en';
let lastRefreshReport = null;
let refreshReportDismissedWindowKey = null;
function formatNumber(value) { return String(value); }
function formatTokenCount(value) { return `${value}t`; }
function formatCurrency(value) { return `$${value}`; }
function formatToolName(tool) { return tool; }
const report = (status, extra = {}) => ({
  status, completedAt: 'not a date', rangeLabel: 'Last 7 days', windowKey: 'X_X',
  tokens: 25, cost: 0, messages: null, baseline: true, toolsChanged: 0,
  retained: [], unavailable: [], error: '', ageSeconds: null, ...extra,
});
"""


def _panel_program(scenario: str) -> str:
    """showUsageRefreshReport and its helpers over a stub panel, with the real
    dictionary as REAL_I18N and `I18N` free for a scenario to narrow."""
    source = index_source()
    return "\n".join([
        PANEL_PRELUDE,
        extract_i18n_literal(source).replace("const I18N = {", "const REAL_I18N = {", 1),
        "let I18N = REAL_I18N;",
        next(line.strip() for line in source.splitlines() if line.strip().startswith("const LANG_LOCALES = ")),
        *(
            extract_js_function(source, signature)
            for signature in (
                "function t(key, vars) {",
                "function langLocale(lang = currentLang) {",
                "function formatDuration(ms, exact = false) {",
                "function signedRefreshValue(value, formatter) {",
                "function refreshIssueLabel(item) {",
                "function refreshReportRangeLabel() {",
                "function showUsageRefreshReport(report) {",
                "function hideUsageRefreshReport({ focusButton = false, dismissed = false } = {}) {",
            )
        ),
        scenario,
    ])


def _run_panel(tmp_path: Path, scenario: str) -> dict:
    harness = tmp_path / "refresh-panel.js"
    harness.write_text(_panel_program(scenario), encoding="utf-8")
    return json.loads(run_node(harness))


def test_the_cached_status_is_wired_into_the_panel_and_every_locale(tmp_path: Path) -> None:
    """A status with no title of its own falls back to 'Refresh complete' -- the bug.

    The panel picks its title key from the status at run time, so the parity test
    cannot see those six keys. Each language is given only its own dictionary
    here, so a missing title prints the bare key instead of quietly borrowing the
    English one. The age note follows the age, not one status name, so the
    composed status keeps it; and formatDuration renders a dash below half a
    second, so a sub-second age gets no note rather than "cached data — old".
    """
    out = _run_panel(tmp_path, """
const STATUSES = __STATUSES__;
const out = { titles: {}, statusAttr: [] };
for (const lang of Object.keys(REAL_I18N)) {
  currentLang = lang;
  I18N = { [lang]: REAL_I18N[lang], en: lang === 'en' ? REAL_I18N.en : {} };
  out.titles[lang] = STATUSES.map((status) => {
    showUsageRefreshReport(report(status));
    out.statusAttr.push(node('refreshReport').dataset.status === status);
    return node('refreshReportTitle').textContent;
  });
}
I18N = REAL_I18N;
currentLang = 'en';
const meta = (extra) => { showUsageRefreshReport(report('cached', extra)); return node('refreshReportMeta').textContent; };
out.unknownStatus = (showUsageRefreshReport(report('mystery')), node('refreshReportTitle').textContent);
out.metaOld = meta({ ageSeconds: 4300 });
out.metaComposed = (showUsageRefreshReport(report('cachedPreserved', { ageSeconds: 4300 })), node('refreshReportMeta').textContent);
out.metaSubSecond = meta({ ageSeconds: 0.4 });
out.metaNoAge = meta({ ageSeconds: null });
out.english = Object.fromEntries(['refreshReportComplete', 'refreshReportCached', 'refreshReportCachedPreserved',
  'refreshReportCachedAge', 'refreshReportRange'].map((key) => [key, REAL_I18N.en[key]]));
process.stdout.write(JSON.stringify(out));
""".replace("__STATUSES__", json.dumps(STATUSES)))

    assert sorted(out["titles"]) == sorted(LANGUAGES)
    for lang, titles in out["titles"].items():
        missing = [status for status, title in zip(STATUSES, titles) if title.startswith("refreshReport")]
        assert not missing, f"{lang} has no title for {missing}"
        assert len(set(titles)) == len(STATUSES), f"{lang} gives two statuses one title: {titles}"
    assert all(out["statusAttr"]), "the panel's data-status (and so its styling) did not follow the report"
    english = out["english"]
    cached, cached_preserved = out["titles"]["en"][4:]
    assert cached == english["refreshReportCached"] == "Nothing refreshed · served from cache"
    assert cached_preserved == english["refreshReportCachedPreserved"]
    assert "Refresh complete" not in cached_preserved, "the composed title claims a completed refresh"
    assert out["unknownStatus"] == english["refreshReportComplete"]

    base = english["refreshReportRange"].replace("{range}", "Last 7 days").replace("{time}", "")
    note = english["refreshReportCachedAge"].replace("{age}", "1h 11m")
    assert out["metaOld"] == f"{base} · {note}"
    assert out["metaComposed"] == f"{base} · {note}", "the composed status lost the age note"
    assert out["metaSubSecond"] == base, "a sub-second age printed a note"
    assert out["metaNoAge"] == base
    # The panel's styling hangs off data-status, so each status the panel can
    # name needs a rule; a browser is the only other way to see a missing one.
    html = INDEX_HTML.read_text(encoding="utf-8")
    for status in ("cached", "cachedPreserved"):
        assert f'.refresh-report[data-status="{status}"]' in html, f"{status} has no styling"
    # Picked at run time, so the parity test cannot see them.
    assert html.count("refreshReportCached:") == len(LANGUAGES)
    assert html.count("refreshReportCachedPreserved:") == len(LANGUAGES)


def test_refresh_report_labels_come_from_the_javascript_pass(tmp_path: Path) -> None:
    """The deltas group and the close button are named by the panel itself, in
    the page's language, each time it opens; the markup's English label is only
    the first paint's. Closing it hands the expanded state back to the Refresh
    button, focuses the button when asked, and remembers a dismissal for the range.
    """
    out = _run_panel(tmp_path, """
const out = { labels: {} };
for (const lang of Object.keys(REAL_I18N)) {
  currentLang = lang;
  showUsageRefreshReport(report('complete'));
  out.labels[lang] = [deltas.getAttribute('aria-label'), node('refreshReportClose').getAttribute('aria-label')];
}
out.expected = Object.fromEntries(Object.keys(REAL_I18N).map((lang) =>
  [lang, [REAL_I18N[lang].refreshReportDiffLabel, REAL_I18N[lang].refreshReportClose]]));
out.open = { hidden: node('refreshReport').hidden, expanded: node('refreshBtn').getAttribute('aria-expanded') };
hideUsageRefreshReport();
out.closed = { hidden: node('refreshReport').hidden, expanded: node('refreshBtn').getAttribute('aria-expanded'),
  focused: node('refreshBtn').focused, dismissed: refreshReportDismissedWindowKey };
showUsageRefreshReport(report('complete', { windowKey: 'Y_Y' }));
hideUsageRefreshReport({ focusButton: true, dismissed: true });
out.dismissed = { focused: node('refreshBtn').focused, dismissed: refreshReportDismissedWindowKey };
process.stdout.write(JSON.stringify(out));
""")

    assert sorted(out["labels"]) == sorted(LANGUAGES)
    assert out["labels"] == out["expected"]
    assert all(label for pair in out["labels"].values() for label in pair), out["labels"]
    assert len({pair[0] for pair in out["labels"].values()}) == len(LANGUAGES), "a language reused another's label"
    assert out["open"] == {"hidden": False, "expanded": "true"}
    assert out["closed"] == {"hidden": True, "expanded": "false", "focused": 0, "dismissed": None}
    assert out["dismissed"] == {"focused": 1, "dismissed": "Y_Y"}


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
