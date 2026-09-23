// =========================================================================
// TOKDASH v4.0 — TABS: HERMES HUB
// =========================================================================

import { api } from '../api.js';

export async function mount(containerEl, routeState) {
  containerEl.innerHTML = `
    <div id="hermes-content" class="tab-content active p-6 max-w-7xl mx-auto space-y-6 animate-fade-in">
      
      <!-- Hermes Hero Banner -->
      <section class="surface p-5 sm:p-6 relative overflow-hidden rounded-xl border border-white/5" style="background: linear-gradient(135deg, rgba(88, 28, 135, 0.4) 0%, rgba(15, 23, 42, 0.95) 100%);">
        <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div class="flex items-center gap-3.5">
            <div class="w-12 h-12 rounded-xl bg-purple-500/20 border border-purple-500/40 flex items-center justify-center text-purple-300 font-bold text-xl font-mono">
              H
            </div>
            <div>
              <div class="flex items-center gap-2">
                <h2 class="text-xl font-bold mono text-slate-100">Hermes Agent Hub</h2>
                <span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 font-mono">Connected</span>
              </div>
              <p class="text-xs text-slate-400 mt-1">Autonomous telemetry, tool execution distribution, and subagent orchestration.</p>
            </div>
          </div>
          <div class="flex items-center gap-2">
            <a href="#/sessions?tool=hermes" class="btn btn-primary text-xs px-3 py-1.5 rounded-lg font-mono">View Sessions Explorer ↗</a>
          </div>
        </div>
      </section>

      <!-- Hermes Analytics KPIs -->
      <section class="grid grid-cols-2 md:grid-cols-4 gap-4" id="hermesKpisGrid">
        <div class="surface p-4 border border-white/5">
          <div class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Recorded Sessions</div>
          <div class="text-2xl font-extrabold mt-1 font-mono text-purple-400" id="hermesTotalSessionsKpi">-</div>
        </div>
        <div class="surface p-4 border border-white/5">
          <div class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Total Tool Calls</div>
          <div class="text-2xl font-extrabold mt-1 font-mono text-indigo-400" id="hermesTotalToolsKpi">-</div>
        </div>
        <div class="surface p-4 border border-white/5">
          <div class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Total Messages</div>
          <div class="text-2xl font-extrabold mt-1 font-mono text-cyan-400" id="hermesTotalMessagesKpi">-</div>
        </div>
        <div class="surface p-4 border border-white/5">
          <div class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Database Status</div>
          <div class="text-2xl font-extrabold mt-1 font-mono text-emerald-400">Online</div>
        </div>
      </section>

      <!-- Two-column: Tools Distribution & Projects -->
      <section class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="surface p-5 border border-white/5 space-y-3">
          <div class="flex items-center justify-between pb-2 border-b border-slate-800">
            <h3 class="text-sm font-bold mono text-slate-200">Tool Execution Distribution</h3>
            <span class="text-[10px] text-purple-400 font-mono">Top Tools</span>
          </div>
          <div id="hermesToolsList" class="space-y-2 text-xs font-mono">
            <div class="text-slate-500 py-6 text-center">Loading tools...</div>
          </div>
        </div>

        <div class="surface p-5 border border-white/5 space-y-3">
          <div class="flex items-center justify-between pb-2 border-b border-slate-800">
            <h3 class="text-sm font-bold mono text-slate-200">Active Projects</h3>
            <span class="text-[10px] text-indigo-400 font-mono">Workspaces</span>
          </div>
          <div id="hermesProjectsList" class="space-y-2 text-xs font-mono">
            <div class="text-slate-500 py-6 text-center">Loading projects...</div>
          </div>
        </div>
      </section>

    </div>
  `;

  await loadHermesData();
}

export function unmount() {}

async function loadHermesData() {
  try {
    const data = await api.getHermesAnalytics();
    
    document.getElementById('hermesTotalSessionsKpi').textContent = (data.total_sessions || 0).toLocaleString();
    document.getElementById('hermesTotalToolsKpi').textContent = (data.total_tool_calls || 0).toLocaleString();
    document.getElementById('hermesTotalMessagesKpi').textContent = (data.total_messages || 0).toLocaleString();

    const toolsContainer = document.getElementById('hermesToolsList');
    if (toolsContainer && data.tools) {
      toolsContainer.innerHTML = data.tools.slice(0, 10).map(t => `
        <div class="flex items-center justify-between p-2 rounded bg-slate-900/60 border border-slate-800">
          <span class="text-purple-300 font-bold">${t.name}</span>
          <span class="text-slate-300 font-mono">${(t.count || 0).toLocaleString()} calls</span>
        </div>
      `).join('');
    }

    const projectsContainer = document.getElementById('hermesProjectsList');
    if (projectsContainer && data.projects) {
      projectsContainer.innerHTML = data.projects.slice(0, 8).map(p => `
        <div class="flex items-center justify-between p-2 rounded bg-slate-900/60 border border-slate-800">
          <span class="text-indigo-300 font-bold truncate max-w-[220px]" title="${p.name}">${p.name}</span>
          <span class="text-slate-300 font-mono">${p.count || 0} sessions</span>
        </div>
      `).join('');
    }
  } catch (err) {
    console.error('Error loading Hermes hub:', err);
  }
}
