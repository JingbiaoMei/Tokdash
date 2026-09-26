// =========================================================================
// TOKDASH v4.0 — TABS: SESSIONS EXPLORER (QUERY OBJECTS & COMPARISON)
// =========================================================================

import { api } from '../api.js';
import { createChart, destroyChart } from '../charts.js';

let cachedSessions = [];
let selectedSessionsForCompare = [];
let comparisonChart = null;
let pricingModels = [];

export async function mount(containerEl, routeState) {
  const { params, router } = routeState;
  const activeTool = params.tool || 'hermes';

  containerEl.innerHTML = `
    <div id="sessions-content" class="tab-content active p-6 max-w-7xl mx-auto space-y-6 animate-fade-in">
      
      <!-- Section Header (Feature 3: Tool logo appears once here) -->
      <section class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2 border-b border-slate-800/80">
        <div class="flex items-center gap-3">
          <div class="w-10 h-10 rounded-xl bg-purple-500/15 border border-purple-500/30 flex items-center justify-center text-purple-300 font-bold font-mono">
            ${activeTool.substring(0, 2).toUpperCase()}
          </div>
          <div>
            <div class="flex items-center gap-2">
              <h2 class="text-xl font-bold mono text-slate-100 uppercase tracking-tight">${escapeHtml(activeTool)} Sessions</h2>
              <span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-purple-500/15 text-purple-300 border border-purple-500/25 font-mono" id="sessionsCountBadge">0 sessions</span>
            </div>
            <p class="text-xs text-slate-400 mt-0.5">Filter, search, inspect execution traces, and compare multi-turn sessions.</p>
          </div>
        </div>

        <!-- Tool Switcher Chips -->
        <div class="flex items-center gap-1.5 overflow-x-auto pb-1 sm:pb-0" id="sessionsToolChips">
          <button type="button" class="query-preset-chip ${activeTool === 'hermes' ? 'active !border-purple-500 !text-purple-300 !bg-purple-500/20' : ''}" data-tool="hermes">Hermes</button>
          <button type="button" class="query-preset-chip ${activeTool === 'antigravity_cli' ? 'active !border-purple-500 !text-purple-300 !bg-purple-500/20' : ''}" data-tool="antigravity_cli">Antigravity</button>
          <button type="button" class="query-preset-chip ${activeTool === 'codex' ? 'active !border-purple-500 !text-purple-300 !bg-purple-500/20' : ''}" data-tool="codex">Codex</button>
          <button type="button" class="query-preset-chip ${activeTool === 'claude' ? 'active !border-purple-500 !text-purple-300 !bg-purple-500/20' : ''}" data-tool="claude">Claude Code</button>
        </div>
      </section>

      <!-- 6A. Query Bar & Filter Controls -->
      <section class="space-y-3">
        <div class="query-bar flex flex-wrap items-center gap-2.5">
          <span class="text-xs text-slate-400 font-mono font-bold">Filter:</span>
          
          <select id="queryFieldSelect" class="ui-input text-xs py-1.5 px-2.5 rounded-lg font-mono">
            <option value="model">Model</option>
            <option value="tokens_total">Total Tokens</option>
            <option value="cost">Cost ($)</option>
            <option value="tokens_in">Input Tokens</option>
            <option value="tokens_out">Output Tokens</option>
          </select>

          <select id="queryOpSelect" class="ui-input text-xs py-1.5 px-2.5 rounded-lg font-mono">
            <option value="contains">contains</option>
            <option value="gt">&gt;</option>
            <option value="lt">&lt;</option>
            <option value="eq">=</option>
          </select>

          <input type="text" id="queryValInput" class="ui-input text-xs py-1.5 px-3 rounded-lg flex-1 min-w-[140px] font-mono" placeholder="Value..." />

          <button id="queryApplyBtn" class="btn btn-primary text-xs px-3 py-1.5 rounded-lg font-mono">Apply</button>
          <button id="queryClearBtn" class="btn btn-ghost text-xs px-3 py-1.5 rounded-lg border border-slate-700 text-slate-400 font-mono">Clear</button>

          <!-- Compare Toggle -->
          <div class="ml-auto flex items-center gap-2">
            <button id="toggleCompareModeBtn" class="btn btn-ghost text-xs px-3 py-1.5 rounded-lg border border-indigo-500/30 text-indigo-300 font-mono hover:bg-indigo-500/15">
              <span>Compare (0/2)</span>
            </button>
          </div>
        </div>

        <!-- 6D. Preset Query Chips -->
        <div class="flex items-center gap-2 text-xs font-mono">
          <span class="text-slate-500 text-[11px]">Presets:</span>
          <button type="button" class="query-preset-chip" data-preset="cost">&gt; $0.50 High Cost</button>
          <button type="button" class="query-preset-chip" data-preset="large">&gt; 100K Tokens Context</button>
          <button type="button" class="query-preset-chip" data-preset="cache">&lt; 50% Low Cache</button>
        </div>
      </section>

      <!-- Active Filters Tag Strip -->
      <div id="activeFiltersContainer" class="flex flex-wrap items-center gap-2"></div>

      <!-- 6B. Session Table -->
      <section class="surface overflow-hidden border border-white/5 rounded-xl shadow-sm">
        <div class="overflow-x-auto">
          <table class="w-full text-left border-collapse text-xs font-mono">
            <thead>
              <tr class="border-b border-slate-800 bg-slate-950/40 text-slate-400">
                <th class="py-3 px-4 w-10 text-center" id="thCompareCheckbox" style="display:none;">Sel</th>
                <th class="py-3 px-4">Session & Project</th>
                <th class="py-3 px-4">Model & Cost</th>
                <th class="py-3 px-4 text-right">In Tokens</th>
                <th class="py-3 px-4 text-right">Cache</th>
                <th class="py-3 px-4 text-right">Out Tokens</th>
                <th class="py-3 px-4 text-right">Total Tokens</th>
                <th class="py-3 px-4 text-right">Action</th>
              </tr>
            </thead>
            <tbody id="sessionsTableBody" class="divide-y divide-slate-800/60">
              <tr><td colspan="8" class="text-center py-12 text-slate-500">Loading session telemetry...</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- 6C. Comparison Panel (Revealed when 2 sessions selected) -->
      <section id="sessionsComparePanel" class="comparison-panel hidden">
        <div class="flex items-center justify-between pb-3 border-b border-indigo-500/25 mb-4">
          <div class="flex items-center gap-2">
            <span class="text-sm font-bold text-slate-100 font-mono">Side-by-Side Session Comparison</span>
            <span class="text-[10px] px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 font-mono font-bold">Diff Analysis</span>
          </div>
          <button id="closeCompareBtn" class="text-slate-400 hover:text-slate-200 text-xs font-mono">Close</button>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-4" id="compareMetricsContainer">
          <!-- Populated by JS -->
        </div>

        <div class="mt-4 h-48 w-full relative">
          <canvas id="sessionsCompareChartCanvas"></canvas>
        </div>
      </section>

      <!-- 10A. "What if you used a different model?" Pricing Calculator -->
      <section class="surface p-4 border border-white/5 rounded-xl space-y-3" id="whatIfPanel">
        <div class="flex items-center justify-between">
          <h4 class="text-xs font-extrabold mono text-slate-200">What if you used a different model?</h4>
          <span class="text-[10px] text-slate-400 font-mono">Pricing Database Lens</span>
        </div>
        <div class="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 text-xs font-mono">
          <div class="text-slate-400">Selected session: <span id="whatIfSessionTitle" class="text-slate-200 font-bold">—</span></div>
          <div class="flex items-center gap-2 sm:ml-auto">
            <span class="text-slate-400">Compare against:</span>
            <select id="whatIfModelSelect" class="ui-input text-xs py-1 px-2 rounded-lg font-mono">
              <option value="">Choose comparison model...</option>
            </select>
          </div>
        </div>
        <div id="whatIfResultBox" class="p-3 rounded-lg bg-slate-950/60 border border-slate-800 text-xs font-mono text-slate-300 hidden">
          <!-- Populated dynamically on select -->
        </div>
      </section>

    </div>
  `;

  // Bind Events
  containerEl.querySelectorAll('#sessionsToolChips button').forEach(chip => {
    chip.addEventListener('click', () => {
      const tool = chip.dataset.tool;
      router.setQueryParams({ tool });
    });
  });

  const queryField = containerEl.querySelector('#queryFieldSelect');
  const queryOp = containerEl.querySelector('#queryOpSelect');
  const queryVal = containerEl.querySelector('#queryValInput');
  const queryApply = containerEl.querySelector('#queryApplyBtn');
  const queryClear = containerEl.querySelector('#queryClearBtn');

  queryApply.addEventListener('click', () => {
    const field = queryField.value;
    const op = queryOp.value;
    const val = queryVal.value.trim();
    if (!val) return;
    router.setQueryParams({ [`filter_${field}_${op}`]: val });
  });

  queryClear.addEventListener('click', () => {
    const params = { ...routeState.params };
    Object.keys(params).forEach(k => {
      if (k.startsWith('filter_')) delete params[k];
    });
    router.navigate(routeState.path, { tool: activeTool });
  });

  // Preset chips
  containerEl.querySelectorAll('[data-preset]').forEach(btn => {
    btn.addEventListener('click', () => {
      const p = btn.dataset.preset;
      if (p === 'cost') router.setQueryParams({ filter_cost_gt: '0.5' });
      else if (p === 'large') router.setQueryParams({ filter_tokens_total_gt: '100000' });
      else if (p === 'cache') router.setQueryParams({ filter_cache_hit_lt: '0.5' });
    });
  });

  // Compare mode toggle
  const compareBtn = containerEl.querySelector('#toggleCompareModeBtn');
  compareBtn.addEventListener('click', () => {
    const th = containerEl.querySelector('#thCompareCheckbox');
    const isShowing = th.style.display !== 'none';
    th.style.display = isShowing ? 'none' : 'table-cell';
    containerEl.querySelectorAll('.td-compare-checkbox').forEach(td => {
      td.style.display = isShowing ? 'none' : 'table-cell';
    });
  });

  containerEl.querySelector('#closeCompareBtn').addEventListener('click', () => {
    containerEl.querySelector('#sessionsComparePanel').classList.add('hidden');
    selectedSessionsForCompare = [];
    updateCompareCheckboxes();
  });

  // Load Data
  await loadSessionsData(activeTool, routeState);
}

