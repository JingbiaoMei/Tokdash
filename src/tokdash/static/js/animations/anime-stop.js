/**
 * TokDash v4 Animation System - Stopping an anime instance
 *
 * The bundled anime 4.0.0 instances have no stop() method — pause() and
 * cancel() are the API. A typeof-guarded `anim.stop()` therefore compiles,
 * runs, and does nothing: the "cancelled" animation keeps ticking and keeps
 * writing to its target. That is how an empty-period reset got its stale
 * cache-bar width restored under it, and why stacked pulse/rate animations
 * raced each other on the same element.
 *
 * pause() is the right call (not cancel()): it detaches the instance from
 * the engine without writing anything, so callers own the final state
 * through their own style writes.
 */

export function stopAnimation(anim) {
  if (!anim) return null;
  try {
    if (typeof anim.pause === 'function') anim.pause();
    else if (typeof anim.stop === 'function') anim.stop(); // future anime builds
  } catch (e) {
    // already finished
  }
  return null;
}
