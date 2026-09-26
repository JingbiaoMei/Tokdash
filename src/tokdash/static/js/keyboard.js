// =========================================================================
// TOKDASH v4.0 — KEYBOARD SHORTCUTS & HELP OVERLAY (?)
// =========================================================================

export class KeyboardManager {
  constructor(router) {
    this.router = router;
    this.overlayEl = null;
    this.tabsList = [
      { num: '1', path: '/', name: 'Overview' },
      { num: '2', path: '/sessions', name: 'Sessions' },
      { num: '3', path: '/hermes', name: 'Hermes Hub' },
      { num: '4', path: '/stats', name: 'Heatmap & Stats' },
      { num: '5', path: '/models', name: 'Models Intelligence' },
      { num: '6', path: '/pricing', name: 'Pricing DB' },
      { num: '7', path: '/tools', name: 'Tools Registry' },
      { num: '8', path: '/achievements', name: 'Achievements' },
      { num: '9', path: '/quota', name: 'Quota' },
    ];
  }

  init() {
    this.createDom();
    this.bindEvents();
  }

  createDom() {
    let overlay = document.getElementById('shortcutsHelpOverlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'shortcutsHelpOverlay';
      overlay.className = 'search-modal-backdrop hidden';
      overlay.innerHTML = `
        <div class="search-modal max-w-lg" onclick="event.stopPropagation()">
          <div class="p-4 border-b border-slate-800 flex items-center justify-between bg-slate-900/60">
            <div class="flex items-center gap-2">
              <span class="text-base font-bold text-slate-100 font-mono">Keyboard Shortcuts</span>
              <span class="text-[10px] px-2 py-0.5 rounded bg-indigo-500/15 text-indigo-300 border border-indigo-500/30 font-mono">v4.0</span>
            </div>
            <button onclick="document.getElementById('shortcutsHelpOverlay').classList.add('hidden')" class="p-1 rounded text-slate-400 hover:text-slate-200">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
            </button>
          </div>
          <div class="p-5 space-y-4 max-h-[480px] overflow-y-auto text-xs font-mono">
            <div>
              <div class="text-[11px] font-bold uppercase tracking-wider text-indigo-400 mb-2 font-sans">Navigation</div>
              <div class="grid grid-cols-2 gap-2 text-slate-300">
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>Overview</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">1</kbd></div>
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>Sessions</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">2</kbd></div>
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>Hermes Hub</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">3</kbd></div>
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>Stats & Heatmap</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">4</kbd></div>
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>Models</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">5</kbd></div>
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>Pricing</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">6</kbd></div>
              </div>
            </div>

            <div>
              <div class="text-[11px] font-bold uppercase tracking-wider text-indigo-400 mb-2 font-sans">Quick Date Ranges</div>
              <div class="grid grid-cols-3 gap-2 text-slate-300">
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>Today</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">T</kbd></div>
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>This Week</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">W</kbd></div>
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>This Month</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">M</kbd></div>
              </div>
            </div>

            <div>
              <div class="text-[11px] font-bold uppercase tracking-wider text-indigo-400 mb-2 font-sans">Actions & Modals</div>
              <div class="space-y-1.5 text-slate-300">
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>Global Search Finder</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">Cmd / Ctrl + K</kbd></div>
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>Close modal / drawer</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">Esc</kbd></div>
                <div class="flex items-center justify-between p-2 rounded bg-slate-900/40 border border-slate-800"><span>Show this keyboard guide</span> <kbd class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">?</kbd></div>
              </div>
            </div>
          </div>
          <div class="px-4 py-2 bg-slate-950/60 border-t border-slate-800/80 text-[11px] text-slate-500 font-mono text-center">
            Press <kbd class="px-1 bg-slate-800 rounded">Esc</kbd> to close
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
    }
    this.overlayEl = overlay;
  }

  bindEvents() {
    this.overlayEl.addEventListener('click', () => this.hideHelp());

    window.addEventListener('keydown', (e) => {
      // Ignore if user is currently typing in an input or textarea
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) {
        if (e.key === 'Escape') {
          e.target.blur();
        }
        return;
      }

      if (e.key === '?' && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        this.toggleHelp();
      } else if (e.key === 'Escape') {
        this.hideHelp();
        // Close slide-over drawer if open
        const drawer = document.getElementById('sessionDrawerBackdrop');
        if (drawer && drawer.classList.contains('open')) {
          drawer.classList.remove('open');
        }
      } else if (e.key >= '1' && e.key <= '9' && !e.ctrlKey && !e.metaKey) {
        const item = this.tabsList.find(t => t.num === e.key);
        if (item) {
          e.preventDefault();
          this.router.navigate(item.path);
        }
      } else if (e.key.toLowerCase() === 't' && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        const btn = document.querySelector('[data-range="today"]');
        if (btn) btn.click();
      } else if (e.key.toLowerCase() === 'w' && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        const btn = document.querySelector('[data-range="week"]');
        if (btn) btn.click();
      } else if (e.key.toLowerCase() === 'm' && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        const btn = document.querySelector('[data-range="month"]');
        if (btn) btn.click();
      }
    });
  }

  showHelp() {
    this.overlayEl.classList.remove('hidden');
  }

  hideHelp() {
    this.overlayEl.classList.add('hidden');
  }

  toggleHelp() {
    if (this.overlayEl.classList.contains('hidden')) {
      this.showHelp();
    } else {
      this.hideHelp();
    }
  }
}
