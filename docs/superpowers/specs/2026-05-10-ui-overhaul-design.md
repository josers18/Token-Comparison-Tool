# UI Overhaul — Spatial Glass theme system + 12 wow features

**Status:** Draft, awaiting user review
**Date:** 2026-05-10
**Goal:** Replace the editorial-light SPA with a Spatial Glass design language (4 palettes × light/dark), and ship 12 high-impact UX features across the eight existing surfaces. Two-phase rollout. No backend or DB schema changes — all work lives in `static/` plus a small set of new server-rendered preview routes.

---

## Why

The current UI is well-crafted but quiet. Numbers don't pop, the catalog reads as a flat list, and live runs feel like waiting at a status page. The tool is positioned for three audiences (executives, engineers, demos) and each currently sees the same single voice. The redesign:

1. **Speaks all three voices** through a theme system (light = exec/print, dark = engineer/demo, palette = brand affinity).
2. **Turns each surface into a moment** — the verdict bar becomes a hero, the run page becomes theater, the share link becomes a story.
3. **Adds power-user surface area** (⌘K, animated trace diff, leaderboard) without burying the editorial voice.

---

## Architecture

### What stays unchanged

- `src/token_compare/` — all backend modules untouched. No new endpoints required for Phase 1; Phase 2 adds two render endpoints (`/og/<token>.png`, `/api/events/stream`) and a `tokenmeter_theme` cookie for theme persistence.
- `db.py` — schema is unchanged. Phase 2 adds two append-only tables (`og_cache`, `event_log`) — see Phase 2 sections. `localStorage` holds the theme preference; cookie only mirrors it for SSR'd OG images.
- All routes (`/`, `/history`, `/compare`, `/admin`, `/share/<token>`) keep their URLs.
- `app.js` keeps its no-`innerHTML` discipline. All new DOM construction goes through `document.createElement` + `textContent`.

### What changes

- **`static/styles.css`** — gets reorganized into theme tokens (CSS custom properties for the 8 looks) + components. The current ~2,700-line file is split into:
  - `static/css/tokens.css` — color/typography/spacing/motion tokens, switchable via `[data-theme="dark"][data-palette="teal-coral"]` etc.
  - `static/css/base.css` — body, layout, header, common components.
  - `static/css/views.css` — per-view styles (catalog, scenario detail, summary, compare, share).
  - `static/css/motion.css` — keyframes + reduced-motion fallbacks.
  - `static/styles.css` — `@import`s the four files, kept for backwards-compat link tags.
- **`static/app.js`** — gains a `theme.js` module (loaded inline in `<head>` to prevent flash of wrong theme) and a `motion.js` helper (animated counters, intersection-observer reveal). Module size grows ~600 lines.
- **`static/index.html`** + the four sibling pages — header gets the theme puck, body gets `data-theme` + `data-palette` attributes set by the inline pre-script.
- **New file `src/token_compare/og_render.py`** — Phase 2 — Pillow-based PNG renderer for OG cards. No HTML template, no headless browser; renders directly from theme tokens + report data. Caches in Postgres (`og_cache` table).

### File layout after Phase 2

```
static/
├── index.html          # catalog + landing + setup + scenario + summary (existing entry)
├── share.html          # guest-mode entry (existing)
├── compare.html        # /compare entry (existing)
├── history.html        # leaderboard (Phase 2)
├── admin.html          # admin (existing)
├── fonts/              # NEW (Phase 2) — self-hosted Fraunces + JetBrains Mono for Pillow OG renderer
├── css/
│   ├── tokens.css      # NEW — theme tokens (8 looks)
│   ├── base.css        # NEW — header, layout, common
│   ├── views.css       # NEW — per-view
│   └── motion.css      # NEW — keyframes + reduced-motion
├── styles.css          # @imports above (kept for any cached HTML)
├── js/
│   ├── theme.js        # NEW — palette/mode switch + persist
│   ├── motion.js       # NEW — counters, reveal, springs
│   ├── cmdk.js         # NEW (Phase 2) — command palette
│   └── ticker.js       # NEW (Phase 2) — live event ticker
├── app.js              # SPA controller (slimmed)
├── compare.js          # /compare (existing, themed)
├── history.js          # NEW (Phase 2) — leaderboard
└── chart.min.js        # untouched
```

