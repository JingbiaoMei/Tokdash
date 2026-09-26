/**
 * TokDash v4 Animation System - Theme Change Breathing Animation
 * Transitions body background smoothly and animates .surface cards
 * with scale and brightness staggering for a living, breathing theme shift.
 */

import { animate, stagger } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

export function animateThemeChange() {
  if (respectsReducedMotion()) return;

  const surfaces = Array.from(document.querySelectorAll('.surface')).slice(0, 50);

  // Animate surface cards breathing effect
  if (surfaces.length > 0) {
    try {
      animate(surfaces, {
        scale: [0.98, 1],
        filter: ['brightness(0.9)', 'brightness(1)'],
        delay: stagger(40),
        duration: 500,
        ease: 'outExpo',
        onComplete: () => {
          surfaces.forEach(s => {
            s.style.transform = '';
            s.style.filter = '';
          });
        }
      });
    } catch (e) {
      animate(surfaces, {
        scale: [0.98, 1],
        filter: ['brightness(0.9)', 'brightness(1)'],
        delay: (el, i) => i * 40,
        duration: 500,
        ease: 'outExpo',
        onComplete: () => {
          surfaces.forEach(s => {
            s.style.transform = '';
            s.style.filter = '';
          });
        }
      });
    }
  }

  // Smooth body background transition
  const body = document.body;
  if (body) {
    animate(body, {
      opacity: [0.95, 1],
      duration: 500,
      ease: 'inOutSine',
      onComplete: () => {
        body.style.opacity = '';
      }
    });
  }
}

export default {
  animateThemeChange
};
