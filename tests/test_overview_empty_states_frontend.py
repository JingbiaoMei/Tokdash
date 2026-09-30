"""An Overview range must read as one consistent state, not per-metric zeros.

Seven ways this regresses, none of which string assertions on the page can see:

- a stale count-up finishing over a freshly rendered value. anime.js hands back
  different objects from createAnimatable() and animate(), and only one of them
  can actually be cancelled, so the wrong verb silently threw and every
  superseded animation ran to the end of its duration;
- Cost/Messages updating only from animation callbacks, so fitOverviewKpis()
  measured the previous text and clipped large values, and a background tab —
  where anime pauses its engine — kept the old range's numbers;
- an incomplete read dressing up as an empty range, and an empty range
  unpainting itself after a failed refresh, because the wrong flag was
  consulted. Both halves matter: a read that came back short of a full answer
  still has numbers, and "No data" over them reads as a proven zero;
- a "n/a" hit rate painted over by the previous range's count-up;
- the next range counting up from a value that was never on screen — because the
  counter base was read before the running animation handed its live value back,
  or because a range with no token figure of its own left the old total behind;
- a count-up frame formatted while it still held a fraction, which paints a
  string wider than the value the card was just fitted to.

These run the real renderOverviewTab against a stub DOM, together with the real
animation counters module and the real anime.js engine — imported from disk, not
stand-ins — so the supersede/cancel behaviour is exercised for real.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"
COUNTERS_JS = INDEX_HTML.parent / "js" / "animations" / "counters.js"
ANIME_JS = INDEX_HTML.parent / "js" / "anime.esm.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _extract_js_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start >= 0, f"{signature} not found"
    # Skip the parameter list: a default such as `value = x` or `options = {}`
    # puts braces inside parentheses, and counting those as the body truncates
    # the function at the default value.
    parens = 0
    body_start = -1
    for index in range(start, len(src)):
        char = src[index]
        if char == "(":
            parens += 1
        elif char == ")":
            parens -= 1
        elif char == "{" and parens == 0:
            body_start = index
            break
    assert body_start >= 0, f"no body for {signature}"
    depth = 0
    for index in range(body_start, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


HARNESS_HEAD = """
// --- a DOM just deep enough for the functions under test ---------------------
const nodes = {};
function node(id) {
  if (!nodes[id]) {
    nodes[id] = {
      id, textContent: '', innerHTML: '', style: {}, attrs: {}, dataset: {},
      _currentValue: undefined,
      setAttribute(name, value) { nodes[id].attrs[name] = value; },
      removeAttribute(name) { delete nodes[id].attrs[name]; },
    };
  }
  return nodes[id];
}
const document = {
  getElementById: (id) => node(id),
  createElement: () => ({ style: {}, appendChild() {}, setAttribute() {}, addEventListener() {} }),
};

// --- the browser globals the animation engine expects ------------------------
const window = globalThis;
window.matchMedia = () => ({ matches: false });
if (typeof requestAnimationFrame === 'undefined') {
  window.requestAnimationFrame = (fn) => setTimeout(() => fn(performance.now()), 16);
}

