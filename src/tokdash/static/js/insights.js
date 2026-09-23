// =========================================================================
// TOKDASH v4.0 — RULE-BASED INSIGHTS STRIP DETECTOR
// =========================================================================

export function generateInsights(usageData = {}, sessions = []) {
  const insights = [];

  const totalTokens = usageData.total_tokens || 0;
  const totalCost = usageData.total_cost || 0;
  const cacheHitRate = usageData.cache_hit_rate;

  // 1. Spending Comparison (Today vs Yesterday / Period Comparison)
  const deltas = usageData.comparison_deltas || {};
  if (deltas.tokens_pct !== undefined && deltas.tokens_pct !== null) {
    const pct = deltas.tokens_pct;
    if (pct > 25) {
      insights.push({
        type: 'spending_up',
        icon: '▲',
        color: 'text-amber-400 bg-amber-500/10 border-amber-500/25',
        text: `Token spend is ${pct.toFixed(0)}% higher than the previous period.`
      });
    } else if (pct < -15) {
      insights.push({
        type: 'spending_down',
        icon: '▼',
        color: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/25',
        text: `Token spend is ${Math.abs(pct).toFixed(0)}% lower than the previous period.`
      });
    }
  }

  // 2. Loop / Large Session Detector (>500K tokens)
  if (Array.isArray(sessions) && sessions.length > 0) {
    const largeSessions = sessions.filter(s => (s.tokens || 0) > 500000);
    if (largeSessions.length > 0) {
      insights.push({
        type: 'loop_warning',
        icon: '⚠️',
        color: 'text-rose-400 bg-rose-500/10 border-rose-500/25',
        text: `${largeSessions.length} session${largeSessions.length > 1 ? 's' : ''} exceeded 500K tokens — possible recursive agent loops.`
      });
    }
  }

  // 3. Cache Hit Rate Anomaly (<50% when models usually benefit from prompt caching)
  if (cacheHitRate !== null && cacheHitRate !== undefined && cacheHitRate < 0.5 && totalTokens > 10000) {
    insights.push({
      type: 'cache_low',
      icon: '⚡',
      color: 'text-sky-400 bg-sky-500/10 border-sky-500/25',
      text: `Cache hit rate is ${(cacheHitRate * 100).toFixed(1)}% (below 50% target). Heavy prompt variations detected.`
    });
  } else if (cacheHitRate !== null && cacheHitRate > 0.85) {
    insights.push({
      type: 'cache_opt',
      icon: '✨',
      color: 'text-teal-400 bg-teal-500/10 border-teal-500/25',
      text: `Cache efficiency at ${(cacheHitRate * 100).toFixed(1)}%. Prompt caching saved ~${((usageData.cache_read_tokens || 0) / 1000000).toFixed(2)}M redundant tokens.`
    });
  }

  // 4. Model Dominance
  if (usageData.top_models && usageData.top_models.length > 0) {
    const top = usageData.top_models[0];
    const topPct = totalTokens > 0 ? (top.tokens / totalTokens) * 100 : 0;
    if (topPct > 70 && top.name) {
      insights.push({
        type: 'model_dominance',
        icon: '🧠',
        color: 'text-purple-400 bg-purple-500/10 border-purple-500/25',
        text: `${top.name} represents ${topPct.toFixed(0)}% of your total workload volume.`
      });
    }
  }

  // Fallback if everything is normal or no anomalies
  if (insights.length === 0) {
    insights.push({
      type: 'normal',
      icon: '👍',
      color: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/25',
      text: 'All agent metrics within normal operating range.'
    });
  }

  return insights.slice(0, 3);
}

export function renderInsightsStrip(container, insights = []) {
  if (!container) return;
  if (!insights.length) {
    container.innerHTML = '';
    return;
  }

  container.innerHTML = `
    <div class="insights-strip">
      ${insights.map(item => `
        <div class="insight-chip ${item.color} border">
          <span class="font-bold select-none">${item.icon}</span>
          <span>${item.text}</span>
        </div>
      `).join('')}
    </div>
  `;
}
