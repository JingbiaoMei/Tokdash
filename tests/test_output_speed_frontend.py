"""Dashboard-side rules for the output-speed surfaces.

Two surfaces, and only two: the standalone output-speed page (one comparison
table, plus a within-model time-of-day view) and the session modal's response
table. The properties worth automating are the ones a screenshot cannot keep
from drifting later:

* the comparison table is one table, not a stack of sections;
* nothing asks the server for speed until the subtab is opened;
* absence sorts last, formats as a dash, and never becomes a zero;
* every element the speed code reaches for actually exists in the markup;
* every speed string is present in all six shipped languages.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"
SRC = INDEX_HTML.read_text(encoding="utf-8")

SPEED_SURFACES = ("usageReportSpeedPanel", "sessionSpeedPanel")
LANGUAGES = ("en", "zh", "ja", "ko", "es", "pt")


def _extract(signature: str) -> str:
    """One function (or object literal), whole.

    The body's opening brace is the one after the parameter list, not the first
    brace in the signature: ``function f(options = {})`` would otherwise extract
    the default value and call it the body.
    """
    start = SRC.find(signature)
    assert start >= 0, f"{signature} not found in index.html"
    # The body is the brace after the parameter list. Not "the next brace":
    # ``function f(options = {})`` carries a brace object inside its own
    # signature, and extracting that as the body returns the signature alone.
    open_paren = SRC.find("(", start, start + len(signature) + 40)
    close_paren = SRC.find(")", start) if open_paren >= 0 else -1
    if open_paren >= 0 and close_paren > open_paren:
        head = close_paren
    else:
        head = start
    open_at = SRC.index("{", head)
    depth = 0
    for index in range(open_at, len(SRC)):
        char = SRC[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return SRC[start:index + 1]
    raise AssertionError(f"unterminated: {signature}")


def _run_node(tmp_path: Path, name: str, script: str):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    harness = tmp_path / f"{name}.js"
    harness.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(harness)], check=True,
                            capture_output=True, encoding="utf-8")
    return json.loads(result.stdout)


STUB_LINES = [
    "function langLocale() { return 'en-GB'; }",
    "function t(key) { return key; }",
    "function escapeHtml(value) {",
        "  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')",
        "    .replace(/>/g, '&gt;').replace(/\"/g, '&quot;');",
    "}",
    "function formatNumber(value) { return String(value); }",
    "function formatPercent(value) { return `${Math.round(value * 100)}%`; }",
    "function formatTokenCount(value) { return String(value); }",
    "function formatTimeOnly(iso) { return String(iso).slice(11, 16); }",
    "function formatToolName(value) { return value; }",
]
STUBS = chr(10).join(STUB_LINES) + chr(10)


def _rows_js() -> str:
    """The pure sorting/formatting half of the speed view, for node."""
    return chr(10).join([
        STUBS,
        _extract("const usageReportSpeedState = {"),
        # The sort reads through the tool filter, because the request now returns
        # every tool and the table shows one of them.
        _extract("function usageReportSpeedVisibleRows()"),
        _extract("function usageReportSpeedTools()"),
        _extract("function usageReportSpeedSortedRows()"),
        # The measured-day union the note under the table reads.
        _extract("function usageReportSpeedMeasuredDays(rows)"),
        _extract("function usageReportSpeedFormatRate(rate)"),
        _extract("function usageReportSpeedAbsence(row)"),
        _extract("function usageReportSpeedText(text, className)"),
        _extract("function usageReportSpeedSampleNode("),
    ])


# --------------------------------------------------------------------------
# Shape of the feature in the page
# --------------------------------------------------------------------------


def test_output_speed_renders_in_exactly_two_places():
    for element_id in SPEED_SURFACES:
        assert f'id="{element_id}"' in SRC, element_id
    assert 'id="usageReportSubtabs"' not in SRC
    assert 'data-tab-target="speed"' in SRC
    assert 'data-tab="speed"' in SRC
    assert SRC.index('id="report-content"') < SRC.index('id="speed-content"') < SRC.index('id="usageReportSpeedPanel"')
    speed_panel = SRC[SRC.index('id="speed-content"'):SRC.index('id="quota-content"')]
    assert 'data-period=' not in speed_panel, "only the global range controls select speed dates"
    # No leftover from the earlier four-section demo: the across view is a single
    # table, and its method detail is a row inside it rather than a section.
    across = SRC[SRC.index('<div id="usageReportSpeedAcross">'):SRC.index('<div id="usageReportSpeedTemporal"')]
    assert across.count("<table") == 1, "the across view is one comparison table"
    # The method row is built as an element, so it is a class assignment here and
    # not a markup attribute: the Report tab never writes innerHTML.
    assert "tr.className = 'speed-detail-row'" in SRC
    for dead in ("usageReportSpeedMethod", "usageReportSpeedCoverage",
                 "usageReportSpeedExclusions", "usageReportSpeedInstrument"):
        assert f'id="{dead}"' not in SRC, f"{dead} is a section the design no longer has"


def test_the_time_of_day_view_is_within_model_not_another_table_of_models():
    temporal = SRC[SRC.index('<div id="usageReportSpeedTemporal"'):SRC.index('</section>', SRC.index('<div id="usageReportSpeedTemporal"'))]
    # One picker for one model, one hour strip, one six-hour table.
    assert 'id="usageReportSpeedPick"' in temporal
    assert 'id="usageReportSpeedHours"' in temporal
    assert temporal.count("<table") == 1
    assert "data-speed-sort" not in temporal, "no second sortable model ranking"


def test_the_page_asks_for_speed_only_when_the_subtab_is_open():
    """Laziness is the point: the summary report must not pay for this view.

    The rule is one gate the whole view dispatches through, so it holds for the
    report's own load, a subtab click, a tab return and a window that was hidden
    when the request was queued. Asserted by running the loader rather than by
    reading for a guard at each call site: the call sites are the part that
    multiplies, and a load that forgets the gate is invisible to a string match.
    """
    callers = [
        name for name in ("usageReportLoadSpeed", "usageReportEnsureTemporal")
        if "/api/output-speed" in _extract(
            "async function usageReportLoadSpeed("
            if name == "usageReportLoadSpeed"
            else "function usageReportEnsureTemporal("
        )
    ]
    assert callers == ["usageReportLoadSpeed", "usageReportEnsureTemporal"]
    loader = _extract("async function loadUsageReport(options = {})")
    assert len(loader) > 500, "the extractor returned a signature, not a body"
    assert "/api/output-speed" not in loader
    assert "usageReportLoadSpeed" not in loader
    activate = _extract("function activateDashboardTab(")
    assert "if (tab === 'speed')" in activate
    assert "usageReportSpeedResume()" in activate


def test_a_hidden_or_otherwise_unviewed_panel_asks_for_nothing(tmp_path):
    """Nothing about this view is fetched when nothing can show it.

    The three ways that happens -- the report page is not the page on screen, the
    summary subtab is open, the browser tab is in the background -- each cost the
    server an aggregation of every stored row in the window, and each used to fire
    anyway: the report's load dispatched speed off the front row, and a period
    clicked while the panel was hidden dispatched it from there."""
    script = chr(10).join([
        STUBS,
        _extract("const usageReportState = {"),
        _extract("const usageReportSpeedState = {"),
        "const currentStartDate = new Date(2026, 8, 1), currentEndDate = new Date(2026, 8, 5);",
        _extract("function formatLocalDateYmd(date)"),
        "function outputSpeedRenderControls() {}",
        _extract("function usageReportSpeedInvalidate("),
        "function outputSpeedServer() { return { id: 'local' }; }",
        _extract("function usageReportSpeedWindow()"),
        _extract("function usageReportSpeedKey()"),
        _extract("function usageReportSpeedQuery(extra)"),
        _extract("function usageReportSpeedIsVisible()"),
        _extract("function usageReportSpeedRelease(token)"),
        _extract("async function usageReportLoadSpeed("),
        "const asked = [];",
        "function usageReportRenderSpeedState() {}",
        "function usageReportRenderSpeed() {}",
        "function usageReportSpeedPickDefaultTool() {}",
        "function usageReportEnsureTemporal() {}",
        "function fetchJsonWithRetry(server, url, options, retry) {",
        "  asked.push(url);",
        "  if (retry && retry.shouldContinue && !retry.shouldContinue()) {",
        "    return Promise.reject(Object.assign(new Error('503'), { status: 503 }));",
        "  }",
        "  return Promise.resolve({ rows: [] });",
        "}",
        "const document = { hidden: false };",
        """
