/* tokenmeter — theme.js
 *
 * Source of truth for theme state. Persists to localStorage AND a
 * cookie (the cookie is for server-rendered surfaces like /og/<token>.png).
 *
 * Public API:
 *   applyTheme({mode, palette, matchSystem})
 *   getTheme()
 *   subscribe(callback)  -- callback({mode, palette, matchSystem}) on every change
 *
 * The pre-paint inline script (in each <head>) calls applyTheme() before
 * first paint to prevent FOUC. That script is a stripped-down inline copy
 * of just the apply-to-DOM part; see Task 1.4.
 */

(function () {
  const STORAGE_KEY = "tokenmeter_theme";
  const COOKIE_KEY = "tokenmeter_theme";

  const VALID_PALETTES = ["teal-coral", "emerald-violet", "cyan-amber", "forest-terracotta"];
  const DEFAULT = { mode: "light", palette: "teal-coral", matchSystem: true };

  const subscribers = [];

  function readSystemMode() {
    try {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    } catch (_) {
      return "light";
    }
  }

  function loadStored() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const v = JSON.parse(raw);
      if (!VALID_PALETTES.includes(v.palette)) return null;
      if (v.mode !== "light" && v.mode !== "dark") return null;
      return { mode: v.mode, palette: v.palette, matchSystem: !!v.matchSystem };
    } catch (_) {
      return null;
    }
  }

  function persist(theme) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(theme));
    } catch (_) { /* private mode etc — silently degrade */ }
    // 1-year cookie, SameSite=Lax, NOT HttpOnly (client + server both read).
    const value = encodeURIComponent(JSON.stringify(theme));
    const oneYear = 60 * 60 * 24 * 365;
    document.cookie = `${COOKIE_KEY}=${value}; path=/; max-age=${oneYear}; SameSite=Lax`;
  }

  function applyToDom(mode, palette) {
    const html = document.documentElement;
    html.dataset.theme = mode;
    html.dataset.palette = palette;
  }

  function resolveEffective(theme) {
    if (theme.matchSystem) return { mode: readSystemMode(), palette: theme.palette };
    return { mode: theme.mode, palette: theme.palette };
  }

  function applyTheme(input) {
    const stored = loadStored() || DEFAULT;
    const theme = { ...stored, ...input };
    if (!VALID_PALETTES.includes(theme.palette)) theme.palette = DEFAULT.palette;
    if (theme.mode !== "light" && theme.mode !== "dark") theme.mode = DEFAULT.mode;
    persist(theme);
    const eff = resolveEffective(theme);
    applyToDom(eff.mode, eff.palette);
    subscribers.forEach((cb) => { try { cb(theme); } catch (_) {} });
  }

  function getTheme() {
    return loadStored() || { ...DEFAULT };
  }

  function subscribe(cb) {
    subscribers.push(cb);
    return () => {
      const idx = subscribers.indexOf(cb);
      if (idx >= 0) subscribers.splice(idx, 1);
    };
  }

  // React to system mode changes when user has matchSystem on.
  try {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      const t = loadStored();
      if (t && t.matchSystem) applyTheme(t);
    });
  } catch (_) { /* older Safari */ }

  // Bootstrap on script load (idempotent — pre-paint script may have run already).
  applyTheme({});

  // Expose to window for the puck + other consumers.
  window.tokenmeter = window.tokenmeter || {};
  window.tokenmeter.theme = { applyTheme, getTheme, subscribe };
})();
