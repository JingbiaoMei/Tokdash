"""Overview sparklines must only ever draw what the loaded responses can prove.

The six KPI cards used to carry fixed decorative paths. The replacement reads the raw
per-server Usage and Stats payloads the dashboard already fetches, which puts every
one of these risks in play at once:

- a curve drawn for a range, server set, or in-flight generation that is not the one
  the headline belongs to, so two different answers share a card;
- a missing field read as a recorded zero, or an unreconciled daily fold rescaled
  until it agrees with the card;
- a cache rate averaged from daily percentages instead of summed numerators, which
  turns a 1/1 day beside a 0/9 day into 50% instead of 10%;
- the Top Model curve switching to whichever model won each day, or to a value the
  card never named;
- a curve bought with a request, which is the whole cost this feature is supposed to
  avoid;
- a smoothed path that overshoots a neighbour, dips under its zero baseline, or emits
  NaN for a gap, a single anchor, or a flat series;
- a date outside the Stats response's own coverage -- a payload computed yesterday
  cannot have measured today -- promoted into a measured zero, or a curve carried over
  from an old read billed as fresh;
- an explicit `null` in a payload coerced into a measured zero, or two per-server
  rounding errors that each fit the card's tolerance and then add up past it;
- a guard written against a field the API has never sent, which passes every
  invented fixture and then clears the real payload -- so one case runs a response
  captured from a running build, `fixtures/overview_sparklines/live_contract.json`.

These run the real module, the real `normalizeModelName`, the real `t()` and the real
English dictionary, all imported out of `static/index.html` against a stub DOM, so a
regression has to be in the shipped code rather than in a stand-in.
Completion-order cases run the real async loaders in a second script, so the shared
harness stays free of the names those loaders stub.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"
MODULE_START = "// ===== Overview sparklines"
LIVE_CONTRACT = Path(__file__).parent / "fixtures" / "overview_sparklines" / "live_contract.json"
MODULE_END = "    function renderOverviewTab(data) {"
METRICS = ("tokens", "cost", "messages", "agent_time", "cache_hit_rate", "top_model")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _extract_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start >= 0, f"{signature} not found in static/index.html"
    # Skip the parameter list: a default such as `values = []` puts braces inside the
    # parentheses, and counting those as the body truncates the function at the default.
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


def _extract_module(src: str) -> str:
    start = src.find(MODULE_START)
    assert start >= 0, "sparkline module marker not found"
    end = src.find(MODULE_END, start)
    assert end > start, "sparkline module end marker not found"
    return src[start:end]


def _extract_i18n_en(src: str) -> str:
    start = src.find("const I18N = {")
    assert start != -1, "const I18N = { not found"
    head = "\n      en: {"
    end = src.find(head, start)
    assert end > start
    stop = src.find("\n      },", end)
    assert stop > end
    return "const I18N = { en: {" + src[end + len(head) : stop] + "\n      },\n    };"


HARNESS = r"""
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) {
    const el = {
      id, textContent: '', attrs: {}, children: [], firstChild: null,
      setAttribute(name, value) { el.attrs[name] = String(value); },
      getAttribute(name) { return el.attrs[name]; },
      appendChild(child) { el.children.push(child); el.firstChild = el.children[0]; return child; },
      insertBefore(child, ref) { el.children.unshift(child); el.firstChild = el.children[0]; return child; },
    };
    nodes.set(id, el);
  }
  return nodes.get(id);
}
const document = {
  getElementById: (id) => node(id),
  createElementNS: (_ns, name) => ({ tagName: name, textContent: '' }),
};
// A curve paid for with a request is the failure this feature is not allowed to make,
// so every render is watched for one.
let fetchCalls = 0;
globalThis.fetch = () => { fetchCalls += 1; return Promise.reject(new Error('no request allowed')); };

let currentLang = 'en';
let currentStartDate = null;
let currentEndDate = null;
let lastUsageResponse = null;
const lastWindowKey = null;
function selectedServers() { return [{ id: 'local' }]; }
function usageServerKeyFor(servers = selectedServers()) {
  return servers.map((s) => String(s.id)).sort().join(',');
}
function isOverviewActive() { return true; }

__I18N__
__T__
__DATE_KEYS__
__NORMALIZE__
__SOURCE_ERRORS__
__MODULE__

// --- fixtures: existing response shapes, synthetic values -------------------------
const DAY_KEYS = ['input', 'output', 'cacheRead', 'cacheWrite', 'reasoning'];
function makeDay(date, spec = {}) {
  const day = { date, totals: {}, tokenBreakdown: {}, sources: [] };
  for (const [key, value] of Object.entries(spec.totals || {})) day.totals[key] = value;
  for (const key of DAY_KEYS) {
    if (spec.breakdown && key in spec.breakdown) day.tokenBreakdown[key] = spec.breakdown[key];
  }
  for (const model of spec.models || []) {
    day.sources.push({
      source: 'fixture', providerId: 'fixture', modelId: model.name,
      tokens: model.tokens, cost: model.cost || 0, messages: model.messages || 0,
    });
  }
  return day;
}
function fullTokens(input = 0, output = 0, cacheRead = 0) {
  return { input, output, cacheRead, cacheWrite: 0, reasoning: 0 };
}
function usagePayload(spec = {}) {
  return {
    range: spec.range || { from: '2026-09-21', to: '2026-09-27' },
    total_tokens: spec.tokens ?? 0,
    total_cost: spec.cost ?? 0,
    total_messages: spec.messages ?? 0,
    cache_hit_rate: 'cache' in spec ? spec.cache : null,
    // Field set as the API actually answers: a failed source arrives as a list of
    // names, and there is no `source_error_count` on any response. An earlier draft
    // of this builder invented that count, the module believed it, and the payload
    // the dashboard really receives drew nothing at all -- see `liveContract`.
    source_errors: spec.sourceErrors || [],
    timestamp: spec.timestamp || '2026-09-28T09:00:00',
    top_models: spec.topModels || [],
    combined_models: spec.combinedModels || spec.topModels || [],
  };
}

// One scenario: the raw per-server rows a real load would have captured, plus the
// window and selection that reference has to match before it may be drawn.
function scenario(rows, usage, context = {}) {
  sparklineUsageRef = null;
  sparklineStatsRef = null;
  const servers = context.servers || rows.map((row) => ({ id: row.serverId }));
  const accepted = rows.map((row) => ({
    server: { id: row.serverId }, payload: row.usage,
    _retained: !!row.retained, _partial: !!row.partial,
  }));
  const combined = usage.combined || usage;
  sparklineCaptureUsageRows(accepted, combined, {
    windowKey: context.windowKey || 'p:test',
    serverKey: context.serverKey || servers.map((s) => s.id).sort().join(','),
    expectedServers: context.expectedServers ?? servers.length,
    retained: context.retained ?? 0,
    unavailable: context.unavailable ?? 0,
  });
  sparklineCaptureStatsRows(rows.map((row) => ({ server: { id: row.serverId }, payload: row.stats })), {
    serverKey: context.statsServerKey ?? (context.serverKey || servers.map((s) => s.id).sort().join(',')),
  });
  lastUsageResponse = combined;
  currentStartDate = context.start instanceof Date ? context.start : parseDateKey(context.start || '2026-09-21');
  currentEndDate = context.end instanceof Date ? context.end : parseDateKey(context.end || '2026-09-27');
  const today = context.today instanceof Date ? context.today : parseDateKey(context.today || '2026-09-27');
  return {
    fetchCalls,
    curves: deriveSparklineCurves(combined, { today }),
  };
}

// A compatible single-server week: seven dated rows that add up to the headline.
function weekFixture(overrides = {}) {
  const perDay = overrides.perDay || [10, 20, 30, 40, 50, 60, 70];
  const total = perDay.reduce((a, b) => a + b, 0);
  const costPerDay = overrides.costPerDay || perDay.map((v) => v / 100);
  const cost = overrides.cost !== undefined ? overrides.cost : Number(costPerDay.reduce((a, b) => a + b, 0).toFixed(4));
  const messages = overrides.messages !== undefined ? overrides.messages : perDay.length * 2;
  const statsMessages = overrides.statsMessages !== undefined ? overrides.statsMessages : messages;
  const model = overrides.model || 'model-a';
  const days = [];
  let left = statsMessages;
  perDay.forEach((tokens, index) => {
    const date = `2026-09-${21 + index}`;
    const isLast = index === perDay.length - 1;
    const messagesToday = isLast ? left : Math.min(2, left);
    left -= messagesToday;
    const models = [{ name: model, tokens: fullTokens(tokens, 0, 0), cost: costPerDay[index], messages: messagesToday }];
    if (overrides.extraModels) models.push({ name: overrides.extraModels[index] || 'model-b', tokens: fullTokens(1, 1, 1) });
    const breakdown = overrides.breakdown === false
      ? { output: 0, reasoning: 0 }
      : (overrides.breakdownPerDay ? overrides.breakdownPerDay[index] : { input: tokens, cacheRead: 0, cacheWrite: 0, output: 0, reasoning: 0 });
    days.push(makeDay(date, {
      totals: { tokens, cost: costPerDay[index], messages: messagesToday },
      breakdown,
      models,
      missingTotals: overrides.missingTotals || [],
    }));
  });
  if (overrides.dropDates) {
    for (const date of overrides.dropDates) {
      const at = days.findIndex((d) => d.date === date);
      if (at >= 0) days.splice(at, 1);
    }
  }
  const usage = usagePayload({
    tokens: overrides.usageTokens !== undefined ? overrides.usageTokens : total,
    cost,
    messages,
    cache: overrides.cache === null ? null : (overrides.cache !== undefined ? overrides.cache : (total > 0 ? 0 : null)),
    topModels: overrides.topModels !== undefined ? overrides.topModels : [{ name: model, tokens: overrides.modelTokens !== undefined ? overrides.modelTokens : total, cost }],
    timestamp: overrides.timestamp,
    sourceErrors: overrides.sourceErrors,
  });
  return { rows: [{ serverId: 'local', usage, stats: { contributions: days } }], usage, context: overrides.context || {} };
}

function metricView(curves) {
  const out = {};
  for (const id of __METRIC_IDS__) {
    const series = curves.metrics[id] || {};
    const shape = sparklineShape(series.values || [], id === 'cache_hit_rate' ? 'ratio' : 'count');
    out[id] = {
      reason: series.reason || '',
      points: series.points || 0,
      values: series.values || [],
      line: shape.line,
      area: shape.area,
    };
  }
  return out;
}

function paintedView(curves, today = '2026-09-27') {
  const out = {};
  const before = fetchCalls;
  renderOverviewSparklines(lastUsageResponse, { today: parseDateKey(today) });
  for (const card of SPARKLINE_CARDS) {
    const svg = node(card.svg);
    out[card.id] = {
      line: node(`${card.svg}Path`).attrs.d,
      area: node(`${card.svg}Area`).attrs.d,
      aria: svg.attrs['aria-label'],
      role: svg.attrs.role,
      points: svg.attrs['data-points'],
      reason: svg.attrs['data-reason'],
      title: svg.__sparklineTitle ? svg.__sparklineTitle.textContent : null,
    };
  }
  out.__fetches = fetchCalls - before;
  return out;
}