(async () => {
  const out = {};
  usageReportState.active = false;
  usageReportSpeedState.active = false;
  await usageReportLoadSpeed();
  out.reportNotOnScreen = asked.length;

  usageReportState.active = true;
  usageReportSpeedState.active = false;
  await usageReportLoadSpeed();
  out.summarySubtab = asked.length;

  usageReportSpeedState.active = true;
  document.hidden = true;
  await usageReportLoadSpeed();
  out.documentHidden = asked.length;

  document.hidden = false;
  await usageReportLoadSpeed();
  out.visible = { asked: asked.length, loadedKey: usageReportSpeedState.loadedKey,
                  loading: usageReportSpeedState.loading, pendingKey: usageReportSpeedState.pendingKey };

  // The reader leaves while a request is in flight: the answer is discarded, and
  // the guard it was holding has to go with it. A guard left set is a panel that
  // waits forever on a load that already finished for that exact key.
  usageReportSpeedState.loadedKey = '';
  usageReportSpeedState.rows = [];
  const inflight = usageReportLoadSpeed();
  outputSpeedRetirePending();
  await inflight;
  out.afterLeaving = { loading: usageReportSpeedState.loading,
                       pendingKey: usageReportSpeedState.pendingKey,
                       pendingToken: usageReportSpeedState.pendingToken };

  // ... and coming back asks again, because nothing recorded this window as loaded.
  usageReportSpeedState.active = true;
  out.beforeReturning = asked.length;
  await usageReportSpeedResume();
  out.afterReturning = asked.length;
  process.stdout.write(JSON.stringify(out));
})();
""",
    ])
    body = script.replace(
        "const document = { hidden: false };",
        "const document = { hidden: false };",
    )
    # `usageReportRetirePending` and `usageReportSpeedResume` are part of the same
    # gate, so they run here rather than being asserted as text.
    body = body.replace(
        "(async () => {",
        _extract("function outputSpeedRetirePending()") + chr(10)
        + _extract("function usageReportSpeedResume()") + chr(10)
        + "(async () => {",
    )
    out = _run_node(tmp_path, "speed_gate", body)

    assert out["reportNotOnScreen"] == 0, "a report that is not on screen asked for speed"
    assert out["summarySubtab"] == 0, "the summary subtab asked for speed"
    assert out["documentHidden"] == 0, "a hidden browser tab asked for speed"
    assert out["visible"]["asked"] == 1, out
    assert out["visible"]["loadedKey"], "the loaded window was not recorded"
    assert out["visible"]["loading"] is False and out["visible"]["pendingKey"] == ""

    assert out["afterLeaving"] == {"loading": False, "pendingKey": "", "pendingToken": 0}, (
        "a discarded speed answer left its in-flight guard behind: the panel would "
        "never ask for that window again"
    )
    assert out["afterReturning"] == out["beforeReturning"] + 1, (
        "coming back to the panel did not re-read the window it left unloaded"
    )


def test_a_failed_temporal_load_is_retried_only_when_the_view_returns(tmp_path):
    script = chr(10).join([
        _extract("const usageReportSpeedState = {"),
        _extract("function usageReportEnsureTemporal("),
        _extract("function usageReportSpeedResume()"),
        "function usageReportSpeedWatchJob() {}",
        "const document = { hidden: false };",
        "function usageReportSpeedIsVisible() { return !document.hidden; }",
        "function usageReportSpeedKey() { return 'window'; }",
        "function outputSpeedRenderControls() {}",
        "function usageReportSpeedSelection() { return { source: 'kimi', model: 'kimi-k3',",
        "  measurement_kind: 'server_decode', token_basis: 'output_reasoning_unspecified' }; }",
        "function outputSpeedServer() { return { id: 'local' }; }",
        "function usageReportSpeedQuery() { return 'view=time-of-day'; }",
        "function usageReportRenderSpeedTemporal() {}",
        "function usageReportRenderSpeed() {}",
        "function usageReportLoadSpeed() { throw new Error('comparison was already loaded'); }",
        "const requests = [];",
        "function fetchJsonWithRetry() { return new Promise((resolve, reject) => requests.push({ resolve, reject })); }",
        """
(async () => {
  const out = {};
  const settle = () => new Promise(resolve => setImmediate(resolve));
  usageReportSpeedState.loadedKey = 'window';
  usageReportSpeedState.mode = 'temporal';
  usageReportSpeedResume();
  document.hidden = true;
  requests[0].reject(new Error('503: temporarily unavailable'));
  await settle();
  out.afterFailure = { error: usageReportSpeedState.temporal.__error,
    loading: usageReportSpeedState.temporalLoading };
  usageReportSpeedResume();
  out.whileHidden = requests.length;

  document.hidden = false;
  usageReportSpeedResume();
  out.afterReturn = requests.length;
  if (requests[1]) {
    requests[1].resolve({ hours: [{ hour: 12, speed_calls: 4, output_tok_per_s: 40 }] });
    await settle();
  }
  out.recovered = !!usageReportSpeedState.temporal.hours;
  out.loading = usageReportSpeedState.temporalLoading;
  usageReportSpeedResume();
  out.cachedReturn = requests.length;
  process.stdout.write(JSON.stringify(out));
})();
""",
    ])
    out = _run_node(tmp_path, "temporal_retry_on_return", script)
    assert out["afterFailure"] == {"error": "503: temporarily unavailable", "loading": False}
    assert out["whileHidden"] == 1, "the hidden view retried its failed request"
    assert out["afterReturn"] == 2, "an error was cached as a successfully loaded chart"
    assert out["recovered"] is True and out["loading"] is False
    assert out["cachedReturn"] == 2, "a successful chart should be reused on return"


def test_a_stale_retry_chain_stops_instead_of_retrying(tmp_path):
    """The retries obey the gate too, and they are where hidden work hid.

    Three attempts and their backoff used to run to the end on an answer that
    could no longer be shown, which is the same cost the gate exists to avoid
    paid a second and third time."""
    script = chr(10).join([
        "function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, 0)); }",
        "const seen = [];",
        "function fetchJson(url) { seen.push(url); return Promise.reject(Object.assign(new Error('503'), { status: 503 })); }",
        "function fetchFromHost(server, url) { return fetchJson(url); }",
        _extract("async function fetchJsonWithRetry("),
        """
