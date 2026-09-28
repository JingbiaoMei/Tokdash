/**
 * TokDash v4 Animation System - Chart.js Integration with Anime.js
 * Layers smooth wrapper animations (scale 0.95->1, opacity 0->1) onto Chart.js canvases
 * when they enter the viewport without interfering with Chart.js's internal rendering.
 */

import { animate } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

let chartObserver = null;

// A page-level decoration canvas (#linkyBg background) is a direct child of
// <body>, so its "wrapper" is body itself. Animating body — even to an
// identity scale(1) — makes it the containing block for every
// position:fixed descendant: the settings modal's inset:0 backdrop then
// resolved against the full document and opened ~2600px below the viewport.
// Never animate a wrapper that contains the page's fixed overlays.
function isViewportDecoration(canvas, wrapper) {
  if (wrapper === document.body || wrapper === document.documentElement) return true;
  try {
    return window.getComputedStyle(canvas).position === 'fixed';
  } catch (e) {
    return false;
  }
}

export function animateChartEntry(element) {
  if (!element) return;
  const target = element.parentElement || element;
  if (isViewportDecoration(element, target)) return;

  if (target.dataset.chartAnimated === 'true') return;
  target.dataset.chartAnimated = 'true';

  if (respectsReducedMotion()) {
    target.style.opacity = '1';
    target.style.transform = 'none';
    return;
  }

  animate(target, {
    scale: [0.95, 1],
    opacity: [0, 1],
    duration: 800,
    ease: 'outExpo',
    onComplete: () => {
      // Release the inline transform: a residual scale(1) would linger as a
      // containing-block trap for fixed overlays (same class of bug on any
      // ancestor). Matches the cleanup idiom in theme.js/table-rows.js.
      target.style.transform = '';
      target.style.opacity = '';
    }
  });
}

export function observeCharts(root = document) {
  if (typeof IntersectionObserver === 'undefined') return;

  if (chartObserver) {
    chartObserver.disconnect();
  }

  chartObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        animateChartEntry(entry.target);
        chartObserver.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1 });

  const canvases = root.querySelectorAll('canvas');
  canvases.forEach(canvas => {
    // Wrap or target canvas container
    const wrapper = canvas.parentElement || canvas;
    if (isViewportDecoration(canvas, wrapper)) return;
    if (wrapper.dataset.chartAnimated !== 'true') {
      wrapper.style.opacity = '0';
      wrapper.style.transform = 'scale(0.95)';
      chartObserver.observe(canvas);
    }
  });
}

export default {
  animateChartEntry,
  observeCharts
};