const results = {};
const en = I18N.en;
function run(name, fn) { results[name] = fn(); }

// 1. a compatible week draws five curves and leaves agent time honest
run('baseline', () => {
  const f = weekFixture();
  const { curves, fetches } = Object.assign(scenario(f.rows, f.usage, f.context), {});
  return { metrics: metricView(curves), dates: curves.dates, recordedOn: curves.recordedOn, fetches: fetchCalls };
});

// 2. messages that do not reconcile drop only the messages curve
run('messageMismatch', () => {
  const f = weekFixture({ messages: 15, statsMessages: 3 });
  const { curves } = scenario(f.rows, f.usage, f.context);
  return metricView(curves);
});

// 3. cost tolerance: the card prints two decimals, so that is the agreement it may claim
run('costTolerance', () => {
  const near = weekFixture({ cost: 1.0549, costPerDay: [0.0007, 0.0007, 0.0007, 0.0007, 0.0007, 0.0007, 1.0507] });
  const nearMetrics = metricView(scenario(near.rows, near.usage, near.context).curves);
  const far = weekFixture({ cost: 1.08, costPerDay: [0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 1.05] });
  const farMetrics = metricView(scenario(far.rows, far.usage, far.context).curves);
  return { near: nearMetrics.cost, far: farMetrics.cost, nearTokens: nearMetrics.tokens.reason };
});

// 4. cache: summed, never averaged; and a mismatched headline is refused
run('cacheWeighting', () => {
  const mixed = weekFixture({
    perDay: [10, 90, 0, 0, 0, 0, 0],
    breakdownPerDay: [
      { input: 0, cacheRead: 1, cacheWrite: 0, output: 0, reasoning: 0 },
      { input: 9, cacheRead: 0, cacheWrite: 0, output: 0, reasoning: 0 },
      { input: 0, cacheRead: 0, cacheWrite: 0, output: 0, reasoning: 0 },
      { input: 0, cacheRead: 0, cacheWrite: 0, output: 0, reasoning: 0 },
      { input: 0, cacheRead: 0, cacheWrite: 0, output: 0, reasoning: 0 },
      { input: 0, cacheRead: 0, cacheWrite: 0, output: 0, reasoning: 0 },
      { input: 0, cacheRead: 0, cacheWrite: 0, output: 0, reasoning: 0 },
    ],
    cache: 0.1,
    modelTokens: 100,
  });
  const summed = metricView(scenario(mixed.rows, mixed.usage, mixed.context).curves).cache_hit_rate;
  const averaged = weekFixture({
    perDay: [10, 90, 0, 0, 0, 0, 0],
    breakdownPerDay: mixed.rows[0].stats.contributions.map((d) => d.tokenBreakdown),
    cache: 0.5,
    modelTokens: 100,
  });
  const averagedMetrics = metricView(scenario(averaged.rows, averaged.usage, averaged.context).curves);
  const noInput = weekFixture({ perDay: [1, 2, 3, 4, 5, 6, 7], breakdownPerDay: null, cache: null, modelTokens: 28 });
  const noInputRows = noInput.rows.map((row) => ({
    serverId: row.serverId, usage: row.usage,
    stats: { contributions: row.stats.contributions.map((d) => makeDay(d.date, { totals: d.totals, breakdown: { cacheWrite: 0 } })) },
  }));
  const noInputMetrics = metricView(scenario(noInputRows, noInput.usage, noInput.context).curves);
  return { summed, averagedCache: averagedMetrics.cache_hit_rate, averagedTokens: averagedMetrics.tokens.reason, noInput: noInputMetrics.cache_hit_rate };
});

// 5. missing or foreign-shaped cache components are unknown, never 0%
run('cacheComponents', () => {
  const noBreakdown = weekFixture({ breakdown: false });
  const missing = metricView(scenario(noBreakdown.rows, noBreakdown.usage, noBreakdown.context).curves).cache_hit_rate;
  const writesIntoInput = weekFixture({
    breakdownPerDay: Array.from({ length: 7 }, (_, i) => ({ input: 10 * (i + 1), cacheRead: 0, cacheWrite: 5, output: 0, reasoning: 0 })),
    cache: 0,
  });
  const writes = metricView(scenario(writesIntoInput.rows, writesIntoInput.usage, writesIntoInput.context).curves).cache_hit_rate;
  return { missing, writes };
});

// 6. the Top Model curve follows the card's winner on every date, not each day's winner
run('fixedModel', () => {
  const switchy = weekFixture({ extraModels: true, modelTokens: 280 });
  const drawn = metricView(scenario(switchy.rows, switchy.usage, switchy.context).curves).top_model;
  const wrongTotal = weekFixture({ modelTokens: 999 });
  const refused = metricView(scenario(wrongTotal.rows, wrongTotal.usage, wrongTotal.context).curves).top_model;
  const absent = weekFixture({ topModels: [{ name: 'never-recorded', tokens: 1000 }] });
  const noRows = metricView(scenario(absent.rows, absent.usage, absent.context).curves).top_model;
  const canonical = weekFixture({
    model: 'Claude Opus 4-5',
    topModels: [{ name: 'claude-opus-4-5', tokens: 280 }],
  });
  const canonicalMetrics = metricView(scenario(canonical.rows, canonical.usage, canonical.context).curves);
  const noSources = weekFixture();
  noSources.rows[0].stats.contributions.forEach((day) => { delete day.sources; });
  const shapeless = metricView(scenario(noSources.rows, noSources.usage, noSources.context).curves).top_model;
  return {
    drawn, refused, noRows, shapeless,
    canonical: canonicalMetrics.top_model, canonicalTokens: canonicalMetrics.tokens.reason,
  };
});

// 7. gaps split the line; an absent date inside proven coverage is a recorded zero
run('gapsAndZeros', () => {
  const dropped = weekFixture({ perDay: [10, 20, 30, 0, 50, 60, 70], dropDates: ['2026-09-24'] });
  const zero = metricView(scenario(dropped.rows, dropped.usage, dropped.context).curves).tokens;
  const hole = weekFixture({ perDay: [10, 20, 30, 40, 50, 60, 70] });
  hole.rows[0].stats.contributions[3] = { date: '2026-09-24', totals: { cost: 0.4 } };
  hole.usage = usagePayload({ tokens: 10 + 20 + 30 + 50 + 60 + 70, cost: hole.usage.total_cost, messages: 14, cache: 0, topModels: [{ name: 'model-a', tokens: 240 - 40 }] });
  hole.rows[0].stats.contributions.forEach((d, i) => {
    if (d.date !== '2026-09-24') d.sources = [{ source: 'f', modelId: 'model-a', tokens: fullTokens(d.totals.tokens, 0, 0) }];
  });
  const unreconcilable = metricView(scenario(hole.rows, hole.usage, hole.context).curves).tokens;
  const split = sparklineShape([1, 2, null, null, 5, null, 7], 'count');
  return {
    zero,
    unreconcilable,
    splitLineSegments: split.line.split('M').length - 1,
    splitAreaSegments: split.area.split('M').length - 1,
  };
});

// 8. shape integrity: no NaN, no overshoot, no negative baseline, safe edge cases
run('shape', () => {
  const spike = weekFixture({ perDay: [0, 900, 0, 900, 0, 900, 0] });
  const spikeShape = sparklineShape([0, 900, 0, 900, 0, 900, 0], 'count');
  const flatShape = sparklineShape([5, 5, 5, 5, 5, 5, 5], 'count');
  const zeroShape = sparklineShape([0, 0, 0, 0, 0, 0, 0], 'count');
  const singleShape = sparklineShape([null, null, 7, null, null, null, null], 'count');
  const brokenShape = sparklineShape([null, null, null, null, null, null, null], 'count');
  const ratioShape = sparklineShape([0.25, 1, 0.5, 0, 0.75, 0.1, 0.9], 'ratio');
  const numbers = (d) => (d.match(/-?\d+(?:\.\d+)?/g) || []).map(Number);
  const overshoot = (d) => {
    // control points must stay inside the y range of the segment they shape
    const segs = d.split('C').slice(1);
    return segs.some((chunk) => {
      const ys = numbers(chunk).filter((_, i) => i % 2 === 1);
      return ys.some((y) => y < Math.min(...ys) - 0.01 || y > Math.max(...ys) + 0.01);
    });
  };
  return {
    spike: { line: spikeShape.line, hasNaN: /NaN/.test(spikeShape.line), max: Math.max(...numbers(spikeShape.line).filter((_, i) => i % 2 === 1)) },
    flat: flatShape.line, flatArea: flatShape.area,
    zero: { line: zeroShape.line, area: zeroShape.area },
    single: { line: singleShape.line, area: singleShape.area },
    broken: brokenShape,
    ratio: { line: ratioShape.line, hasNaN: /NaN/.test(ratioShape.line) },
    anyNegativeY: numbers(spikeShape.line).filter((_, i) => i % 2 === 1).some((y) => y > 34.01 || y < 3.99),
    overshoot: overshoot(spikeShape.line),
  };
});

// 9. races: another range, another server set, or an uncommitted read paints nothing
run('races', () => {
  const ok = weekFixture();
  const good = metricView(scenario(ok.rows, ok.usage, ok.context).curves);

  const moved = weekFixture();
  const rangeMoved = metricView(scenario(moved.rows, moved.usage, { ...moved.context, end: '2026-09-26' }).curves);

  const longer = weekFixture();
  const longRange = scenario(longer.rows, longer.usage, { ...longer.context, start: '2026-09-14', end: '2026-09-30' });
  const longMetrics = metricView(longRange.curves);

  const shifted = weekFixture();
  const shiftedUsage = Object.assign({}, shifted.usage, { range: { from: '2026-09-22', to: '2026-09-28' } });
  const shiftedMetrics = metricView(scenario(
    [{ serverId: 'local', usage: shiftedUsage, stats: shifted.rows[0].stats }], shiftedUsage, shifted.context,
  ).curves);

  const otherServers = weekFixture();
  const mismatched = metricView(scenario(otherServers.rows, otherServers.usage, {
    ...otherServers.context, statsServerKey: 'other-host',
  }).curves);

  const neverLoaded = weekFixture();
  const s = scenario(neverLoaded.rows, neverLoaded.usage, neverLoaded.context);
  sparklineUsageRef = { ...sparklineUsageRef, combined: { not: 'the rendered payload' } };
  const uncommitted = metricView(deriveSparklineCurves(neverLoaded.usage, { today: parseDateKey('2026-09-27') }));

  const today = weekFixture();
  const todayOnly = metricView(scenario(today.rows, today.usage, { ...today.context, start: '2026-09-27', end: '2026-09-27' }).curves);
  return {
    good: { tokens: good.tokens.reason, agent: good.agent_time.reason },
    rangeMoved: rangeMoved.tokens, longerRange: longMetrics.tokens, shifted: shiftedMetrics.tokens,
    otherServers: mismatched.tokens, uncommitted: uncommitted.tokens, todayOnly: todayOnly.tokens,
  };
});

