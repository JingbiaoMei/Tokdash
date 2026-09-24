// =========================================================================
// TOKDASH v4.0 — TABS: QUOTA TRACKING
// =========================================================================

import { api } from '../api.js';

export async function mount(containerEl, routeState) {
  containerEl.innerHTML = `
    <div id="quota-content" class="tab-content active p-6 max-w-7xl mx-auto space-y-6 animate-fade-in">
      <section class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2 border-b border-slate-800/80">
        <div>
          <h2 class="text-xl font-bold mono text-slate-100 uppercase tracking-tight">API Quotas & Rate Limits</h2>
          <p class="text-xs text-slate-400 mt-0.5">Track consumption windows, resets, and credential scans.</p>
        </div>
      </section>

      <section class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div class="surface p-5 border border-white/5 rounded-xl space-y-2">
          <div class="text-xs font-bold text-slate-300 font-mono">Claude / Anthropic Tier</div>
          <div class="text-2xl font-extrabold text-indigo-400 font-mono">Tier 4</div>
          <p class="text-xs text-slate-400">Tokens/min: 400,000 · Requests/min: 4,000</p>
        </div>
        <div class="surface p-5 border border-white/5 rounded-xl space-y-2">
          <div class="text-xs font-bold text-slate-300 font-mono">Google Antigravity Rate</div>
          <div class="text-2xl font-extrabold text-emerald-400 font-mono">Unlimited</div>
          <p class="text-xs text-slate-400">Local developer license · Zero cloud quota locks</p>
        </div>
        <div class="surface p-5 border border-white/5 rounded-xl space-y-2">
          <div class="text-xs font-bold text-slate-300 font-mono">Hermes Local Daemon</div>
          <div class="text-2xl font-extrabold text-purple-400 font-mono">Active</div>
          <p class="text-xs text-slate-400">Direct SQLite journal streaming</p>
        </div>
      </section>
    </div>
  `;
}

export function unmount() {}
