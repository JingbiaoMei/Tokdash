// =========================================================================
// TOKDASH v4.0 — API CLIENT & REAL-TIME SSE STREAMING MANAGER
// =========================================================================

export const LOCAL_SERVER = { id: 'local', label: 'Local', path: '' };

export function serverPath(server, endpoint) {
  const base = window.TOKDASH_BASE_PATH || '';
  const cleanEndpoint = endpoint.startsWith('/') ? endpoint : `/${endpoint}`;
  return `${base}${cleanEndpoint}`;
}

export class ApiClient {
  constructor() {
    this.cache = new Map();
    this.sseConnection = null;
    this.sseRetryTimer = null;
    this.sseRetryCount = 0;
    this.liveSessions = new Set();
  }

  async fetchJson(endpoint, options = {}) {
    const url = serverPath(LOCAL_SERVER, endpoint);
    const res = await fetch(url, options);
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }
    return await res.json();
  }

  async getUsage(params = {}) {
    const sp = new URLSearchParams();
    if (params.period) sp.set('period', params.period);
    if (params.date_from) sp.set('date_from', params.date_from);
    if (params.date_to) sp.set('date_to', params.date_to);
    return await this.fetchJson(`/api/usage?${sp.toString()}`);
  }

  async getSessions(tool, period = 'all') {
    return await this.fetchJson(`/api/sessions?tool=${encodeURIComponent(tool)}&period=${encodeURIComponent(period)}`);
  }

  async getSessionDetail(tool, sessionId) {
    return await this.fetchJson(`/api/session?tool=${encodeURIComponent(tool)}&session_id=${encodeURIComponent(sessionId)}`);
  }

  async getStats(year = null) {
    const q = year ? `?year=${year}` : '';
    return await this.fetchJson(`/api/stats${q}`);
  }

  async getPricingDb() {
    return await this.fetchJson('/api/pricing');
  }

  async getHermesAnalytics() {
    return await this.fetchJson('/api/hermes/analytics');
  }

  // ==========================================
  // Real-Time Server-Sent Events (SSE) Manager
  // ==========================================
  initSSE() {
    if (this.sseConnection) return;
    if (typeof EventSource === 'undefined') {
      console.warn('SSE not supported by browser, falling back to polling.');
      return;
    }

    const sseUrl = serverPath(LOCAL_SERVER, '/api/stream');
    try {
      this.sseConnection = new EventSource(sseUrl);

      this.sseConnection.addEventListener('session-update', (event) => {
        try {
          const payload = JSON.parse(event.data);
          this.handleLiveSessionUpdate(payload);
        } catch (err) {
          console.error('Error parsing SSE session-update payload:', err);
        }
      });

      this.sseConnection.addEventListener('idle', () => {
        this.clearLiveIndicators();
      });

      this.sseConnection.onopen = () => {
        this.sseRetryCount = 0;
        const toast = document.getElementById('sseReconnectingToast');
        if (toast) toast.remove();
      };

      this.sseConnection.onerror = () => {
        this.closeSSE();
        this.showReconnectingToast();
        // Exponential backoff reconnect
        const delay = Math.min(30000, 2000 * Math.pow(1.5, this.sseRetryCount++));
        this.sseRetryTimer = setTimeout(() => this.initSSE(), delay);
      };
    } catch (err) {
      console.warn('Failed to initialize EventSource:', err);
    }

    // Page Visibility API integration: pause stream when tab hidden to conserve resources
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        this.closeSSE();
      } else {
        this.initSSE();
      }
    });
  }

  closeSSE() {
    if (this.sseConnection) {
      this.sseConnection.close();
      this.sseConnection = null;
    }
    if (this.sseRetryTimer) {
      clearTimeout(this.sseRetryTimer);
      this.sseRetryTimer = null;
    }
  }

  handleLiveSessionUpdate(payload) {
    const { session_id, tool, tokens_in, tokens_out, delta, model } = payload;
    this.liveSessions.add(session_id);

    // 1. Dispatch custom event for active tabs/views
    window.dispatchEvent(new CustomEvent('tokdash:live-session-update', { detail: payload }));

    // 2. Increment "Tokens Today" headline KPI on Overview if present
    const totalTokensEl = document.getElementById('totalTokens');
    if (totalTokensEl && delta > 0) {
      const currentRaw = parseInt(totalTokensEl.dataset.rawTokens || totalTokensEl.textContent.replace(/[^0-9]/g, '')) || 0;
      const nextVal = currentRaw + delta;
      totalTokensEl.dataset.rawTokens = nextVal;
      totalTokensEl.textContent = nextVal.toLocaleString();
      totalTokensEl.classList.add('text-indigo-300');
      setTimeout(() => totalTokensEl.classList.remove('text-indigo-300'), 500);
    }

    // 3. Find matching session row in visible tables and pulse
    const rows = document.querySelectorAll(`[data-session-id="${session_id}"]`);
    rows.forEach(row => {
      row.classList.add('animate-flash-row');
      setTimeout(() => row.classList.remove('animate-flash-row'), 800);

      // Add or update LIVE badge
      let badge = row.querySelector('.live-badge');
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'live-badge inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30';
        badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse-live"></span>LIVE';
        const titleCell = row.querySelector('.session-title-cell') || row.firstElementChild;
        if (titleCell) titleCell.appendChild(badge);
      }
    });
  }

  clearLiveIndicators() {
    this.liveSessions.clear();
    document.querySelectorAll('.live-badge').forEach(b => b.remove());
  }

  showReconnectingToast() {
    if (document.getElementById('sseReconnectingToast')) return;
    const toast = document.createElement('div');
    toast.id = 'sseReconnectingToast';
    toast.className = 'fixed bottom-4 right-4 z-50 px-3 py-1.5 rounded-lg bg-amber-500/20 text-amber-300 border border-amber-500/30 text-xs font-mono flex items-center gap-2 shadow-lg';
    toast.innerHTML = '<span class="w-2 h-2 rounded-full bg-amber-400 animate-ping"></span> Stream reconnecting...';
    document.body.appendChild(toast);
  }
}

export const api = new ApiClient();
