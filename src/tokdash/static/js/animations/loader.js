/**
 * TokDash v4 Animation System - Hero SVG Loader
 * Controls 3-ring orbital loader, rate reactivity, brand colors, and completion animation.
 */

import { animate } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

let rateTimers = [];
let currentRateObj = { value: 0.8 };
let rateAnimation = null;

export function getLoaderElement() {
  return document.getElementById('tokdash-loader');
}

export function getLoaderSvg() {
  const loader = getLoaderElement();
  return loader ? loader.querySelector('svg.ap') : null;
}

export function setLoaderRate(targetRate, duration = 400) {
  const svg = getLoaderSvg();
  if (!svg) return;

  if (respectsReducedMotion()) {
    svg.style.setProperty('--rate', targetRate);
    currentRateObj.value = targetRate;
    return;
  }

  if (rateAnimation && typeof rateAnimation.stop === 'function') {
    rateAnimation.stop();
  }

  rateAnimation = animate(currentRateObj, {
    value: targetRate,
    duration: duration,
    ease: 'outExpo',
    onUpdate: () => {
      svg.style.setProperty('--rate', currentRateObj.value);
    }
  });
}

export function showLoader({ tool = 'default', initialRate = 0.8 } = {}) {
  const loader = getLoaderElement();
  if (!loader) return;

  clearLoaderTimers();

  loader.hidden = false;
  loader.style.display = 'flex';
  loader.style.opacity = '1';

  const svg = getLoaderSvg();
  if (svg) {
    // Reset ring styles
    const rings = svg.querySelectorAll('.ap-r1, .ap-r2, .ap-r3, .ap-halo');
    rings.forEach(r => { r.style.opacity = ''; });
    const core = svg.querySelector('.ap-core');
    if (core) {
      core.style.transform = '';
      core.style.opacity = '';
    }

    // Set brand color
    if (tool === 'hermes') {
      svg.style.setProperty('--loader-color', '#a855f7');
      svg.style.color = '#a855f7';
    } else {
      svg.style.setProperty('--loader-color', 'var(--color-primary, #1E40AF)');
      svg.style.color = 'var(--color-primary, #1E40AF)';
    }

    svg.style.setProperty('--rate', initialRate);
    currentRateObj.value = initialRate;
  }

  // Rate reactivity schedule:
  // 500ms: rate = 0.8
  // 2s: rate = 1.0
  // 5s: rate = 0.5
  if (!respectsReducedMotion()) {
    rateTimers.push(setTimeout(() => {
      setLoaderRate(0.8, 300);
    }, 500));

    rateTimers.push(setTimeout(() => {
      setLoaderRate(1.0, 500);
    }, 2000));

    rateTimers.push(setTimeout(() => {
      setLoaderRate(0.5, 600);
    }, 5000));
  }
}

export function hideLoader() {
  const loader = getLoaderElement();
  if (!loader || loader.hidden) return;

  clearLoaderTimers();

  const svg = getLoaderSvg();
  if (!svg || respectsReducedMotion()) {
    loader.hidden = true;
    loader.style.display = 'none';
    return;
  }

  const core = svg.querySelector('.ap-core');
  const rings = svg.querySelectorAll('.ap-r1, .ap-r2, .ap-r3, .ap-halo');

  // Trigger completion animation:
  // scale core to 2x, fade all rings to opacity 0, remove in 400ms
  if (core) {
    animate(core, {
      scale: 2,
      opacity: 0,
      duration: 400,
      ease: 'outExpo'
    });
  }

  if (rings.length) {
    animate(rings, {
      opacity: 0,
      duration: 400,
      ease: 'outExpo'
    });
  }

  animate(loader, {
    opacity: 0,
    duration: 400,
    ease: 'outExpo',
    onComplete: () => {
      loader.hidden = true;
      loader.style.display = 'none';
      loader.style.opacity = '1';
    }
  });
}

export function setLoaderCacheHitRate(percentage) {
  const svg = getLoaderSvg();
  if (!svg) return;
  const core = svg.querySelector('.ap-core');
  if (!core) return;

  // Animate core fill color from red (0%) to green (100%) over 1.2s easeInOutSine
  const pct = Math.max(0, Math.min(100, percentage));
  // Interpolate hue from 0 (red) to 120 (green)
  const targetHue = Math.round((pct / 100) * 120);
  const targetColor = `hsl(${targetHue}, 80%, 55%)`;

  if (respectsReducedMotion()) {
    core.style.fill = targetColor;
    return;
  }

  animate(core, {
    fill: targetColor,
    duration: 1200,
    ease: 'inOutSine'
  });
}

function clearLoaderTimers() {
  rateTimers.forEach(t => clearTimeout(t));
  rateTimers = [];
}