(async () => {
  const out = {};
  let alive = false;
  try {
    await fetchJsonWithRetry({ id: 'local' }, '/api/x', {}, { shouldContinue: () => alive });
  } catch (_error) { out.threw = true; }
  out.stoppedAttempts = seen.length;

  seen.length = 0;
  try {
    await fetchJsonWithRetry({ id: 'local' }, '/api/x', {});
  } catch (_error) { /* no predicate: the ordinary chain */ }
  out.ordinaryAttempts = seen.length;
  process.stdout.write(JSON.stringify(out));
})();
""",
    ])
    out = _run_node(tmp_path, "retry_gate", script)
    assert out["threw"] is True
    assert out["stoppedAttempts"] == 1, out
    assert out["ordinaryAttempts"] == 3, "a caller that says nothing must not change the chain"


def test_a_page_left_during_the_backoff_is_not_charged_for_another_request(tmp_path):
    """The predicate is read again once the backoff is over, not only before it.

    A check taken before a sleep of up to 1.6 s certifies a dispatch that happens
    a second later, and a second is enough: the reader who pressed a different tab
    is not on the page when the timer runs out, so that request answers to nobody
    and buys a retry of its own. The chain must stop at the wait it was told about
    while still spending the attempts it was told to spend when nothing changed.
    """
    script = chr(10).join([
        "let visible = true;",
        "let sleeps = 0;",
        "let leaveOnSleep = 0;",
        "const seen = [];",
        "function sleep(ms) {",
        "  return new Promise((resolve) => setTimeout(() => {",
        "    if (leaveOnSleep === (sleeps += 1)) visible = false;",
        "    resolve();",
        "  }, 0));",
        "}",
        "function fetchJson(url) { seen.push(url);",
        "  return Promise.reject(Object.assign(new Error('503'), { status: 503 })); }",
        "function fetchFromHost(server, url) { return fetchJson(url); }",
        _extract("async function fetchJsonWithRetry("),
        """
(async () => {
  const out = {};

  // The reader leaves while the first backoff is running.
  leaveOnSleep = 1;
  try {
    await fetchJsonWithRetry({ id: 'local' }, '/api/x', {}, { shouldContinue: () => visible });
  } catch (_error) { out.threw = true; }
  out.leftDuringFirstWait = seen.length;

  // Nothing changes: the whole chain is still the whole chain.
  seen.length = 0; sleeps = 0; leaveOnSleep = 0;
  try {
    await fetchJsonWithRetry({ id: 'local' }, '/api/x', {});
  } catch (_error) { /* the ordinary chain */ }
  out.ordinaryAttempts = seen.length;

  seen.length = 0; sleeps = 0; visible = true; leaveOnSleep = 3;
  try {
    await fetchJsonWithRetry({ id: 'local' }, '/api/x', {}, { shouldContinue: () => visible });
  } catch (_error) { /* still retrying while the page is up */ }
  out.leftDuringThirdWait = seen.length;

  // A later wait, with more attempts offered: the chain stops at the wait rather
  // than at the first one, so this is not the same rule as giving up on failure.
  seen.length = 0; sleeps = 0; visible = true; leaveOnSleep = 0;
  try {
    await fetchJsonWithRetry({ id: 'local' }, '/api/x', {},
      { attempts: 5, shouldContinue: () => visible });
  } catch (_error) { /* never hidden */ }
  out.neverHidden = seen.length;
  process.stdout.write(JSON.stringify(out));
})();
""",
    ])
    out = _run_node(tmp_path, "retry_backoff_gate", script)
    assert out["threw"] is True
    assert out["leftDuringFirstWait"] == 1, (
        f"a hidden page was charged {out['leftDuringFirstWait']} attempts: the retry "
        "was dispatched on a predicate read before the wait that hid the page"
    )
    assert out["ordinaryAttempts"] == 3, out
    assert out["leftDuringThirdWait"] == 3, (
        "the chain stopped short of the wait the reader actually left during"
    )
    assert out["neverHidden"] == 5, out


def test_range_changes_invalidate_speed_without_charging_the_report():
    invalidate = _extract("function usageReportSpeedInvalidate(")
    for field in ("loadedKey", "temporalKey", "rows", "payload"):
        assert field in invalidate
    loader = _extract("async function usageReportLoadSpeed(")
    assert "usageReportSpeedInvalidate();" in loader
    assert "usageReportSpeedInvalidate" not in _extract("async function loadUsageReport(options = {})")
    route = _extract("function updateDashboardByDateRange(")
    assert "usageReportSpeedState.active" in route
    assert route.index("usageReportLoadSpeed(") < route.index("updateDashboard(null")


def _standalone_loader_js():
    return '\n'.join([
        STUBS,
        _extract('const usageReportSpeedState = {'),
        'let currentStartDate = new Date(2026, 8, 1), currentEndDate = new Date(2026, 8, 30);',
        'const document = {hidden: false};',
        'function outputSpeedServer() { return {id: "local"}; }',
        'function outputSpeedRenderControls() {}',
        'function usageReportRenderSpeed() {}',
        'function usageReportEnsureTemporal() {}',
        'function usageReportSpeedPickDefaultTool() {}',
        'function usageReportSpeedWatchJob() {}',
        'function updateDashboard() { throw new Error("Range switch used Overview loader"); }',
        'const asks = [];',
        'function fetchJsonWithRetry(server, url) { return new Promise(resolve => asks.push({url, resolve})); }',
        'const flush = () => new Promise(resolve => setImmediate(resolve));',
        _extract('function formatLocalDateYmd(date)'),
        _extract('function usageReportSpeedWindow()'),
        _extract('function usageReportSpeedKey()'),
        _extract('function usageReportSpeedQuery(extra)'),
        _extract('function usageReportSpeedIsVisible()'),
        _extract('function usageReportSpeedRelease(token)'),
        _extract('function outputSpeedRetirePending()'),
        _extract('function usageReportSpeedRelease(token)'),
        _extract('function usageReportSpeedInvalidate('),
        _extract('async function usageReportLoadSpeed('),
        _extract('function updateDashboardByDateRange('),
        'usageReportSpeedState.active = true; usageReportSpeedState.tool = "kimi";',
    ])


def test_top_range_starts_a_new_load_before_the_old_one_finishes(tmp_path):
    out = _run_node(tmp_path, 'standalone_ranges', _standalone_loader_js() + '''
(async () => {
  updateDashboardByDateRange(currentStartDate, currentEndDate);
  currentStartDate = new Date(2026, 0, 1);
  updateDashboardByDateRange(currentStartDate, currentEndDate);
  const concurrent = asks.length;
  asks[1].resolve({rows: [{source:'kimi', model:'year', speed_calls:5}]});
  await flush();
  const beforeOld = usageReportSpeedState.rows[0].model;
  asks[0].resolve({rows: [{source:'kimi', model:'month', speed_calls:1}]});
  await flush();
  console.log(JSON.stringify({concurrent, beforeOld, afterOld:usageReportSpeedState.rows[0].model,
    loading:usageReportSpeedState.loading, urls:asks.map(r=>r.url)}));
})();
''')
    assert out['concurrent'] == 2, 'the new range waited behind the old request'
    assert out['beforeOld'] == out['afterOld'] == 'year', 'a stale range replaced the current rows'
    assert not out['loading']
    assert all('/api/output-speed?' in url for url in out['urls'])


def test_manual_speed_refresh_reads_rates_only_after_sync_and_stops_on_exit(tmp_path):
    out = _run_node(tmp_path, 'standalone_refresh', _standalone_loader_js() + """