export function unmount() {
  if (comparisonChart) {
    destroyChart(comparisonChart);
    comparisonChart = null;
  }
}

async function loadSessionsData(activeTool, routeState) {
  const tbody = document.getElementById('sessionsTableBody');
  const badge = document.getElementById('sessionsCountBadge');
  if (!tbody) return;

  try {
    const [res, pricingRes] = await Promise.all([
      api.getSessions(activeTool, 'all'),
      api.getPricingDb().catch(() => ({ models: {} }))
    ]);

    cachedSessions = res.sessions || [];
    pricingModels = Object.keys(pricingRes.models || {});

    // Populate What-if models dropdown
    const modelSelect = document.getElementById('whatIfModelSelect');
    if (modelSelect && pricingModels.length) {
      modelSelect.innerHTML = '<option value="">Choose comparison model...</option>' +
        pricingModels.slice(0, 30).map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('');
    }

    // Apply client-side filters from route params
    const filtered = applyQueryFilters(cachedSessions, routeState.params);

    if (badge) badge.textContent = `${filtered.length} session${filtered.length === 1 ? '' : 's'}`;

    renderSessionsTable(filtered, activeTool);
  } catch (err) {
    console.error('Error loading sessions:', err);
    tbody.innerHTML = `<tr><td colspan="8" class="text-center py-12 text-red-400 font-mono">Failed to load sessions: ${escapeHtml(err.message)}</td></tr>`;
  }
}

