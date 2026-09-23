// =========================================================================
// TOKDASH v4.0 — TABS: TOOLS REGISTRY
// =========================================================================

import { api } from '../api.js';

export async function mount(containerEl, routeState) {
  containerEl.innerHTML = `
    <div id="tools-content" class="tab-content active p-6 max-w-7xl mx-auto space-y-6 animate-fade-in">
      <section class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2 border-b border-slate-800/80">
        <div>
          <h2 class="text-xl font-bold mono text-slate-100 uppercase tracking-tight">Agent Capabilities & Tools</h2>
          <p class="text-xs text-slate-400 mt-0.5">Execution distribution across terminal commands, file edits, and search APIs.</p>
        </div>
      </section>

      <section class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4" id="toolsGrid">
        <div class="surface p-4 rounded-xl border border-white/5 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-bold font-mono text-purple-300">run_command / exec</span>
            <span class="text-[10px] text-emerald-400 font-mono font-bold">Active</span>
          </div>
          <p class="text-xs text-slate-400">Terminal shell execution and build commands.</p>
        </div>
        <div class="surface p-4 rounded-xl border border-white/5 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-bold font-mono text-indigo-300">view_file / read_file</span>
            <span class="text-[10px] text-emerald-400 font-mono font-bold">Active</span>
          </div>
          <p class="text-xs text-slate-400">Filesystem read and context inspection.</p>
        </div>
        <div class="surface p-4 rounded-xl border border-white/5 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-bold font-mono text-teal-300">replace_file_content</span>
            <span class="text-[10px] text-emerald-400 font-mono font-bold">Active</span>
          </div>
          <p class="text-xs text-slate-400">Targeted source code modifications.</p>
        </div>
      </section>
    </div>
  `;
}

export function unmount() {}
