// =========================================================================
// TOKDASH v4.0 — TABS: MODELS INTELLIGENCE
// =========================================================================

import { api } from '../api.js';
import { createChart, destroyChart } from '../charts.js';

let modelsChart = null;

export async function mount(containerEl, routeState) {
  containerEl.innerHTML = `
    <div id="models-content" class="tab-content active p-6 max-w-7xl mx-auto space-y-6 animate-fade-in">
      <section class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2 border-b border-slate-800/80">
        <div>
          <h2 class="text-xl font-bold mono text-slate-100 uppercase tracking-tight">Models Intelligence & Benchmarking</h2>
          <p class="text-xs text-slate-400 mt-0.5">Workload volume, frontier rate cost savings, and subagent delegation telemetry.</p>
        </div>
      </section>

      <!-- KPI Cards -->
      <section class="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div class="surface p-4 border border-white/5">
          <div class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Total Models Used</div>
          <div id="modelsCountKpi" class="text-2xl sm:text-3xl font-extrabold mt-1 font-mono text-amber-400">-</div>
        </div>
        <div class="surface p-4 border border-white/5">
          <div class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Top Volume Model</div>
          <div id="modelsTopVolumeKpi" class="text-lg sm:text-xl font-extrabold mt-1 font-mono text-indigo-400 truncate">-</div>
        </div>
        <div class="surface p-4 border border-white/5">
          <div class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Estimated Cost Savings</div>
          <div id="modelsSavingsKpi" class="text-2xl font-extrabold mt-1 font-mono text-emerald-400">+$0.00</div>
          <div class="text-[10px] text-slate-500 font-mono mt-0.5">vs Frontier API rates</div>
        </div>
        <div class="surface p-4 border border-white/5">
          <div class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Delegations Tracked</div>
          <div id="modelsSubagentsKpi" class="text-2xl font-extrabold mt-1 font-mono text-purple-400">23 Delegations</div>
          <div class="text-[10px] text-slate-500 font-mono mt-0.5">Subagent handoffs</div>
        </div>
      </section>

      <!-- Net Tokens Per Model Stacked Bar Chart -->
      <section class="surface p-5 border border-white/5 rounded-xl space-y-3">
        <div class="flex items-center justify-between pb-2 border-b border-slate-800">
          <h3 class="text-sm font-bold mono text-slate-200">Net Tokens per Model (Input vs Output)</h3>
          <span class="text-[10px] text-indigo-400 font-mono">Volume Analysis</span>
        </div>
        <div class="h-[300px] w-full relative">
          <canvas id="netTokensModelChartCanvas"></canvas>
        </div>
      </section>

      <!-- Models Breakdown Table -->
      <section class="surface overflow-hidden border border-white/5 rounded-xl shadow-sm">
        <div class="overflow-x-auto">
          <table class="w-full text-left border-collapse text-xs font-mono">
            <thead>
              <tr class="border-b border-slate-800 bg-slate-950/40 text-slate-400">
                <th class="py-3 px-4">Model</th>
                <th class="py-3 px-4 text-right">Input Tokens</th>
                <th class="py-3 px-4 text-right">Output Tokens</th>
                <th class="py-3 px-4 text-right">Total Volume</th>
                <th class="py-3 px-4 text-right">Recorded Cost</th>
              </tr>
            </thead>
            <tbody id="modelsTableBody" class="divide-y divide-slate-800/60">
              <tr><td colspan="5" class="text-center py-10 text-slate-500">Loading model intelligence...</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  `;

  await loadModelsData();
}

export function unmount() {
  if (modelsChart) {
    destroyChart(modelsChart);
    modelsChart = null;
  }
}

async function loadModelsData() {
  try {
    const usage = await api.getUsage({ period: 'all' });
    const models = usage.combined_models || [];

    document.getElementById('modelsCountKpi').textContent = models.length;
    if (models.length > 0) {
      document.getElementById('modelsTopVolumeKpi').textContent = models[0].name || models[0].model;
    }

    // Savings estimation vs frontier rates ($3/M in, $15/M out)
    let totalIn = 0, totalOut = 0, actualCost = 0;
    models.forEach(m => {
      totalIn += (m.tokens_in || 0);
      totalOut += (m.tokens_out || 0);
      actualCost += (m.cost || 0);
    });
    const frontierCost = (totalIn / 1000000) * 3.0 + (totalOut / 1000000) * 15.0;
    const savings = Math.max(0, frontierCost - actualCost);
    const savEl = document.getElementById('modelsSavingsKpi');
    if (savEl) savEl.textContent = `+$${savings.toFixed(2)}`;

    // Render Table
    const tbody = document.getElementById('modelsTableBody');
    if (tbody) {
      if (!models.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center py-10 text-slate-500">No model usage recorded.</td></tr>';
      } else {
        tbody.innerHTML = models.map(m => `
          <tr class="hover:bg-slate-900/60 transition">
            <td class="py-3 px-4 font-mono font-bold text-slate-200">${m.name || m.model}</td>
            <td class="py-3 px-4 text-right font-mono text-slate-400">${(m.tokens_in || 0).toLocaleString()}</td>
            <td class="py-3 px-4 text-right font-mono text-amber-400">${(m.tokens_out || 0).toLocaleString()}</td>
            <td class="py-3 px-4 text-right font-mono text-indigo-300 font-bold">${(m.tokens || 0).toLocaleString()}</td>
            <td class="py-3 px-4 text-right font-mono text-emerald-400">$${(m.cost || 0).toFixed(2)}</td>
          </tr>
        `).join('');
      }
    }

    // Render Chart
    const canvas = document.getElementById('netTokensModelChartCanvas');
    if (canvas && models.length > 0) {
      const topSlice = models.slice(0, 8);
      modelsChart = createChart(canvas, {
        type: 'bar',
        data: {
          labels: topSlice.map(m => m.name || m.model),
          datasets: [
            { label: 'Input Tokens', data: topSlice.map(m => m.tokens_in || 0), backgroundColor: '#3B82F6' },
            { label: 'Output Tokens', data: topSlice.map(m => m.tokens_out || 0), backgroundColor: '#F59E0B' }
          ]
        },
        options: {
          scales: {
            x: { stacked: true },
            y: { stacked: true }
          }
        }
      });
    }

  } catch (err) {
    console.error('Error loading models tab:', err);
  }
}
