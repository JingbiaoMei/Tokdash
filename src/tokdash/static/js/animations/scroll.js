/**
 * TokDash v4 Animation System - Scroll-Triggered Section Reveals
 * Applies onScroll reveals to Profile Summary band, Activity Insights, and Breakdown cards.
 */

import { animate, onScroll, stagger } from '../anime.esm.js';
import { respectsReducedMotion } from './reduced-motion.js';

let scrollObservers = [];

export function initScrollReveals(containerSelector = '.dashboard-main') {
  if (respectsReducedMotion()) return;

  const container = document.querySelector(containerSelector) || document.documentElement;

  // Profile Summary Band
  const profileBand = document.getElementById('overviewProfileSummary');
  if (profileBand && profileBand.dataset.animated !== 'true') {
    setupScrollTrigger(profileBand, container, (el) => {
      el.dataset.animated = 'true';
      animate(el, {
        translateY: [40, 0],
        opacity: [0, 1],
        duration: 600,
        ease: 'outExpo'
      });
    });
  }

  // Activity Insights
  const activityInsights = document.getElementById('overviewActivityInsights');
  if (activityInsights && activityInsights.dataset.animated !== 'true') {
    setupScrollTrigger(activityInsights, container, (el) => {
      el.dataset.animated = 'true';
      animate(el, {
        translateY: [40, 0],
        opacity: [0, 1],
        delay: 150,
        duration: 600,
        ease: 'outExpo'
      });
    });
  }

  // Per-tool breakdown cards
  const breakdownCards = document.querySelectorAll('#rankedToolBreakdown, #rankedModelBreakdown, #appsBreakdown > div');
  if (breakdownCards.length > 0) {
    const unanCards = Array.from(breakdownCards).filter(c => c.dataset.animated !== 'true');
    if (unanCards.length > 0) {
      setupScrollTrigger(unanCards[0], container, () => {
        unanCards.forEach(c => { c.dataset.animated = 'true'; });
        try {
          animate(unanCards, {
            scale: [0.96, 1],
            opacity: [0, 1],
            delay: stagger(50),
            duration: 500,
            ease: 'outExpo'
          });
        } catch (e) {
          animate(unanCards, {
            scale: [0.96, 1],
            opacity: [0, 1],
            delay: (el, i) => i * 50,
            duration: 500,
            ease: 'outExpo'
          });
        }
      });
    }
  }
}

function setupScrollTrigger(element, container, callback) {
  if (!element) return;

  // Try Anime.js onScroll if available
  if (typeof onScroll === 'function') {
    try {
      const scrollAnim = onScroll({
        container: container === document.documentElement ? window : container,
        sync: 'play',
        thresholds: { top: 'bottom-=100' },
        onEnter: () => {
          callback(element);
        }
      });
      scrollObservers.push(scrollAnim);
      return;
    } catch (e) {
      // Fallback to IntersectionObserver
    }
  }

  // IntersectionObserver fallback with 100px bottom offset
  if (typeof IntersectionObserver !== 'undefined') {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          callback(entry.target);
          observer.unobserve(entry.target);
        }
      });
    }, {
      root: container === document.documentElement ? null : container,
      rootMargin: '0px 0px -100px 0px',
      threshold: 0.05
    });

    observer.observe(element);
    scrollObservers.push(observer);
  } else {
    // Immediate fallback
    callback(element);
  }
}

export default {
  initScrollReveals
};
