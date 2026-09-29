"""An Overview range must read as one consistent state, not per-metric zeros.

Four ways this regresses, none of which string assertions can see:

- a stale count-up finishing over a freshly rendered empty state. animateNumber
  builds a new animation per call and used to never cancel the previous one, so
  switching from a populated range to an empty one left "5.2M" on the card;
- the $0 "FREE" cost state being swallowed by a "no cost data" branch, even
  though tokens were recorded and every model is simply unpriced;
- a broken read (source errors) dressing up as an empty range, because
  reconciled totals are zeros too;
- a later range counting up from a stale previous value instead of from the
  value actually on screen.

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
function fitKpiValue() {}
function fitOverviewKpis() {}
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
const { animateNumber, cancelCounter } = await import(
  pathToFileURL('__COUNTERS_PATH__').href
);
window.TokDashAnimations = { animateNumber, cancelCounter };

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

  const settle = () => new Promise((resolve) => setTimeout(resolve, 5));
  const advance = async (ms) => { await new Promise((resolve) => setTimeout(resolve, ms)); };

  if (scenario === 'stale-animation-cannot-overwrite-empty-state') {
    // A populated range is rendered (starting real 1.2s count-ups), then an
    // empty range lands while those animations are still running. The empty
    // render must win: no in-flight onUpdate may paint over "No data".
    renderOverviewTab({
      range: 'X', total_tokens: 5200000, total_cost: 12.5, total_messages: 300,
      cache_hit_rate: 0.4, by_tool: {}, top_models: [{ name: 'gpt', cost: 12.5, tokens: 5200000 }],
    });
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
    renderOverviewTokenTotal(5200000);
    node('totalCost')._currentValue = 12.5;
    renderOverviewTab(zeroPayload({
      range: 'FREE', total_tokens: 5200000, total_messages: 300,
    }));
    await advance(1400);
    out.tokens = node('totalTokens').textContent;
    out.cost = node('totalCost').textContent;
    out.messages = node('totalMessages').textContent;
  }

  if (scenario === 'broken-read-does-not-claim-empty') {
    // Source errors reconcile to zero totals; the cards must fall through to
    // their numbers instead of stating "No data".
    renderOverviewTab(zeroPayload({
      range: 'BROKEN', _partial: true,
      _source_errors: ['usage.jsonl'],
      by_tool: { codex: { tokens: 0, cost: 0 } },
    }));
    await advance(1400);
    out.tokens = node('totalTokens').textContent;
    out.cost = node('totalCost').textContent;
    out.messages = node('totalMessages').textContent;
  }

  if (scenario === 'next-range-counts-from-the-value-on-screen') {
    // A zero render resets the counter base: the next populated range must not
    // resume from a stale previous value.
    renderOverviewTokenTotal(5200000);
    await settle();
    renderOverviewTokenTotal(0);
    out.zeroShown = node('totalTokens').textContent;
    out.baseAfterZero = node('totalTokens')._currentValue;
    renderOverviewTokenTotal(1000);
    await settle();
    out.recountShown = node('totalTokens').textContent;
  }

  process.stdout.write(JSON.stringify(out));
}

main();
"""


def _run(tmp_path: Path, scenario: str) -> dict:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, signature)
        for signature in (
            "function overviewRangeIsEmpty(data) {",
            "function cancelOverviewCounterAnimation(el) {",
            "function renderOverviewTokenTotal(value = overviewTotalTokensRaw) {",
            "function renderOverviewTab(data) {",
        )
    )
    # `function` declarations cannot be reassigned under the module wrapper.
    functions = functions.replace(
        "function renderOverviewTab(data) {", "var renderOverviewTab = function (data) {", 1
    )
    harness = tmp_path / f"{scenario}.mjs"
    harness.write_text(
        HARNESS_HEAD.replace("__FUNCTIONS__", functions).replace(
            "__COUNTERS_PATH__", COUNTERS_JS.resolve().as_posix()
        )
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


def test_a_broken_read_does_not_claim_the_range_is_empty(tmp_path):
    out = _run(tmp_path, "broken-read-does-not-claim-empty")

    assert out["tokens"] == "0", "a failed read must fall through to its numbers"
    assert out["cost"] == "FREE"
    assert out["messages"] == "0"


def test_the_next_range_counts_from_the_value_on_screen(tmp_path):
    out = _run(tmp_path, "next-range-counts-from-the-value-on-screen")

    assert out["zeroShown"] == "0"
    assert out["baseAfterZero"] == 0, "the counter base must follow the zero actually shown"
    assert out["recountShown"] == "1.0k", "a small recount must not get stuck at the old total"
