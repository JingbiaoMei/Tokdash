// =========================================================================
// TOKDASH v4.0 — TABS: PRICING (YOUR USAGE LENS)
// =========================================================================

import { api } from '../api.js';

export async function mount(containerEl, routeState) {
  containerEl.innerHTML = `
    <div id="pricing-content" class="tab-content active p-6 max-w-7xl mx-auto space-y-6 animate-fade-in">
      <section class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2 border-b border-slate-800/80">
        <div>
          <h2 class="text-xl font-bold mono text-slate-100 uppercase tracking-tight">Model Pricing & Your Consumption</h2>
          <p class="text-xs text-slate-400 mt-0.5">Rates per million tokens overlaid with your actual historical usage.</p>
        </div>
        <div class="flex items-center gap-2">
          <input type="text" id="pricingSearchInput" class="ui-input text-xs py-1.5 px-3 rounded-lg font-mono w-56" placeholder="Filter models..." />
        </div>
      </section>

      <!-- Pricing Table with "Your Usage" Column -->
      <section class="surface overflow-hidden border border-white/5 rounded-xl shadow-sm">
        <div class="overflow-x-auto">
          <table class="w-full text-left border-collapse text-xs font-mono">
            <thead>
              <tr class="border-b border-slate-800 bg-slate-950/40 text-slate-400">
                <th class="py-3 px-4">Model Name</th>
                <th class="py-3 px-4">Provider</th>
                <th class="py-3 px-4 text-right">Prompt / 1M</th>
                <th class="py-3 px-4 text-right">Cache / 1M</th>
                <th class="py-3 px-4 text-right">Output / 1M</th>
                <th class="py-3 px-4 text-right text-indigo-300 font-bold bg-indigo-950/20">Your Usage</th>
              </tr>
            </thead>
            <tbody id="pricingTableBody" class="divide-y divide-slate-800/60">
              <tr><td colspan="6" class="text-center py-12 text-slate-500">Loading pricing intelligence...</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  `;

  await loadPricingData();
}

export function unmount() {}

async function loadPricingData() {
  const tbody = document.getElementById('pricingTableBody');
  const searchInput = document.getElementById('pricingSearchInput');
  if (!tbody) return;

  try {
    const [pricingRes, usageRes] = await Promise.all([
      api.getPricingDb(),
      api.getUsage({ period: 'all' })
    ]);

    const modelsPricing = pricingRes.models || {};
    const userModels = usageRes.combined_models || [];
    const userUsageMap = new Map();
    userModels.forEach(m => {
      userUsageMap.set((m.name || m.model || '').toLowerCase(), m.tokens || 0);
    });

    const rows = [];
    Object.entries(modelsPricing).forEach(([name, rates]) => {
      const lower = name.toLowerCase();
      const userTok = userUsageMap.get(lower) || 0;
      rows.push({
        name,
        provider: rates.provider || 'default',
        input: rates.input_per_million || rates.input || 0,
        cache: rates.cache_read_per_million || rates.cache_read || 0,
        output: rates.output_per_million || rates.output || 0,
        userTokens: userTok
      });
    });

    // Feature 10C: Sort by "Your usage" descending (default)
    rows.sort((a, b) => b.userTokens - a.userTokens);

    function renderRows(items) {
      if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center py-12 text-slate-500">No matching models found.</td></tr>';
        return;
      }

      tbody.innerHTML = items.map(r => {
        const hasUsage = r.userTokens > 0;
        const opacityClass = hasUsage ? 'text-slate-100 font-semibold' : 'text-slate-500 opacity-60';

        return `
          <tr class="hover:bg-slate-900/60 transition ${opacityClass}">
            <td class="py-3 px-4 font-mono font-bold">${escapeHtml(r.name)}</td>
            <td class="py-3 px-4 font-mono text-slate-400">${escapeHtml(r.provider)}</td>
            <td class="py-3 px-4 text-right font-mono">$${r.input.toFixed(2)}</td>
            <td class="py-3 px-4 text-right font-mono text-teal-400">$${r.cache.toFixed(2)}</td>
            <td class="py-3 px-4 text-right font-mono text-amber-400">$${r.output.toFixed(2)}</td>
            <td class="py-3 px-4 text-right font-mono font-bold bg-indigo-950/20 text-indigo-300">
              ${hasUsage ? `${r.userTokens.toLocaleString()} tok` : '—'}
            </td>
          </tr>
        `;
      }).join('');
    }

    renderRows(rows);

    searchInput?.addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase().trim();
      const filtered = rows.filter(r => r.name.toLowerCase().includes(q) || r.provider.toLowerCase().includes(q));
      renderRows(filtered);
    });

  } catch (err) {
    console.error('Error loading pricing tab:', err);
    tbody.innerHTML = `<tr><td colspan="6" class="text-center py-12 text-red-400">Failed to load pricing: ${escapeHtml(err.message)}</td></tr>`;
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