(async () => {
  const load = usageReportLoadSpeed({refresh:true});
  const initial = asks.map(r=>r.url);
  asks[0].resolve({rows:[],cache:{state:'building',job_id:'one'}}); await load;
  const afterQueue = asks.map(r=>r.url);
  const next = usageReportLoadSpeed({refresh:true});
  outputSpeedRetirePending(); usageReportSpeedState.active = false;
  asks[1].resolve({rows:[{model:'late'}]}); await next;
  console.log(JSON.stringify({initial, afterQueue, finalRequests:asks.length,rows:usageReportSpeedState.rows}));
})();
""")
    assert len(out['initial'])==len(out['afterQueue'])==1
    assert '/api/output-speed?' in out['initial'][0] and 'refresh=1' in out['initial'][0]
    assert not any('/api/usage' in u for u in out['afterQueue'])
    assert out['finalRequests']==2 and out['rows']==[], 'hidden completion repainted or dispatched follow-up work'


def test_visible_job_events_are_retired_and_old_callbacks_cannot_refresh_new_navigation(tmp_path):
    script='\n'.join([
        _extract('const usageReportSpeedState = {'),
        _extract('function usageReportSpeedWatchJob('),
        _extract('function outputSpeedRetirePending()'),
        _extract('function usageReportSpeedRelease(token)'),
        "const document={hidden:false}; const events=[];let reads=0;const progress=[],readOptions=[];",
        "function t(k){return k;} function pickRoute(){return null;} function serverPath(s,p){return p;}",
        "function usageReportSpeedKey(){return 'window';}",
        "function usageReportSpeedIsVisible(){return usageReportSpeedState.active && !document.hidden;}",
        "function usageReportLoadSpeed(options){reads++;readOptions.push(options);}",
        "function usageReportRenderSpeedState(s){progress.push(s);}",
        "class EventSource{constructor(url){this.url=url;this.closed=false;events.push(this);}close(){this.closed=true;}}",
        """
usageReportSpeedState.active=true;
const server={id:'local'};
usageReportSpeedWatchJob({state:'building',job_id:'first'},'window',server);
events[0].onmessage({data:JSON.stringify({state:'building',completed_inputs:2,total_inputs:3})});
usageReportSpeedWatchJob({state:'stale',job_id:'second'},'window',server);
events[0].onmessage({data:JSON.stringify({state:'ready'})});
const staleReads=reads,secondRetained=usageReportSpeedState.jobEvents===events[1];
outputSpeedRetirePending();
events[1].onmessage({data:JSON.stringify({state:'ready'})});
const hiddenReads=reads;
usageReportSpeedState.active=true;document.hidden=true;
usageReportSpeedWatchJob({state:'building',job_id:'hidden'},'window',server);
const hiddenSubscriptions=events.length;
document.hidden=false;
usageReportSpeedWatchJob({state:'building',job_id:'third'},'window',server);
events[2].onmessage({data:JSON.stringify({state:'ready'})});
console.log(JSON.stringify({staleReads,hiddenReads,secondRetained,hiddenSubscriptions,reads,readOptions,closed:events.map(e=>e.closed),progress,retired:usageReportSpeedState.jobEvents===null}));
""",
    ])
    out=_run_node(tmp_path,'speed_job_navigation',script)
    assert out['staleReads']==out['hiddenReads']==0 and out['secondRetained']
    assert out['hiddenSubscriptions']==2 and out['reads']==1
    assert out['closed']==[True,True,True] and out['retired']
    assert out['progress']==['loading 2/3']
    assert out['readOptions'] == [{'cacheOnly': True}]


@pytest.mark.parametrize('terminal_state', ['ready', 'error'])
def test_terminal_model_job_reads_snapshot_and_keeps_manual_refresh_explicit(tmp_path, terminal_state):
    program = _standalone_loader_js().replace(
        'function usageReportSpeedWatchJob() {}', _extract('function usageReportSpeedWatchJob(')
    ).replace('function usageReportEnsureTemporal() {}',
              'const hourly=[]; function usageReportEnsureTemporal(options) { hourly.push(options); }')
    program += """
const events=[];
function pickRoute(){return null;}function serverPath(server,path){return path;}
class EventSource{constructor(url){this.url=url;events.push(this);}close(){this.closed=true;}}
(async()=>{
 usageReportSpeedState.mode='temporal';
 const first=usageReportLoadSpeed();asks[0].resolve({rows:[],cache:{state:'building',job_id:'first'}});await first;
 events[0].onmessage({data:JSON.stringify({state:TERMINAL})});
 asks[1].resolve({rows:[{model:'published'}],cache:{state:'stale',job_id:'unexpected'}});await flush();
 const afterSnapshot={urls:asks.map(a=>a.url),subscriptions:events.length,model:usageReportSpeedState.rows[0].model,hourly:[...hourly]};
 const refresh=usageReportLoadSpeed({refresh:true});asks[2].resolve({rows:[],cache:{state:'building',job_id:'manual'}});await refresh;
 console.log(JSON.stringify({afterSnapshot,manual:asks[2].url,subscriptions:events.length}));
})();
""".replace('TERMINAL', json.dumps(terminal_state))
    out = _run_node(tmp_path, 'terminal_model_snapshot', program)
    snapshot = out['afterSnapshot']
    assert len(snapshot['urls']) == 2 and 'cache_only=1' not in snapshot['urls'][0]
    assert 'cache_only=1' in snapshot['urls'][1] and 'refresh=1' not in snapshot['urls'][1]
    assert snapshot['subscriptions'] == 1 and snapshot['model'] == 'published'
    assert snapshot['hourly'] == [{'cacheOnly': False}, {'cacheOnly': True}]
    assert 'refresh=1' in out['manual'] and 'cache_only=1' not in out['manual']
    assert out['subscriptions'] == 2


def test_terminal_hourly_followup_reads_snapshot_but_explicit_hourly_load_can_ensure(tmp_path):
    program = '\n'.join([
        _extract('const usageReportSpeedState = {'), _extract('function usageReportEnsureTemporal('),
        "function usageReportSpeedKey(){return 'range';}function usageReportSpeedIsVisible(){return true;}",
        "function usageReportSpeedSelection(){return {source:'codex',model:'m',measurement_kind:'response_window',token_basis:'output_including_reasoning'};}",
        "function outputSpeedServer(){return {id:'local'};}function usageReportRenderSpeedTemporal(){}",
        "function usageReportSpeedQuery(params){return new URLSearchParams(Object.entries(params).filter(([,v])=>v!==null)).toString();}",
        "const asks=[];function fetchJsonWithRetry(server,url){asks.push(url);return Promise.resolve({hourly:[]});}",
        """
(async()=>{usageReportEnsureTemporal({cacheOnly:true});await new Promise(done=>setImmediate(done));
 usageReportSpeedState.temporalKey='';usageReportEnsureTemporal();await new Promise(done=>setImmediate(done));
 console.log(JSON.stringify(asks));})();