function applyQueryFilters(sessions, params) {
  let result = [...sessions];

  for (const [key, value] of Object.entries(params)) {
    if (!key.startsWith('filter_')) continue;
    const parts = key.replace('filter_', '').split('_');
    const op = parts.pop();
    const field = parts.join('_');

    result = result.filter(s => {
      let rawVal = s[field];
      if (field === 'tokens_total') rawVal = s.tokens;
      if (field === 'model') rawVal = s.model || '';

      if (op === 'contains') {
        return String(rawVal || '').toLowerCase().includes(String(value).toLowerCase());
      } else if (op === 'gt') {
        return Number(rawVal || 0) > Number(value);
      } else if (op === 'lt') {
        return Number(rawVal || 0) < Number(value);
      } else if (op === 'eq') {
        return Number(rawVal || 0) === Number(value);
      }
      return true;
    });
  }

  return result;
}

function renderSessionsTable(sessions, tool) {
  const tbody = document.getElementById('sessionsTableBody');
  if (!tbody) return;

  if (!sessions.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-12 text-slate-500 font-mono">No matching sessions found.</td></tr>';
    return;
  }

  tbody.innerHTML = sessions.slice(0, 60).map(s => {
    const sid = s.session_id;
    const costUsd = Number(s.cost || 0);
    const costBadge = costUsd > 0 ? `<span class="ml-1.5 px-1.5 py-0.2 rounded text-[10px] font-bold bg-emerald-500/15 text-emerald-400 font-mono border border-emerald-500/25">$${costUsd.toFixed(2)}</span>` : '';

    return `
      <tr class="hover:bg-slate-900/60 transition cursor-pointer group" data-session-id="${escapeHtml(sid)}" onclick="openSessionInspector('${escapeHtml(tool)}', '${escapeHtml(sid)}')">
        <td class="py-3 px-4 text-center td-compare-checkbox" style="display:none;" onclick="event.stopPropagation()">
          <input type="checkbox" class="session-compare-cb rounded bg-slate-800 border-slate-700 text-indigo-500 cursor-pointer" data-sid="${escapeHtml(sid)}" onchange="onCompareCheckboxChange(this, '${escapeHtml(sid)}')" />
        </td>
        <td class="py-3 px-4 session-title-cell font-mono">
          <div class="flex items-center gap-2">
            <span class="text-slate-100 font-bold group-hover:text-indigo-300 transition truncate max-w-[200px]" title="${escapeHtml(s.title || sid)}">${escapeHtml(s.title || sid)}</span>
          </div>
          <div class="text-[10px] text-slate-400 font-mono mt-0.5 truncate max-w-[220px]">${escapeHtml(s.project || 'workspace')}</div>
        </td>
        <td class="py-3 px-4 font-mono">
          <div class="flex items-center">
            <span class="text-slate-300 truncate max-w-[140px]" title="${escapeHtml(s.model || '—')}">${escapeHtml(s.model || '—')}</span>
            ${costBadge}
          </div>
        </td>
        <td class="py-3 px-4 text-right text-slate-400 font-mono">${(s.tokens_in || 0).toLocaleString()}</td>
        <td class="py-3 px-4 text-right text-emerald-400 font-mono">${(s.tokens_cache || 0).toLocaleString()}</td>
        <td class="py-3 px-4 text-right text-amber-400 font-mono">${(s.tokens_out || 0).toLocaleString()}</td>
        <td class="py-3 px-4 text-right text-indigo-300 font-bold font-mono">${(s.tokens || 0).toLocaleString()}</td>
        <td class="py-3 px-4 text-right font-mono" onclick="event.stopPropagation()">
          <button class="btn btn-ghost text-[11px] px-2 py-1 border border-slate-800 hover:border-indigo-500/40 text-indigo-300" onclick="openSessionInspector('${escapeHtml(tool)}', '${escapeHtml(sid)}')">
            Inspect ↗
          </button>
        </td>
      </tr>
    `;
  }).join('');
}

