/**
 * TokDash v4 Animation System - Chart.js Integration with Anime.js
 * Layers smooth wrapper animations (scale 0.95->1, opacity 0->1) onto Chart.js canvases
 * when they enter the viewport without interfering with Chart.js's internal rendering.
 */

import { animate } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

let chartObserver = null;

export function animateChartEntry(element) {
  if (!element) return;
  const target = element.parentElement || element;

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
    ease: 'outExpo'
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