// 10. incomplete scans and source failures cannot lend a curve
run('selection', () => {
  const partial = weekFixture();
  const retained = metricView(scenario(
    [{ serverId: 'local', usage: partial.usage, stats: partial.rows[0].stats, retained: true }],
    partial.usage, partial.context,
  ).curves);
  const flagged = weekFixture({ sourceErrors: ['codex: session scan failed'] });
  const errors = metricView(scenario(flagged.rows, flagged.usage, flagged.context).curves);
  const short = weekFixture();
  const shortScan = metricView(scenario(short.rows, short.usage, { ...short.context, unavailable: 1 }).curves);
  const noStats = weekFixture();
  const statsMissing = scenario(noStats.rows, noStats.usage, noStats.context);
  sparklineStatsRef = null;
  const missing = metricView(deriveSparklineCurves(noStats.usage, { today: parseDateKey('2026-09-27') }));
  return { retained, errors, shortScan, missing };
});

// 11. several servers: all must answer and all must reconcile, and raw counts sum first
run('multiServer', () => {
  const build = (id, offset, usageTweak) => {
    const perDay = [10, 20, 30, 40, 50, 60, 70].map((v) => v + offset);
    const total = perDay.reduce((a, b) => a + b, 0);
    const days = perDay.map((tokens, index) => makeDay(`2026-09-${21 + index}`, {
      totals: { tokens, cost: tokens / 100, messages: 2 },
      breakdown: { input: tokens, cacheRead: 0, cacheWrite: 0, output: 0, reasoning: 0 },
      models: [{ name: 'model-a', tokens: fullTokens(tokens, 0, 0) }],
    }));
    const usage = usagePayload({
      range: { from: '2026-09-21', to: '2026-09-27' }, tokens: total,
      cost: Number((total / 100).toFixed(4)), messages: 14, cache: 0,
      topModels: [{ name: 'model-a', tokens: total }],
    });
    return { serverId: id, usage: Object.assign(usage, usageTweak || {}), stats: { contributions: days } };
  };
  const both = [build('a', 0), build('b', 100)];
  const combinedUsage = usagePayload({
    tokens: both[0].usage.total_tokens + both[1].usage.total_tokens,
    cost: Number((both[0].usage.total_cost + both[1].usage.total_cost).toFixed(4)),
    messages: 28, cache: 0,
    topModels: [{ name: 'model-a', tokens: both[0].usage.total_tokens + both[1].usage.total_tokens }],
    combinedModels: [{ name: 'model-a', tokens: both[0].usage.total_tokens + both[1].usage.total_tokens }],
  });
  const rows = both.map((row) => ({ serverId: row.serverId, usage: row.usage, stats: row.stats }));
  const ok = metricView(scenario(rows, combinedUsage, { servers: [{ id: 'a' }, { id: 'b' }] }).curves);
  const okValues = ok.tokens.values;

  const partial = metricView(scenario(
    [{ serverId: 'a', usage: both[0].usage, stats: both[0].stats },
     { serverId: 'b', usage: both[1].usage, stats: null, skipStats: true }],
    combinedUsage, { servers: [{ id: 'a' }, { id: 'b' }] },
  ).curves);

  // The combined winner sits outside server b's returned model array, so b's share of
  // that model is unknown rather than zero and the curve cannot be summed honestly.
  const truncated = [
    { serverId: 'a', usage: both[0].usage, stats: both[0].stats },
    {
      serverId: 'b',
      usage: Object.assign({}, both[1].usage, {
        top_models: [{ name: 'b-local-only', tokens: both[1].usage.total_tokens }],
        combined_models: [{ name: 'b-local-only', tokens: both[1].usage.total_tokens }],
      }),
      stats: both[1].stats,
    },
  ];
  const truncatedModel = scenario(truncated, combinedUsage, { servers: [{ id: 'a' }, { id: 'b' }] })
    .curves.metrics.top_model.reason;

  const skewed = [
    { serverId: 'a', usage: both[0].usage, stats: both[0].stats },
    { serverId: 'b', usage: Object.assign({}, both[1].usage, { total_tokens: both[1].usage.total_tokens + 5 }), stats: both[1].stats },
  ];
  const disagree = metricView(scenario(skewed, combinedUsage, { servers: [{ id: 'a' }, { id: 'b' }] }).curves);
  return {
    okValues, okReason: ok.tokens.reason, partial: partial.tokens.reason,
    disagree: disagree.tokens.reason, truncatedModel,
    okModel: ok.top_model.reason, okCache: ok.cache_hit_rate.reason,
  };
});

// 12. nothing about this feature may make a request, in any state
run('noRequests', () => {
  const before = fetchCalls;
  const f = weekFixture();
  scenario(f.rows, f.usage, f.context);
  paintedView();
  const empty = weekFixture({ breakdown: false });
  scenario(empty.rows, empty.usage, empty.context);
  paintedView();
  sparklineUsageRef = null;
  sparklineStatsRef = null;
  paintedView();
  return { total: fetchCalls - before };
});

// 13. what actually reaches the DOM, including the honest empty states
run('painted', () => {
  const f = weekFixture({ timestamp: '2026-09-27T09:00:00' });
  scenario(f.rows, f.usage, f.context);
  const drawn = paintedView();
  const broken = weekFixture({ messages: 99, statsMessages: 3 });
  scenario(broken.rows, broken.usage, broken.context);
  const mismatched = paintedView();
  const today = weekFixture();
  scenario(today.rows, today.usage, { ...today.context, start: '2026-09-27', end: '2026-09-27' });
  const strict = paintedView();
  const stale = weekFixture({ timestamp: '2026-09-20T09:00:00' });
  scenario(stale.rows, stale.usage, stale.context);
  const stalePaint = paintedView();
  return { drawn, mismatched, strict, stalePaint, en };
});

// 14. the derivation must not bend its inputs to make a curve agree
run('purity', () => {
  const f = weekFixture({ extraModels: true });
  const snapshot = JSON.stringify({ rows: f.rows });
  const s = scenario(f.rows, f.usage, f.context);
  deriveSparklineCurves(f.usage, { today: parseDateKey('2026-09-27') });
  deriveSparklineCurves(f.usage, { today: parseDateKey('2026-09-27') });
  return { unchanged: JSON.stringify({ rows: f.rows }) === snapshot };
});

// 15. week dates: seven rolling local dates ending today, nothing else
run('window', () => {
  return {
    week: sparklineWeekDates(parseDateKey('2026-09-21'), parseDateKey('2026-09-27'), parseDateKey('2026-09-27')),
    short: sparklineWeekDates(parseDateKey('2026-09-25'), parseDateKey('2026-09-27'), parseDateKey('2026-09-27')),
    staleEnd: sparklineWeekDates(parseDateKey('2026-09-20'), parseDateKey('2026-09-26'), parseDateKey('2026-09-27')),
    long: sparklineWeekDates(parseDateKey('2026-09-01'), parseDateKey('2026-09-27'), parseDateKey('2026-09-27')),
    missing: sparklineWeekDates(null, null, parseDateKey('2026-09-27')),
  };
});

// 16. an unavailable field is unknown: null, blank and absent all stay unmeasured
run('unavailableFields', () => {
  const explicitNulls = weekFixture({ perDay: [0, 0, 0, 0, 0, 0, 0], messages: 0 });
  const first = explicitNulls.rows[0].stats.contributions[0];
  first.totals.tokens = null;
  first.totals.cost = null;
  first.totals.messages = null;
  const nulls = metricView(scenario(explicitNulls.rows, explicitNulls.usage, explicitNulls.context).curves);

  // The same zeros, untouched, are a measurement: withholding them would be its own lie.
  const untouched = weekFixture({ perDay: [0, 0, 0, 0, 0, 0, 0], messages: 0 });
  const control = metricView(scenario(untouched.rows, untouched.usage, untouched.context).curves);

  const blankCard = weekFixture();
  blankCard.usage.total_tokens = null;
  const nullHeadline = metricView(scenario(blankCard.rows, blankCard.usage, blankCard.context).curves);

  const nullComponents = weekFixture();
  nullComponents.rows[0].stats.contributions.forEach((day) => {
    day.tokenBreakdown.input = null;
    day.tokenBreakdown.cacheWrite = null;
  });
  const nullParts = metricView(scenario(nullComponents.rows, nullComponents.usage, nullComponents.context).curves);

  const blankStrings = weekFixture();
  blankStrings.rows[0].stats.contributions[0].totals.messages = '';
  const blank = metricView(scenario(blankStrings.rows, blankStrings.usage, blankStrings.context).curves);
  return { nulls, control, nullHeadline, nullParts, blankMessages: blank.messages };
});

