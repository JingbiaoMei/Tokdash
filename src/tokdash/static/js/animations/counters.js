/**
 * TokDash v4 Animation System - Number Counting & KPI Animations
 * Implements createAnimatable counting, skeleton crossfade, and SSE update pulse.
 */

import { animate, createAnimatable, createSpring } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

export function animateNumber(el, targetValue, duration = 1200, format = null) {
  if (!el) return;

  const target = typeof targetValue === 'number' ? targetValue : parseFloat(targetValue) || 0;

  // Check if this is a cost counter with 0.00 (free tier)
  const isCost = format && typeof format === 'function' && format(1).toString().includes('$');
  if (isCost && target === 0) {
    el.textContent = 'FREE';
    el._currentValue = 0;
    return;
  }

  // Reduced motion: set final value immediately
  if (respectsReducedMotion()) {
    el._currentValue = target;
    el.textContent = format ? format(target) : Math.round(target).toLocaleString();
    return;
  }

  const startValue = typeof el._currentValue === 'number' ? el._currentValue : 0;
  const isCacheHit = format && typeof format === 'function' && format(1).toString().includes('%');

  const state = { val: startValue };

  try {
    const easeSetting = isCacheHit ? createSpring({ bounce: 0.2 }) : 'outExpo';
    const animatable = createAnimatable(state, {
      val: {
        duration: duration,
        ease: easeSetting
      },
      onUpdate: () => {
        el.textContent = format ? format(state.val) : Math.round(state.val).toLocaleString();
      },
      onComplete: () => {
        el._currentValue = target;
        el.textContent = format ? format(target) : Math.round(target).toLocaleString();
      }
    });

    animatable.val(target);
  } catch (err) {
    // Fallback using animate directly
    animate(state, {
      val: target,
      duration: duration,
      ease: isCacheHit ? 'inOutSine' : 'outExpo',
      onUpdate: () => {
        el.textContent = format ? format(state.val) : Math.round(state.val).toLocaleString();
      },
      onComplete: () => {
        el._currentValue = target;
        el.textContent = format ? format(target) : Math.round(target).toLocaleString();
      }
    });
  }
}

export function crossfadeSkeletonToNumber(skeletonEl, numberEl) {
  if (respectsReducedMotion()) {
    if (skeletonEl) skeletonEl.style.display = 'none';
    if (numberEl) {
      numberEl.style.opacity = '1';
      numberEl.style.transform = 'none';
    }
    return;
  }

  if (skeletonEl) {
    animate(skeletonEl, {
      opacity: [1, 0],
      duration: 200,
      ease: 'inOutSine',
      onComplete: () => {
        skeletonEl.style.display = 'none';
      }
    });
  }

  if (numberEl) {
    numberEl.style.opacity = '0';
    numberEl.style.transform = 'translateY(8px)';
    animate(numberEl, {
      opacity: [0, 1],
      translateY: [8, 0],
      duration: 500,
      ease: 'outExpo'
    });
  }
}

export function pulseTokenDelta(dotEl) {
  if (!dotEl || respectsReducedMotion()) return;
  dotEl.hidden = false;
  dotEl.style.display = 'inline-block';
  animate(dotEl, {
    scale: [1, 1.5, 1],
    opacity: [1, 0.6, 1],
    duration: 800,
    ease: 'inOutSine',
    onComplete: () => {
      dotEl.hidden = true;
      dotEl.style.display = 'none';
    }
  });
}

export default {
  animateNumber,
  crossfadeSkeletonToNumber,
  pulseTokenDelta
};
