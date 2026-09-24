// =========================================================================
// TOKDASH v4.0 — TABS: OVERVIEW (PROGRESSIVE DISCLOSURE)
// =========================================================================

import { api } from '../api.js';
import { createChart, destroyChart } from '../charts.js';
import { generateInsights, renderInsightsStrip } from '../insights.js';

let activeBreakdownChart = null;

export async function mount(containerEl, routeState) {
  const { params, router } = routeState;
  const currentTool = params.tool || 'all';

  containerEl.innerHTML = `
    <div id="overview-content" class="tab-content active p-6 max-w-7xl mx-auto space-y-6 animate-fade-in">
      
      <!-- 2A. Headline Row (Always Visible - 3 KPIs Only) -->
      <section class="grid grid-cols-1 md:grid-cols-3 gap-5" id="overviewHeadlineKpis">
        <!-- KPI 1: Tokens Today -->
        <div class="surface p-5 relative overflow-hidden flex flex-col justify-between min-h-[140px] border border-white/5 shadow-sm">
          <div>
            <div class="flex items-center justify-between">
              <span class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Tokens Today</span>
              <span id="overviewTokensDelta" class="text-[10px] font-mono font-bold px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400">0.0%</span>
            </div>
            <div id="totalTokens" class="text-3xl sm:text-4xl font-extrabold mt-2 font-mono text-indigo-400">-</div>
          </div>
          <svg id="sparklineTokens" class="kpi-sparkline text-indigo-400/60" viewBox="0 0 100 35" preserveAspectRatio="none" fill="none">
            <path id="sparklineTokensPath" d="M0 28 L100 28" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/>
          </svg>
        </div>

        <!-- KPI 2: Cost Today (with Projected Monthly Cost Tooltip) -->
        <div class="surface p-5 relative overflow-hidden flex flex-col justify-between min-h-[140px] border border-white/5 shadow-sm group cursor-help" id="costKpiCard" title="Hover to view monthly cost projection">
          <div>
            <div class="flex items-center justify-between">
              <span class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Cost Today</span>
              <span id="overviewCostDelta" class="text-[10px] font-mono font-bold px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400">$0.00</span>
            </div>
            <div id="totalCost" class="text-3xl sm:text-4xl font-extrabold mt-2 font-mono text-emerald-400">$0.00</div>
          </div>
          <!-- Projected Cost Badge on Card -->
          <div id="projectedMonthlyBadge" class="text-[11px] font-mono text-slate-400 mt-1 flex items-center gap-1.5">
            <span class="text-slate-500">Projected:</span>
            <span id="projectedMonthlyCost" class="text-emerald-400 font-bold">$0.00 / mo</span>
          </div>
          <svg id="sparklineCost" class="kpi-sparkline text-emerald-400/60" viewBox="0 0 100 35" preserveAspectRatio="none" fill="none">
            <path id="sparklineCostPath" d="M0 28 L100 28" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/>
          </svg>
        </div>

        <!-- KPI 3: Cache Hit Rate -->
        <div class="surface p-5 relative overflow-hidden flex flex-col justify-between min-h-[140px] border border-white/5 shadow-sm">
          <div>
            <div class="flex items-center justify-between">
              <span class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Cache Hit Rate</span>
              <span id="cacheHitBadge" class="text-[10px] font-mono font-bold px-1.5 py-0.5 rounded bg-teal-500/10 text-teal-400">Prompt Caching</span>
            </div>
            <div id="avgCacheHitRate" class="text-3xl sm:text-4xl font-extrabold mt-2 font-mono text-cyan-400">-</div>
          </div>
          <div id="avgCacheHitMeta" class="text-[11px] font-mono text-slate-500 mt-1">Share of input served from cache</div>
          <svg id="sparklineCacheRate" class="kpi-sparkline text-cyan-400/60" viewBox="0 0 100 35" preserveAspectRatio="none" fill="none">
            <path id="sparklineCacheRatePath" d="M0 28 L100 28" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/>
          </svg>
        </div>
      </section>

      <!-- Insights Strip (Feature 11) -->
      <section id="overviewInsightsContainer">
        <div class="text-xs text-slate-500 font-mono py-2">Analyzing telemetry patterns...</div>
      </section>

      <!-- 2B. "Breakdown" Collapsible / Focused Section -->
      <section class="surface p-5 border border-white/5 space-y-4 shadow-sm">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2 border-b border-slate-800/80">
          <div>
            <h3 class="text-base font-extrabold mono text-slate-100 flex items-center gap-2">
              <span>Workload Breakdown</span>
              <span class="text-[10px] px-2 py-0.5 rounded bg-indigo-500/15 text-indigo-300 border border-indigo-500/25 font-mono" id="breakdownRangeLabel">Today</span>
            </h3>
            <p class="text-xs text-slate-400 mt-0.5">Explore input, cache, and output distributions by tool or across all agents.</p>
          </div>
          
          <!-- Tool Selector Chips -->
          <div class="flex items-center gap-1.5 overflow-x-auto pb-1 sm:pb-0" id="overviewToolChips">
            <button type="button" class="query-preset-chip ${currentTool === 'all' ? 'active !border-indigo-500 !text-indigo-300 !bg-indigo-500/20' : ''}" data-tool="all">All Tools</button>
            <button type="button" class="query-preset-chip ${currentTool === 'hermes' ? 'active !border-indigo-500 !text-indigo-300 !bg-indigo-500/20' : ''}" data-tool="hermes">Hermes</button>
            <button type="button" class="query-preset-chip ${currentTool === 'antigravity_cli' ? 'active !border-indigo-500 !text-indigo-300 !bg-indigo-500/20' : ''}" data-tool="antigravity_cli">Antigravity</button>
            <button type="button" class="query-preset-chip ${currentTool === 'codex' ? 'active !border-indigo-500 !text-indigo-300 !bg-indigo-500/20' : ''}" data-tool="codex">Codex</button>
            <button type="button" class="query-preset-chip ${currentTool === 'claude' ? 'active !border-indigo-500 !text-indigo-300 !bg-indigo-500/20' : ''}" data-tool="claude">Claude Code</button>
          </div>
        </div>

        <!-- Breakdown Visualizations Container -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 pt-2">
          <!-- Chart Column (2 spans) -->
          <div class="lg:col-span-2 space-y-2">
            <div class="flex items-center justify-between text-xs text-slate-400 font-mono">
              <span id="breakdownChartTitle">Token Composition</span>
              <span class="text-[10px] text-slate-500">Live Telemetry</span>
            </div>
            <div class="h-[280px] w-full relative">
              <canvas id="overviewBreakdownChart"></canvas>
            </div>
          </div>

          <!-- Top Models / Rankings Column (1 span) -->
          <div class="space-y-3 bg-slate-950/40 p-4 rounded-xl border border-slate-800/80 flex flex-col justify-between">
            <div>
              <div class="text-xs font-bold uppercase tracking-wider text-slate-300 font-mono mb-2" id="breakdownRankingsTitle">Top Models</div>
              <div id="overviewRankingsContainer" class="space-y-2 text-xs font-mono">
                <div class="text-slate-500 text-center py-8">Loading rankings...</div>
              </div>
            </div>
            <a href="#/sessions" class="btn btn-ghost text-xs w-full text-center py-2 border border-slate-800 hover:border-indigo-500/40 text-indigo-300 flex items-center justify-center gap-1.5">
              <span>View In Sessions Explorer</span>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 18 6-6-6-6"/></svg>
            </a>
          </div>
        </div>
      </section>

      <!-- 2C. Profile Summary Strip ("Your Year in Tokens" linking to #/stats) -->
      <section class="surface p-4 border border-white/5 hover:border-indigo-500/30 transition cursor-pointer" onclick="window.location.hash='#/stats'">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div class="flex items-center gap-3">
            <div class="w-8 h-8 rounded-lg bg-indigo-500/15 border border-indigo-500/30 flex items-center justify-center text-indigo-400 font-bold text-xs">
              52W
            </div>
            <div>
              <div class="text-xs font-bold text-slate-200 font-mono">Your Year in Tokens</div>
              <p class="text-[11px] text-slate-400 mt-0.5">Explore 3D heatmap, daily activity rhythms, and historical token velocity.</p>
            </div>
          </div>
          <div class="flex items-center gap-4 text-xs font-mono text-slate-300">
            <span id="overviewStreakBadge" class="px-2 py-0.5 rounded bg-amber-500/10 text-amber-300 border border-amber-500/25">🔥 Active Streak</span>
            <span class="text-indigo-400 flex items-center gap-1 font-semibold">Open Stats ↗</span>
          </div>
        </div>
      </section>

    </div>
  `;

  // Wire tool selector chips
  containerEl.querySelectorAll('#overviewToolChips button').forEach(chip => {
    chip.addEventListener('click', () => {
      const tool = chip.dataset.tool;
      router.setQueryParams({ tool: tool === 'all' ? null : tool });
    });
  });

  // Load and render data
  await loadOverviewData(currentTool);
}