---

## Theme system

### Eight looks

Two modes × four palettes = eight exhaustive themes. Each theme is a pair of CSS custom property layers; switching mutates `data-theme` and `data-palette` on `<html>`.

| Mode | Palette | Native gradient | MCP gradient |
|------|---------|------------------|---------------|
| light | `teal-coral` | `#0F766E → #14B8A6` | `#E11D48 → #F472B6` |
| light | `emerald-violet` | `#047857 → #10B981` | `#7C3AED → #A78BFA` |
| light | `cyan-amber` | `#0369A1 → #0EA5E9` | `#D97706 → #F59E0B` |
| light | `forest-terracotta` | `#14532D → #15803D` | `#9A3412 → #C2410C` |
| dark | `teal-coral` | `#14B8A6 → #5EEAD4` | `#F43F5E → #FB7185` |
| dark | `emerald-violet` | `#10B981 → #6EE7B7` | `#8B5CF6 → #C4B5FD` |
| dark | `cyan-amber` | `#0EA5E9 → #7DD3FC` | `#F59E0B → #FCD34D` |
| dark | `forest-terracotta` | `#22C55E → #BBF7D0` | `#EA580C → #FDBA74` |

### Surface tokens

Every theme defines the same set of tokens; only the values change:

- `--surface-canvas` — page background
- `--surface-card` — card fill
- `--surface-glass` — translucent glass overlays (frosted in light, smoke in dark)
- `--ink-primary`, `--ink-secondary`, `--ink-tertiary` — text
- `--accent-native`, `--accent-native-fade`, `--accent-native-glow`
- `--accent-mcp`, `--accent-mcp-fade`, `--accent-mcp-glow`
- `--accent-warn`, `--accent-error`
- `--hair`, `--hair-2`, `--hair-3` — borders
- `--glow-radius-sm`, `--glow-radius-lg`, `--glow-opacity`
- `--motion-spring`, `--motion-snap`, `--motion-out` — easings
- `--shadow-card`, `--shadow-card-hover`, `--shadow-modal`

### Default for new visitors (decided)

- **Mode:** follows `prefers-color-scheme`. Falls through to **light** if the user has no system preference set (kinder for projector demos and shared workstations).
- **Palette:** `teal-coral` — the most distinctive of the four, least "default Tailwind". The other three are one click away in the puck.
- **Override:** once a user touches the puck, `localStorage` wins forever; system preference is no longer consulted unless they re-enable "Match system" in the dropdown.

### The puck (theme selector)

The chosen UX from brainstorming: a labeled pill in the header showing `[gradient puck] Teal · Light  ▾`. Clicking opens a dropdown with:

