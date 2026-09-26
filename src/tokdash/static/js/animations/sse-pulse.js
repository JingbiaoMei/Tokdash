/**
 * TokDash v4 Animation System - SSE Live Pulse Indicator
 * Controls the live green dot pulse next to Hermes Hub tab when SSE is active.
 */

import { animate, createSpring } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

let pulseAnimation = null;
let currentDotEl = null;
let isConnectedState = false;

export function initSSEPulse(dotElOrSelector = '#hermesSsePulseDot') {
  currentDotEl = typeof dotElOrSelector === 'string'
    ? document.querySelector(dotElOrSelector)
    : dotElOrSelector;
  return currentDotEl;
}

export function setSSEConnected(isConnected) {
  if (!currentDotEl) {
    initSSEPulse();
  }
  const dot = currentDotEl;
  if (!dot) return;

  if (pulseAnimation && typeof pulseAnimation.stop === 'function') {
    pulseAnimation.stop();
    pulseAnimation = null;
  }

  if (respectsReducedMotion()) {
    dot.style.display = isConnected ? 'inline-block' : 'none';
    dot.style.opacity = isConnected ? '1' : '0';
    dot.style.transform = 'none';
    isConnectedState = isConnected;
    return;
  }

  if (isConnected) {
    dot.style.display = 'inline-block';

    const startPulse = () => {
      pulseAnimation = animate(dot, {
        scale: [1, 1.3, 1],
        opacity: [1, 0.5, 1],
        duration: 1500,
        ease: 'inOutSine',
        loop: true
      });
    };

    if (!isConnectedState) {
      // Reconnect spring entry
      try {
        const springEase = createSpring({ bounce: 0.3, duration: 300 });
        animate(dot, {
          scale: [0, 1],
          opacity: [0, 1],
          duration: 300,
          ease: springEase,
          onComplete: () => {
            startPulse();
          }
        });
      } catch (e) {
        animate(dot, {
          scale: [0, 1],
          opacity: [0, 1],
          duration: 300,
          ease: 'outExpo',
          onComplete: () => {
            startPulse();
          }
        });
      }
    } else {
      startPulse();
    }
  } else {
    // Stream disconnected: animate out
    animate(dot, {
      scale: [1, 0],
      opacity: [1, 0],
      duration: 300,
      ease: 'inOutSine',
      onComplete: () => {
        dot.style.display = 'none';
      }
    });
  }

  isConnectedState = isConnected;
}

export default {
  initSSEPulse,
  setSSEConnected
};