export function unmount() {
  if (activeBreakdownChart) {
    destroyChart(activeBreakdownChart);
    activeBreakdownChart = null;
  }
}

async function loadOverviewData(selectedTool = 'all') {
  try {
    const [usageData, sessionsData] = await Promise.all([
      api.getUsage({ period: 'today' }),
      api.getSessions(selectedTool === 'all' ? 'hermes' : selectedTool, 'today').catch(() => ({ sessions: [] }))
    ]);

    const sessions = sessionsData.sessions || [];

    // Render 3 Headline KPIs
    const totalTokens = usageData.total_tokens || 0;
    const totalCost = usageData.total_cost || 0.0;
    const cacheHitRate = usageData.cache_hit_rate;

    const tokEl = document.getElementById('totalTokens');
    if (tokEl) tokEl.textContent = totalTokens.toLocaleString();

    const costEl = document.getElementById('totalCost');
    if (costEl) costEl.textContent = `$${totalCost.toFixed(2)}`;

    // Monthly projection: today * 30
    const projectedMonthly = totalCost * 30;
    const projEl = document.getElementById('projectedMonthlyCost');
    if (projEl) projEl.textContent = `$${projectedMonthly.toFixed(2)} / mo`;

    // Cache Hit Rate & Color coding
    const hitEl = document.getElementById('avgCacheHitRate');
    if (hitEl) {
      if (cacheHitRate !== null && cacheHitRate !== undefined) {
        const pct = (cacheHitRate * 100).toFixed(1);
        hitEl.textContent = `${pct}%`;
        if (cacheHitRate > 0.8) {
          hitEl.className = 'text-3xl sm:text-4xl font-extrabold mt-2 font-mono text-emerald-400';
        } else if (cacheHitRate >= 0.5) {
          hitEl.className = 'text-3xl sm:text-4xl font-extrabold mt-2 font-mono text-amber-400';
        } else {
          hitEl.className = 'text-3xl sm:text-4xl font-extrabold mt-2 font-mono text-rose-400';
        }
      } else {
        hitEl.textContent = '0.0%';
      }
    }

    // Sparklines (1/5th baseline y=28)
    const costPath = document.getElementById('sparklineCostPath');
    if (costPath && totalCost === 0) {
      costPath.setAttribute('d', 'M0 28 L100 28');
    }

    // Render Insights Strip
    const insightsContainer = document.getElementById('overviewInsightsContainer');
    const insights = generateInsights(usageData, sessions);
    renderInsightsStrip(insightsContainer, insights);

    // Render Breakdown Chart & Rankings
    renderBreakdown(usageData, selectedTool);

  } catch (err) {
    console.error('Error loading Overview data:', err);
  }
}