// Global hook for comparison checkbox
window.onCompareCheckboxChange = function(cb, sid) {
  const session = cachedSessions.find(s => s.session_id === sid);
  if (!session) return;

  if (cb.checked) {
    if (selectedSessionsForCompare.length >= 2) {
      cb.checked = false;
      alert('You can compare a maximum of 2 sessions. Deselect one to compare.');
      return;
    }
    selectedSessionsForCompare.push(session);
  } else {
    selectedSessionsForCompare = selectedSessionsForCompare.filter(s => s.session_id !== sid);
  }

  const btn = document.getElementById('toggleCompareModeBtn');
  if (btn) btn.innerHTML = `<span>Compare (${selectedSessionsForCompare.length}/2)</span>`;

  if (selectedSessionsForCompare.length === 2) {
    renderComparisonPanel(selectedSessionsForCompare[0], selectedSessionsForCompare[1]);
  } else {
    document.getElementById('sessionsComparePanel')?.classList.add('hidden');
  }
};

function renderComparisonPanel(s1, s2) {
  const panel = document.getElementById('sessionsComparePanel');
  const metricsEl = document.getElementById('compareMetricsContainer');
  if (!panel || !metricsEl) return;
  panel.classList.remove('hidden');

  const diffTokens = ((s2.tokens - s1.tokens) / Math.max(1, s1.tokens)) * 100;
  const diffCost = (s2.cost || 0) - (s1.cost || 0);

  metricsEl.innerHTML = `
    <div class="p-3 rounded-lg bg-slate-900/60 border border-slate-800 text-xs font-mono">
      <div class="text-slate-400 font-bold mb-1">Session A (${escapeHtml(s1.model || 'unknown')})</div>
      <div>Total: <span class="text-indigo-300 font-bold">${(s1.tokens || 0).toLocaleString()}</span> tokens</div>
      <div>Cost: <span class="text-emerald-400">$${(s1.cost || 0).toFixed(2)}</span></div>
    </div>
    <div class="p-3 rounded-lg bg-slate-900/60 border border-slate-800 text-xs font-mono">
      <div class="text-slate-400 font-bold mb-1">Session B (${escapeHtml(s2.model || 'unknown')})</div>
      <div>Total: <span class="text-indigo-300 font-bold">${(s2.tokens || 0).toLocaleString()}</span> tokens</div>
      <div>Cost: <span class="text-emerald-400">$${(s2.cost || 0).toFixed(2)}</span></div>
    </div>
    <div class="p-3 rounded-lg bg-indigo-950/30 border border-indigo-500/30 text-xs font-mono">
      <div class="text-indigo-300 font-bold mb-1">Comparative Variance</div>
      <div>Volume: <span class="${diffTokens >= 0 ? 'text-amber-400' : 'text-emerald-400'} font-bold">${diffTokens >= 0 ? '+' : ''}${diffTokens.toFixed(1)}%</span></div>
      <div>Cost Diff: <span class="text-emerald-400 font-bold">${diffCost >= 0 ? '+' : ''}$${diffCost.toFixed(2)}</span></div>
    </div>
  `;

  const canvas = document.getElementById('sessionsCompareChartCanvas');
  if (canvas) {
    comparisonChart = createChart(canvas, {
      type: 'bar',
      data: {
        labels: ['Input Tokens', 'Cache Reads', 'Output Tokens'],
        datasets: [
          { label: 'Session A', data: [s1.tokens_in || 0, s1.tokens_cache || 0, s1.tokens_out || 0], backgroundColor: '#3B82F6' },
          { label: 'Session B', data: [s2.tokens_in || 0, s2.tokens_cache || 0, s2.tokens_out || 0], backgroundColor: '#818CF8' }
        ]
      }
    });
  }
}