// 17. Stats provenance: coverage, staleness and the age of the read that holds the curve
run('statsProvenance', () => {
  // Yesterday's Stats cannot have measured today, even when the six days it did
  // record still add up to today's card.
  const rolled = weekFixture({ perDay: [10, 20, 30, 40, 50, 60, 0] });
  rolled.rows[0].stats.contributions = rolled.rows[0].stats.contributions
    .filter((day) => day.date !== '2026-09-27');
  rolled.usage.total_messages = 12;
  rolled.usage.timestamp = '2026-09-27T21:00:00';
  rolled.rows[0].stats.timestamp = '2026-09-26T23:00:00';
  rolled.rows[0].stats.response_cache = { status: 'stale', served_from_cache: true, age_seconds: 79200 };
  const stale = scenario(rolled.rows, rolled.usage, rolled.context);
  const staleMetrics = metricView(stale.curves);
  const staleCoverage = sparklineStatsRef.rows[0].coverageThrough;
  const staleGeneratedOn = sparklineStatsRef.generatedOn;
  const staleRecordedOn = stale.curves.recordedOn;
  const stalePaint = paintedView();

  // Same rollover, but today did have usage: the recorded days no longer add up to
  // the card, so the card's own curve is withheld rather than drawn six days deep.
  const active = weekFixture();
  active.rows[0].stats.contributions = active.rows[0].stats.contributions
    .filter((day) => day.date !== '2026-09-27');
  active.rows[0].stats.timestamp = '2026-09-26T23:30:00';
  const activeMetrics = metricView(scenario(active.rows, active.usage, active.context).curves);

  // A year-old answer with nothing in it: no date in this window was ever covered.
  const outside = weekFixture({ perDay: [0, 0, 0, 0, 0, 0, 0], messages: 0 });
  outside.rows[0].stats.contributions = [];
  outside.rows[0].stats.timestamp = '2025-09-27T08:00:00';
  const outsideMetrics = metricView(scenario(outside.rows, outside.usage, outside.context).curves);

  // Today's read, today's dates, but served out of a cache: still dated.
  const cached = weekFixture({ timestamp: '2026-09-27T09:00:00' });
  cached.rows[0].stats.timestamp = '2026-09-27T08:00:00';
  cached.rows[0].stats.response_cache = { status: 'stale', served_from_cache: true, age_seconds: 32400 };
  scenario(cached.rows, cached.usage, cached.context);
  const cachedRecordedOn = deriveSparklineCurves(lastUsageResponse, { today: parseDateKey('2026-09-27') }).recordedOn;
  const cachedPaint = paintedView();

  // The shape the Stats route actually answers with: a generation stamp and no cache
  // metadata at all, against a Usage response that proves it was just computed. The
  // curve still reconciles, so it still draws -- but it is a snapshot from 08:00 and
  // has to say so, or a card at nine at night reads as if it measured the whole day.
  const noMeta = weekFixture({ timestamp: '2026-09-27T21:00:00' });
  noMeta.rows[0].stats.timestamp = '2026-09-27T08:00:00';
  delete noMeta.rows[0].stats.response_cache;
  noMeta.usage.response_cache = { status: 'recomputed', served_from_cache: false, age_seconds: 0 };
  const noMetaCurves = scenario(noMeta.rows, noMeta.usage, noMeta.context).curves;
  const noMetaPaint = paintedView();

  const olderUsage = weekFixture({ timestamp: '2026-09-27T07:30:00' });
  olderUsage.rows[0].stats.timestamp = '2026-09-27T21:00:00';
  const olderUsageCutoff = scenario(olderUsage.rows, olderUsage.usage).curves.recordedOn;

  // A newer combined timestamp must not hide an older per-server response.
  // This defensive fixture deliberately gives the combined stamp a newer time.
  const hostA = weekFixture({ timestamp: '2026-09-27T21:00:00' });
  const hostB = weekFixture({ timestamp: '2026-09-27T07:30:00' });
  hostA.rows[0].stats.timestamp = '2026-09-27T21:00:00';
  hostB.rows[0].stats.timestamp = '2026-09-27T21:00:00';
  const combined = usagePayload({ tokens: 560, cost: 5.6, messages: 28, cache: 0,
    timestamp: hostA.usage.timestamp, topModels: [{ name: 'model-a', tokens: 560 }] });
  const secondHostCutoff = scenario([
    { serverId: 'a', usage: hostA.usage, stats: hostA.rows[0].stats },
    { serverId: 'b', usage: hostB.usage, stats: hostB.rows[0].stats },
  ], combined).curves.recordedOn;

  const changed = weekFixture({ timestamp: '2026-09-27T21:00:00', usageTokens: 281 });
  changed.rows[0].stats.timestamp = '2026-09-27T08:00:00';
  scenario(changed.rows, changed.usage);
  const refusedSnapshot = paintedView().tokens;

  // The one case allowed to go undated: every payload behind the curve proves it was
  // computed just now. Stats can never do that today, so this is the control, not the
  // common path.
  const bothNow = weekFixture({ timestamp: '2026-09-27T09:00:00' });
  bothNow.rows[0].stats.timestamp = '2026-09-27T09:00:00';
  bothNow.rows[0].stats.response_cache = { status: 'recomputed', served_from_cache: false, age_seconds: 0 };
  bothNow.usage.response_cache = { status: 'recomputed', served_from_cache: false, age_seconds: 0 };
  const bothNowCurves = scenario(bothNow.rows, bothNow.usage, bothNow.context).curves;
  return {
    noMetaRecordedOn: noMetaCurves.recordedOn,
    noMetaPaint,
    noMetaMetrics: metricView(noMetaCurves),
    olderUsageCutoff, secondHostCutoff, refusedSnapshot,
    bothNowRecordedOn: bothNowCurves.recordedOn,
    staleMetrics, stalePaint,
    recordedOn: staleRecordedOn,
    coverageThrough: staleCoverage,
    generatedOn: staleGeneratedOn,
    activeMetrics, outsideMetrics,
    cachedRecordedOn,
    cachedPaint,
  };
});

// 18. every server's own window has to be the selected week, not the first one's
run('perServerRanges', () => {
  const week = (overrides) => weekFixture(overrides || {});
  const combine = (a, b, tweaks) => Object.assign({
    range: { from: '2026-09-21', to: '2026-09-27' },
    total_tokens: a.total_tokens + b.total_tokens,
    total_cost: a.total_cost + b.total_cost,
    total_messages: a.total_messages + b.total_messages,
    cache_hit_rate: 0,
    source_errors: [],
    timestamp: '2026-09-28T09:00:00',
    top_models: [{ name: 'model-a', tokens: a.total_tokens + b.total_tokens }],
    combined_models: [{ name: 'model-a', tokens: a.total_tokens + b.total_tokens }],
  }, tweaks || {});

  const agree = [week(), week()];
  const ok = metricView(scenario([
    { serverId: 'a', usage: agree[0].usage, stats: agree[0].rows[0].stats },
    { serverId: 'b', usage: agree[1].usage, stats: agree[1].rows[0].stats },
  ], combine(agree[0].usage, agree[1].usage), { servers: [{ id: 'a' }, { id: 'b' }] }).curves);

  // Only the second server's window is wrong, and the combined copy says 21-27.
  const drifted = [week(), week()];
  drifted[1].usage.range = { from: '2026-09-01', to: '2026-09-07' };
  const driftedMetrics = metricView(scenario([
    { serverId: 'a', usage: drifted[0].usage, stats: drifted[0].rows[0].stats },
    { serverId: 'b', usage: drifted[1].usage, stats: drifted[1].rows[0].stats },
  ], combine(drifted[0].usage, drifted[1].usage), { servers: [{ id: 'a' }, { id: 'b' }] }).curves);

  // A server that states no window at all is not contradicted, and still agrees.
  const unstated = [week(), week()];
  delete unstated[1].usage.range;
  const unstatedMetrics = metricView(scenario([
    { serverId: 'a', usage: unstated[0].usage, stats: unstated[0].rows[0].stats },
    { serverId: 'b', usage: unstated[1].usage, stats: unstated[1].rows[0].stats },
  ], combine(unstated[0].usage, unstated[1].usage), { servers: [{ id: 'a' }, { id: 'b' }] }).curves);

  // The contract's own shape: a failure list that is empty and no count field
  // anywhere. This is what a live response looks like, so it has to draw.
  const clean = metricView(scenario([
    { serverId: 'a', usage: agree[0].usage, stats: agree[0].rows[0].stats },
    { serverId: 'b', usage: agree[1].usage, stats: agree[1].rows[0].stats },
  ], combine(agree[0].usage, agree[1].usage, { source_errors: [] }),
    { servers: [{ id: 'a' }, { id: 'b' }] }).curves);

  // A failure the combined answer names, whatever the per-server copies say.
  const named = combine(agree[0].usage, agree[1].usage, { source_errors: ['gemini: timed out'] });
  const combinedError = metricView(scenario([
    { serverId: 'a', usage: agree[0].usage, stats: agree[0].rows[0].stats },
    { serverId: 'b', usage: agree[1].usage, stats: agree[1].rows[0].stats },
  ], named, { servers: [{ id: 'a' }, { id: 'b' }] }).curves);

  // ... and the same failure named by only the second server, which the combined
  // copy never mentions: one host's broken scan cannot lend a curve to the card.
  const oneHost = combine(agree[0].usage, agree[1].usage, { source_errors: [] });
  const broken = week();
  broken.usage.source_errors = ['gemini: timed out'];
  const serverError = metricView(scenario([
    { serverId: 'a', usage: agree[0].usage, stats: agree[0].rows[0].stats },
    { serverId: 'b', usage: broken.usage, stats: broken.rows[0].stats },
  ], oneHost, { servers: [{ id: 'a' }, { id: 'b' }] }).curves);

  return {
    ok: { tokens: ok.tokens.reason, values: ok.tokens.values },
    drifted: driftedMetrics.tokens,
    unstated: unstatedMetrics.tokens.reason,
    clean: clean.tokens.reason,
    combinedError: combinedError.tokens.reason,
    serverError: serverError.tokens.reason,
  };
});

// 19. the merged curve has to agree with the headline the card prints
run('combinedParity', () => {
  const cheap = () => weekFixture({ cost: 0, costPerDay: [0, 0, 0, 0, 0, 0, 0.0049] });
  const rounding = [cheap(), cheap()];
  const roundingMetrics = metricView(scenario([
    { serverId: 'a', usage: rounding[0].usage, stats: rounding[0].rows[0].stats },
    { serverId: 'b', usage: rounding[1].usage, stats: rounding[1].rows[0].stats },
  ], {
    range: { from: '2026-09-21', to: '2026-09-27' },
    total_tokens: rounding[0].usage.total_tokens + rounding[1].usage.total_tokens,
    total_cost: 0,
    total_messages: 28,
    cache_hit_rate: 0,
    source_errors: [],
    timestamp: '2026-09-28T09:00:00',
    top_models: [{ name: 'model-a', tokens: 560 }],
    combined_models: [{ name: 'model-a', tokens: 560 }],
  }, { servers: [{ id: 'a' }, { id: 'b' }] }).curves);

  const fits = () => weekFixture({ cost: 0.002, costPerDay: [0, 0, 0, 0, 0, 0, 0.002] });
  const inside = [fits(), fits()];
  const insideMetrics = metricView(scenario([
    { serverId: 'a', usage: inside[0].usage, stats: inside[0].rows[0].stats },
    { serverId: 'b', usage: inside[1].usage, stats: inside[1].rows[0].stats },
  ], {
    range: { from: '2026-09-21', to: '2026-09-27' },
    total_tokens: inside[0].usage.total_tokens + inside[1].usage.total_tokens,
    total_cost: 0.004,
    total_messages: 28,
    cache_hit_rate: 0,
    source_errors: [],
    timestamp: '2026-09-28T09:00:00',
    top_models: [{ name: 'model-a', tokens: 560 }],
    combined_models: [{ name: 'model-a', tokens: 560 }],
  }, { servers: [{ id: 'a' }, { id: 'b' }] }).curves);

  // Each server agrees with its own card; the combined winner does not.
  const modelDrift = [weekFixture(), weekFixture()];
  const modelMetrics = metricView(scenario([
    { serverId: 'a', usage: modelDrift[0].usage, stats: modelDrift[0].rows[0].stats },
    { serverId: 'b', usage: modelDrift[1].usage, stats: modelDrift[1].rows[0].stats },
  ], {
    range: { from: '2026-09-21', to: '2026-09-27' },
    total_tokens: 560,
    total_cost: 5.6,
    total_messages: 28,
    cache_hit_rate: 0,
    source_errors: [],
    timestamp: '2026-09-28T09:00:00',
    top_models: [{ name: 'model-a', tokens: 561 }],
    combined_models: [{ name: 'model-a', tokens: 561 }],
  }, { servers: [{ id: 'a' }, { id: 'b' }] }).curves);

  // A combined cache rate the response never answered is not a 0% to match against.
  const noRate = [weekFixture(), weekFixture()];
  const noRateMetrics = metricView(scenario([
    { serverId: 'a', usage: noRate[0].usage, stats: noRate[0].rows[0].stats },
    { serverId: 'b', usage: noRate[1].usage, stats: noRate[1].rows[0].stats },
  ], {
    range: { from: '2026-09-21', to: '2026-09-27' },
    total_tokens: 560,
    total_cost: 5.6,
    total_messages: 28,
    cache_hit_rate: null,
    source_errors: [],
    timestamp: '2026-09-28T09:00:00',
    top_models: [{ name: 'model-a', tokens: 560 }],
    combined_models: [{ name: 'model-a', tokens: 560 }],
  }, { servers: [{ id: 'a' }, { id: 'b' }] }).curves);
  return {
    roundingCost: roundingMetrics.cost, roundingTokens: roundingMetrics.tokens.reason,
    roundingSum: rounding.reduce(
      (total, fixture) => total + fixture.rows[0].stats.contributions
        .reduce((dayTotal, day) => dayTotal + day.totals.cost, 0),
      0,
    ),
    roundingHeadline: 0,
    insideCost: insideMetrics.cost.reason, insideCostValues: insideMetrics.cost.values,
    modelDrift: modelMetrics.top_model.reason, modelTokens: modelMetrics.tokens.reason,
    noRate: noRateMetrics.cache_hit_rate.reason,
  };
});

