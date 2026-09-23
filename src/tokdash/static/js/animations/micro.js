/**
 * TokDash v4 Animation System - Micro-Interactions
 * Handles chip hover/click spring pulses, cache hit rate progress bars,
 * filter row transitions, and settings panel reveals.
 */

import { animate, createSpring } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

export function initChipInteractions(selector = '.modern-tool-pill, .quick-range-btn') {
  if (respectsReducedMotion()) return;

  const chips = document.querySelectorAll(selector);
  chips.forEach(chip => {
    if (chip.dataset.chipAnimBound === 'true') return;
    chip.dataset.chipAnimBound = 'true';

    chip.addEventListener('mouseenter', () => {
      animate(chip, {
        scale: 1.05,
        duration: 300,
        ease: 'outBack'
      });
    });

    chip.addEventListener('mouseleave', () => {
      animate(chip, {
        scale: 1.0,
        duration: 200,
        ease: 'outExpo'
      });
    });

    chip.addEventListener('click', () => {
      try {
        const springEase = createSpring({ bounce: 0.4, duration: 500 });
        animate(chip, {
          scale: [0.92, 1],
          ease: springEase,
          duration: 500
        });
      } catch (e) {
        animate(chip, {
          scale: [0.92, 1],
          duration: 300,
          ease: 'outBack'
        });
      }
    });
  });
}

export function animateCacheHitBar(barEl, percentage) {
  if (!barEl) return;
  const pct = Math.max(0, Math.min(100, Number(percentage) || 0));

  let targetColor = '#ef4444'; // Red < 50%
  if (pct >= 80) {
    targetColor = '#22c55e'; // Green > 80%
  } else if (pct >= 50) {
    targetColor = '#f59e0b'; // Amber 50-80%
  }

  if (respectsReducedMotion()) {
    barEl.style.width = `${pct}%`;
    barEl.style.backgroundColor = targetColor;
    return;
  }

  // Animate width from 0% to actual% over 1000ms
  barEl.style.width = '0%';
  animate(barEl, {
    width: `${pct}%`,
    duration: 1000,
    ease: 'outExpo'
  });

  // Animate color transition over 600ms
  animate(barEl, {
    backgroundColor: targetColor,
    duration: 600,
    ease: 'inOutSine'
  });
}

export function animateFilterRowAdd(rowEl) {
  if (!rowEl) return;
  if (respectsReducedMotion()) {
    rowEl.style.opacity = '1';
    rowEl.style.height = '';
    return;
  }

  rowEl.style.opacity = '0';
  rowEl.style.overflow = 'hidden';

  const fullHeight = rowEl.scrollHeight || 40;
  rowEl.style.height = '0px';

  animate(rowEl, {
    height: [`0px`, `${fullHeight}px`],
    opacity: [0, 1],
    duration: 300,
    ease: 'outExpo',
    onComplete: () => {
      rowEl.style.height = '';
      rowEl.style.overflow = '';
    }
  });
}

export function animateFilterRowRemove(rowEl, onComplete) {
  if (!rowEl) {
    if (typeof onComplete === 'function') onComplete();
    return;
  }

  if (respectsReducedMotion()) {
    if (rowEl.parentElement) rowEl.remove();
    if (typeof onComplete === 'function') onComplete();
    return;
  }

  const fullHeight = rowEl.offsetHeight || 40;
  rowEl.style.overflow = 'hidden';

  animate(rowEl, {
    height: [`${fullHeight}px`, '0px'],
    opacity: [1, 0],
    duration: 200,
    ease: 'inOutSine',
    onComplete: () => {
      if (rowEl.parentElement) rowEl.remove();
      if (typeof onComplete === 'function') onComplete();
    }
  });
}

export function animateSettingsToggle(panelEl, isOpen, onComplete) {
  if (!panelEl) {
    if (typeof onComplete === 'function') onComplete();
    return;
  }

  if (respectsReducedMotion()) {
    panelEl.style.opacity = isOpen ? '1' : '0';
    panelEl.style.transform = 'none';
    panelEl.style.display = isOpen ? 'block' : 'none';
    if (typeof onComplete === 'function') onComplete();
    return;
  }

  if (isOpen) {
    panelEl.style.display = 'block';
    panelEl.style.opacity = '0';
    panelEl.style.transform = 'translateY(-8px)';

    animate(panelEl, {
      translateY: [-8, 0],
      opacity: [0, 1],
      duration: 300,
      ease: 'outExpo',
      onComplete: () => {
        if (typeof onComplete === 'function') onComplete();
      }
    });
  } else {
    animate(panelEl, {
      translateY: [0, -8],
      opacity: [1, 0],
      duration: 200,
      ease: 'inOutSine',
      onComplete: () => {
        panelEl.style.display = 'none';
        panelEl.style.opacity = '';
        panelEl.style.transform = '';
        if (typeof onComplete === 'function') onComplete();
      }
    });
  }
}

export default {
  initChipInteractions,
  animateCacheHitBar,
  animateFilterRowAdd,
  animateFilterRowRemove,
  animateSettingsToggle
};
