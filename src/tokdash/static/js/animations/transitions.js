/**
 * TokDash v4 Animation System - Tab & Page Transitions
 * Incoming slide/fade in and spring bounce on the tab button.
 *
 * Class commitment is synchronous by design: the .active removal used to live
 * in an anime onComplete, and anime v4's stop() does not run onComplete — a
 * second click inside the 200ms window silently cancelled the previous
 * panel's removal, leaving two .tab-content.active panels stacked (or zero,
 * when a stale removal hit the panel that had become current again).
 */

import { animate, createSpring } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

let activeTransitions = [];
let transitionElements = [];

function clearTransitionStyles() {
  transitionElements.forEach((el) => {
    if (el) {
      el.style.opacity = '';
      el.style.transform = '';
    }
  });
  transitionElements = [];
}

export function stopTabTransitions() {
  activeTransitions.forEach((anim) => {
    try {
      if (anim && typeof anim.stop === 'function') anim.stop();
    } catch (e) {
      // already finished
    }
  });
  activeTransitions = [];
  // stop() skips onComplete — release any inline styles the killed animations owned.
  clearTransitionStyles();
}

export function animateTabTransition(outEl, inEl, tabBtn) {
  stopTabTransitions();

  // Synchronous, single-source-of-truth panel state.
  if (outEl && outEl !== inEl) outEl.classList.remove('active');
  if (!inEl) return;
  inEl.classList.add('active');

  if (respectsReducedMotion()) return;

  // Sidebar tab button spring bounce
  if (tabBtn) {
    try {
      const springEase = createSpring({ bounce: 0.3, duration: 600 });
      const btnAnim = animate(tabBtn, {
        scale: [0.94, 1],
        ease: springEase,
        duration: 600
      });
      activeTransitions.push(btnAnim);
      transitionElements.push(tabBtn);
    } catch (e) {
      // No spring support: the tab switch still works.
    }
  }

  // Incoming tab content: fade opacity 0->1, translateY 8px->0 over 300ms.
  // (The outgoing panel is display:none the moment .active comes off it, so an
  // exit animation could never be seen; animating it was what created the race.)
  inEl.style.opacity = '0';
  inEl.style.transform = 'translateY(8px)';
  transitionElements.push(inEl);

  const inAnim = animate(inEl, {
    opacity: [0, 1],
    translateY: [8, 0],
    duration: 300,
    ease: 'outExpo',
    onComplete: () => {
      inEl.style.opacity = '';
      inEl.style.transform = '';
    }
  });
  activeTransitions.push(inAnim);
}

export default animateTabTransition;