// --- page stand-ins ----------------------------------------------------------
let currentLang = 'en';
const LANG_LOCALES = { en: 'en-US', zh: 'zh-CN', ja: 'ja-JP', ko: 'ko-KR', es: 'es-ES', pt: 'pt-BR' };
function langLocale(lang = currentLang) { return LANG_LOCALES[lang] || 'en-US'; }
const LABELS = { noData: 'No data', tokensUnit: 'tokens' };
function t(key) { return LABELS[key] || key; }
let overviewReadableTokens = true;
let overviewTotalTokensRaw = 0;
let overviewRenderToken = 0;
// reconcileUsageRows defaults to these; every scenario passes its own cache.
const lastUsageRowsByServer = new Map();
function selectedServers() { return [{ id: 'local' }]; }
let lastWindowKey = null;
let lastCombinedModels = [];
let lastAppsBreakdown = null;
let lastByTool = {};
let overviewBreakdownWindowKey = null;
const statsCache = { default: null };
function formatNumber(value) { return Number(value || 0).toLocaleString('en-US'); }
function formatCurrency(num) { const n = Number(num ?? 0); return '$' + n.toFixed(2); }
function formatTokenCount(value, includeUnit = false) {
  const n = Number(value || 0);
  const unit = includeUnit ? ' tokens' : '';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M' + unit;
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k' + unit;
  return n + unit;
}
function formatCompactTokenCount(value) { return formatTokenCount(value); }
function normalizeOverviewTokenCount(value) { return Number(value) || 0; }
function formatHitRate(rate) {
  if (rate === null || rate === undefined || Number.isNaN(Number(rate))) return 'n/a';
  return (Number(rate) * 100).toFixed(1) + '%';
}
// fitKpiValue only shrinks text that is already on screen, so record what each
// card held at fit time: that is exactly the measurement the real fit uses.
const seenAtFit = {};
function fitKpiValue(el) { if (el) seenAtFit[el.id] = el.textContent; }
function fitOverviewKpis() { ['totalTokens', 'totalCost', 'totalMessages', 'overviewActiveTime'].forEach((id) => fitKpiValue(node(id))); }
function renderOverviewActiveTime() {}
function updateComparisonDeltas() {}
function updateToolChart() {}
function updateModelChart() {}
function updateToolsTable() {}
function renderOverviewProfilePreview() {}
function reconcileTodayProfileContribution(cached) { return cached; }
function updateTokenCompositionBar() {}
function updateStatsInsights() {}
function updateAppsBreakdown() {}
function updateCombinedModelsTable() {}
function scheduleIdle(fn) { fn(); }

// --- the code under test -----------------------------------------------------
__FUNCTIONS__

// --- the real animation module and the real anime.js engine, imported --------
import { pathToFileURL } from 'node:url';
const { animateNumber, cancelCounter, setCounterText } = await import(
  pathToFileURL('__COUNTERS_PATH__').href
);
// The engine itself, so a test can tell "stopped" from "running but ignored".
const { engine } = await import(pathToFileURL('__ANIME_PATH__').href);
window.TokDashAnimations = { animateNumber, cancelCounter, setCounterText };