""",
    ])
    out = _run_node(tmp_path, 'terminal_hourly_snapshot', program)
    assert len(out) == 2 and 'cache_only=1' in out[0] and 'cache_only' not in out[1]



def test_tool_picker_includes_unmeasured_harnesses(tmp_path):
    out = _run_node(tmp_path, 'standalone_tools', _rows_js() + '''
usageReportSpeedState.rows = [{source:'kimi',speed_calls:50}];
usageReportSpeedState.payload = {source_status:[{source:'codex',usage_rows:1000},{source:'claude',usage_rows:2000}]};
console.log(JSON.stringify(usageReportSpeedTools()));
''')
    assert out == [{'source': 'kimi', 'calls': 50}, {'source': 'claude', 'calls': 0},
                   {'source': 'codex', 'calls': 0}]


def test_initial_comparison_keeps_all_measured_tools_visible(tmp_path):
    script = _standalone_loader_js() + _extract('function usageReportSpeedVisibleRows()') + '''
(async () => {
  usageReportSpeedState.tool = '';
  const load = usageReportLoadSpeed();
  asks[0].resolve({rows:[{source:'kimi',speed_calls:20},{source:'omp',speed_calls:10}]});
  await load;
  console.log(JSON.stringify({tool:usageReportSpeedState.tool, rows:usageReportSpeedVisibleRows()}));
})();
'''
    out = _run_node(tmp_path, 'all_measured_tools', script)
    assert out['tool'] == ''
    assert {row['source'] for row in out['rows']} == {'kimi', 'omp'}


def test_view_measured_dates_commits_the_shared_local_calendar_range(tmp_path):
    script = _standalone_loader_js() + '\n'.join([
        'function usageReportSpeedWritePrefs() {}',
        'let committed = null;',
        '''function commitDateSelection(start, end, options) {
          committed = {from:formatLocalDateYmd(start), to:formatLocalDateYmd(end), options};
        }''',
        _extract('function usageReportSpeedViewMeasuredDates()'),
        '''usageReportSpeedState.tool = 'codex';
        usageReportSpeedState.payload = {available_measurement_range:{from:'2026-07-16',to:'2026-09-15'}};
        usageReportSpeedViewMeasuredDates();
        console.log(JSON.stringify({committed, tool:usageReportSpeedState.tool, requests:asks.length}));''',
    ])
    out = _run_node(tmp_path, 'view_recorded_dates', script)
    assert out['committed'] == {'from': '2026-07-16', 'to': '2026-09-15', 'options': {'rangeKey': None}}
    assert out['tool'] == ''
    assert out['requests'] == 0, 'the recovery action bypassed the shared date router'


def test_speed_deep_link_clears_the_header_loading_placeholder(tmp_path):
    script = _standalone_loader_js() + '\n'.join([
        "const header = {textContent:'Loading...'}; document.getElementById = () => header;",
        _extract('function usageReportSpeedCacheMessage('),
        _extract('function usageReportSpeedUpdateHeader()'),
        '''usageReportSpeedState.loading = false;
        usageReportSpeedUpdateHeader();
        console.log(JSON.stringify({text:header.textContent}));''',
    ])
    assert _run_node(tmp_path, 'speed_header_loaded', script)['text'] == 'usageReportSpeedCachedHistory'


def test_model_snapshot_status_distinguishes_stale_pending_active_and_error_without_clearing_rates(tmp_path):
    script = STUBS + '\n'.join([
        _extract('const usageReportSpeedState = {'),
        _extract('function usageReportSpeedCacheMessage('),
        _extract('function usageReportSpeedUpdateHeader()'),
        _extract('function usageReportRenderSpeed()'),
        """
function element(){return {parts:[],dataset:{},appendChild(x){this.parts.push(x);},replaceChildren(...xs){this.parts=xs;},setAttribute(){},addEventListener(){}};}
const body=element(),header=element();let message='';
const document={getElementById:id=>id==='usageReportSpeedBody'?body:id==='lastUpdate'?header:null,createElement:element};
function usageReportRenderSpeedEmptyAction(){}function usageReportRenderSpeedMode(){}function usageReportRenderSpeedSortHeaders(){}
function usageReportSpeedPartialLine(){}function usageReportRenderSpeedSources(){}function usageReportRenderSpeedPicker(){}
function usageReportSpeedTools(){return [];}function usageReportSpeedSortedRows(){return usageReportSpeedState.rows;}
function usageReportSpeedKindLabel(kind){return kind;}function usageReportSpeedCell(value){return {value};}
function usageReportSpeedCallsCell(row){return {value:row.speed_calls};}function usageReportSpeedCoverageCell(row){return {value:row.coverage};}
function usageReportSpeedFormatRate(rate){return String(rate);}function usageReportRenderSpeedState(text){message=text;}
usageReportSpeedState.active=true;usageReportSpeedState.rows=[{source:'codex',model:'m',output_tok_per_s:72,speed_calls:4,coverage:1}];
const states=[{state:'stale',job_id:null},{state:'stale',job_id:'active'},{state:'building',job_id:null},
 {state:'pending',job_id:null},{state:'not-indexed',job_id:null},{state:'error',job_id:null,error:'worker failed'},{state:'ready',job_id:null}];
console.log(JSON.stringify(states.map(cache=>{usageReportSpeedState.payload={cache};usageReportRenderSpeed();
 return {message,header:header.textContent,rows:body.parts.length,rate:body.parts[0].parts[2].value};})));
""",
    ])
    out = _run_node(tmp_path, 'snapshot_status', script)
    assert [row['message'] for row in out] == [
        '', 'loading', 'loading', 'sessionSpeedPendingHint',
        'sessionSpeedPendingHint', 'usageReportSpeedError: worker failed', '',
    ]
    assert [row['header'] for row in out] == [
        'sessionSpeedStale', 'loading', 'loading', 'sessionSpeedPendingHint',
        'sessionSpeedPendingHint', 'usageReportSpeedError: worker failed', 'usageReportSpeedCachedHistory',
    ]
    assert all(row['rows'] == 1 and row['rate'] == '72' for row in out)


def test_hourly_snapshot_note_preserves_measurements_without_repeating_cache_status(tmp_path):
    script = STUBS + '\n'.join([
        _extract('const usageReportSpeedState = {'),
        _extract('function usageReportSpeedCacheMessage('),
        _extract('function usageReportSpeedHourRate('),
        _extract('function usageReportRenderSpeedTemporal()'),
        """
