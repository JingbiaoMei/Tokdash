"""An Overview range must read as one consistent state, not per-metric zeros.

Seven ways this regresses, none of which string assertions on the page can see:

- a stale count-up finishing over a freshly rendered value. anime.js hands back
  different objects from createAnimatable() and animate(), and only one of them
  can actually be cancelled, so the wrong verb silently threw and every
  superseded animation ran to the end of its duration;
- Cost/Messages updating only from animation callbacks, so fitOverviewKpis()
  measured the previous text and clipped large values, and a background tab —
  where anime pauses its engine — kept the old range's numbers;
- a failed read dressing up as an empty range, and an empty range unpainting
  itself after a failed refresh, because the wrong flags were consulted;
- a failed read showing $0 "FREE", which tells the user their usage cost
  nothing when in fact the read failed;
- a "n/a" hit rate painted over by the previous range's count-up;
- the next range counting up from a value that was never on screen.

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

  if (scenario === 'broken-read-does-not-claim-empty-or-free') {
    // combineUsagePayloads builds the response from row.payload, so _partial —
    // which reconcileUsageRows puts on the row wrappers — never reaches here.
    // _has_incomplete_server_rows is the flag production actually sets.
    renderOverviewTab(zeroPayload({
      range: 'BROKEN',
      _has_incomplete_server_rows: true,
      _source_errors: ['usage.jsonl'],
      by_tool: { codex: { tokens: 0, cost: 0 } },
    }));
    await advance(1400);
    out.tokens = node('totalTokens').textContent;
    out.cost = node('totalCost').textContent;
    out.messages = node('totalMessages').textContent;
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
    "function overviewRangeIsBroken(data) {",
    "function overviewRangeIsEmpty(data) {",
    "function setOverviewCounterText(el, text, value) {",
    "function animateOverviewCounter(el, value, duration, format) {",
    "function renderOverviewTokenTotal(value = overviewTotalTokensRaw, staticText = null) {",
    "function renderOverviewTab(data) {",
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


def test_a_broken_read_neither_claims_empty_nor_free(tmp_path):
    """A failed read is neither an empty range nor a free one.

    "FREE" tells the user their usage cost nothing when the read actually failed,
    which is precisely the case this feature exists to tell apart from an empty
    range. A dash is the only value that is not a claim.
    """
    out = _run(tmp_path, "broken-read-does-not-claim-empty-or-free")

    assert out["tokens"] == "—", "a failed read must not state there are no tokens"
    assert out["cost"] == "—", 'a failed read must not report $0 "FREE"'
    assert out["messages"] == "—"


def test_a_failed_refresh_keeps_the_empty_state(tmp_path):
    out = _run(tmp_path, "a-failed-refresh-keeps-the-empty-state")

    assert out["beforeFailure"] == "No data"
    assert out["afterFailedRefresh"] == "No data", (
        "_server_rows_stale marks the Servers tab's rows as stale, not this "
        "range as incomplete; one failed auto-refresh must not repaint an empty "
        "range as 0 / FREE / 0"
    )
    assert out["tokensAfterFailedRefresh"] == "No data"


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
    out = _run(tmp_path, "a-null-token-payload-is-not-animated-over")

    assert out["rightAfter"] == "No data"
    assert out["after"] == "No data", (
        "a null token payload left the previous range's total in "
        "overviewTotalTokensRaw, and the count-up animated it over the card"
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