// --- driving ------------------------------------------------------------------
"""

SCENARIOS = """
async function main() {
  const scenario = process.argv[2];
  const out = {};
  const zeroPayload = (extra = {}) => ({
    range: 'EMPTY',
    total_tokens: 0,
    total_cost: 0,
    total_messages: 0,
    cache_hit_rate: null,
    by_tool: {},
    top_models: [],
    ...extra,
  });
  const populated = (extra = {}) => ({
    range: 'X',
    total_tokens: 5200000,
    total_cost: 12.5,
    total_messages: 300,
    cache_hit_rate: 0.4,
    by_tool: {},
    top_models: [{ name: 'gpt', cost: 12.5, tokens: 5200000 }],
    ...extra,
  });

  // One server's /api/usage body. A scenario that needs a partial read builds
  // the response through the real reconcileUsageRows + combineUsagePayloads
  // rather than hand-setting flags on a literal, so which flags production
  // actually sets — and what the totals end up being — is the code's answer,
  // not the test's guess.
  const serverPayload = (extra = {}) => ({
    range: 'S',
    timestamp: '2026-09-30T12:00:00Z',
    total_tokens: 5200000,
    total_cost: 12.5,
    total_messages: 300,
    cache_hit_rate: 0.4,
    by_tool: { codex: { tokens: 5200000, cost: 12.5 } },
    combined_models: [{ name: 'gpt', tokens: 5200000, cost: 12.5 }],
    ...extra,
  });

  function combinedFrom(rows, servers) {
    const reconciliation = reconcileUsageRows(rows, 'test-window', servers, new Map());
    const combined = combineUsagePayloads(reconciliation.rows.map((row) => row.payload));
    // Mirrors updateDashboard's `.then`: the combined response is built from the
    // row payloads, and the flags are set on the result.
    combined._has_incomplete_server_rows = reconciliation.unavailable.length > 0;
    combined._has_retained_server_rows = reconciliation.retained.length > 0;
    return { combined, reconciliation };
  }

  const kpiRow = () => ({
    tokens: node('totalTokens').textContent,
    cost: node('totalCost').textContent,
    messages: node('totalMessages').textContent,
  });

  const settle = () => new Promise((resolve) => setTimeout(resolve, 5));
  const advance = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  if (scenario === 'stale-animation-cannot-overwrite-empty-state') {
    // A populated range is rendered (starting real 1.2s count-ups), then an
    // empty range lands while those animations are still running. The empty
    // render must win: no in-flight onUpdate may paint over "No data".
    renderOverviewTab(populated());
    await advance(400);
    out.midFlightValue = node('totalTokens').textContent;
    renderOverviewTab(zeroPayload());
    out.rightAfterEmptyRender = node('totalTokens').textContent;
    out.costRightAfter = node('totalCost').textContent;
    out.messagesRightAfter = node('totalMessages').textContent;
    await advance(1400);
    out.afterStaleAnimationWouldHaveFinished = node('totalTokens').textContent;
    out.costAfter = node('totalCost').textContent;
    out.messagesAfter = node('totalMessages').textContent;
  }

  if (scenario === 'cancelling-actually-stops-the-animation') {
    // The engine returns an Animatable from createAnimatable() that exposes only
    // revert(). Calling pause() on it throws, and swallowing that left superseded
    // animations registered with the engine for their whole duration.
    //
    // The element's text is NOT a usable signal here: the identity guards in
    // onUpdate/onComplete keep a superseded animation from writing even while it
    // keeps running, so the text stays correct in both cases. What separates them
    // is whether the engine is still holding the animation, so assert on that -
    // with a duration far longer than the wait, "still registered" can only mean
    // the cancel did not take.
    const el = { _t: '', _currentValue: undefined };
    Object.defineProperty(el, 'textContent', {
      get() { return this._t; },
      set(value) { this._t = value; },
    });
    animateNumber(el, 100, 2000, (v) => '$' + Number(v).toFixed(2));
    await advance(200);
    // engine._head is the head of the engine's linked list of live instances. It
    // is private to the bundled anime 4.0.0 — the engine's only public surface is
    // fps/speed/timeUnit/precision, and an Animatable exposes nothing but val()
    // and revert(), so there is no public way to ask whether one is still
    // registered. Pinned by test_the_engine_probe_is_pinned_to_the_bundled_anime.
    out.registeredWhileAnimating = !!engine._head;
    cancelCounter(el);
    out.liveValueOnCancel = typeof el._currentValue === 'number';
    await advance(600);
    out.stillRegisteredAfterCancel = !!engine._head;
    out.stopped = out.stillRegisteredAfterCancel === false;
  }

  if (scenario === 'large-values-are-measured-at-their-final-width') {
    // fitOverviewKpis() runs after the row is written and only shrinks text that
    // is already there. Cost/Messages used to update purely from animation
    // callbacks, so the fit measured the previous range's text and a big value
    // stayed clipped until the window was resized.
    renderOverviewTab(populated({ total_cost: 12345.67, total_messages: 12345678 }));
    out.costAtFit = seenAtFit.totalCost;
    out.messagesAtFit = seenAtFit.totalMessages;
    out.expectedCost = formatCurrency(12345.67);
    out.expectedMessages = formatNumber(12345678);
    await advance(1400);
    out.costSettled = node('totalCost').textContent;
    out.messagesSettled = node('totalMessages').textContent;
  }

  if (scenario === 'a-null-hit-rate-cancels-the-in-flight-count-up') {
    // "n/a" is a static write. Without a cancel the previous range's 40% count-up
    // finishes on top of it and the card shows a number next to its own "n/a".
    renderOverviewTab(populated({ cache_hit_rate: 0.4 }));
    await advance(300);
    renderOverviewTab(populated({ cache_hit_rate: null, total_tokens: 900, total_cost: 1, total_messages: 5 }));
    out.hitRightAfter = node('avgCacheHitRate').textContent;
    await advance(1400);
    out.hitAfter = node('avgCacheHitRate').textContent;
  }

  if (scenario === 'empty-range-agrees-across-the-row') {
    renderOverviewTab(zeroPayload());
    await settle();
    out.tokens = node('totalTokens').textContent;
    out.cost = node('totalCost').textContent;
    out.messages = node('totalMessages').textContent;
    out.topModel = node('topModel').textContent;
  }

  if (scenario === 'unpriced-but-populated-keeps-free') {
    // Tokens and messages exist, every model is unpriced: this is data, not
    // emptiness. Cost must show the $0 FREE state and animate back to 0.
    renderOverviewTab(populated({ total_cost: 0 }));
    await advance(1400);
    out.tokens = node('totalTokens').textContent;
    out.cost = node('totalCost').textContent;
    out.messages = node('totalMessages').textContent;
  }

  if (scenario === 'an-incomplete-read-shows-the-numbers-it-has') {
    // Case 1: the only selected server answered with source errors and has no
    // cached snapshot for this range, so it lands in `unavailable` — and its
    // (partial) totals are all the page is ever going to get.
    const degraded = combinedFrom(
      [{ server: { id: 'local' }, payload: serverPayload({ source_errors: ['sessions.jsonl'] }) }],
      [{ id: 'local' }],
    );
    out.flaggedIncomplete = degraded.reconciliation.unavailable.length > 0;
    renderOverviewTab(degraded.combined);
    await advance(1400);
    out.degradedCards = kpiRow();

    // Case 2: two servers, the second unreachable with no snapshot, the first
    // clean. The combined answer covers one machine, which is not the same as
    // covering none of them.
    const partial = combinedFrom(
      [{ server: { id: 'lan' }, payload: serverPayload({ total_tokens: 1000, total_cost: 2.25, total_messages: 40 }) }],
      [{ id: 'lan' }, { id: 'offline' }],
    );
    out.secondServerUnavailable = partial.reconciliation.unavailable.length > 0;
    renderOverviewTab(partial.combined);
    await advance(1400);
    out.partialCards = kpiRow();
  }

  if (scenario === 'an-incomplete-read-with-zero-totals-is-not-no-data') {
    // Zeroes from a short read are not a proven-empty range either, so the row
    // must not claim "No data". It renders what it was given, which is what the
    // row did before these states existed.
    const short = combinedFrom(
      [{ server: { id: 'local' }, payload: serverPayload({
        total_tokens: 0, total_cost: 0, total_messages: 0, cache_hit_rate: null,
        by_tool: {}, combined_models: [], source_errors: ['usage.jsonl'],
      }) }],
      [{ id: 'local' }],
    );
    out.flaggedIncomplete = short.reconciliation.unavailable.length > 0;
    renderOverviewTab(short.combined);
    await settle();
    out.cards = kpiRow();
  }

  if (scenario === 'a-failed-refresh-keeps-the-empty-state') {
    // The catch path sets _server_rows_stale on the last good response so the
    // Servers tab reads its rows as stale. It says nothing about this range, and
    // treating it as a broken read repainted an empty range as 0 / FREE / 0
    // until the next successful refresh flipped it back.
    const payload = zeroPayload();
    renderOverviewTab(payload);
    await settle();
    out.beforeFailure = node('totalCost').textContent;
    payload._server_rows_stale = true;
    renderOverviewTab(payload);
    await settle();
    out.afterFailedRefresh = node('totalCost').textContent;
    out.tokensAfterFailedRefresh = node('totalTokens').textContent;
  }

  if (scenario === 'next-range-counts-from-the-value-on-screen') {
    // Driven through renderOverviewTab, which is what writes _currentValue on the
    // Cost and Messages cards. renderOverviewTokenTotal never animates, so a test
    // that only calls it could not tell whether the resets are there.
    renderOverviewTab(populated({ total_cost: 12.5, total_messages: 300 }));
    await settle();
    out.costBaseAfterPopulated = node('totalCost')._currentValue;
    renderOverviewTab(zeroPayload());
    out.costBaseAfterEmpty = node('totalCost')._currentValue;
    out.messagesBaseAfterEmpty = node('totalMessages')._currentValue;
    renderOverviewTab(populated({ total_cost: 1, total_messages: 2, total_tokens: 1000 }));
    await advance(60);
    out.recountCost = node('totalCost').textContent;
    out.recountMessages = node('totalMessages').textContent;
  }

  if (scenario === 'a-rerender-mid-count-resumes-from-the-live-value') {
    // A language switch or the readable-tokens toggle re-renders the range while
    // a count-up is mid-flight. The replacement animation must pick up the number
    // that is actually on screen, not the last total that finished animating —
    // which for a first render is nothing at all, so the card drops to $0 and
    // climbs again while the user watches.
    renderOverviewTab(populated({ total_cost: 5000, total_messages: 20000 }));
    await advance(250);
    out.liveBefore = node('totalCost').textContent;
    renderOverviewTab(populated({ total_cost: 4000, total_messages: 20000 }));
    await advance(80);
    out.shortlyAfter = node('totalCost').textContent;
    await advance(1600);
    out.settled = node('totalCost').textContent;
  }

  if (scenario === 'a-range-without-a-token-total-does-not-count-down') {
    // A range with cost and messages but no token figure of its own is not an
    // empty range, so the Tokens card falls back to a dash. A dash is not a
    // number: if the previous total survives as the counter base, the next range
    // animates down from 5.2M rather than up from nothing.
    overviewReadableTokens = false;
    renderOverviewTab(populated({ total_cost: 12.5, total_messages: 300 }));
    await advance(1600);
    out.settledFirst = node('totalTokens').textContent;
    renderOverviewTab(populated({ total_tokens: null, total_cost: 3, total_messages: 7 }));
    out.rightAfter = node('totalTokens').textContent;
    await advance(200);
    out.duringNullRange = node('totalTokens').textContent;
    renderOverviewTab(populated({ total_tokens: 1000, total_cost: 1, total_messages: 2 }));
    await advance(80);
    out.startOfNextCount = node('totalTokens').textContent;
    await advance(1600);
    out.settledNext = node('totalTokens').textContent;
  }

  if (scenario === 'count-up-frames-are-whole-numbers') {
    // Messages, and Tokens with readable tokens off, run their frames through
    // formatNumber. The animation value is a float, so a frame came out as
    // "74,745,113.682" — a string wider than the value the card was just fitted
    // to, and nonsense for a count of messages as well.
    overviewReadableTokens = false;
    renderOverviewTab(populated({
      total_tokens: 74745113, total_messages: 74745113, total_cost: 5,
    }));
    out.frames = [];
    for (let i = 0; i < 8; i += 1) {
      await advance(40);
      out.frames.push(node('totalMessages').textContent, node('totalTokens').textContent);
    }
  }

  if (scenario === 'the-empty-render-writes-the-card-once') {
    // renderOverviewTokenTotal owns the Tokens card, so the range tells it what
    // to show instead of writing "0" and correcting it on the next line. Record
    // every value the card actually receives.
    const el = node('totalTokens');
    const seen = [];
    let current = '';
    Object.defineProperty(el, 'textContent', {
      get() { return current; },
      set(value) { current = value; seen.push(value); },
    });
    renderOverviewTab(zeroPayload());
    await settle();
    out.writes = seen;
    out.final = current;
  }

  if (scenario === 'a-null-token-payload-is-not-animated-over') {
    // total_tokens: null with zero cost and messages is an empty range. The
    // null branch used to leave the previous range's total in
    // overviewTotalTokensRaw, and the count-up then animated it over the card.
    renderOverviewTab(populated({ total_cost: 12.5, total_messages: 300 }));
    await advance(300);
    renderOverviewTab(zeroPayload({ total_tokens: null }));
    out.rightAfter = node('totalTokens').textContent;
    await advance(1600);
    out.after = node('totalTokens').textContent;
  }

  process.stdout.write(JSON.stringify(out));
}

