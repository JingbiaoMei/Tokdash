// =========================================================================
// TOKDASH v4.0 — TABS: USAGE REPORT
// =========================================================================

import { api } from '../api.js';

export async function mount(containerEl, routeState) {
  containerEl.innerHTML = `
    <div id="report-content" class="tab-content active p-6 max-w-7xl mx-auto space-y-6 animate-fade-in">
      <section class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2 border-b border-slate-800/80">
        <div>
          <h2 class="text-xl font-bold mono text-slate-100 uppercase tracking-tight">Executive Telemetry Report</h2>
          <p class="text-xs text-slate-400 mt-0.5">High-fidelity export cards, podium rankings, and workload cadence.</p>
        </div>
        <div class="flex items-center gap-2">
          <a href="/api/export/markdown" download="tokdash_report.md" class="btn btn-primary text-xs px-3 py-1.5 rounded-lg font-mono">Export Markdown ↗</a>
        </div>
      </section>

      <section class="surface p-6 border border-white/5 rounded-xl space-y-4">
        <h3 class="text-sm font-bold text-slate-200 font-mono">Work Rhythm & Active Hours</h3>
        <p class="text-xs text-slate-400">Peak execution velocity across weekday morning and evening coding sprints.</p>
      </section>
    </div>
  `;
}

export function unmount() {}
