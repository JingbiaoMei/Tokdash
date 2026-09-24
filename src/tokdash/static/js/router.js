// =========================================================================
// TOKDASH v4.0 — HASH ROUTER WITH DEEP LINKING (#/:tab?query)
// =========================================================================

export class HashRouter {
  constructor(routes = {}, defaultRoute = '/') {
    this.routes = routes;
    this.defaultRoute = defaultRoute;
    this.currentRoute = null;
    this.currentParams = {};
    this.activeTabModule = null;
    this.mountContainer = null;
    this._debounceTimer = null;

    window.addEventListener('hashchange', () => this.handleRoute());
  }

  init(mountContainer) {
    this.mountContainer = mountContainer;
    this.handleRoute();
  }

  register(path, tabModule) {
    this.routes[path] = tabModule;
  }

  parseHash() {
    let hash = window.location.hash || '#/';
    if (!hash.startsWith('#/')) {
      hash = '#/';
    }
    const raw = hash.slice(2); // strip '#/'
    const [pathPart, queryPart] = raw.split('?');
    const path = '/' + (pathPart || '').toLowerCase().replace(/\/+$/, '');

    const params = {};
    if (queryPart) {
      const searchParams = new URLSearchParams(queryPart);
      for (const [key, value] of searchParams.entries()) {
        params[key] = value;
      }
    }
    return { path: path === '' ? '/' : path, params };
  }

  getRouteState() {
    return {
      path: this.currentRoute,
      params: { ...this.currentParams }
    };
  }

  navigate(path, params = {}, pushHistory = true) {
    let queryString = '';
    if (params && Object.keys(params).length > 0) {
      const sp = new URLSearchParams();
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null && v !== '') {
          sp.set(k, v);
        }
      }
      const qs = sp.toString();
      if (qs) queryString = '?' + qs;
    }

    const cleanPath = path.startsWith('/') ? path.slice(1) : path;
    const targetHash = `#/${cleanPath}${queryString}`;

    if (pushHistory) {
      window.location.hash = targetHash;
    } else {
      const currentUrl = window.location.href.split('#')[0] + targetHash;
      window.history.replaceState(null, '', currentUrl);
      this.handleRoute();
    }
  }

  setQueryParams(newParams, debounceMs = 300) {
    const updated = { ...this.currentParams, ...newParams };
    // Remove null or undefined keys
    for (const [k, v] of Object.entries(newParams)) {
      if (v === null || v === undefined || v === '') {
        delete updated[k];
      }
    }

    if (debounceMs > 0) {
      if (this._debounceTimer) clearTimeout(this._debounceTimer);
      this._debounceTimer = setTimeout(() => {
        this.navigate(this.currentRoute, updated, false);
      }, debounceMs);
    } else {
      this.navigate(this.currentRoute, updated, false);
    }
  }

  async handleRoute() {
    const { path, params } = this.parseHash();
    let targetModule = this.routes[path];

    if (!targetModule) {
      // Fall back to default route
      targetModule = this.routes[this.defaultRoute] || this.routes['/'];
    }

    if (!targetModule) {
      console.warn(`No module registered for route: ${path}`);
      return;
    }

    // Call unmount on previous tab if exists
    if (this.activeTabModule && typeof this.activeTabModule.unmount === 'function') {
      try {
        this.activeTabModule.unmount();
      } catch (err) {
        console.error('Error in tab unmount:', err);
      }
    }

    this.currentRoute = path;
    this.currentParams = params;
    this.activeTabModule = targetModule;

    // Sync active tab in DOM (nav links)
    this.syncNavElements(path);

    // Mount active tab module into mountContainer
    if (this.mountContainer && typeof targetModule.mount === 'function') {
      try {
        await targetModule.mount(this.mountContainer, { path, params, router: this });
      } catch (err) {
        console.error(`Error mounting tab for ${path}:`, err);
        this.mountContainer.innerHTML = `<div class="p-8 text-center text-red-400 font-mono text-xs">Failed to load view: ${err.message}</div>`;
      }
    }

    // Dispatch global route-changed event
    window.dispatchEvent(new CustomEvent('tokdash:route-changed', {
      detail: { path, params }
    }));
  }

  syncNavElements(path) {
    // Map path to tab name (e.g. '/' -> 'overview', '/sessions' -> 'sessions')
    const tabName = path === '/' ? 'overview' : path.slice(1);

    document.querySelectorAll('.tab-btn, .sidebar-nav-item').forEach(el => {
      const target = el.dataset.tab || el.dataset.tabTarget;
      if (target === tabName) {
        el.classList.add('active');
        el.setAttribute('aria-selected', 'true');
      } else {
        el.classList.remove('active');
        el.setAttribute('aria-selected', 'false');
      }
    });
  }
}
