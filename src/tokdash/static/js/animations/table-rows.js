/**
 * TokDash v4 Animation System - Session Table Row Stagger
 * Animates session table rows with 30ms stagger, max 50 rows, and handles filter transitions.
 */

import { animate, stagger } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

export function staggerInRows(rows) {
  if (!rows || rows.length === 0) return;

  const rowList = Array.from(rows);
  const animatedRows = rowList.slice(0, 50);
  const instantRows = rowList.slice(50);

  // Instantly display rows beyond 50
  instantRows.forEach(row => {
    row.style.opacity = '1';
    row.style.transform = 'none';
  });

  if (respectsReducedMotion()) {
    animatedRows.forEach(row => {
      row.style.opacity = '1';
      row.style.transform = 'none';
    });
    return;
  }

  // Set initial state
  animatedRows.forEach(row => {
    row.style.opacity = '0';
    row.style.transform = 'translateY(12px)';
  });

  try {
    animate(animatedRows, {
      opacity: [0, 1],
      translateY: [12, 0],
      delay: stagger(30),
      duration: 400,
      ease: 'outExpo',
      onComplete: () => {
        animatedRows.forEach(r => {
          r.style.opacity = '';
          r.style.transform = '';
        });
      }
    });
  } catch (e) {
    // Fallback if stagger syntax differs
    animate(animatedRows, {
      opacity: [0, 1],
      translateY: [12, 0],
      delay: (el, i) => i * 30,
      duration: 400,
      ease: 'outExpo',
      onComplete: () => {
        animatedRows.forEach(r => {
          r.style.opacity = '';
          r.style.transform = '';
        });
      }
    });
  }
}

export function filterTransitionRows(outRows, inRows, onReady) {
  const outList = outRows ? Array.from(outRows).slice(0, 50) : [];

  if (respectsReducedMotion() || outList.length === 0) {
    if (typeof onReady === 'function') onReady();
    if (inRows) staggerInRows(inRows);
    return;
  }

  animate(outList, {
    opacity: [1, 0],
    translateY: [0, -8],
    duration: 200,
    ease: 'inOutSine',
    onComplete: () => {
      if (typeof onReady === 'function') onReady();
      if (inRows) staggerInRows(inRows);
    }
  });
}

export default {
  staggerInRows,
  filterTransitionRows
};