function element(){return {parts:[],replaceChildren(...xs){this.parts=xs;}};}
const host=element(),bins=element(),note=element();
const document={getElementById:id=>id==='usageReportSpeedHours'?host:id==='usageReportSpeedBinsBody'?bins:note};
function usageReportRenderSpeedPicker(){}function usageReportSpeedHourCell(row,index,rate){return {rate};}
function usageReportSpeedAbsence(){return {excluded:0};}function usageReportSpeedFormatStamp(ms){return String(ms);}
usageReportSpeedState.mode='temporal';
const states=[{state:'stale',job_id:null},{state:'stale',job_id:'active'},{state:'error',job_id:null,error:'worker failed'}];
console.log(JSON.stringify(states.map(cache=>{
 usageReportSpeedState.temporal={cache,hourly:[{label:'00',output_tok_per_s:42}],six_hour:[],days_with_measurements:1,selected_days:3};
 usageReportRenderSpeedTemporal();return {note:note.textContent,rate:host.parts[0].rate};
})));
""",
    ])
    out = _run_node(tmp_path, 'hourly_snapshot_status', script)
    assert out == [
        {'note': 'usageReportSpeedDaysWith 1/3', 'rate': 42},
        {'note': 'usageReportSpeedDaysWith 1/3', 'rate': 42},
        {'note': 'usageReportSpeedDaysWith 1/3', 'rate': 42},
    ]


def test_another_pages_refresh_invalidates_speed_without_fetching_while_hidden(tmp_path):
    script = _standalone_loader_js() + _extract('function usageReportSpeedResume()') + '\n'
    script += _extract('function outputSpeedUsageChanged(serverId)') + '''
usageReportSpeedState.active = false;
usageReportSpeedState.loadedKey = 'cached';
outputSpeedUsageChanged('other-server');
const afterOther = usageReportSpeedState.loadedKey;
outputSpeedUsageChanged('local');
console.log(JSON.stringify({afterOther, loadedKey:usageReportSpeedState.loadedKey, requests:asks.length}));
'''
    out = _run_node(tmp_path, 'hidden_speed_invalidation', script)
    assert out['afterOther'] == 'cached', 'a different server invalidated this host'
    assert out['loadedKey'] == '', 'a successful sync left stale rates cached'
    assert out['requests'] == 0, 'refreshing another page fetched hidden speed data'


def test_returning_from_speed_loads_the_shared_range_before_painting_old_overview(tmp_path):
    script = '\n'.join([
        'let currentStartDate = new Date(2026,0,1), currentEndDate = new Date(2026,9,8);',
        'let lastUsageResponse = {title:"old"}, lastWindowKey = "old", lastUsageServerKey = "local";',
        'const actions = [];',
        'function usageServerKeyFor() {return "local";}',
        'function updateDashboardByDateRange(start,end) {actions.push("load:"+formatLocalDateYmd(start));}',
        'function renderOverviewTab() {actions.push("paint");}',
        'function ensureSessionsLoaded() {actions.push("sessions");}',
        _extract('function formatLocalDateYmd(date)'),
        _extract('function windowKeyFor('),
        _extract('function resumeSharedDashboardRange(tab)'),
        '''
resumeSharedDashboardRange('overview');
lastWindowKey = windowKeyFor(null,'2026-01-01','2026-10-08');
resumeSharedDashboardRange('overview');
resumeSharedDashboardRange('sessions');
console.log(JSON.stringify(actions));
''',
    ])
    assert _run_node(tmp_path, 'shared_range_return', script) == ['load:2026-01-01', 'paint', 'sessions']


def test_queued_overview_work_is_not_dispatched_on_the_speed_page(tmp_path):
    script = _standalone_loader_js() + _extract('async function updateDashboard(') + '''
(async () => {
  await updateDashboard(null,'2026-09-01','2026-09-30',{forceRefresh:true});
  console.log(JSON.stringify({requests:asks.length}));
})();
'''
    assert _run_node(tmp_path, 'obsolete_overview_queue', script)['requests'] == 0


def test_every_element_the_speed_code_reaches_for_exists():
    """A missing id is silent in a browser and loud here.

    The tool filter and the model picker each need their own element: one select
    cannot serve two purposes, and the first draft wrote both through the same
    id, which silently removed the per-tool filter from the page.
    """
    ids = set(re.findall(r"getElementById\(['\"](usageReportSpeed[A-Za-z]*|sessionSpeed[A-Za-z]*)['\"]\)", SRC))
    ids |= set(re.findall(r"querySelector(?:All)?\(['\"]#(usageReportSpeed[A-Za-z]*|sessionSpeed[A-Za-z]*)", SRC))
    assert ids, "the speed code should reference elements"
    for element_id in sorted(ids):
        assert f'id="{element_id}"' in SRC, f"{element_id} is referenced but never rendered"
    assert "usageReportSpeedTool" in ids
    assert "usageReportSpeedPick" in ids
    assert 'for="usageReportSpeedTool"' in SRC


def test_no_report_call_site_names_a_function_that_does_not_exist():
    """A typo in a call site is a ReferenceError that eats the rest of the handler.

    ``activateDashboardTab('report')`` restores the subtab and then loads the
    report; while the restore call named a function that was never defined, the
    exception swallowed the load as well and the Report tab rendered nothing but
    placeholder dashes. A browser shows that as a blank panel, so the page's own
    call graph is checked here instead.
    """
    called = set(re.findall(r"\b(usageReport[A-Za-z0-9_]*)\s*\(", SRC))
    defined = set(re.findall(r"function\s+(usageReport[A-Za-z0-9_]*)\s*\(", SRC))
    defined |= set(re.findall(r"(?:const|let|var)\s+(usageReport[A-Za-z0-9_]*)\s*=", SRC))
    assert called, "expected the report code to have call sites"
    missing = sorted(called - defined)
    assert not missing, f"called but never defined: {missing}"


def test_standalone_speed_activation_sets_its_gate_before_loading():
    body = _extract("function activateDashboardTab(")
    assert body.index("usageReportSpeedState.active = tab === 'speed'") < body.index("usageReportSpeedResume()")
    assert "outputSpeedRetirePending()" in body
    assert "usageReportSpeedSetSubtab" not in body


def test_the_table_and_its_spanned_rows_agree_on_the_column_count():
    headers = re.findall(r'data-speed-sort="([a-z_]+)"', SRC)
    assert headers == ["model", "source", "output_tok_per_s", "speed_calls", "coverage"]
    # Both spanned rows are built by helpers now, so the span is a property rather
    # than an attribute in a template string.
    spanned = "\n".join([
        _extract("function usageReportSpeedMessageRow(message)"),
        _extract("function usageReportSpeedDetailRow(row)"),
    ])
    spans = set(re.findall(r"colSpan = (\d+)", spanned))
    assert spans == {"5"}, f"spanned rows must match the {len(headers)} columns"


# --------------------------------------------------------------------------
# Behaviour, run for real under node
# --------------------------------------------------------------------------


def test_an_unmeasured_hour_is_a_gap_not_a_zero_rate(tmp_path):
    """The hour strip asks whether the hour was measured before it coerces.

    ``Number(null)`` is ``0``, so the first version of the time-of-day view drew
    every unmeasured hour as a labelled ``0.0 tok/s`` bar slot: a number, for a
    thing that never happened. The guard has to sit ahead of the coercion.
    """
    script = STUBS + _extract("function usageReportSpeedHourRate(raw)") + """
const cases = [null, undefined, '', '72.4', 0, 12.5, NaN, Infinity, 'abc'];
console.log(JSON.stringify(cases.map((v) => usageReportSpeedHourRate(v))));
"""
    out = _run_node(tmp_path, "hour-rate", script)
    assert out[:4] == [None, None, None, 72.4], out
    assert out[4] == 0, "a measured hour whose rate rounds to zero is still a measurement"
    assert out[5] == 12.5
    assert out[6:] == [None, None, None], "non-finite and unparseable rates are absence"


def test_the_hour_strip_uses_that_guard():
    body = _extract("function usageReportRenderSpeedTemporal()")
    assert "usageReportSpeedHourRate(row.output_tok_per_s)" in body
    assert "Number(row.output_tok_per_s)" not in body


def test_hour_tooltips_expose_values_and_keep_missing_hours_unmeasured(tmp_path):
    script = _rows_js() + _extract('function usageReportSpeedHourCell(') + '''
function element(tag) {
  return {tag, style:{}, attributes:{}, parts:[], events:{},
    setAttribute(key,value) {this.attributes[key] = value;},
    addEventListener(key,callback) {this.events[key] = callback;},
    appendChild(child) {this.parts.push(child);},
    classList:{add(){},remove(){}}};
}
global.document = {createElement:element};
const measured = usageReportSpeedHourCell({label:'09–10',speed_calls:15},9,48.6,80,false);
const missing = usageReportSpeedHourCell({label:'03–04',speed_calls:0},3,null,0,false);
console.log(JSON.stringify([measured,missing]));
'''
    measured, missing = _run_node(tmp_path, 'hour_tooltip_values', script)
    assert measured['tabIndex'] == missing['tabIndex'] == 0
    assert '48.6 tok/s' in measured['parts'][1]['textContent']
    assert 'n=15' in measured['attributes']['aria-label']
    assert 'usageReportSpeedNoMeasurements' in missing['parts'][1]['textContent']
    assert '0.0 tok/s' not in missing['attributes']['aria-label']
    assert missing['parts'][0]['style']['height'] == '0%'


def test_method_explanations_are_collapsed_and_model_summary_is_aggregated():
    note = SRC[SRC.index('<details class="speed-method-note"'):SRC.index('</details>', SRC.index('<details class="speed-method-note"'))]
    assert ' open' not in note
    assert 'usageReportSpeedCompleteness' in note
    assert 'usageReportSpeedMethodSummary' in note
    picker = _extract('function usageReportRenderSpeedPicker()')
    assert 'current.output_tok_per_s' in picker
    assert 'usageReportSpeedAcrossSessions' in picker


def test_measured_days_counts_the_union_rather_than_the_maximum(tmp_path):
    """Two models measured on two days is a tool measured on two days.

    Reported as an understated report: the note under the table read the maximum of
    the per-row day counts, so model A measured on day one and model B on day two
    still printed "1/2 days". A maximum cannot build a union and a sum counts the
    days the models share twice, so the count arrives from the query -- per source
    for the one-tool view the page shows, and payload-wide when a view really does
    span sources.
    """
    script = _rows_js() + """