main();
"""

FUNCTIONS_UNDER_TEST = (
    "function overviewRangeIsEmpty(data) {",
    "function setOverviewCounterText(el, text, value) {",
    "function animateOverviewCounter(el, value, duration, format) {",
    "function renderOverviewTokenTotal(value = overviewTotalTokensRaw, staticText = null) {",
    "function renderOverviewTab(data) {",
    # A partial read is built here, not asserted from a hand-written flag: these
    # are the two functions that turn per-server rows into the response body the
    # Overview renders.
    "function usageSourceErrors(payload) {",
    "function reconcileUsageRows(rows, windowKey, servers = selectedServers(), cache = lastUsageRowsByServer) {",
    "function combineUsagePayloads(list) {",
)


def _run(tmp_path: Path, scenario: str) -> dict:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, signature) for signature in FUNCTIONS_UNDER_TEST
    )
    # `function` declarations cannot be reassigned under the module wrapper.
    functions = functions.replace(
        "function renderOverviewTab(data) {", "var renderOverviewTab = function (data) {", 1
    )
    harness = tmp_path / f"{scenario}.mjs"
    harness.write_text(
        HARNESS_HEAD.replace("__FUNCTIONS__", functions)
        .replace("__COUNTERS_PATH__", COUNTERS_JS.resolve().as_posix())
        .replace("__ANIME_PATH__", ANIME_JS.resolve().as_posix())
        + SCENARIOS,
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), scenario],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_stale_animation_cannot_overwrite_the_empty_state(tmp_path):
    out = _run(tmp_path, "stale-animation-cannot-overwrite-empty-state")

    assert out["midFlightValue"].endswith("M"), "the populated range must really be counting"
    assert out["rightAfterEmptyRender"] == "No data"
    assert out["costRightAfter"] == "No data"
    assert out["messagesRightAfter"] == "No data"
    assert out["afterStaleAnimationWouldHaveFinished"] == "No data", (
        "the cancelled count-up for the previous range must not paint over the empty state"
    )
    assert out["costAfter"] == "No data"
    assert out["messagesAfter"] == "No data"


def test_cancelling_a_counter_actually_stops_the_animation(tmp_path):
    """The identity guards are not a substitute for stopping the engine.

    The guards in onUpdate/onComplete keep a superseded animation from writing,
    so the text looks right either way — but the animation itself stayed
    registered with the engine for its whole 1.2s duration, on every KPI card,
    for every range switch. That is the difference between cancelling and merely
    ignoring, and only the engine knows the difference.
    """
    out = _run(tmp_path, "cancelling-actually-stops-the-animation")

    assert out["registeredWhileAnimating"], "the counter must really be animating"
    assert out["stopped"], (
        "cancelCounter() left the animation registered with the engine; it called "
        "pause() on an Animatable, which has no pause method, and swallowed the "
        "TypeError"
    )
    assert out["liveValueOnCancel"], (
        "cancelling must hand the live value back, or a re-render mid-flight "
        "resumes from the last completed total and visibly jumps"
    )


def test_the_engine_probe_is_pinned_to_the_bundled_anime():
    """engine._head is a private field, so the pin has to live somewhere.

    The cancellation test above reads anime's linked list directly. If anime is
    ever upgraded, that read could start meaning something else — or nothing —
    and the test would go quietly green. This one goes loudly.
    """
    source = ANIME_JS.read_text(encoding="utf-8")

    assert 'version:"4.0.0"' in source, (
        "the bundled anime is no longer 4.0.0: re-check that engine._head still "
        "means 'an instance is registered with the engine' before bumping this pin"
    )


def test_large_values_are_measured_at_their_final_width(tmp_path):
    out = _run(tmp_path, "large-values-are-measured-at-their-final-width")

    assert out["costAtFit"] == out["expectedCost"], (
        "the Cost card must hold its final text before fitOverviewKpis() "
        "measures it, or a large value is never shrunk and stays clipped"
    )
    assert out["messagesAtFit"] == out["expectedMessages"], (
        "the Messages card must hold its final text before the fit measures it"
    )
    assert out["costSettled"] == out["expectedCost"]
    assert out["messagesSettled"] == out["expectedMessages"]


def test_a_null_hit_rate_cancels_the_in_flight_count_up(tmp_path):
    out = _run(tmp_path, "a-null-hit-rate-cancels-the-in-flight-count-up")

    assert out["hitRightAfter"] == "n/a"
    assert out["hitAfter"] == "n/a", (
        "the previous range's count-up finished on top of the static n/a"
    )


def test_an_empty_range_agrees_across_the_whole_row(tmp_path):
    out = _run(tmp_path, "empty-range-agrees-across-the-row")

    assert out["tokens"] == "No data"
    assert out["cost"] == "No data"
    assert out["messages"] == "No data"
    assert out["topModel"] == "—"


def test_unpriced_but_populated_keeps_the_free_state(tmp_path):
    out = _run(tmp_path, "unpriced-but-populated-keeps-free")

    assert out["tokens"] == "5.2M", "tokens were recorded; this is not an empty range"
    assert out["cost"] == "FREE", "$0 with real tokens means unpriced models, not missing data"
    assert out["messages"] == "300"


def test_an_incomplete_read_shows_the_numbers_it_has(tmp_path):
    """A short read is a partial answer, not an absent one.

    One server with source errors, or one machine of two that cannot be reached
    at all, sets _has_incomplete_server_rows — and the totals that did come back
    are still the honest sum of what Tokdash can see. Replacing them with a dash
    on every card would hide real usage behind a symbol that means "unknown",
    which is worse than the 0 it replaced: a dash cannot be compared, summed, or
    believed.
    """
    out = _run(tmp_path, "an-incomplete-read-shows-the-numbers-it-has")

    assert out["flaggedIncomplete"], "the fixture must really produce an incomplete read"
    assert out["degradedCards"] == {"tokens": "5.2M", "cost": "$12.50", "messages": "300"}
    assert out["secondServerUnavailable"], "the unreachable server must land in unavailable"
    assert out["partialCards"] == {"tokens": "1.0k", "cost": "$2.25", "messages": "40"}


def test_an_incomplete_read_with_zero_totals_does_not_say_no_data(tmp_path):
    """Zeros the page cannot vouch for are not a claim that the range is empty."""
    out = _run(tmp_path, "an-incomplete-read-with-zero-totals-is-not-no-data")

    assert out["flaggedIncomplete"]
    assert "No data" not in out["cards"].values(), (
        f"an unvouched zero rendered as a proven-empty range: {out['cards']}"
    )


def test_a_failed_refresh_keeps_the_empty_state(tmp_path):
    out = _run(tmp_path, "a-failed-refresh-keeps-the-empty-state")

    assert out["beforeFailure"] == "No data"
    assert out["afterFailedRefresh"] == "No data", (
        "_server_rows_stale marks the Servers tab's rows as stale, not this "
        "range as incomplete; one failed auto-refresh must not repaint an empty "
        "range as 0 / FREE / 0"
    )
    assert out["tokensAfterFailedRefresh"] == "No data"


def test_a_rerender_mid_count_resumes_from_the_number_on_screen(tmp_path):
    """Cost went $1935.30 -> $2000.00 -> $1528.06 on every language switch.

    The resume point lives in the running animation, and cancelCounter() is what
    copies it onto the element. Reading _currentValue before the cancel reads the
    last total that *finished*, so a re-render mid-count restarted the climb from
    the bottom — or from zero outright, when nothing had finished yet.
    """
    out = _run(tmp_path, "a-rerender-mid-count-resumes-from-the-live-value")

    live = float(out["liveBefore"].replace("$", ""))
    assert 1000 < live < 4500, (
        f"the first count-up must still be running when the range re-renders: {out}"
    )
    resumed = float(out["shortlyAfter"].replace("$", ""))
    assert resumed > live * 0.6, (
        f"the re-render restarted from the last completed total instead of the live "
        f"value: {out['liveBefore']} -> {out['shortlyAfter']}"
    )
    assert out["settled"] == "$4000.00"


def test_a_range_without_a_token_total_does_not_count_down_from_the_old_one(tmp_path):
    """A dash leaves nothing to count from.

    The null branch of renderOverviewTokenTotal writes the card through
    setCounterText, and a call without a value leaves el._currentValue on the
    previous range's total. The next range then animated down from 5.2M, and the
    old total itself stayed live for any caller that reached the count-up first.
    """
    out = _run(tmp_path, "a-range-without-a-token-total-does-not-count-down")

    assert out["settledFirst"] == "5,200,000"
    assert out["rightAfter"] == "-"
    assert out["duringNullRange"] == "-", (
        "the previous range's token total was still live and animated over the dash"
    )
    assert float(out["startOfNextCount"].replace(",", "")) < 1000, (
        f"the next range counted down from the old total: {out['startOfNextCount']}"
    )
    assert out["settledNext"] == "1,000"


def test_count_up_frames_are_whole_numbers(tmp_path):
    """Nobody counted 0.682 of a message."""
    out = _run(tmp_path, "count-up-frames-are-whole-numbers")

    frames = out["frames"]
    assert any(frame != "74,745,113" for frame in frames), (
        "the cards never got caught mid-count, so the run proves nothing"
    )
    fractional = [frame for frame in frames if "." in frame]
    assert not fractional, f"a count-up frame rendered a fraction: {fractional[:4]}"
    assert all(int(frame.replace(",", "")) >= 0 for frame in frames)


def test_the_empty_render_writes_the_tokens_card_once(tmp_path):
    """One write, not "0" followed by a correction.

    renderOverviewTokenTotal owns the Tokens card, its tooltip and its aria
    wiring, so the range hands it the text to show. Writing the number first and
    correcting it on the next line also left a stray "0" in the DOM for anything
    observing the card mid-render.
    """
    out = _run(tmp_path, "the-empty-render-writes-the-card-once")

    assert "0" not in out["writes"], (
        f'the card was written "0" before being corrected: {out["writes"]}'
    )
    assert out["writes"] == ["No data"], (
        f"the empty range should write the card exactly once: {out['writes']}"
    )
    assert out["final"] == "No data"


def test_a_null_token_payload_is_not_animated_over(tmp_path):
    """A null total with nothing else in the range is an empty range.

    The dash a null total writes is not a number to animate, so the row holds
    "No data" through the duration of the range before it. What the null total
    leaves in the counter base is the other half, covered on a non-empty range
    by test_a_range_without_a_token_total_does_not_count_down_from_the_old_one.
    """
    out = _run(tmp_path, "a-null-token-payload-is-not-animated-over")

    assert out["rightAfter"] == "No data"
    assert out["after"] == "No data", (
        "a null token total did not read as an empty range, so the row fell back "
        "to 0 / FREE / 0 over a range that has nothing in it"
    )


def test_the_next_range_counts_from_the_value_on_screen(tmp_path):
    out = _run(tmp_path, "next-range-counts-from-the-value-on-screen")

    assert out["costBaseAfterEmpty"] == 0, (
        "the Cost counter base must follow the value actually shown; otherwise "
        "the next range counts up from the range before it"
    )
    assert out["messagesBaseAfterEmpty"] == 0
    # Counted a moment into a 1.2s animation, so this is a partial value. The
    # previous range showed 12.5, so anything far below that proves the new
    # count-up started from 0 rather than resuming from the stale total.
    assert float(out["recountCost"].replace("$", "")) < 5, (
        f"the recount resumed from a stale total instead of 0: {out['recountCost']}"
    )
    assert float(out["recountMessages"].replace(",", "")) < 100, (
        f"the Messages recount resumed from a stale total: {out['recountMessages']}"
    )
