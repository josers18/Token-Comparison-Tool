/* tokenmeter — motion.js
 *
 * Two helpers:
 *   animateCounter(el, from, to, opts) — tweens textContent over duration
 *   revealOnScroll(els)                — IntersectionObserver-based reveal
 *
 * Both honor prefers-reduced-motion (jump to final state, no animation).
 */
(function () {
  function prefersReduced() {
    try {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (_) { return false; }
  }

  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

  function animateCounter(el, from, to, opts) {
    opts = opts || {};
    const duration = opts.duration || 600;
    const formatter = opts.format || ((v) => v.toFixed(1));
    if (prefersReduced()) {
      el.textContent = formatter(to);
      return;
    }
    const start = performance.now();
    function tick(now) {
      const t = Math.min(1, (now - start) / duration);
      const v = from + (to - from) * easeOutCubic(t);
      el.textContent = formatter(v);
      if (t < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  function revealOnScroll(els) {
    if (prefersReduced()) {
      for (const el of els) el.classList.add("is-visible");
      return () => {};
    }
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          io.unobserve(entry.target);
        }
      }
    }, { rootMargin: "-10% 0px" });
    for (const el of els) io.observe(el);
    return () => io.disconnect();
  }

  window.tokenmeter = window.tokenmeter || {};
  window.tokenmeter.motion = { animateCounter, revealOnScroll };
})();