const kimi = [
  {model: 'a', source: 'kimi', days_with_measurements: 1, source_days_with_measurements: 2},
  {model: 'b', source: 'kimi', days_with_measurements: 1, source_days_with_measurements: 2},
];
usageReportSpeedState.rows = kimi;
usageReportSpeedState.tool = 'kimi';
usageReportSpeedState.payload = {days_with_measurements: 4};
const out = {oneTool: usageReportSpeedMeasuredDays(usageReportSpeedVisibleRows())};

// Rows from two tools at once: neither tool's own count describes the view.
usageReportSpeedState.rows = kimi.concat([
  {model: 'c', source: 'omp', days_with_measurements: 3, source_days_with_measurements: 3},
]);
usageReportSpeedState.tool = '';
out.twoTools = usageReportSpeedMeasuredDays(usageReportSpeedVisibleRows());

// A cached payload from before either field existed still prints the old maximum.
delete usageReportSpeedState.payload;
usageReportSpeedState.rows = kimi.map((row) => ({
  model: row.model, source: row.source, days_with_measurements: row.days_with_measurements,
}));
out.legacy = usageReportSpeedMeasuredDays(usageReportSpeedVisibleRows());
console.log(JSON.stringify(out));
"""
    out = _run_node(tmp_path, "measured-days", script)
    assert out["oneTool"] == 2, "the union, where the maximum said 1 and the sum said 4"
    assert out["twoTools"] == 4, "a view spanning sources takes the payload-wide union"
    assert out["legacy"] == 1, "an older payload degrades to the number it can support"


def test_the_note_under_the_table_reads_that_union():
    body = _extract("function usageReportSpeedPartialLine()")
    assert "usageReportSpeedMeasuredDays(rows)" in body
    assert "Math.max(...rows.map((row) => Number(row.days_with_measurements" not in body


def test_absence_sorts_last_in_both_directions(tmp_path):
    script = _rows_js() + """
usageReportSpeedState.rows = [
  {model: 'b', source: 'kimi', output_tok_per_s: 10, speed_calls: 5, coverage: 0.5},
  {model: 'c', source: 'kimi', output_tok_per_s: null, speed_calls: null, coverage: null},
  {model: 'a', source: 'omp', output_tok_per_s: 30, speed_calls: 12, coverage: 1.0},
];
const out = {};
for (const field of ['output_tok_per_s', 'speed_calls', 'coverage']) {
  for (const direction of ['desc', 'asc']) {
    usageReportSpeedState.sort = {field, direction};
    out[`${field}/${direction}`] = usageReportSpeedSortedRows().map((r) => r.model);
  }
}
console.log(JSON.stringify(out));
"""
    out = _run_node(tmp_path, "sort", script)
    for key, order in out.items():
        assert order[-1] == "c", f"{key} put absence at the top"
    assert out["output_tok_per_s/desc"] == ["a", "b", "c"]
    assert out["output_tok_per_s/asc"] == ["b", "a", "c"]
    assert out["speed_calls/desc"] == ["a", "b", "c"]


def test_a_missing_rate_formats_as_a_dash_not_as_zero(tmp_path):
    script = _rows_js() + """
console.log(JSON.stringify([
  usageReportSpeedFormatRate(null),
  usageReportSpeedFormatRate(undefined),
  usageReportSpeedFormatRate(0),
  usageReportSpeedFormatRate(1234.567),
]));
"""
    got = _run_node(tmp_path, "format", script)
    assert got[0] == "\u2014" and got[1] == "\u2014"
    assert got[2].startswith("0."), "a real zero rate is a measurement and prints as one"
    assert got[3].startswith("1,234.6"), got[3]


def test_absence_counts_come_from_the_models_scope(tmp_path):
    script = _rows_js() + """
const scoped = usageReportSpeedAbsence({
  unmeasured: {scope: 'source_model', missing_timing_calls: 7,
               excluded_calls_by_reason: {invalid_timing: 3, ambiguous_pair: 2}},
});
const bare = usageReportSpeedAbsence({
  missing_timing_calls: 1, excluded_calls_by_reason: {invalid_timing: 4},
});
const nothing = usageReportSpeedAbsence({});
console.log(JSON.stringify([scoped, bare, nothing]));
"""
    scoped, bare, nothing = _run_node(tmp_path, "absence", script)
    assert (scoped["missing"], scoped["excluded"], scoped["byReason"]["ambiguous_pair"]) == (7, 5, 2)
    assert (bare["missing"], bare["excluded"]) == (1, 4)
    assert nothing == {"excluded": 0, "missing": 0, "byReason": {}}


def _session_render_js() -> str:
    return chr(10).join([
        STUBS,
        _extract("function usageReportSpeedFormatRate(rate)"),
        _extract("function usageReportSpeedKindLabel(kind)"),
        _extract("function usageReportSpeedBasisLabel(basis)"),
        _extract("function renderSessionResponseSpeed(data)"),
    ])


def _session_dom_script(data_json: str) -> str:
    return f"""
