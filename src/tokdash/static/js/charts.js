// =========================================================================
// TOKDASH v4.0 — CHART.JS INTEGRATED WITH CSS VARIABLES & THEMES
// =========================================================================

const activeCharts = new Map();

export function getThemeTokens() {
  const styles = getComputedStyle(document.documentElement);
  return {
    primary: styles.getPropertyValue('--color-primary').trim() || '#3B82F6',
    secondary: styles.getPropertyValue('--color-secondary').trim() || '#60A5FA',
    cta: styles.getPropertyValue('--color-cta').trim() || '#818CF8',
    text: styles.getPropertyValue('--color-text').trim() || '#F1F5F9',
    muted: styles.getPropertyValue('--color-muted').trim() || '#94A3B8',
    border: styles.getPropertyValue('--color-border').trim() || 'rgba(148,163,184,0.18)',
    surfaceGlass: styles.getPropertyValue('--color-surface-glass').trim() || 'rgba(15,23,42,0.92)',
    cost: styles.getPropertyValue('--color-cost').trim() || '#10B981',
    messages: styles.getPropertyValue('--color-messages').trim() || '#A855F7',
    activeTime: styles.getPropertyValue('--color-active-time').trim() || '#38BDF8',
    cacheRead: styles.getPropertyValue('--color-cache-read').trim() || '#14B8A6',
    topModel: styles.getPropertyValue('--color-top-model').trim() || '#FBBF24',
  };
}

export function createChart(canvas, config, options = {}) {
  if (!canvas) return null;
  const canvasId = canvas.id || `chart_${Math.random().toString(36).substring(2, 9)}`;
  canvas.id = canvasId;

  // Destroy previous instance if registered
  if (activeCharts.has(canvasId)) {
    destroyChart(canvasId);
  }

  const tokens = getThemeTokens();

  // Deep clone config to avoid mutating original
  const themedConfig = JSON.parse(JSON.stringify(config));

  // Configure global options with CSS variables
  themedConfig.options = themedConfig.options || {};
  themedConfig.options.responsive = true;
  themedConfig.options.maintainAspectRatio = false;

  // Scales styling
  if (themedConfig.options.scales) {
    Object.keys(themedConfig.options.scales).forEach((axisKey) => {
      const axis = themedConfig.options.scales[axisKey];
      axis.ticks = axis.ticks || {};
      axis.ticks.color = tokens.muted;
      axis.ticks.font = { family: "'Fira Code', monospace", size: 10 };

      axis.grid = axis.grid || {};
      axis.grid.color = tokens.border;
      axis.grid.drawBorder = false;
    });
  }

  // Plugins styling (Legend & Tooltips)
  themedConfig.options.plugins = themedConfig.options.plugins || {};

  // Legend
  if (themedConfig.options.plugins.legend) {
    themedConfig.options.plugins.legend.labels = themedConfig.options.plugins.legend.labels || {};
    themedConfig.options.plugins.legend.labels.color = tokens.text;
    themedConfig.options.plugins.legend.labels.font = { family: "'Inter', sans-serif", size: 11 };
  }

  // Tooltip
  themedConfig.options.plugins.tooltip = themedConfig.options.plugins.tooltip || {};
  themedConfig.options.plugins.tooltip.backgroundColor = tokens.surfaceGlass;
  themedConfig.options.plugins.tooltip.borderColor = tokens.border;
  themedConfig.options.plugins.tooltip.borderWidth = 1;
  themedConfig.options.plugins.tooltip.titleColor = tokens.text;
  themedConfig.options.plugins.tooltip.bodyColor = tokens.muted;
  themedConfig.options.plugins.tooltip.padding = 10;
  themedConfig.options.plugins.tooltip.cornerRadius = 8;

  // Apply CSS color variables to datasets if not explicitly provided
  const palette = [tokens.primary, tokens.secondary, tokens.cta, tokens.cost, tokens.messages, tokens.cacheRead, tokens.topModel];
  if (themedConfig.data && themedConfig.data.datasets) {
    themedConfig.data.datasets.forEach((ds, idx) => {
      if (!ds.backgroundColor && !ds.borderColor) {
        ds.backgroundColor = palette[idx % palette.length];
        ds.borderColor = palette[idx % palette.length];
      }
    });
  }

  if (typeof Chart === 'undefined') {
    console.warn('Chart.js library not loaded on page.');
    return null;
  }

  const chartInstance = new Chart(canvas, themedConfig);
  activeCharts.set(canvasId, {
    instance: chartInstance,
    canvas,
    originalConfig: config,
    options
  });

  return chartInstance;
}

export function destroyChart(chartOrId) {
  const id = typeof chartOrId === 'string' ? chartOrId : (chartOrId?.canvas?.id || null);
  if (!id) return;
  const entry = activeCharts.get(id);
  if (entry && entry.instance) {
    entry.instance.destroy();
    activeCharts.delete(id);
  }
}

export function recreateAllCharts() {
  const entries = Array.from(activeCharts.entries());
  for (const [id, entry] of entries) {
    if (document.body.contains(entry.canvas)) {
      destroyChart(id);
      createChart(entry.canvas, entry.originalConfig, entry.options);
    } else {
      activeCharts.delete(id);
    }
  }
}

// Watch theme changes and trigger full chart re-render within 200ms
let themeObserverTimer = null;
const observer = new MutationObserver(() => {
  if (themeObserverTimer) clearTimeout(themeObserverTimer);
  themeObserverTimer = setTimeout(() => {
    recreateAllCharts();
  }, 100);
});

observer.observe(document.documentElement, {
  attributes: true,
  attributeFilter: ['class', 'data-ui-theme']
});
