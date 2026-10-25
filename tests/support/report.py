"""Run activateDashboardTab, the dashboard's tab switch, under Node.

The Report and output-speed pages hang their request gates off the tab switch:
arriving sets a page's `active` flag and then loads or resumes it, leaving
retires whatever that page still had in flight, and the top bar swaps the quick
ranges for the report's period chips. No other test runs the switch, so this
module builds it once for both pages' test files.

The program carries the real function and the real `usageReportState` and
`usageReportSpeedState` literals, over a stub DOM with one button and one panel
per tab. Every dispatch the switch can make is a stub that logs its name and the
gate it found when it was called, so a test reads both what ran and in what
order the flag was set. `visit(tab)` runs one switch and returns that log with
the panels, flags and top-bar strips it left behind.
"""
from __future__ import annotations

from support.frontend import extract_js_function, index_source

ACTIVATE_PRELUDE = r"""
const log = [];
function stubNode(id) {
  const classes = new Set();
  return {
    id, hidden: false, dataset: {}, attrs: {},
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, on) => {
        const want = on === undefined ? !classes.has(name) : !!on;
        if (want) classes.add(name); else classes.delete(name);
        return want;
      },
    },
    setAttribute(name, value) { this.attrs[name] = String(value); },
  };
}
const TABS = ['overview', 'sessions', 'stats', 'report', 'speed', 'quota', 'pricing', 'servers'];
const tabButtons = {};
const tabPanels = {};
const sidebarItems = [];
TABS.forEach((tab) => {
  tabButtons[tab] = stubNode(`${tab}-button`);
  tabButtons[tab].dataset.tab = tab;
  tabPanels[`${tab}-content`] = stubNode(`${tab}-content`);
  const item = stubNode(`${tab}-sidebar`);
  item.dataset.tabTarget = tab;
  sidebarItems.push(item);
});
tabPanels['overview-content'].classList.add('active');
const topBar = { overviewQuickRanges: stubNode('overviewQuickRanges'), reportPeriodChips: stubNode('reportPeriodChips') };
topBar.reportPeriodChips.hidden = true;
const document = {
  getElementById: (id) => tabPanels[id] || topBar[id] || null,
  querySelector(selector) {
    const button = /^\.tab-btn\[data-tab="([^"]+)"\]$/.exec(selector);
    if (button) return tabButtons[button[1]] || null;
    const item = /^\.sidebar-nav-item\[data-tab-target="([^"]+)"\]$/.exec(selector);
    if (item) return sidebarItems.find((entry) => entry.dataset.tabTarget === item[1]) || null;
    throw new Error(`unexpected selector ${selector}`);
  },
  querySelectorAll(selector) {
    if (selector === '.tab-btn') return Object.values(tabButtons);
    if (selector === '.tab-content') return Object.values(tabPanels);
    if (selector === '.tab-content.active') {
      return Object.values(tabPanels).filter((panel) => panel.classList.contains('active'));
    }
    if (selector === '.sidebar-nav-item') return sidebarItems;
    throw new Error(`unexpected selector ${selector}`);
  },
};
// No TokDashAnimations: the switch takes its synchronous branch.
const window = {};
let currentDashboardTab = 'overview';
let preserveStatsNavigationOnNextActivation = false;
let quotaLoaded = true;
let pricingDbLoaded = true;
function sessionSpeedExplorerNavigation() {}
function loadStats() { log.push('loadStats'); }
function renderServersTab() {}
function loadQuota() {}
function loadPricingDb() {}
function resumeSharedDashboardRange(tab) { log.push(`resumeShared:${tab}`); }
// The Report page. The loader stub finishes its load, as the real one does by
// the time the reader can switch tabs again.
function usageReportRenderPeriodChips() { log.push('renderPeriodChips'); }
function usageReportRetirePending() { log.push(`retireReport:active=${usageReportState.active}`); }
function loadUsageReport() {
  log.push(`loadReport:active=${usageReportState.active}`);
  usageReportState.loaded = true;
}
function usageReportFireStaleReread() {
  log.push(`fireStale:active=${usageReportState.active}`);
  usageReportState.staleReread = null;
}
function usageReportResumePending() { log.push(`resumeReport:active=${usageReportState.active}`); }
// The output-speed page.
function outputSpeedRetirePending() { log.push(`retireSpeed:active=${usageReportSpeedState.active}`); }
function usageReportWireSpeed() { log.push(`wireSpeed:active=${usageReportSpeedState.active}`); }
function usageReportSpeedResume() { log.push(`resumeSpeed:active=${usageReportSpeedState.active}`); }

function visit(tab) {
  log.length = 0;
  activateDashboardTab(tab);
  return {
    log: log.slice(),
    panels: Object.keys(tabPanels).filter((id) => tabPanels[id].classList.contains('active')),
    selected: TABS.filter((name) => tabButtons[name].attrs['aria-selected'] === 'true'),
    rangesHidden: topBar.overviewQuickRanges.hidden,
    chipsHidden: topBar.reportPeriodChips.hidden,
    reportActive: usageReportState.active,
    speedActive: usageReportSpeedState.active,
  };
}
"""


def activate_dashboard_tab_program(scenario: str) -> str:
    """The real tab switch over the stub page, then ``scenario``.

    ``scenario`` drives it with ``visit(tab)`` and writes its own JSON to stdout.
    """
    source = index_source()
    return "\n".join(
        [
            ACTIVATE_PRELUDE,
            extract_js_function(source, "const usageReportState = {"),
            extract_js_function(source, "const usageReportSpeedState = {"),
            extract_js_function(source, "function activateDashboardTab(tab) {"),
            scenario,
        ]
    )