const elements = {{
  sessionSpeedPanel: {{hidden: true, id: 'sessionSpeedPanel'}},
  sessionSpeedBody: {{innerHTML: ''}},
  sessionSpeedCoverage: {{textContent: ''}},
  sessionSpeedNote: {{textContent: ''}},
}};
global.document = {{getElementById: (id) => elements[id] || null}};
{_session_render_js()}
renderSessionResponseSpeed({data_json});
console.log(JSON.stringify({{
  hidden: elements.sessionSpeedPanel.hidden,
  coverage: elements.sessionSpeedCoverage.textContent,
  note: elements.sessionSpeedNote.textContent,
  body: elements.sessionSpeedBody.innerHTML,
}}));
"""


def test_the_session_panel_shows_measured_and_unmeasured_turns_differently(tmp_path):
    data = {
        "turns": [
            {"turn_index": 1, "timestamp": "2026-06-01T12:00:00+00:00", "tokens_out": 400,
             "output_speed": {"output_tok_per_s": 200.0, "speed_calls": 1,
                              "measurement_kind": "server_decode",
                              "token_basis": "output_reasoning_unspecified"}},
            {"turn_index": 2, "timestamp": "2026-06-01T12:05:00+00:00", "tokens_out": 60,
             "output_speed": None},
        ],
        "speed_measurement": {
            "responses": 2, "measured_responses": 1, "measured_calls": 1,
            "components": [{"measurement_kind": "server_decode",
                            "token_basis": "output_reasoning_unspecified"}],
            "output_tok_per_s": 200.0,
        },
    }
    out = _run_node(tmp_path, "session", _session_dom_script(json.dumps(data)))
    assert out["hidden"] is False
    assert "sessionSpeedMeasured" in out["coverage"]
    assert "sessionSpeedWindow" in out["note"]
    assert "speed-unmeasured" in out["body"], "the unmeasured turn must read as absent"
    assert out["body"].count("<tr>") == 2


def test_a_source_with_no_timing_at_all_says_so_instead_of_printing_dashes(tmp_path):
    data = {
        "turns": [{"turn_index": 1, "timestamp": "2026-06-01T12:00:00+00:00",
                   "tokens_out": 10, "output_speed": None}],
        "speed_measurement": {"responses": 1, "measured_responses": 0, "measured_calls": 0,
                              "components": []},
    }
    out = _run_node(tmp_path, "session_none", _session_dom_script(json.dumps(data)))
    assert out["coverage"] == "sessionSpeedNoneMeasured"
    assert out["note"] == "sessionSpeedUnmeasuredSource"


def test_two_methods_in_one_session_are_named_rather_than_averaged(tmp_path):
    data = {
        "turns": [
            {"turn_index": 1, "timestamp": "2026-06-01T12:00:00+00:00", "tokens_out": 10,
             "output_speed": {"output_tok_per_s": 100.0, "speed_calls": 1,
                              "measurement_kind": "server_decode",
                              "token_basis": "output_reasoning_unspecified"}},
            {"turn_index": 2, "timestamp": "2026-06-01T12:01:00+00:00", "tokens_out": 10,
             "output_speed": {"output_tok_per_s": 50.0, "speed_calls": 1,
                              "measurement_kind": "post_first_token",
                              "token_basis": "output_including_reasoning"}},
        ],
        "speed_measurement": {
            "responses": 2, "measured_responses": 2, "measured_calls": 2,
            "components": [
                {"measurement_kind": "server_decode", "token_basis": "output_reasoning_unspecified"},
                {"measurement_kind": "post_first_token", "token_basis": "output_including_reasoning"},
            ],
        },
    }
    out = _run_node(tmp_path, "session_mixed", _session_dom_script(json.dumps(data)))
    assert out["note"].startswith("sessionSpeedMixedMethods")
    # The stub t() hands back the key it was given, which is how the labels are
    # looked up: two named methods, not one blended figure.
    assert "speedKindServerDecode" in out["note"]
    assert "speedKindPostFirstToken" in out["note"]


# --------------------------------------------------------------------------
# Localisation
# --------------------------------------------------------------------------


def _i18n_keys() -> dict[str, set[str]]:
    """Key sets per language from the single ``const I18N = {...}`` literal."""
    start = SRC.index("const I18N = {")
    depth = 0
    end = None
    for index in range(SRC.index("{", start), len(SRC)):
        char = SRC[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    block = SRC[start:end]
    out: dict[str, set[str]] = {}
    for language in LANGUAGES:
        marker = f"\n      {language}: {{"
        at = block.find(marker)
        assert at >= 0, f"language block {language} not found"
        depth = 0
        stop = None
        for index in range(block.index("{", at), len(block)):
            char = block[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    stop = index
                    break
        body = block[at:stop]
        out[language] = set(re.findall(r"^        ([A-Za-z][A-Za-z0-9_]*):", body, re.M))
    return out


def test_every_speed_string_ships_in_all_six_languages():
    used = set(re.findall(r'data-i18n="(usageReportSpeed[A-Za-z]*|sessionSpeed[A-Za-z]*|usageReportSubSpeed)"', SRC))
    used |= set(re.findall(r"t\('(usageReportSpeed[A-Za-z]*|sessionSpeed[A-Za-z]*|usageReportSubSpeed)'\)", SRC))
    used |= set(re.findall(r"\bt\(`(usageReportSpeed[A-Za-z]*|sessionSpeed[A-Za-z]*)`", SRC))
    assert len(used) >= 25, f"expected the speed view to carry its own strings, found {len(used)}"
    keys = _i18n_keys()
    for language, language_keys in keys.items():
        missing = used - language_keys
        assert not missing, f"{language} is missing {sorted(missing)}"

def test_the_sample_node_keeps_measured_excluded_and_unmeasured_apart(tmp_path):
    # "n=15 - 3 excluded - 2 without timing" is three statements, not one sum:
    # a sum would turn a decision the reader can disagree with into a missing
    # instrument count, and vice versa. Built as elements, because the Report tab
    # renders log-derived text and has a standing rule against innerHTML.
    script = _rows_js() + """
function el(tag) {
  return {tag, className: '', dataset: {}, style: {}, parts: [],
          setAttribute() {}, addEventListener() {},
          appendChild(child) { this.parts.push(child); return child; },
          set textContent(value) { this.parts = [String(value)]; },
          get textContent() {
            return this.parts.map((p) => (typeof p === 'string' ? p
              : (p.textContent !== undefined ? p.textContent : String(p)))).join('');
          }};
}
global.document = {createElement: (tag) => el(tag),
                   createTextNode: (value) => ({textContent: String(value)})};
const classes = (node) => node.parts.filter((p) => p && p.className).map((p) => p.className);
const render = (row) => {
  const node = usageReportSpeedSampleNode(row);
  return {text: node.textContent, classes: classes(node), className: node.className};
};
console.log(JSON.stringify([
  render({speed_calls: 15,
          unmeasured: {scope: 'source_model', missing_timing_calls: 2,
                       excluded_calls_by_reason: {invalid_timing: 3}}}),
  render({speed_calls: 40}),
]));
"""
    with_absence, clean = _run_node(tmp_path, "sample_node", script)
    assert with_absence["className"] == "speed-sample"
    assert "n=15" in with_absence["text"]
    assert "3 usageReportSpeedExcluded" in with_absence["text"]
    assert "2 usageReportSpeedWithoutTiming" in with_absence["text"]
    assert with_absence["text"].index("n=15") < with_absence["text"].index("usageReportSpeedExcluded")
    assert with_absence["classes"] == ["speed-excluded"], "only the exclusion is styled"
    assert clean["text"] == "n=40" and not clean["classes"], (
        "a fully measured row gets no absence clauses")


def test_reloading_with_a_tool_chosen_keeps_the_other_tools(tmp_path):
    """The picker is built from the answer, so the answer must be the whole answer.

    Reported as Kimi + omp turning into Kimi on the second load: the reload asked
    the server for the saved tool's rows only, the picker was then built from
    whatever came back, and the tool the reader had not picked vanished from the
    page they were comparing. Asking for every tool and filtering here is what
    keeps the comparison a comparison.
    """
    script = _rows_js() + """
    usageReportSpeedState.rows = [
      {source: 'kimi', model: 'k3', speed_calls: 10, output_tok_per_s: 40},
      {source: 'omp', model: 'gpt', speed_calls: 3, output_tok_per_s: 60},
    ];
    usageReportSpeedState.tool = 'kimi';
    console.log(JSON.stringify({
      visible: usageReportSpeedVisibleRows().map((row) => row.source),
      sorted: usageReportSpeedSortedRows().map((row) => row.source),
      tools: usageReportSpeedTools().map((tool) => tool.source),
    }));
    """
    out = _run_node(tmp_path, "speed_tool_reload", script)
    assert out["visible"] == ["kimi"], "the filter still filters"
    assert out["sorted"] == ["kimi"]
    assert out["tools"] == ["kimi", "omp"], "omp survives the reload"

    loader = _extract("async function usageReportLoadSpeed(")
    queries = re.findall(r"usageReportSpeedQuery\([^)]*\)", loader)
    assert queries, "the loader builds its own query"
    for query in queries:
        assert "source" not in query, (
            f"{query} asks the server for one tool, which is what empties the picker"
        )
