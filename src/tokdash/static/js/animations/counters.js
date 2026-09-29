/**
 * TokDash v4 Animation System - Number Counting & KPI Animations
 * Implements createAnimatable counting, skeleton crossfade, and SSE update pulse.
 */

import { animate, createAnimatable, createSpring } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

// Each element runs at most one counting animation. A render that writes static
// text (a zero, "FREE", a "No data" state) cancels the in-flight one first, or
// its onUpdate keeps writing for the rest of the duration and overwrites the
// static value when the range has already changed. Cancellation only detaches
// the callbacks: el._currentValue stays where the last completed animation left
// it, so the next count-up still starts from the value on screen.
const activeCounters = new WeakMap();

export function cancelCounter(el) {
  if (!el) return;
  const active = activeCounters.get(el);
  if (active) {
    try {
      active.pause();
    } catch (err) {
      // The engine may have already reclaimed this instance; nothing to do.
    }
    activeCounters.delete(el);
  }
}

export function animateNumber(el, targetValue, duration = 1200, format = null) {
  if (!el) return;

  const target = typeof targetValue === 'number' ? targetValue : parseFloat(targetValue) || 0;

  // Check if this is a cost counter with 0.00 (free tier)
  const isCost = format && typeof format === 'function' && format(1).toString().includes('$');
  if (isCost && target === 0) {
    cancelCounter(el);
    el.textContent = 'FREE';
    el._currentValue = 0;
    return;
  }

  // Reduced motion: set final value immediately
  if (respectsReducedMotion()) {
    cancelCounter(el);
    el._currentValue = target;
    el.textContent = format ? format(target) : Math.round(target).toLocaleString();
    return;
  }

  // Read the resume point before the new animation supersedes the running one.
  const startValue = typeof el._currentValue === 'number' ? el._currentValue : 0;
  cancelCounter(el);

  const isCacheHit = format && typeof format === 'function' && format(1).toString().includes('%');

  const state = { val: startValue };
  const finish = () => {
    if (activeCounters.get(el) !== instance) return;
    el._currentValue = target;
    el.textContent = format ? format(target) : Math.round(target).toLocaleString();
    activeCounters.delete(el);
  };
  let instance = null;

  const onUpdate = () => {
    if (activeCounters.get(el) !== instance) return;
    el.textContent = format ? format(state.val) : Math.round(state.val).toLocaleString();
  };

  try {
    const easeSetting = isCacheHit ? createSpring({ bounce: 0.2 }) : 'outExpo';
    const animatable = createAnimatable(state, {
      val: {
        duration: duration,
        ease: easeSetting
      },
      onUpdate,
      onComplete: finish
    });
    instance = animatable;
    activeCounters.set(el, instance);
    animatable.val(target);
  } catch (err) {
    // Fallback using animate directly
    const fallback = animate(state, {
      val: target,
      duration: duration,
      ease: isCacheHit ? 'inOutSine' : 'outExpo',
      onUpdate,
      onComplete: finish
    });
    instance = fallback;
    activeCounters.set(el, fallback);
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
  cancelCounter,
  crossfadeSkeletonToNumber,
  pulseTokenDelta
};