// 20. the response the dashboard actually gets, field names and all
//
// Every other case here is a fixture somebody wrote. This one is a payload captured
// from a running build, which is the only way to catch a module that reads a field
// the API has never sent: the real Week payload carries `source_errors: []`, and a
// guard that demanded `source_error_count === 0` refused all five curves on it.
run('liveContract', () => {
  const capture = __LIVE_CONTRACT__;
  const usage = capture.usage;
  const contributions = capture.stats.contributions;
  const first = contributions[0].date;
  const last = contributions[contributions.length - 1].date;
  const before = fetchCalls;
  const { curves } = scenario(
    [{ serverId: 'local', usage, stats: { contributions, timestamp: capture.stats.timestamp } }],
    usage,
    { start: first, end: last, today: last, serverKey: 'local' },
  );
  const painted = paintedView(curves, last);
  return Object.assign(metricView(curves), {
    aria: painted.tokens.aria,
    costAria: painted.cost.aria,
    agentTimeAria: painted.agent_time.aria,
    drawnReasons: __METRIC_IDS__.map((id) => painted[id].reason),
    __fetches: fetchCalls - before,
  });
});

process.stdout.write(JSON.stringify(results));
"""


HARNESS_MARKER = "process.stdout.write(JSON.stringify(results));"


def _harness_script(src: str, tail: str = "") -> str:
    """The shared harness, with the sparkline module and its real helpers grafted in.

    `tail` replaces the final stdout write, which is how a second script gets the same
    fixtures and the same real `loadStats()` without duplicating either.
    """
    harness = HARNESS if not tail else HARNESS.replace(HARNESS_MARKER, tail)
    return (
        harness
        .replace("__I18N__", _extract_i18n_en(src))
        .replace("__T__", _extract_function(src, "function t(key, vars)"))
        .replace(
            "__DATE_KEYS__",
            _extract_function(src, "function parseDateKey(value)")
            + "\n"
            + _extract_function(src, "function formatDateKey(date)"),
        )
        .replace("__NORMALIZE__", _extract_function(src, "function normalizeModelName(name)"))
        .replace(
            "__SOURCE_ERRORS__",
            _extract_function(src, "function usageSourceErrors(payload)"),
        )
        .replace("__MODULE__", _extract_module(src))
        .replace("__METRIC_IDS__", json.dumps(list(METRICS)))
        .replace("__LIVE_CONTRACT__", LIVE_CONTRACT.read_text(encoding="utf-8"))
    )


def _run_script(tmp_path: Path, script: str, name: str):
    path = tmp_path / f"{name}.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(path)], capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"node harness failed:\n{result.stderr[-4000:]}"
    return json.loads(result.stdout)


def _run_harness(tmp_path: Path, name: str = "sparklines"):
    return _run_script(tmp_path, _harness_script(_source()), name)


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    return _run_harness(tmp_path_factory.mktemp("sparklines"))


def _points(metric):
    return [value for value in metric["values"] if value is not None]


def test_compatible_week_draws_five_curves_and_leaves_agent_time_empty(results):
    baseline = results["baseline"]
    assert baseline["fetches"] == 0, "deriving a curve must not make a request"
    assert baseline["dates"] == [f"2026-09-{day}" for day in range(21, 28)]
    for metric in ("tokens", "cost", "messages", "cache_hit_rate", "top_model"):
        assert baseline["metrics"][metric]["reason"] == "", metric
        assert baseline["metrics"][metric]["points"] == 7, metric
    assert baseline["metrics"]["agent_time"]["reason"] == "noBuckets"
    assert baseline["metrics"]["agent_time"]["values"] == []


def test_message_fold_mismatch_omits_only_the_messages_curve(results):
    metrics = results["messageMismatch"]
    assert metrics["messages"]["reason"] == "mismatch"
    assert metrics["messages"]["values"] == []
    # A rejected fold must not discard the data that does agree.
    for metric in ("tokens", "cost", "cache_hit_rate", "top_model"):
        assert metrics[metric]["reason"] == "", metric


def test_cost_agrees_only_within_the_printed_precision(results):
    assert results["costTolerance"]["near"]["reason"] == ""
    assert results["costTolerance"]["far"]["reason"] == "mismatch"
    assert results["costTolerance"]["nearTokens"] == ""


def test_cache_rate_is_weighted_by_its_own_components(results):
    summed = results["cacheWeighting"]["summed"]
    assert summed["reason"] == ""
    # 1/1 then 0/9: the day points are 100% and 0%, and the range they describe is 10%.
    assert summed["values"][0] == pytest.approx(1.0)
    assert summed["values"][1] == pytest.approx(0.0)
    assert sum(1 for value in summed["values"] if value is None) == 5
    # The mean of the daily rates is not the range rate, so it is not drawed either.
    assert results["cacheWeighting"]["averagedCache"]["reason"] == "mismatch"
    assert results["cacheWeighting"]["averagedTokens"] == ""


def test_cache_without_explicit_components_is_unavailable_not_zero(results):
    assert results["cacheComponents"]["missing"]["reason"] == "components"
    assert results["cacheComponents"]["missing"]["values"] == []
    assert results["cacheComponents"]["writes"]["reason"] == "components"


def test_top_model_curve_tracks_the_card_winner_on_every_date(results):
    drawn = results["fixedModel"]["drawn"]
    assert drawn["reason"] == ""
    # Every day contributes the same model the card names, not that day's own leader.
    assert drawn["values"] == [10, 20, 30, 40, 50, 60, 70]
    assert results["fixedModel"]["refused"]["reason"] == "mismatch"
    # A winner with no matching daily source rows is contradicted by the same payload.
    assert results["fixedModel"]["noRows"]["reason"] == "mismatch"
    # Daily rows with no model breakdown at all are unknown, not a flat zero line.
    assert results["fixedModel"]["shapeless"]["reason"] == "components"
    # The card's display name and the Stats model id reach the same canonical key.
    assert results["fixedModel"]["canonical"]["reason"] == ""


def test_recorded_zero_and_unknown_holes_are_different_things(results):
    gaps = results["gapsAndZeros"]
    # A date the response simply does not list is zero inside coverage the parity check
    # proves, so the anchor is drawn and the seven points still add up to the card.
    assert gaps["zero"]["reason"] == ""
    assert gaps["zero"]["points"] == 7
    assert gaps["zero"]["values"][3] == 0
    # A date that arrived without the field cannot be summed against the headline at
    # all, so the whole metric is withheld rather than half-claimed.
    assert gaps["unreconcilable"]["reason"] == "components"
    # Where a curve does carry holes -- the cache card's days with no prompt input --
    # line and fill break instead of being drawn across the gap.
    assert gaps["splitLineSegments"] == 3
    assert gaps["splitAreaSegments"] == 1


def test_smoothed_paths_stay_inside_the_measured_values(results):
    shape = results["shape"]
    assert shape["spike"]["hasNaN"] is False
    assert shape["anyNegativeY"] is False
    assert shape["overshoot"] is False
    assert shape["ratio"]["hasNaN"] is False
    assert shape["broken"] == {"line": "", "area": ""}
    assert shape["single"]["area"] == ""
    assert shape["single"]["line"].startswith("M")
    assert "C" not in shape["single"]["line"]
    assert shape["zero"]["area"] == ""
    assert "C" in shape["flat"]


def test_a_curve_never_pairs_another_selection(results):
    races = results["races"]
    assert races["good"]["tokens"] == ""
    assert races["good"]["agent"] == "noBuckets"
    assert races["rangeMoved"]["reason"] == "rangeNotWeek"
    assert races["longerRange"]["reason"] == "rangeNotWeek"
    assert races["shifted"]["reason"] == "selection"
    assert races["otherServers"]["reason"] == "noDaily"
    assert races["uncommitted"]["reason"] == "noDaily"
    assert races["todayOnly"]["reason"] == "rangeNotWeek"


def test_partial_scans_and_source_failures_refuse_to_draw(results):
    selection = results["selection"]
    assert selection["retained"]["tokens"]["reason"] == "selection"
    assert selection["errors"]["tokens"]["reason"] == "selection"
    assert selection["shortScan"]["tokens"]["reason"] == "selection"
    assert selection["missing"]["tokens"]["reason"] == "noDaily"


def test_every_selected_server_must_answer_and_reconcile(results):
    multi = results["multiServer"]
    # 10..70 on one host and 110..170 on the other: raw counts sum before anything divides.
    assert multi["okReason"] == ""
    assert multi["okValues"] == [120, 140, 160, 180, 200, 220, 240]
    assert multi["okModel"] == ""
    assert multi["okCache"] == ""
    # A selected server whose Stats response never arrived leaves the combined answer
    # short, so nothing is drawn instead of one host's trend.
    assert multi["partial"] == "selection"
    assert multi["disagree"] == "mismatch"
    # The combined winner outside one server's returned model array means that host's
    # share of it is unknown; a truncated top list is not a zero.
    assert multi["truncatedModel"] == "components"


def test_sparklines_never_make_a_request(results):
    assert results["noRequests"]["total"] == 0


def test_cards_report_meaning_and_gaps_to_assistive_technology(results):
    painted = results["painted"]
    drawn = painted["drawn"]
    en = painted["en"]
    assert drawn["__fetches"] == 0
    assert drawn["tokens"]["role"] == "img"
    assert drawn["tokens"]["line"].startswith("M")
    assert drawn["tokens"]["aria"].startswith(en["totalTokens"])
    assert en["sparklineTodayPartial"] in drawn["tokens"]["aria"]
    assert drawn["tokens"]["title"] == drawn["tokens"]["aria"]
    assert drawn["agent_time"]["line"] == ""
    assert en["sparklineNoDailyBuckets"] in drawn["agent_time"]["aria"]
    mismatched = painted["mismatched"]
    assert mismatched["messages"]["line"] == ""
    assert en["sparklineHeadlineMismatch"] in mismatched["messages"]["aria"]
    strict = painted["strict"]
    # Strict Today: every card empty, and empty for a stated reason.
    for metric in METRICS:
        assert strict[metric]["line"] == "", metric
        assert strict[metric]["area"] == "", metric
    assert en["sparklineNeedsWeekRange"] in strict["tokens"]["aria"]
    # A curve carried over from an older read is dated, not billed as fresh.
    assert en["sparklineRecordedAt"].replace("{date}", "2026-09-20 09:00") in painted["stalePaint"]["tokens"]["aria"]


def test_derivation_does_not_rescale_its_inputs(results):
    assert results["purity"]["unchanged"] is True


def test_week_means_seven_rolling_local_dates_ending_today(results):
    window = results["window"]
    assert window["week"] == [f"2026-09-{day}" for day in range(21, 28)]
    assert window["short"] is None
    assert window["staleEnd"] is None
    assert window["long"] is None
    assert window["missing"] is None


def test_markup_ships_no_decorative_curve_and_no_new_endpoint():
    src = _source()
    for line in (l for l in src.splitlines() if "<svg id=\"sparkline" in l):
        assert 'd=""' in line, "the fixed decorative paths must be retired"
        assert ' Q' not in line and ' L100 ' not in line


# ---------------------------------------------------------------------------
# Stats-load races: the real `loadStats()` and `scheduleStatsWarm()`, with only
# the fetch completions and the existing Stats UI standing in. Completion order
# is the variable under test, so this runs in its own script rather than the
# shared harness.
# ---------------------------------------------------------------------------

LOAD_ENV = r"""
let statsLoadSeq = 0;
let statsLoaded = false;
let statsWarmScheduled = false;
let contributionsData = [];
const statsCache = { default: null, years: new Map() };
const calendarState = { view: 'month' };
const pending = [];
function fetchSelectedServers(path) {
  const call = { path };
  pending.push(call);
  call.promise = new Promise((resolve, reject) => {
    call.resolve = resolve;
    call.reject = reject;
  });
  return call.promise;
}
async function loadActivityInsights() {}
function setStatsStatus() {}
function combineStatsPayloads(payloads) { return payloads[0]; }
function formatTokenCount(value) { return String(value); }
function formatNumber(value) { return String(value); }
function formatCurrency(value) { return String(value); }
function pluralDays(value) { return String(value); }
function fillMissingDays(values) { return values; }
function fillYearDays(values) { return values; }
function reconcileTodayProfileContribution(values) { return values; }
function renderCalendar() {}
function renderOverviewProfilePreview() {}
function setOverviewProfileState() {}
function ensureYearStatsLoaded() {}
function scheduleIdle(fn) { fn(); }
// No waiting in this file: the loaders' deferrals run inline, and settling a
// completion is a handful of microtask turns.
function setTimeout(fn) { fn(); return 0; }
const drain = async () => {
  for (let turn = 0; turn < 12; turn += 1) await Promise.resolve();
};
const realSparklineRenderer = renderOverviewSparklines;
renderOverviewSparklines = (usage = lastUsageResponse) => realSparklineRenderer(
  usage, { today: parseDateKey('2026-09-27') },
);
const asStats = (fixture) => [{ server: { id: 'local' }, payload: fixture.rows[0].stats }];
const firstDayTokens = () => (sparklineStatsRef
  ? sparklineStatsRef.rows[0].payload.contributions[0].totals.tokens
  : null);
