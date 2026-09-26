// =========================================================================
// TOKDASH v4.0 — GLOBAL SEARCH INDEX & MODAL (CMD+K / CTRL+K)
// =========================================================================

import { api } from './api.js';

export class GlobalSearch {
  constructor(router) {
    this.router = router;
    this.sessionsIndex = [];
    this.modelsIndex = [];
    this.toolsIndex = [
      { id: 'hermes', name: 'Hermes Agent', description: 'Autonomous agent runtime' },
      { id: 'antigravity_cli', name: 'Google Antigravity', description: 'Developer AI platform' },
      { id: 'codex', name: 'Codex CLI', description: 'Code generator and terminal agent' },
      { id: 'claude', name: 'Claude Code', description: 'Anthropic Claude CLI' },
      { id: 'opencode', name: 'OpenCode', description: 'Open source coding agent' },
      { id: 'kimi', name: 'Kimi CLI', description: 'Moonshot AI terminal agent' },
    ];
    this.selectedIndex = 0;
    this.currentResults = [];
    this.modalEl = null;
    this.inputEl = null;
    this.resultsContainer = null;
  }

  async buildIndex() {
    try {
      const usage = await api.getUsage({ period: 'all' });
      if (usage && usage.combined_models) {
        this.modelsIndex = usage.combined_models.map(m => ({
          name: m.name || m.model,
          tokens: m.tokens || 0,
          cost: m.cost || 0
        }));
      }

      // Fetch sample sessions from primary tools
      const [hermesRes, agRes] = await Promise.allSettled([
        api.getSessions('hermes', 'all'),
        api.getSessions('antigravity_cli', 'all')
      ]);

      const items = [];
      if (hermesRes.status === 'fulfilled' && hermesRes.value?.sessions) {
        hermesRes.value.sessions.forEach(s => items.push({ ...s, tool: 'hermes' }));
      }
      if (agRes.status === 'fulfilled' && agRes.value?.sessions) {
        agRes.value.sessions.forEach(s => items.push({ ...s, tool: 'antigravity_cli' }));
      }
      this.sessionsIndex = items;
    } catch (err) {
      console.warn('Error building search index:', err);
    }
  }

  init() {
    this.createDom();
    this.bindEvents();
    this.buildIndex();
  }

  createDom() {
    let backdrop = document.getElementById('searchModalBackdrop');
    if (!backdrop) {
      backdrop = document.createElement('div');
      backdrop.id = 'searchModalBackdrop';
      backdrop.className = 'search-modal-backdrop hidden';
      backdrop.innerHTML = `
        <div class="search-modal" onclick="event.stopPropagation()">
          <div class="search-input-wrap">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-indigo-400 mr-2.5 flex-shrink-0"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
            <input type="text" id="globalSearchInput" placeholder="Search sessions, models, tools... (↑↓ to navigate, Enter to open, Esc to close)" autocomplete="off" spellcheck="false" />
            <kbd class="px-1.5 py-0.5 rounded bg-slate-800 text-[10px] text-slate-400 font-mono ml-2 border border-slate-700">ESC</kbd>
          </div>
          <div id="globalSearchResults" class="search-results-list"></div>
          <div class="px-4 py-2 bg-slate-950/60 border-t border-slate-800/80 text-[11px] text-slate-500 font-mono flex items-center justify-between">
            <span>Navigation: <kbd class="px-1 bg-slate-800 rounded">↑</kbd> <kbd class="px-1 bg-slate-800 rounded">↓</kbd> to move · <kbd class="px-1 bg-slate-800 rounded">↵</kbd> select</span>
            <span>TokDash Quick Finder</span>
          </div>
        </div>
      `;
      document.body.appendChild(backdrop);
    }
    this.modalEl = backdrop;
    this.inputEl = document.getElementById('globalSearchInput');
    this.resultsContainer = document.getElementById('globalSearchResults');
  }