// Global Session Inspector Drawer trigger
window.openSessionInspector = async function(tool, sessionId) {
  const backdrop = document.getElementById('sessionDrawerBackdrop');
  if (!backdrop) return;
  backdrop.classList.add('open');

  const titleEl = document.getElementById('drawerTitle');
  const metaEl = document.getElementById('drawerMeta');
  const msgContainer = document.getElementById('drawerMessagesContainer');
  const toolsContainer = document.getElementById('drawerToolsContainer');

  if (titleEl) titleEl.textContent = `Session ${sessionId.substring(0, 8)}...`;
  if (metaEl) metaEl.textContent = 'Loading detailed conversation history and tool traces...';
  if (msgContainer) msgContainer.innerHTML = '<div class="text-xs text-slate-500 text-center py-8 font-mono">Loading messages...</div>';
  if (toolsContainer) toolsContainer.innerHTML = '<div class="text-xs text-slate-500 text-center py-8 font-mono">Loading tools...</div>';

  // Set what-if session title
  const whatIfTitle = document.getElementById('whatIfSessionTitle');
  if (whatIfTitle) whatIfTitle.textContent = `${tool} · ${sessionId.substring(0, 8)}...`;

  try {
    const data = await api.getSessionDetail(tool, sessionId);
    const session = data.session || {};
    const messages = data.messages || [];
    const toolCalls = data.tool_calls || data.tool_executions || [];

    if (titleEl) titleEl.textContent = session.title || session.project || sessionId;
    if (metaEl) metaEl.textContent = `${tool.toUpperCase()} · ${session.model || 'unknown'} · ${messages.length} messages · ${toolCalls.length} tool executions`;

    // Render Conversation
    renderDrawerMessages(messages);

    // Render Tools
    renderDrawerTools(toolCalls);

    // Render Metrics
    document.getElementById('drawerMetricIn').textContent = (session.tokens_in || 0).toLocaleString();
    document.getElementById('drawerMetricCache').textContent = (session.tokens_cache || 0).toLocaleString();
    document.getElementById('drawerMetricOut').textContent = (session.tokens_out || 0).toLocaleString();
    document.getElementById('drawerMetricTotal').textContent = (session.tokens || 0).toLocaleString();
  } catch (err) {
    console.error('Error loading session detail for inspector:', err);
    if (metaEl) metaEl.textContent = 'Failed to load telemetry detail.';
  }
};