const asyncResults = {};
"""

LOAD_CASES = r"""
(async () => {
  const current = weekFixture();                       // 10, 20 ... 70
  const superseded = weekFixture({ perDay: [70, 60, 50, 40, 30, 20, 10] });

  // 1. Two foreground reads, newest answered first. The Stats UI keeps the newest,
  //    and so must the reference the curves are drawn from.
  scenario(current.rows, current.usage, current.context);
  statsLoaded = false;
  const older = loadStats();
  const newest = loadStats();
  const olderCall = pending[pending.length - 2];
  const newestCall = pending[pending.length - 1];
  newestCall.resolve(asStats(current));
  await newest;
  const afterNewest = firstDayTokens();
  olderCall.resolve(asStats(superseded));
  await older;
  asyncResults.reversedCompletion = {
    statsUi: statsCache.default[0].totals.tokens,
    acceptedAfterNewest: afterNewest,
    acceptedAfterSuperseded: firstDayTokens(),
    curve: deriveSparklineCurves(current.usage, { today: parseDateKey('2026-09-27') })
      .metrics.tokens.values,
  };

  // 2. The warm loader issued first, the foreground read answered, then the warm
  //    one landed. A later generation has already been accepted, so it cannot move.
  sparklineStatsRef = null;
  statsLoaded = false;
  statsWarmScheduled = false;
  scenario(current.rows, current.usage, current.context);
  scheduleStatsWarm(true);
  const warmCall = pending[pending.length - 1];
  const foreground = loadStats();
  pending[pending.length - 1].resolve(asStats(current));
  await foreground;
  const foregroundPayload = sparklineStatsRef.rows[0].payload;
  warmCall.resolve(asStats(superseded));
  await drain();
  asyncResults.warmLandsLate = {
    keepsForeground: sparklineStatsRef.rows[0].payload === foregroundPayload,
    // The curve a later render draws is still the accepted newest week.
    curve: deriveSparklineCurves(current.usage, { today: parseDateKey('2026-09-27') })
      .metrics.tokens.values,
  };

  // 3. A read that failed cannot clear or replace what an accepted read committed.
  const beforeFailure = sparklineStatsRef;
  const failing = loadStats();
  pending[pending.length - 1].reject(new Error('stats unavailable'));
  await failing;
  asyncResults.failedRead = { keepsReference: sparklineStatsRef === beforeFailure };

  // 4. A warm read issued after a failed foreground read is the newest answer there
  //    is, so the same rule has to let it through.
  statsLoaded = false;
  statsWarmScheduled = false;
  const failedAgain = loadStats();
  pending[pending.length - 1].reject(new Error('still unavailable'));
  scheduleStatsWarm(true);
  const warmAgain = pending[pending.length - 1];
  await failedAgain;
  warmAgain.resolve(asStats(superseded));
  await drain();
  asyncResults.warmAfterFailure = {
    accepted: sparklineStatsRef.rows[0].payload === superseded.rows[0].stats,
  };

  // 5. The rule itself, stated directly: older generations are refused, and a
  //    capture without a generation (a caller outside either loader) still works.
  const generation = sparklineIssueStatsGeneration();
  const admitted = sparklineAcceptStatsRows(asStats(current), { serverKey: 'local', generation });
  const acceptedBefore = sparklineStatsRef;
  const rejected = sparklineAcceptStatsRows(asStats(superseded), {
    serverKey: 'local', generation: sparklineIssueStatsGeneration() - 2,
  });
  asyncResults.generationRule = {
    admitted,
    olderRefused: rejected === false && sparklineStatsRef === acceptedBefore,
    currentAccepted: acceptedBefore.rows[0].payload === current.rows[0].stats,
  };
  sparklineCaptureStatsRows(asStats(superseded), { serverKey: 'local' });
  asyncResults.generationRule.directCapture = (
    sparklineStatsRef.rows[0].payload === superseded.rows[0].stats
  );

  process.stdout.write(JSON.stringify(asyncResults));
})().catch((error) => { console.error(error && error.stack); process.exitCode = 1; });
"""


def _run_load_harness(tmp_path: Path, name: str = "sparkline-stats-load"):
    src = _source()
    tail = (
        LOAD_ENV
        + "\n" + _extract_function(src, "async function loadStats(options")
        + "\n" + _extract_function(src, "function scheduleStatsWarm(force")
        + "\n" + LOAD_CASES
    )
    return _run_script(tmp_path, _harness_script(src, tail), name)


@pytest.fixture(scope="module")
def race_results(tmp_path_factory):
    return _run_load_harness(tmp_path_factory.mktemp("sparkline-stats-load"))


def test_a_superseded_stats_response_cannot_move_the_curve(race_results):
    case = race_results["reversedCompletion"]
    # The existing Stats UI and the sparkline reference agree on the same response.
    assert case["statsUi"] == 10
    assert case["acceptedAfterNewest"] == 10
    assert case["acceptedAfterSuperseded"] == 10, "an older read that landed last still owns the curve"
    # Reversing the anchors would keep every parity sum intact, so the values
    # themselves are what has to be pinned.
    assert case["curve"] == [10, 20, 30, 40, 50, 60, 70]


def test_both_stats_loaders_share_one_acceptance_rule(race_results):
    assert race_results["warmLandsLate"]["keepsForeground"] is True
    assert race_results["warmLandsLate"]["curve"] == [10, 20, 30, 40, 50, 60, 70]
    assert race_results["failedRead"]["keepsReference"] is True
    # The same rule admits a later read, so refusing the older one is not a freeze.
    assert race_results["warmAfterFailure"]["accepted"] is True
    rule = race_results["generationRule"]
    assert rule["admitted"] is True
    assert rule["olderRefused"] is True
    assert rule["currentAccepted"] is True
    assert rule["directCapture"] is True


def test_explicit_nulls_stay_unavailable_rather_than_measured(results):
    case = results["unavailableFields"]
    for metric in ("tokens", "cost", "messages"):
        assert case["nulls"][metric]["reason"] == "components", metric
        assert case["nulls"][metric]["values"] == [], metric
    # A real zero is still a measurement, so it keeps its anchor.
    assert case["control"]["tokens"]["reason"] == ""
    assert case["control"]["tokens"]["points"] == 7
    # Headlines and raw components are read the same strict way.
    assert case["nullHeadline"]["tokens"]["reason"] == "components"
    assert case["nullParts"]["cache_hit_rate"]["reason"] == "components"
    assert case["blankMessages"]["reason"] == "components"


def test_stats_from_yesterday_cannot_measure_today(results):
    case = results["statsProvenance"]
    assert case["coverageThrough"] == "2026-09-26"
    assert case["generatedOn"] == "2026-09-26"
    assert case["recordedOn"] == "2026-09-26 23:00"
    stale = case["staleMetrics"]
    # Six recorded days that add up to the card are six measured days -- the day the
    # response cannot have seen is left out rather than invented as a zero.
    for metric in ("tokens", "cost", "messages", "cache_hit_rate", "top_model"):
        assert stale[metric]["reason"] == "", metric
        assert stale[metric]["points"] == 6, metric
        assert stale[metric]["values"][6] is None, metric
    painted = case["stalePaint"]["tokens"]
    en = results["painted"]["en"]
    assert en["sparklineMeasuredDays"].replace("{n}", "6").replace("{total}", "7") in painted["aria"]
    assert en["sparklineRecordedAt"].replace("{date}", "2026-09-26 23:00") in painted["aria"]
    assert en["sparklineTodayPartial"] not in painted["aria"]
    assert painted["reason"] == "drawn"


def test_a_same_day_snapshot_is_dated_even_with_no_cache_metadata(results):
    """A Stats response that proves nothing about its own age is still a snapshot.

    The Stats route answers with a generation stamp and no cache metadata at all, so a
    card read at nine from a response computed at eight has to say when its daily values
    stopped -- every total on the card still reconciles, so nothing else flags it.
    """
    case = results["statsProvenance"]
    en = results["painted"]["en"]
    assert case["noMetaRecordedOn"] == "2026-09-27 08:00"
    painted = case["noMetaPaint"]["tokens"]
    assert en["sparklineRecordedAt"].replace("{date}", "2026-09-27 08:00") in painted["aria"]
    # A qualifier, not a refusal: the curve still draws all seven anchors.
    assert painted["reason"] == "drawn"
    for metric in ("tokens", "cost", "messages", "cache_hit_rate", "top_model"):
        assert case["noMetaMetrics"][metric]["reason"] == "", metric
        assert case["noMetaMetrics"][metric]["points"] == 7, metric
    # The control keeps the rule honest in the other direction: missing metadata is
    # never read as "computed now", and a payload that does prove it goes undated.
    assert case["bothNowRecordedOn"] is None


def test_snapshot_cutoff_includes_the_older_usage_from_every_host(results):
    case = results["statsProvenance"]
    assert case["olderUsageCutoff"] == "2026-09-27 07:30"
    assert case["secondHostCutoff"] == "2026-09-27 07:30"


def test_refused_snapshot_explains_how_to_recheck_without_promising_recovery(results):
    case = results["statsProvenance"]["refusedSnapshot"]
    en = results["painted"]["en"]
    assert case["reason"] == "mismatch"
    assert case["points"] == "0"
    assert case["line"] == ""
    assert en["sparklineHeadlineMismatch"] in case["aria"]
    assert en["sparklineRecordedAt"].replace("{date}", "2026-09-27 08:00") in case["aria"]


def test_a_day_that_cannot_be_proved_is_left_out_of_the_curve(results):
    case = results["statsProvenance"]
    # Same rollover, but today did have usage: the recorded days no longer add up to
    # the printed card, so those curves are withheld rather than drawn six deep.
    active = case["activeMetrics"]
    for metric in ("tokens", "cost", "messages", "top_model"):
        assert active[metric]["reason"] == "mismatch", metric
        assert active[metric]["values"] == [], metric
    # The cache rate the response can still prove is not thrown away with them.
    assert active["cache_hit_rate"]["reason"] == ""
    assert active["cache_hit_rate"]["points"] == 6
    # An answer from a year ago covered none of this window.
    outside = case["outsideMetrics"]
    for metric in ("tokens", "cost", "messages", "cache_hit_rate", "top_model"):
        assert outside[metric]["reason"] == "noDaily", metric
        assert outside[metric]["values"] == [], metric
    # A same-day read out of a stale cache is dated rather than billed as now.
    assert case["cachedRecordedOn"] == "2026-09-27 08:00"
    en = results["painted"]["en"]
    assert en["sparklineRecordedAt"].replace("{date}", "2026-09-27 08:00") in case["cachedPaint"]["tokens"]["aria"]


def test_every_server_has_to_report_the_selected_week(results):
    case = results["perServerRanges"]
    assert case["ok"]["tokens"] == ""
    assert case["ok"]["values"] == [20, 40, 60, 80, 100, 120, 140]
    # Only the second server's window is wrong, and the combined copy carries the
    # first server's, so matching totals are no proof of a shared window.
    assert case["drifted"]["reason"] == "rangeNotWeek"
    assert case["drifted"]["values"] == []
    # Saying nothing is not the same as saying something else.
    assert case["unstated"] == ""
    # The contract's own shape -- `source_errors: []` and no count field at all -- is
    # a clean scan. A fixture that invented a count once made this refuse to draw.
    assert case["clean"] == ""
    # A failed source is an unknown whether the combined answer names it or only one
    # server's copy does, which the combined copy never repeats.
    assert case["combinedError"] == "selection"
    assert case["serverError"] == "selection"


def test_the_merged_curve_has_to_agree_with_the_printed_headline(results):
    case = results["combinedParity"]
    # Each server's rounding fits the card's own tolerance; added together they do
    # not, so the curve that would have printed $0.01 against $0.00 is refused.
    assert case["roundingSum"] == pytest.approx(0.0098)
    assert case["roundingHeadline"] == 0
    assert case["roundingCost"]["reason"] == "mismatch"
    assert case["roundingCost"]["values"] == []
    # and only that metric: the counts that do reconcile are still drawn.
    assert case["roundingTokens"] == ""
    assert case["insideCost"] == ""
    assert case["insideCostValues"][6] == pytest.approx(0.004)
    # The fixed model's combined total is proved the same way.
    assert case["modelDrift"] == "mismatch"
    assert case["modelTokens"] == ""
    # A combined cache rate the response never answered is not a 0% to match.
    assert case["noRate"] == "mismatch"


def test_the_payload_the_api_actually_sends_still_draws(results):
    """Drawn from a response captured off a running build, not from a written fixture.

    Guards the one mistake a hand-written fixture cannot: reading a field the API has
    never sent. The real Week payload answers with `source_errors: []` and no count,
    and a guard that demanded a zero count cleared every curve off it.
    """
    live = results["liveContract"]
    assert live["__fetches"] == 0
    for metric in ("tokens", "cost", "messages", "cache_hit_rate", "top_model"):
        assert live[metric]["reason"] == "", metric
        assert live[metric]["points"] == 7, metric
        assert live[metric]["line"].startswith("M"), metric
    # Active Time stays honest even when everything around it reconciles.
    assert live["agent_time"]["reason"] == "noBuckets"
    assert live["drawnReasons"].count("drawn") == 5

    capture = json.loads(LIVE_CONTRACT.read_text(encoding="utf-8"))
    assert live["tokens"]["values"] == [
        day["totals"]["tokens"] for day in capture["stats"]["contributions"]
    ]
    assert "7 of 7 days measured." in live["aria"]
    assert "Today is still in progress." in live["aria"]
    assert live["costAria"].startswith("Total Cost")


def test_module_makes_no_request_and_touches_no_backend_surface():
    module = _extract_module(_source())
    for forbidden in (
        "fetch(", "fetchJson", "fetchSelectedServers", "fetchFromHost", "XMLHttpRequest",
        "/api/", "refresh=1", "scheduleStatsWarm", "localStorage", "sessionStorage",
        "indexedDB", "setTimeout", "setInterval", "navigator.sendBeacon",
    ):
        assert forbidden not in module, f"sparkline module must not use {forbidden}"

BUCKET_CASES = r"""
const output = {};
let overviewActiveTimeState = { data: null, key: null };
function activeTimeRequestKey(_days, from, to) { return `${from}:${to}`; }
function upgrade(f) {
  const buckets = f.rows[0].stats.contributions.map((day) => ({
    key: day.date, ...day.totals,
    input: day.tokenBreakdown.input + (day.tokenBreakdown.cacheWrite || 0),
    cache: day.tokenBreakdown.cacheRead,
    models: Object.fromEntries(day.sources.map((source) => [normalizeModelName(source.modelId), sparklineSourceTokens(source)])),
  }));
  f.usage.sparkline = { granularity: 'day', buckets };
  return f;
}
let f = upgrade(weekFixture());
f.rows[0].stats.contributions[0].totals.tokens = 123456;
f.rows[0].stats.contributions[0].totals.messages = 1000;
output.week = metricView(scenario(f.rows, f.usage).curves);
output.statsAbsent = metricView(scenario([{...f.rows[0], stats: {}}], f.usage).curves);
const hourly = usagePayload({ range: {from:'2026-09-27',to:'2026-09-27'}, tokens: 100,
  cost: 1, messages: 9, cache: 0.9, topModels: [{name:'model-a',tokens:100}], timestamp: '2026-09-27T02:45:00' });