- Top: light/dark segmented control
- Below: 4 palette tiles, each rendering a 64×40 mini preview (the current view's bars in that palette, animated on hover)
- Bottom: "Match system" toggle (uses `prefers-color-scheme`)

Persisted in `localStorage` under `tokenmeter_theme` as JSON `{"mode":"light","palette":"teal-coral","matchSystem":false}`. The same value mirrors to the `tokenmeter_theme` cookie (URL-encoded JSON, 1-year TTL, `SameSite=Lax`, not `HttpOnly` — the client needs to read it after server bootstraps too) for server-rendered surfaces (PDF, OG cards). On `<html>`, an inline pre-script reads localStorage and sets `data-theme` + `data-palette` *before* the first paint to prevent FOUC.

### Reduced motion

`@media (prefers-reduced-motion: reduce)` flattens all animations: counters jump to final value, bars render at full width, glows render static, hover transforms are removed. Theme switching itself stays animated (200ms cross-fade) regardless — it's the kind of motion that aids comprehension.

---

## Phase 1 — Visual refresh + foundational wow

Roughly one week. Ships as `tier-e` branch → `tier-e-v1` tag.

### F1 · Theme system + puck (foundation)

`tokens.css` with all 8 looks. `theme.js` with `applyTheme(mode, palette)`, `getTheme()`, `subscribe(callback)`. Puck component with dropdown built in vanilla JS, no `innerHTML`. FOUC-blocking inline pre-script in every page's `<head>`. Persistence via localStorage + cookie.

**Acceptance:** All four pages (`index`, `share`, `compare`, `admin`) honor the theme. Switching is instant (<16ms paint). Refresh preserves choice. PDF export reads the cookie and renders in light + the chosen palette.

### F2 · Cinematic verdict hero (scenario detail)

Replaces the small green verdict bar at the top of the scenario detail view. New component: serif Fraunces headline with the cost multiplier as gradient text, two callout pills below (savings-at-scale dollars, token delta), animated counter on the multiplier from 1× to final value (~600ms ease-out). Takes the full content width.

**Acceptance:** Hero replaces `#scenario-verdict-bar`. Counter animates once on view, never on refocus. Reduced-motion shows final state immediately. Existing verdict copy logic in `app.js` reused — only the rendering changes.

### F3 · Cost forecast slider (summary)

Replaces the static "cost at scale" card with an interactive slider: log-scale 10 → 1,000,000 runs/month, with sticky annotations at $1k, $10k, $100k thresholds. Drag = `requestAnimationFrame`-throttled recompute. Existing `/api/reports/{id}/projection` endpoint already accepts a `volume` query param — use it.

**Acceptance:** Slider is keyboard-accessible (←/→ steps, Home/End jump). Annotations appear when crossing thresholds. Tooltip shows monthly $ for both paths. Mobile: single-thumb slider, no dragging numbers.

### F4 · Catalog scenario cards (home)

Replaces the 6-row scenario table with a card grid. Each card shows:
- Category dot (color from palette per category)
- Scenario id + title (Fraunces italic)
- Difficulty pill
- Tiny sparkline of the latest report's Native vs MCP cost for this scenario, if any
- Hover: card lifts + sparkline animates

The "Run benchmark" CTA + runs-per-path / model / max-turns controls float as a sticky footer panel, not a separate card. Selection still tri-state, now via card-level checkbox in the top-right of each card.

**Acceptance:** Same scenarios load. Sparkline data fetched from a new lightweight endpoint `GET /api/scenarios/sparkline?ids=s01,s02,...` returning `{s01: {native: [0.62, 0.58, 0.61], mcp: [0.93, 0.91, 0.95]}}`. If no recent runs, sparkline is omitted. Tri-state selection preserved.

### F5 · Server-rendered OG cards (share)

When a user issues a share link, the response includes an `og_url` pointing at `GET /og/<token>.png?theme=<mode>&palette=<palette>`. Server fetches the report and renders the PNG via **Pillow** (no headless browser): gradient rectangle background per palette tokens, Fraunces title, JetBrains Mono numbers (both fonts shipped in `static/fonts/` and registered with Pillow's `ImageFont.truetype`). Gradient *text* (the `1.5×` multiplier) renders as a flat mid-tone green from the palette since Pillow can't fill text with a gradient — visually 95% indistinguishable in Slack unfurls. Up to 8 PNGs per share token (one per look) are cached in a new `og_cache` Postgres table keyed by `(token, theme, palette)`. Cache TTL = same as share token TTL. The link recipient's `tokenmeter_theme` cookie picks which cached variant the unfurler is served via a 302 redirect; if no cookie, served the default variant.

**Why not Playwright/Chromium:** the Heroku Chromium buildpack adds ~200MB to slug size, slows boot, and adds a moving part. Pillow is a small (~3MB) addition to `requirements.txt` and well-maintained. If fancier rendering is needed later, swap to Satori-on-Lambda or a dedicated worker — don't bloat the main dyno.

`<head>` of `share.html` ships:
```html
<meta property="og:image" content="/og/{token}.png">
<meta property="og:title" content="...">
<meta name="twitter:card" content="summary_large_image">
```

**Acceptance:** Slack/Twitter/iMessage unfurl shows the gradient hero card. Theme respected (cookie-driven). First request blocks ~1.5s for render; subsequent requests serve from cache. Migrating playwright-on-Heroku considerations: spec calls out using the Heroku Chromium buildpack.

### F6 · Visual refresh of every existing screen

Every existing screen rebuilt against the new tokens:
- **Catalog** — F4 above.
- **Setup / progress** — glass cards on the canvas, palette-tinted progress bars.
- **Scenario detail** — F2 hero + restyled comparison chart + restyled trace table.
- **Summary** — F3 slider + restyled stat cards + restyled "When Native wins / MCP wins" grid.
- **Compare** — restyled cards, gradient-text deltas, soft glows on regressions.
- **Share** — F5 + same restyled summary + scenario views.
- **Admin** — minimal restyle, mostly tokens.
- **History** — placeholder; full leaderboard in F11.

**Acceptance:** No layouts regress on width breakpoints (1180, 960, 720, 480). All text passes WCAG AA contrast in both modes against all four palettes. Print stylesheet (PDF export) renders in light + chosen palette with no glows or animations.

---

## Phase 2 — Interactive depth

Roughly one week. Ships as `tier-f` branch → `tier-f-v1` tag.

### F7 · Live theater mode (run page)

During an active SSE-streamed run, the progress view becomes a hero panel:
- Top strip: scrolling progress (`s03 · turn 4 / 12 · 2.341s elapsed`)
- Two-column live cost: Native (teal) vs MCP (coral) with `wfm-num` counters that tick as `cost_delta` events arrive
- Below: animated bars race in real-time
- Below the bars: scrolling event ribbon (last 5 events as compact rows)

The current step-card-list view stays as a collapsed strip on the side. Click a step to jump to its detail.

**Acceptance:** Bars and counters animate smoothly under typical SSE event rates (≤10/sec). If SSE drops, the polling fallback fills in progress without visual jank. Reduced-motion shows static progress + final values per step.

### F8 · Animated trace diff (scenario detail)

Replaces the current side-by-side trace table with an interactive component:
- Top: per-turn token bars side-by-side (Native left, MCP right), bars colored by path
- Each turn = one row; clicking a row expands the existing prompt/tool/reply detail below
- Hover a turn: bars animate from baseline to value (~250ms)
- Top-right: "Step through" button that auto-scrubs through turns at 1/sec, useful for demos
- Mini-toolbar: "Show divergence" (highlights turns where MCP took >2× tokens), "Show tool calls only"

**Acceptance:** Existing `/api/scenarios/{id}/trace` endpoint reused unchanged — all interactivity client-side. Step-through respects reduced-motion (jumps without easing). Keyboard navigation: ↓/↑ between turns, ↵ to expand.

### F9 · Animated regression flow (`/compare`)

Reframes the cube-vs-cube diff as a story:
- Top: "Report A → Report B" header with started_at, model, scope
- Center: a stacked timeline of scenarios, each as a "before/after" mini-strip:
  - Bar A on the left, bar B on the right, connected by a flowing line
  - Line color = green for improvement, red for regression, gray for unchanged
  - Animated draw on render (left-to-right, ~400ms)
- Hover a scenario: a popover shows which turns drove the change (data from `/api/scenarios/{id}/trace`)
- Sort: regressions surface first (already implemented in `compare.py`)

**Acceptance:** Existing `/api/reports/compare` endpoint reused. Hover-driven trace lookup batched (one fetch per hovered scenario, cached per page load).

### F10 · Storytelling share view (`/share/<token>`)

Replaces the current "embedded summary" share view with a long-form scrollytelling page:

1. **Hero (100vh)** — gradient background, single Fraunces headline ("Native is **1.5× cheaper** here."), three large stat numbers, scroll cue.
2. **The setup** — short editorial paragraph explaining the run conditions.
3. **The numbers** — full-width animated comparison chart, scroll-triggered.
4. **The detail** — per-scenario cards, each revealing on scroll-into-view.
5. **The takeaway** — "When Native wins / When MCP wins" grid.
6. **Foot** — verbatim disclosure (run metadata, methodology, timestamp, expiry).

Built with intersection-observer reveals (no scroll-jacking). Sections use `scroll-snap` lightly for the hero only. Mobile reduces hero to ~70vh and stacks stats.

**Acceptance:** All existing data hydrated from `/api/share/<token>/data`. Reveals graceful-degrade when JS disabled (everything visible at once). The `<head>` keeps the F5 OG meta tags.

### F11 · `/history` leaderboard

Reshape the existing flat list into a leaderboard:
- Top: "Champion" — the lowest-cost report, highlighted with palette accent + crown icon
- Below: a sortable table of reports with run-over-run delta arrows (`$0.62 ↓ from 0.65`)
- Each row expandable to show per-scenario sparkline
- Filter chips: model, operator, scope, date range
- "Compare to champion" button on each row → `/compare?a=<champion>&b=<row>`

**Acceptance:** Same `/api/reports?limit=200` endpoint. Sort + filter client-side.

**Champion logic (decided):**
- **Eligibility:** only reports that ran ≥80% of the *current* scenario catalog compete. Reports that ran a tiny subset can't accidentally win.
- **Rule:** lowest median Native cost across the catalog wins.
- **Tiebreaker:** most recent wins (rewards iteration).
- **Manual pin:** an admin button on `/history` pins a specific report as champion regardless of the rule. Useful when leadership wants the "official benchmark of record" frozen for a quarter. Pin state stored in a new `app_settings` row keyed by `champion_pin_report_id`.
- **Footnote:** a small "How was this picked?" link under the champion ribbon expands to explain the rule + show whether the pin is active.

Delta arrow uses the next-most-recent same-model report as comparison.

### F12 · ⌘K command palette

Global keyboard shortcut. Opens a Linear-style palette that supports:
- Navigate: `go to s04`, `open report rpt_a3f`, `go to compare`, `go to history`
- Actions: `run benchmark`, `share latest report`, `export PDF`, `toggle theme`, `switch palette`
- Search: free-text matches against scenario titles, recent reports, theme names
- Footer: `↵ to select  ⇥ to expand  ⌘+ to copy`

Built with vanilla JS; depends on `theme.js` for theme actions and a new `commands.js` for the registry. Ships with `?` overlay showing all shortcuts.

**Acceptance:** ⌘K (or Ctrl+K) opens from anywhere. Keyboard-only navigation works. Esc closes. Searching is fuzzy (substring match weighted by title position). No layout shift when opening — overlay is a fixed-position modal.

### F13 · First-run onboarding

Replaces the current login splash with a 3-step guided card:
1. **Connect Salesforce** — OAuth popup, ~30s
2. **Pick scenarios** — preselects the catalog, lets you trim
3. **Run** — preflight check + "Run benchmark →" big button

Each step is a card that animates in, completed steps collapse. Skippable for returning users (cookie-detected).

**Acceptance:** Existing `/api/preflight` + OAuth flow reused. Returning users (with `tokenmeter_returning=1` cookie, set on first successful run) skip directly to landing. First-timers see the wizard; "Skip setup" link in step 1 lets them out and sets the cookie too.

### F14 · Live event ticker

Always-available, collapsed-by-default panel pinned bottom of the app (or as a slide-out drawer on the right). Streams low-level events:
- Tool calls (`mcp__sf__soql · 121ms`)
- Cache hits (`cache_read · 1,892 tok`)
- Errors (`rate_limit · retrying in 2s`)
- SSE state (`stream connected`, `stream dropped → polling`)

Toggle: `⌥E` or click the small pulse indicator in the header. Persists across navigation. Auto-clears on benchmark complete unless pinned.

**Acceptance:** Doesn't interfere with primary content (max-height 240px when expanded, never overlaps the main panel). Reads from a new `/api/events/stream` endpoint that mirrors run events + adds **tool calls, cache hits, and errors only** (decided scope — these affect the cost narrative the user is watching). DB writes, OAuth refreshes, and other plumbing events are intentionally excluded — they'd drown the signal. Scoped by `sid` (the existing signed-cookie session id from `sessions.py` — same auth posture as `/api/run`). Falls back to log entries from `/api/run/status` when SSE unavailable. New `event_log` table (append-only, 7-day retention) stores recent events for the polling fallback.

---

## Cross-cutting concerns

### Accessibility

- WCAG AA contrast on all 8 themes verified via automated test (axe-core in CI).
- All interactive components reachable by Tab; focus-visible rings use `--accent-native` per palette.
- Screen-reader-only labels on every icon-only button.
- `aria-live="polite"` on the live theater counters; not the event ticker (would be too noisy).
- `prefers-reduced-motion` honored everywhere.

### Performance

- Initial CSS payload (compressed, all themes inline) target: <40KB.
- `theme.js` + inline pre-script <2KB.
- Animated counters use `requestAnimationFrame`, capped at 60fps.
- Live theater counters batch SSE events at 10fps (bucket by 100ms windows).
- Intersection-observer reveals use `rootMargin: -10% 0px` to avoid thrash.
- Sparkline data on catalog cards lazy-fetched after main paint (≥50ms after).

### Browser support

- Modern evergreens (Chrome 110+, Firefox 110+, Safari 16+).
- `backdrop-filter` is the only feature without a fallback — older browsers see solid card backgrounds.
- `:has()` used in 2-3 places; no fallback needed (matches browser support targets).

### Testing

- **Unit:** No backend changes in Phase 1; existing 179 tests pass unchanged. Phase 2 adds:
  - `tests/test_og_renderer.py` — F5 OG cache + render
  - `tests/test_event_stream.py` — F14 event endpoint
  - `tests/test_history_champion.py` — F11 champion logic
- **Visual regression:** Add a `tests/visual/` directory with Playwright snapshot tests covering all 8 themes × 5 surfaces = 40 baseline images. Pixel-diff threshold 0.1%.
- **Manual smoke:** Per-phase smoke checklist covering theme switching, share link creation, run-with-live-theater, ⌘K, OG card unfurl in Slack.

### What we're explicitly NOT doing

- No backend rewrite, no schema migrations, no new auth flows.
- No mobile-first redesign — site stays desktop-primary. Phones get a usable-but-compressed view, not a different IA.
- No internationalization. Copy stays English-only.
- No A/B testing infrastructure. Theme is user-chosen, not bucketed.
- No real-time multi-user features. Sessions stay single-operator.

---

## Phasing summary

| Phase | Branch | Tag | Features | Duration |
|-------|--------|-----|----------|----------|
| 1 | `tier-e` | `tier-e-v1` | F1 theme + F2 hero + F3 forecast + F4 catalog + F5 OG + F6 visual refresh | ~5 dev days |
| 2 | `tier-f` | `tier-f-v1` | F7 theater + F8 trace diff + F9 compare flow + F10 share story + F11 leaderboard + F12 ⌘K + F13 onboarding + F14 ticker | ~5 dev days |

Both phases ship behind no flags — direct to `main` after smoke. Old screens are removed in Phase 1 (no "classic UI" toggle); PDF export keeps a print-friendly variant of the chosen theme.