function renderBreakdown(usageData, selectedTool) {
  const canvas = document.getElementById('overviewBreakdownChart');
  if (!canvas) return;

  const rankingsContainer = document.getElementById('overviewRankingsContainer');
  const titleEl = document.getElementById('breakdownChartTitle');
  const rankTitleEl = document.getElementById('breakdownRankingsTitle');

  if (selectedTool !== 'all') {
    // Specific tool selected: Stacked Input vs Cache vs Output
    if (titleEl) titleEl.textContent = `${selectedTool.toUpperCase()} — Input vs Cache vs Output`;
    if (rankTitleEl) rankTitleEl.textContent = 'Top Models Used';

    const byTool = usageData.by_tool?.[selectedTool] || {};
    const inTok = byTool.tokens_in || 0;
    const cacheTok = byTool.tokens_cache || 0;
    const outTok = byTool.tokens_out || 0;

    activeBreakdownChart = createChart(canvas, {
      type: 'bar',
      data: {
        labels: ['Tokens'],
        datasets: [
          { label: 'Prompt Input', data: [inTok], backgroundColor: '#3B82F6' },
          { label: 'Cache Reads', data: [cacheTok], backgroundColor: '#10B981' },
          { label: 'Agent Output', data: [outTok], backgroundColor: '#F59E0B' }
        ]
      },
      options: {
        scales: {
          x: { stacked: true },
          y: { stacked: true }
        }
      }
    });

    // Top 5 models table for this tool
    const models = usageData.top_models || [];
    if (rankingsContainer) {
      if (!models.length) {
        rankingsContainer.innerHTML = '<div class="text-slate-500 text-center py-6">No models recorded for this tool today.</div>';
      } else {
        rankingsContainer.innerHTML = models.slice(0, 5).map(m => `
          <div class="flex items-center justify-between p-2 rounded bg-slate-900/60 border border-slate-800">
            <span class="truncate max-w-[140px] text-slate-200" title="${m.name}">${m.name}</span>
            <div class="text-right">
              <div class="font-bold text-slate-100">${(m.tokens || 0).toLocaleString()}</div>
              <div class="text-[10px] text-emerald-400">$${(m.cost || 0).toFixed(2)}</div>
            </div>
          </div>
        `).join('');
      }
    }

  } else {
    // "All" tools selected: Grouped bar chart per tool + Ranked tool list
    if (titleEl) titleEl.textContent = 'All Tools — Volume by Agent';
    if (rankTitleEl) rankTitleEl.textContent = 'Tools by Volume';

    const byTool = usageData.by_tool || {};
    const toolEntries = Object.entries(byTool).filter(([_, data]) => (data.tokens || 0) > 0);
    toolEntries.sort((a, b) => (b[1].tokens || 0) - (a[1].tokens || 0));

    const labels = toolEntries.map(e => e[0]);
    const inData = toolEntries.map(e => e[1].tokens_in || 0);
    const cacheData = toolEntries.map(e => e[1].tokens_cache || 0);
    const outData = toolEntries.map(e => e[1].tokens_out || 0);

    activeBreakdownChart = createChart(canvas, {
      type: 'bar',
      data: {
        labels: labels.length ? labels : ['No Data'],
        datasets: [
          { label: 'Input', data: inData.length ? inData : [0], backgroundColor: '#3B82F6' },
          { label: 'Cache', data: cacheData.length ? cacheData : [0], backgroundColor: '#10B981' },
          { label: 'Output', data: outData.length ? outData : [0], backgroundColor: '#F59E0B' }
        ]
      },
      options: {
        scales: {
          x: { stacked: true },
          y: { stacked: true }
        }
      }
    });

    if (rankingsContainer) {
      if (!toolEntries.length) {
        rankingsContainer.innerHTML = '<div class="text-slate-500 text-center py-6">No tool data recorded today.</div>';
      } else {
        rankingsContainer.innerHTML = toolEntries.map(([tName, tData]) => `
          <div class="flex items-center justify-between p-2 rounded bg-slate-900/60 border border-slate-800">
            <span class="capitalize text-slate-200">${tName}</span>
            <span class="font-bold text-indigo-300 font-mono">${(tData.tokens || 0).toLocaleString()} tok</span>
          </div>
        `).join('');
      }
    }
  }
}