  bindEvents() {
    window.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        this.open();
      } else if (e.key === 'Escape' && this.isOpen()) {
        this.close();
      }
    });

    this.modalEl.addEventListener('click', () => this.close());

    this.inputEl.addEventListener('input', (e) => {
      this.search(e.target.value.trim());
    });

    this.inputEl.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        this.moveSelection(1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        this.moveSelection(-1);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        this.executeSelection();
      }
    });
  }

  open() {
    this.modalEl.classList.remove('hidden');
    this.inputEl.value = '';
    this.inputEl.focus();
    this.search('');
  }

  close() {
    this.modalEl.classList.add('hidden');
  }

  isOpen() {
    return !this.modalEl.classList.contains('hidden');
  }

  search(query) {
    const q = query.toLowerCase();
    const results = [];

    // Match Tools
    this.toolsIndex.forEach(t => {
      if (!q || t.name.toLowerCase().includes(q) || t.id.includes(q)) {
        results.push({
          type: 'Tool',
          title: t.name,
          subtitle: t.description,
          action: () => this.router.navigate('/sessions', { tool: t.id })
        });
      }
    });

    // Match Models
    this.modelsIndex.forEach(m => {
      if (!q || m.name.toLowerCase().includes(q)) {
        results.push({
          type: 'Model',
          title: m.name,
          subtitle: `${m.tokens.toLocaleString()} tokens`,
          action: () => this.router.navigate('/sessions', { filter_model: m.name })
        });
      }
    });

    // Match Sessions
    this.sessionsIndex.forEach(s => {
      const matchId = (s.session_id || '').toLowerCase().includes(q);
      const matchProj = (s.project || '').toLowerCase().includes(q);
      const matchModel = (s.model || '').toLowerCase().includes(q);
      if (!q || matchId || matchProj || matchModel) {
        results.push({
          type: 'Session',
          title: s.title || s.project || s.session_id,
          subtitle: `${s.tool || 'hermes'} · ${s.model || 'unknown'} · ${(s.tokens || 0).toLocaleString()} tokens`,
          action: () => this.router.navigate('/sessions', { tool: s.tool || 'hermes', session: s.session_id })
        });
      }
    });

    this.currentResults = results.slice(0, 15);
    this.selectedIndex = 0;
    this.renderResults();
  }

  renderResults() {
    if (!this.currentResults.length) {
      this.resultsContainer.innerHTML = '<div class="text-xs text-slate-500 text-center py-8 font-mono">No matching results found.</div>';
      return;
    }

    this.resultsContainer.innerHTML = this.currentResults.map((r, idx) => {
      const isSelected = idx === this.selectedIndex;
      const typeColor = r.type === 'Tool' ? 'bg-purple-500/15 text-purple-300 border-purple-500/25' : r.type === 'Model' ? 'bg-amber-500/15 text-amber-300 border-amber-500/25' : 'bg-indigo-500/15 text-indigo-300 border-indigo-500/25';

      return `
        <div class="search-result-item ${isSelected ? 'selected' : ''}" data-index="${idx}">
          <div class="flex items-center gap-3">
            <span class="px-2 py-0.5 rounded text-[10px] font-bold border uppercase font-mono ${typeColor}">${r.type}</span>
            <div>
              <div class="text-xs font-semibold text-slate-200 font-mono">${escapeHtml(r.title)}</div>
              <div class="text-[11px] text-slate-400 mt-0.5">${escapeHtml(r.subtitle)}</div>
            </div>
          </div>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-slate-500"><polyline points="9 18 15 12 9 6"/></svg>
        </div>
      `;
    }).join('');

    this.resultsContainer.querySelectorAll('.search-result-item').forEach(item => {
      item.addEventListener('click', () => {
        const idx = parseInt(item.dataset.index, 10);
        this.selectedIndex = idx;
        this.executeSelection();
      });
    });
  }

  moveSelection(dir) {
    if (!this.currentResults.length) return;
    this.selectedIndex = (this.selectedIndex + dir + this.currentResults.length) % this.currentResults.length;
    this.renderResults();
  }

  executeSelection() {
    const item = this.currentResults[this.selectedIndex];
    if (item && typeof item.action === 'function') {
      this.close();
      item.action();
    }
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