hourly.sparkline = {granularity:'hour', buckets:[
  {key:'2026-09-27T01',tokens:10,cost:0.1,messages:4,input:0,cache:10,models:{'model-a':10}},
  {key:'2026-09-27T02',tokens:90,cost:0.9,messages:5,input:10,cache:80,models:{'model-a':90}},
]};
const dayContext = {start:'2026-09-27',end:'2026-09-27'};
output.today = metricView(scenario([{serverId:'local', usage:hourly,stats:{}}],hourly,dayContext).curves);
overviewActiveTimeState = {key:'2026-09-27:2026-09-27',data:{active_ms_sum:180000,_sparklineServerKey:'local',
  _sparklinePayloads:[{range:hourly.range,timestamp:hourly.timestamp,active_ms_sum:180000,unavailable_tools:[],
    sparkline:{granularity:'hour',buckets:[{key:'2026-09-27T01',agent_ms:60000},{key:'2026-09-27T02',agent_ms:120000}]}}]}};
output.agent = metricView(deriveSparklineCurves(hourly));
overviewActiveTimeState.key = 'old';
output.agentMoved = metricView(deriveSparklineCurves(hourly));
overviewActiveTimeState.data = null;
output.paint = renderOverviewSparklines(hourly, {today:parseDateKey('2026-09-27')});
output.hourlyAria = node('sparklineTokens').attrs['aria-label'];
hourly.timestamp = '2026-09-28T03:00:00';
output.yesterday = metricView(scenario([{serverId:'local',usage:hourly,stats:{}}],hourly,dayContext).curves);
hourly.sparkline.keys = Array.from({length:24},(_,i)=>`2026-09-27T${String(i).padStart(2,'0')}`).filter(key=>key!=='2026-09-27T03');
output.skippedClockHour = metricView(deriveSparklineCurves(hourly));
delete hourly.sparkline.keys;
hourly.sparkline.buckets[0].messages = null;
output.nulls = metricView(deriveSparklineCurves(hourly));
hourly.sparkline.buckets[0].messages = 4;
hourly.sparkline.buckets[0].models['model-a'] = null;
output.nullModel = metricView(deriveSparklineCurves(hourly));
hourly.sparkline.buckets[0].models['model-a'] = 10;
hourly.sparkline.buckets.push({...hourly.sparkline.buckets[0]});
output.duplicate = metricView(deriveSparklineCurves(hourly));
f = upgrade(weekFixture());
f.usage.total_messages = 999;
output.mismatch = metricView(scenario(f.rows,f.usage).curves);
f = upgrade(weekFixture());
const b = upgrade(weekFixture({perDay:[1,2,3,4,5,6,7]}));
const combined = usagePayload({tokens:308,cost:3.08,messages:28,cache:0,topModels:[{name:'model-a',tokens:308}]});
output.multi = metricView(scenario([f.rows[0],{...b.rows[0],serverId:'remote'}],combined).curves);
f.usage.sparkline.buckets[0].cost += .004;
b.usage.sparkline.buckets[0].cost += .004;
output.multiRounded = metricView(scenario([f.rows[0],{...b.rows[0],serverId:'remote'}],combined).curves);
f.usage.sparkline.buckets[0].cost -= .004;
b.usage.sparkline.buckets[0].cost -= .004;
b.usage.sparkline.buckets.forEach(row => { row.models = {'model-b':row.tokens}; });
b.usage.top_models = b.usage.combined_models = [{name:'model-b',tokens:28}];
const disjoint = {...combined, top_models:[{name:'model-a',tokens:280}],
  combined_models:[{name:'model-a',tokens:280},{name:'model-b',tokens:28}]};
