/**
 * TokDash v4 Animation System - Reduced Motion Detection
 * Checks window.matchMedia('(prefers-reduced-motion: reduce)')
 * Every animation module checks this before executing any Anime.js calls.
 */

export function respectsReducedMotion() {
  if (typeof window === 'undefined' || !window.matchMedia) {
    return false;
  }
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) {
    return false;
  }
}

export default respectsReducedMotion;
