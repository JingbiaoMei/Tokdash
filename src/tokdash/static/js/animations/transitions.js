/**
 * TokDash v4 Animation System - Tab & Page Transitions
 * Handles outgoing fade/translate up, incoming slide/fade in, and spring bounce on tab button.
 */

import { animate, createSpring } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

let activeTransitions = [];

export function animateTabTransition(outEl, inEl, tabBtn) {
  // Cancel previous transitions
  activeTransitions.forEach(anim => {
    if (anim && typeof anim.stop === 'function') anim.stop();
  });
  activeTransitions = [];

  if (respectsReducedMotion()) {
    if (outEl) {
      outEl.classList.remove('active');
      outEl.style.opacity = '';
      outEl.style.transform = '';
    }
    if (inEl) {
      inEl.classList.add('active');
      inEl.style.opacity = '';
      inEl.style.transform = '';
    }
    return;
  }

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
    } catch (e) {
      // Fallback
    }
  }

  // Outgoing tab content: fade opacity 1->0, translateY 0->-8px over 200ms, ease inOutSine
  if (outEl && outEl !== inEl) {
    const outAnim = animate(outEl, {
      opacity: [1, 0],
      translateY: [0, -8],
      duration: 200,
      ease: 'inOutSine',
      onComplete: () => {
        outEl.classList.remove('active');
        outEl.style.opacity = '';
        outEl.style.transform = '';
      }
    });
    activeTransitions.push(outAnim);
  }

  // Incoming tab content: fade opacity 0->1, translateY 8px->0 over 300ms, delay 100ms, ease outExpo
  if (inEl) {
    inEl.classList.add('active');
    inEl.style.opacity = '0';
    inEl.style.transform = 'translateY(8px)';

    const inAnim = animate(inEl, {
      opacity: [0, 1],
      translateY: [8, 0],
      delay: 100,
      duration: 300,
      ease: 'outExpo',
      onComplete: () => {
        inEl.style.opacity = '';
        inEl.style.transform = '';
      }
    });
    activeTransitions.push(inAnim);
  }
}

export default animateTabTransition;
