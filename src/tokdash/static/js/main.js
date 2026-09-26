// =========================================================================
// TOKDASH v4.0 — MAIN APPLICATION BOOTSTRAPPER & MODULE COORDINATOR
// =========================================================================

import { HashRouter } from './router.js';
import { api } from './api.js';
import { GlobalSearch } from './search.js';
import { KeyboardManager } from './keyboard.js';
import { recreateAllCharts } from './charts.js';

// Import Tab Modules
import * as OverviewTab from './tabs/overview.js';
import * as SessionsTab from './tabs/sessions.js';
import * as HermesTab from './tabs/hermes.js';
import * as StatsTab from './tabs/stats.js';
import * as ModelsTab from './tabs/models.js';
import * as PricingTab from './tabs/pricing.js';
import * as ToolsTab from './tabs/tools.js';
import * as AchievementsTab from './tabs/achievements.js';
import * as QuotaTab from './tabs/quota.js';
import * as ReportTab from './tabs/report.js';

// Setup Router
const router = new HashRouter({
  '/': OverviewTab,
  '/overview': OverviewTab,
  '/sessions': SessionsTab,
  '/hermes': HermesTab,
  '/stats': StatsTab,
  '/models': ModelsTab,
  '/pricing': PricingTab,
  '/tools': ToolsTab,
  '/achievements': AchievementsTab,
  '/quota': QuotaTab,
  '/report': ReportTab
}, '/');

// Global Services
const search = new GlobalSearch(router);
const keyboard = new KeyboardManager(router);

document.addEventListener('DOMContentLoaded', () => {
  const appMount = document.getElementById('app-mount');
  if (appMount) {
    router.init(appMount);
  }

  // Initialize Global Features
  search.init();
  keyboard.init();
  api.initSSE();

  // Setup Theme Switcher
  initThemeSupport();

  // Wire Topbar Actions
  wireTopbarActions();
});

// ==========================================
// Theme Management & WCAG AA Compliance
// ==========================================
function initThemeSupport() {
  const storedTheme = localStorage.getItem('tokdash-theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  applyTheme(storedTheme);

  const quickToggle = document.getElementById('themeToggleQuick');
  if (quickToggle) {
    quickToggle.addEventListener('click', toggleThemeQuick);
  }

  const styleSelect = document.getElementById('styleThemeSelect');
  if (styleSelect) {
    styleSelect.addEventListener('change', (e) => {
      document.documentElement.dataset.uiTheme = e.target.value;
      localStorage.setItem('tokdash-style-theme', e.target.value);
      recreateAllCharts();
    });
  }
}

export function toggleThemeQuick() {
  const isDark = document.documentElement.classList.contains('dark');
  const targetTheme = isDark ? 'light' : 'dark';
  applyTheme(targetTheme);
  localStorage.setItem('tokdash-theme', targetTheme);
  recreateAllCharts();
}

export function applyTheme(theme) {
  const isDark = theme === 'dark';
  document.documentElement.classList.toggle('dark', isDark);

  const sun = document.getElementById('themeQuickSun');
  const moon = document.getElementById('themeQuickMoon');
  if (sun && moon) {
    if (isDark) {
      sun.classList.remove('hidden');
      moon.classList.add('hidden');
    } else {
      sun.classList.add('hidden');
      moon.classList.remove('hidden');
    }
  }

  const metaTheme = document.querySelector('meta[name="theme-color"]');
  if (metaTheme) {
    metaTheme.content = isDark ? '#0B0F19' : '#1E40AF';
  }
}

// ==========================================
// Topbar Actions & Modals
// ==========================================
function wireTopbarActions() {
  // Global search button trigger
  const searchBtn = document.getElementById('topbarSearchBtn');
  if (searchBtn) {
    searchBtn.addEventListener('click', () => search.open());
  }

  // Refresh button
  const refreshBtn = document.getElementById('refreshBtn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      refreshBtn.classList.add('animate-spin');
      router.handleRoute().finally(() => {
        setTimeout(() => refreshBtn.classList.remove('animate-spin'), 600);
      });
    });
  }

  // Settings Panel
  const settingsBtn = document.getElementById('settingsToggle');
  const settingsPanel = document.getElementById('settingsPanel');
  if (settingsBtn && settingsPanel) {
    settingsBtn.addEventListener('click', () => {
      const isHidden = settingsPanel.hidden;
      settingsPanel.hidden = !isHidden;
    });
  }
}

// Global window hooks for HTML compatibility
window.toggleThemeQuick = toggleThemeQuick;
window.applyTheme = applyTheme;
window.openSettingsModal = function() {
  const panel = document.getElementById('settingsPanel');
  if (panel) panel.hidden = false;
};
window.toggleSidebarExportMenu = function(e) {
  if (e) e.stopPropagation();
  const popover = document.getElementById('sidebarExportPopover');
  if (popover) {
    const isHidden = popover.style.display === 'none' || !popover.style.display;
    popover.style.display = isHidden ? 'block' : 'none';
  }
};
window.closeSessionDrawer = function() {
  const drawer = document.getElementById('sessionDrawerBackdrop');
  if (drawer) drawer.classList.remove('open');
};