function renderDrawerMessages(messages) {
  const container = document.getElementById('drawerMessagesContainer');
  if (!container) return;
  if (!messages.length) {
    container.innerHTML = '<div class="text-xs text-slate-500 text-center py-8 font-mono">No messages recorded for this session.</div>';
    return;
  }

  container.innerHTML = messages.map((m, idx) => {
    const isUser = m.role === 'user';
    const isTool = m.role === 'tool' || m.role === 'function';
    const roleBadge = isUser ? 'bg-indigo-500/15 text-indigo-300 border-indigo-500/30' : isTool ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' : 'bg-purple-500/15 text-purple-300 border-purple-500/30';
    const cardBorder = isUser ? 'border-l-2 border-l-indigo-500' : isTool ? 'border-l-2 border-l-emerald-500' : 'border-l-2 border-l-purple-500';

    const reasoningHtml = m.reasoning ? `
      <details class="my-2 rounded-lg bg-purple-950/20 border border-purple-500/25 overflow-hidden">
        <summary class="px-3 py-1.5 bg-purple-900/15 cursor-pointer text-xs font-semibold text-purple-300 hover:bg-purple-900/25 flex items-center justify-between">
          <span>💭 Thinking Trace</span>
          <span class="text-[10px] text-purple-400 font-mono">expand</span>
        </summary>
        <div class="p-3 text-xs text-purple-200/90 whitespace-pre-wrap font-mono leading-relaxed bg-black/40 border-t border-purple-500/15 max-h-60 overflow-y-auto">${escapeHtml(m.reasoning)}</div>
      </details>
    ` : '';

    return `
      <div class="surface p-4 rounded-xl border border-slate-800/80 ${cardBorder} shadow-sm">
        <div class="flex items-center justify-between mb-2">
          <span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase font-mono border ${roleBadge}">${isTool ? 'Tool Output' : m.role} #${idx + 1}</span>
          <button class="text-[11px] text-slate-400 hover:text-slate-200 font-mono" onclick="navigator.clipboard.writeText(\`${escapeJs(m.content || '')}\`)">Copy</button>
        </div>
        ${reasoningHtml}
        <div class="text-xs text-slate-200 whitespace-pre-wrap font-mono leading-relaxed break-words overflow-x-auto">${escapeHtml(m.content || '')}</div>
      </div>
    `;
  }).join('');
}

function renderDrawerTools(toolCalls) {
  const container = document.getElementById('drawerToolsContainer');
  if (!container) return;
  if (!toolCalls.length) {
    container.innerHTML = '<div class="text-xs text-slate-500 text-center py-8 font-mono">No tool executions recorded.</div>';
    return;
  }

  container.innerHTML = toolCalls.map(tc => {
    const tName = tc.tool_name || tc.name || 'tool';
    const tArgs = typeof tc.args === 'object' ? JSON.stringify(tc.args, null, 2) : String(tc.args || tc.arguments || '');
    const tOutput = tc.output || tc.result || '';

    return `
      <div class="surface p-3.5 rounded-xl border border-slate-800/80 border-l-2 border-l-indigo-500 shadow-sm space-y-2">
        <div class="flex items-center justify-between">
          <span class="px-2 py-0.5 rounded text-xs font-mono font-bold bg-indigo-500/15 text-indigo-300 border border-indigo-500/30">${escapeHtml(tName)}</span>
          <span class="text-[10px] text-emerald-400 font-mono font-bold">Executed</span>
        </div>
        <div class="text-xs text-slate-300 font-mono bg-slate-950/80 p-2.5 rounded-lg border border-slate-800/80 overflow-x-auto max-h-36">
          <pre class="m-0">${escapeHtml(tArgs)}</pre>
        </div>
        ${tOutput ? `
          <details class="text-xs">
            <summary class="cursor-pointer text-slate-400 hover:text-indigo-300 select-none py-1 flex items-center justify-between font-mono text-[11px]">
              <span>View Output Payload</span>
              <button class="text-slate-500 hover:text-slate-300" onclick="navigator.clipboard.writeText(\`${escapeJs(tOutput)}\`)">Copy Output</button>
            </summary>
            <pre class="mt-1 p-3 rounded-lg bg-black/50 text-emerald-300 font-mono text-[11px] overflow-x-auto max-h-52 whitespace-pre-wrap border border-slate-800">${escapeHtml(tOutput)}</pre>
          </details>
        ` : ''}
      </div>
    `;
  }).join('');
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escapeJs(str) {
  if (!str) return '';
  return String(str).replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/\$/g, '\\$');
}
