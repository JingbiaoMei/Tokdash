/**
 * TokDash v4 Animation System - Number Counting & KPI Animations
 * Implements createAnimatable counting, skeleton crossfade, and SSE update pulse.
 */

import { animate, createAnimatable, createSpring } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

// Each element runs at most one counting animation, tracked here so a render
// that writes static text (a zero, "FREE", "No data", a dash) can cancel the
// in-flight one first. Otherwise its onUpdate keeps writing for the rest of the
// duration and paints over the value the new range just put there.
const activeCounters = new WeakMap();

// anime.js 4 hands back different objects depending on the path taken:
// createAnimatable() returns an Animatable that exposes only revert(), while
// animate() returns a JSAnimation with cancel()/pause(). Calling pause() on the
// former throws, and swallowing that error left every superseded animation
// running to the end of its duration with only the identity guards below
// standing between it and the element. So use the strongest verb the instance
// actually has.
function stopCounterInstance(instance) {
  if (!instance) return;
  if (typeof instance.cancel === 'function') { instance.cancel(); return; }
  if (typeof instance.revert === 'function') { instance.revert(); return; }
  if (typeof instance.pause === 'function') { instance.pause(); }
}

export function cancelCounter(el) {
  if (!el) return;
  const active = activeCounters.get(el);
  if (!active) return;
  // Hand the live value back before stopping. _currentValue otherwise only
  // moves when an animation completes, so a re-render mid-flight (language
  // switch, readable-tokens toggle) would resume from the last *completed*
  // total and visibly jump away from what is on screen.
  if (active.state && typeof active.state.val === 'number') el._currentValue = active.state.val;
  stopCounterInstance(active.instance);
  activeCounters.delete(el);
}

// Every static write into a counter element goes through here, so the three
// things that must happen together actually do: the in-flight count-up is
// stopped, the text is replaced, and the counter base follows the text.
export function setCounterText(el, text, value) {
  if (!el) return;
  cancelCounter(el);
  el.textContent = text;
  if (typeof value === 'number') el._currentValue = value;
}

export function animateNumber(el, targetValue, duration = 1200, format = null) {
  if (!el) return;

  const target = typeof targetValue === 'number' ? targetValue : parseFloat(targetValue) || 0;

  // Check if this is a cost counter with 0.00 (free tier)
  const isCost = format && typeof format === 'function' && format(1).toString().includes('$');
  if (isCost && target === 0) {
    setCounterText(el, 'FREE', 0);
    return;
  }

  // Reduced motion: set final value immediately
  if (respectsReducedMotion()) {
    setCounterText(el, format ? format(target) : Math.round(target).toLocaleString(), target);
    return;
  }

  // Cancel first, *then* read the resume point: cancelCounter() is the call that
  // copies the live value out of the running animation into _currentValue. Read
  // it the other way round and a re-render mid-count (a language switch, the
  // readable-tokens toggle) restarts from the last completed total, so the card
  // visibly jumps away from the number on screen before climbing back.
  cancelCounter(el);
  const startValue = typeof el._currentValue === 'number' ? el._currentValue : 0;

  const isCacheHit = format && typeof format === 'function' && format(1).toString().includes('%');

  const state = { val: startValue };
  const isCurrent = () => {
    const active = activeCounters.get(el);
    return !!active && active.instance === instance;
  };
  const finish = () => {
    if (!isCurrent()) return;
    el._currentValue = target;
    el.textContent = format ? format(target) : Math.round(target).toLocaleString();
    activeCounters.delete(el);
  };
  let instance = null;

  const onUpdate = () => {
    if (!isCurrent()) return;
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
    activeCounters.set(el, { instance, state });
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
    activeCounters.set(el, { instance: fallback, state });
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
  setCounterText,
  crossfadeSkeletonToNumber,
  pulseTokenDelta
};