output.disjoint = metricView(scenario([f.rows[0],{...b.rows[0],serverId:'remote'}],disjoint).curves);
b.usage.sparkline.buckets.forEach(row => { row.models = {'model-a':row.tokens}; });
b.usage.top_models = b.usage.combined_models = [{name:'model-a',tokens:28}];
delete b.usage.sparkline;
output.mixed = metricView(scenario([f.rows[0],{...b.rows[0],serverId:'remote'}],combined).curves);
output.partial = metricView(scenario([{...f.rows[0],retained:true}],f.usage).curves);
const annual = usagePayload({range:{from:'2026-01-15',to:'2026-09-27'}, tokens:100,
  cost:1,messages:9,cache:.9,topModels:[{name:'model-a',tokens:100}],timestamp:'2026-09-27T02:45:00'});
annual.sparkline = {granularity:'month',buckets:[
  {key:'2026-01',tokens:10,cost:.1,messages:4,input:0,cache:10,models:{'model-a':10}},
  {key:'2026-09',tokens:90,cost:.9,messages:5,input:10,cache:80,models:{'model-a':90}},
]};
overviewActiveTimeState = {key:'2026-01-15:2026-09-27',data:{active_ms_sum:180000,_sparklineServerKey:'local',
  _sparklinePayloads:[{range:annual.range,timestamp:annual.timestamp,active_ms_sum:180000,unavailable_tools:[],
    sparkline:{granularity:'month',buckets:[{key:'2026-01',agent_ms:60000},{key:'2026-09',agent_ms:120000}]}}]}};
output.year = metricView(scenario([{serverId:'local',usage:annual,stats:{}}],annual,
  {start:'2026-01-15',end:'2026-09-27'}).curves);
renderOverviewSparklines(annual,{today:parseDateKey('2026-09-27')});
output.monthlyAria = node('sparklineTokens').attrs['aria-label'];
annual.range.to = '2026-12-31';
output.futureMonths = metricView(scenario([{serverId:'local',usage:annual,stats:{}}],annual,
  {start:'2026-01-15',end:'2026-12-31'}).curves);
output.leapWindow = sparklineWindowKeys(parseDateKey('2024-01-01'),parseDateKey('2024-12-31'));
output.rollingWindow = sparklineWindowKeys(parseDateKey('2025-09-27'),parseDateKey('2026-09-27'));
output.overLimit = sparklineWindowKeys(parseDateKey('2025-09-27'),parseDateKey('2026-09-28'));
output.fetchCalls = fetchCalls;
process.stdout.write(JSON.stringify(output));
"""


@pytest.fixture(scope="module")
def bucket_results(tmp_path_factory):
    return _run_script(tmp_path_factory.mktemp("sparkline-buckets"), _harness_script(_source(), BUCKET_CASES), "buckets")


def test_week_buckets_draw_without_stats_or_snapshot_reconciliation(bucket_results):
    for case in ("week", "statsAbsent"):
        for metric in ("tokens", "cost", "messages", "cache_hit_rate", "top_model"):
            assert bucket_results[case][metric]["points"] == 7, metric
            assert bucket_results[case][metric]["line"], metric
    assert bucket_results["week"]["tokens"]["values"] == [10, 20, 30, 40, 50, 60, 70]
    assert bucket_results["fetchCalls"] == 0


def test_today_uses_hourly_values_and_future_hours_are_unknown(bucket_results):
    case = bucket_results["today"]
    assert case["tokens"]["values"] == [0, 10, 90] + [None] * 21
    assert case["messages"]["values"] == [0, 4, 5] + [None] * 21
    assert case["cache_hit_rate"]["values"][:3] == [None, 1, 80 / 90]
    assert "Hourly recorded values in local time" in bucket_results["hourlyAria"]
    assert "3 of 24 hours measured" in bucket_results["hourlyAria"]


def test_yesterday_is_a_complete_hourly_day(bucket_results):
    assert bucket_results["yesterday"]["tokens"]["values"] == [0, 10, 90] + [0] * 21
    assert bucket_results["yesterday"]["tokens"]["points"] == 24
    assert bucket_results["skippedClockHour"]["tokens"]["points"] == 23
    assert bucket_results["skippedClockHour"]["tokens"]["values"][3] is None


def test_agent_curve_uses_its_own_intervals_and_clears_on_range_change(bucket_results):
    assert bucket_results["agent"]["agent_time"]["values"] == [0, 60000, 120000] + [None] * 21
    assert bucket_results["agentMoved"]["agent_time"]["values"] == []


def test_bucket_validation_rejects_missing_duplicate_or_mismatched_measurements(bucket_results):
    assert bucket_results["nulls"]["messages"]["reason"] == "components"
    assert bucket_results["nullModel"]["top_model"]["reason"] == "components"
    assert bucket_results["duplicate"]["tokens"]["points"] == 0
    assert bucket_results["mismatch"]["messages"]["reason"] == "mismatch"
    assert bucket_results["mismatch"]["tokens"]["points"] == 7
    assert bucket_results["partial"]["tokens"]["points"] == 0


def test_multiple_servers_sum_each_bucket_before_drawing(bucket_results):
    assert bucket_results["multi"]["tokens"]["values"] == [11, 22, 33, 44, 55, 66, 77]
    assert bucket_results["multi"]["top_model"]["values"] == [11, 22, 33, 44, 55, 66, 77]
    assert bucket_results["mixed"]["tokens"]["values"] == [11, 22, 33, 44, 55, 66, 77]


def test_multiple_servers_allow_independent_cost_rounding_and_disjoint_models(bucket_results):
    assert bucket_results["multiRounded"]["cost"]["reason"] == ""
    assert bucket_results["multiRounded"]["cost"]["points"] == 7
    assert bucket_results["disjoint"]["top_model"]["reason"] == ""
    assert bucket_results["disjoint"]["top_model"]["values"] == [10, 20, 30, 40, 50, 60, 70]


def test_yearly_curves_use_months_and_preserve_partial_selected_months(bucket_results):
    case = bucket_results["year"]
    for metric in ("tokens", "cost", "messages", "cache_hit_rate", "top_model", "agent_time"):
        assert case[metric]["reason"] == "", metric
        assert case[metric]["line"], metric
    assert case["tokens"]["values"] == [10] + [0] * 7 + [90]
    assert case["agent_time"]["values"] == [60000] + [0] * 7 + [120000]
    assert case["cache_hit_rate"]["values"] == [1] + [None] * 7 + [80 / 90]
    assert "Monthly recorded values" in bucket_results["monthlyAria"]
    assert "9 of 9 months measured" in bucket_results["monthlyAria"]
    assert "This month is still in progress" in bucket_results["monthlyAria"]


def test_monthly_curves_leave_future_months_unknown_and_bound_leap_years(bucket_results):
    assert bucket_results["futureMonths"]["tokens"]["values"] == [10] + [0] * 7 + [90] + [None] * 3
    assert len(bucket_results["leapWindow"]["dates"]) == 12
    assert bucket_results["leapWindow"]["rangeDates"] == ["2024-01-01", "2024-12-31"]
    assert len(bucket_results["rollingWindow"]["dates"]) == 13
    assert bucket_results["overLimit"] is None


@pytest.mark.parametrize("first,last,granularity,points", [
    ("2026-09-21", "2026-09-21", "hour", 24),
    ("2024-01-01", "2024-12-31", "month", 12),
])
def test_real_usage_response_draws_curves_without_stats(tmp_path, monkeypatch, first, last, granularity, points):
    """Exercise the actual SQL -> compute -> API -> JS contract, including batch messages."""
    from tokdash import api, compute
    from tokdash.dateutil import parse_date_range
    from tokdash.sources.openclaw import _openclaw_usage_from_store
    from tokdash.usage_store import UsageEntryStore, build_source_signature

    since, until = parse_date_range(first, last)
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    offsets = ((3_600_000, 4), (7_200_000, 7)) if granularity == "hour" else ((3_600_000, 4), (60 * 86_400_000, 7))
    rows = [{"source": "codex", "model": "model-a", "timestamp": int(since.timestamp() * 1000) + offset,
             "input": 10, "output": 5, "cacheRead": 20, "cost": .01, "messageCount": count}
            for offset, count in offsets]
    store.sync_source("codex", build_source_signature(files=[["synthetic", 1, 1]], parser={"v": 1}), lambda: rows)

    class Tracker:
        parsers = {}
        source_errors = []

    monkeypatch.setattr(compute, "CodingToolsUsageTracker", Tracker)
    monkeypatch.setattr(compute, "_sync_usage_store", lambda _tracker: (store, ["codex"]))
    monkeypatch.setattr(compute, "_collect_live_coding_entries", lambda *unused: [])
    monkeypatch.setattr(compute, "get_session_usage_range", lambda start, stop: _openclaw_usage_from_store(store, start, stop))
    api._clear_cache()
    try:
        payload = api.get_usage("today", first, last, refresh=True)
    finally:
        api._clear_cache()
    assert payload["total_messages"] == 11
    assert payload["sparkline"]["granularity"] == granularity
    if granularity == "hour":
        assert len(payload["sparkline"]["keys"]) == 24
    tail = "const context = " + json.dumps({"start": first, "end": last, "today": "2026-09-27"}) + ";\n"
    tail += "const payload = " + json.dumps(payload) + ";\n" + r"""
const result = scenario([{serverId:'local',usage:payload,stats:{}}], payload,
  context);
process.stdout.write(JSON.stringify({metrics:metricView(result.curves),fetches:fetchCalls}));
"""
    result = _run_script(tmp_path, _harness_script(_source(), tail), "real-hourly-contract")
    for metric in ("tokens", "cost", "messages", "cache_hit_rate", "top_model"):
        assert result["metrics"][metric]["reason"] == "", metric
        assert result["metrics"][metric]["line"], metric
    assert result["metrics"]["messages"]["points"] == points
    assert result["metrics"]["messages"]["values"] == ([0, 4, 7] + [0] * 21 if granularity == "hour" else [4, 0, 7] + [0] * 9)
    assert result["fetches"] == 0
