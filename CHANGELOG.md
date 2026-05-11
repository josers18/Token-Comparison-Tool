# Changelog

All notable changes to the Token Comparison Tool. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project's release tags are `tier-{x}-v1`.

## [Unreleased]

_No unreleased changes._

---

## Tier E — `tier-e-v1` · 2026-05-10

The **Spatial Glass redesign**: full theme system with 4 palettes ×
light/dark = 8 looks, header chrome (theme puck + auth chip), and
foundational visual upgrades. No backend or DB schema changes; all
work in `static/` plus one new Python module.

### Added — Theme system (F1)

- 4 palettes (Teal+Coral, Emerald+Violet, Cyan+Amber, Forest+Terracotta) ×
  Light + Dark = 8 looks. Each is a layered set of CSS custom
  properties on `<html data-theme data-palette>`.
- Header **theme puck** — labelled pill that opens a dropdown with a
  Light/Dark segmented control, 4 palette tiles, and a "Match system
  color scheme" toggle.
- Persistence via `localStorage` + `tokenmeter_theme` cookie so
  server-rendered surfaces (PDF export, OG cards) see the active
  theme.
- Inline `<head>` pre-paint script in every page blocks FOUC by
  applying `data-theme` + `data-palette` before first paint.
- `prefers-reduced-motion` honored everywhere (counters jump, glows
  static, transitions ~0ms); theme-switch cross-fade kept for
  comprehension.

### Added — Cinematic verdict hero (F2)

- Per-scenario hero replaces the old verdict bar: editorial Fraunces
  headline ("Native is **1.5×** cheaper here.") with the multiplier
  rendered as gradient text (palette-native or palette-mcp depending
  on the winner).
- Animated counter on the multiplier (1.0× → final, ~600ms ease-out)
  on first reveal.
- Two callouts: "Save / 10k runs" (palette-native) and "Token delta"
  (palette-mcp).

### Added — Cost forecast slider (F3)

- Replaces the static "cost at scale" volume input with an
  interactive **log-scale slider** (10 → 1,000,000 monthly runs).
- rAF-throttled + 300ms-debounced recompute against the existing
  `/api/reports/{id}/projection` endpoint.
- Tick labels at 10/100/1k/10k/100k/1M; keyboard-accessible
  (←/→/Home/End on the range input).

### Added — Catalog scenario cards (F4)

- The home catalog renders as a **card grid** instead of a flat
  table. Each card shows a category dot, scenario id + title,
  difficulty pill, and a tiny **sparkline** of recent Native vs MCP
  cost.
- New endpoint `GET /api/scenarios/sparkline?ids=s01,s02,...` —
  returns up to 20 most-recent runs of native/mcp median cost per
  scenario. Lazy-fetched after main paint.
- Tri-state "Select all" preserved via the existing master
  checkbox.

### Added — Server-rendered OG cards (F5)

- New module `src/token_compare/og_render.py` — Pillow-based PNG
  renderer (1200×630), no headless browser. ~150 lines, 8 palette
  variants mirror the CSS tokens.
- New endpoint `GET /og/{token}.png?theme=&palette=` — verifies the
  share token, renders or serves cached PNG. **In-memory LRU cache**
  (200 entries, FIFO eviction).
- 5 OpenGraph + Twitter meta tags injected into `static/share.html`,
  with a runtime script that fills `og:image` based on the active
  theme.
- `Pillow>=10.0` added to `requirements.txt`.

### Added — Visual refresh of every existing screen (F6)

- `static/styles.css` reorganized into modular CSS files:
  `tokens.css`, `base.css`, `motion.css`, `views.css`,
  `themepuck.css`, `authchip.css`, `overrides.css`, `summary.css`,
  `legacy.css`, `print.css`. Original 2,691-line stylesheet preserved
  as `legacy.css`, with token aliases mapping the legacy
  `--paper-*` / `--ink*` / `--signal*` / `--counter*` names onto the
  new theme tokens so legacy components recolor with the puck.
- New JS modules `static/js/{theme,themepuck,motion,authchip}.js`
  (each ~150-220 lines, IIFE-wrapped, no `innerHTML`).
- Self-hosted fonts: Fraunces (variable, 71KB), JetBrains Mono
  (regular, 264KB), **Inter Tight** (variable, 44KB latin) — added in
  a follow-up after Tier E shipped to fix synthetic-bold artifacts on
  systems without Apple's `-apple-system`.
- Print stylesheet (`print.css`) flattens animations + glows for the
  PDF export.
- Summary page got its own dedicated treatment (`summary.css`):
  Fraunces hero with gradient italic on the verdict, glass cost-totals
  cards with palette accent stripes, glass projection KPIs, per-scenario
  gradient bars, "When Native wins / When MCP wins" framework grid,
  cache-effectiveness tiles, caveats list with amber `!` markers.

### Added — Header auth chip

- New self-contained chip mounted in the header next to the theme
  puck on `index`, `compare`, `admin`, `history` (skipped on `share`).
- Three states: `Sign in` / `Checking…` / `Connected` with palette-tinted
  status dot (breathing pulse when connected).
- Click opens a dropdown with the SF org host + a primary "Connect
  Salesforce →" or secondary "Sign out" button.
- Polls `/api/sf/status` on load, every 30s, and on window focus.
- Broadcasts `tokenmeter:auth-change` events so the SPA reroutes the
  user (login → landing chooser; logout → splash).

### Fixed

- **SVG stroke colors:** sparkline KPI cards (Cost trend, Cache hit,
  Success, p95 wall-clock) and the 12-month projection curve had
  used `path.setAttribute("stroke", "var(--accent-native)")`. SVG
  presentation attributes don't resolve CSS custom properties. All
  four lines were rendering identical default-blue. Switched to
  `path.style.stroke = "var(--accent-native)"` so palettes recolor
  reactively.
- **Stepper visibility:** the `s01-s06 / Summary` chips were rendering
  on the home page because `views.css` declared `.stepper { display:
  flex }` unconditionally. Restored the `body.has-stepper` gate so
  the stepper only shows during the benchmark flow.
- **Token delta callout:** dropped the redundant `" tok"` suffix —
  the label already says "Token Delta" and the suffix overflowed at
  small widths.
- **Variable font weight:** the original Inter Tight TTF I downloaded
  was the single-weight 400 variant but declared as `font-weight:
  100 900`. Browsers asked for 600 on titles, fell back to synthetic
  boldening. Replaced with the real variable woff2 (~44KB latin
  range, 100..900 weight axis).
- **Cool canvas:** `--paper-50` was still pointing at `#FBF9F3`
  (warm cream) on legacy components. Retargeted to
  `var(--surface-canvas)` so legacy widgets sit on the cool Spatial
  Glass canvas.

### Changed

- Tests: 179 → **190 passing**, 5 skipped. Adds: 3 sparkline
  endpoint, 5 OG renderer, 3 OG endpoint coverage.
- README extensively rewritten — full file tree, ~25 endpoint API
  reference (was 14), Spatial Glass screenshots replaced the
  pre-Tier-E captures, screenshots refreshed via reproducible
  `scripts/capture_screenshots.py`.

---

## Tier D — `tier-d-v1` · 2026-05-09

**Share links + two-report comparison.** No DB schema changes.

### Added

- **HMAC-signed share tokens** (`src/token_compare/share_token.py`):
  `issue(report_id, ttl_days=30)` mints a token, `verify(token)` returns
  the report id or raises `ShareTokenError`. SHA-256 over
  `(report_id || expires_iso)` keyed with `SESSION_SECRET`. Rotating
  the secret invalidates every outstanding share link.
- New endpoints under `/api/share/<token>/...` mirroring the
  authenticated equivalents (`/data`, `/projection`,
  `/scenarios/{id}/trace`). Read-only; 410 Gone on tampered or
  expired tokens.
- **Pretty share URL:** `GET /share/<token>` 307-redirects to
  `/share.html?token=...` so links can be sent without exposing the
  query-string detail.
- **Guest mode SPA:** `static/share.html` reuses `app.js` with
  `window.__SHARE_GUEST__ = true` and an `apiPath` indirection that
  routes hits to `/api/share/<token>/...`. Sparklines and
  authenticated-only controls hide via `data-hide-in-guest`.
- Share modal on summary + scenario views: pops over the page with a
  copyable URL, expiry date, and "Regenerate" button.
- **Cube-vs-cube `/compare`** (`src/token_compare/compare.py`):
  `compare_reports(a, b)` produces a `ReportComparison` with regression
  flagging (cost delta > 10% OR success drop > 5pp), tiebreaker on
  shared model, scope changes (added / removed scenarios) called out
  separately.
- New endpoint `GET /api/reports/compare?a=<id>&b=<id>`.
- New page `/compare` with selector for two reports + a results panel
  that surfaces regressions first.
- Compare column in the analytics reports table — pick A on one row,
  vs B on another → jump to `/compare?a=...&b=...`.

### Changed

- Tests: 159 → 179 passing.

---

## Tier C — `tier-c-v1` · 2026-05-08

**Per-turn diff explainer + failed-run replay capture.**

### Added

- `src/token_compare/diff_explainer.py` — pairs Native and MCP turns
  by ordinal, classifies each delta with up to 4 reason chips (cache
  miss, tool overhead, retry, longer prompt). `/api/scenarios/{id}/trace`
  now returns `turn_diffs[]`.
- Trace table on scenario detail gained a **Δ column** with reason
  chips and a per-run row expansion that replays the full input → tool
  → output sequence for that run.
- **Replay capture in the runner:** every `tool_use` block now
  records its input + output (truncated to 2KB) into the
  `RunResult.tool_calls` shape so the trace UI can render the actual
  arguments and results, not just the names.
- **Failure capture:** `anthropic.APIError` populates
  `RunResult.inference_error` with status code + body. MCP gateway
  HTTP errors (the new `mcp_proxy` shim) capture the response body for
  replay debugging. Unhandled exceptions capture a traceback.
- Binary-content guard tightened so Unicode payloads (e.g., a
  Salesforce account name with em-dash) are not misclassified as
  binary and discarded from the replay.

### Changed

- New `RunResult` enrichment fields: `ErrorResponse`, `InferenceError`,
  per-tool input/output detail.

---

## Tier B — `tier-b-v1` · 2026-05-08

**Multi-model cube + cost-at-scale + history walker.**

### Added

- **Multi-model cube schema:** `BenchmarkResult.runs_per_model` and
  `models[]`. `ModelRunBucket` holds Native + MCP runs per model.
  `_normalize_to_cube` shim transparently upgrades legacy
  single-model payloads on read.
- `runs.model` column on the `runs` Postgres table (idempotent
  backfill on dyno startup).
- `/api/run` and `/api/run/freeform` accept `models[]` to sweep
  multiple models in a single benchmark.
- SPA gained a **multi-checkbox model picker** (haiku / sonnet /
  opus). Scenario-view + summary surface a model pill row to switch
  between cube buckets.
- **Cost-at-scale projection panel** on the summary view: monthly
  spend at `volume × period` with growth rate, breakeven thresholds
  ($1k/$10k/$100k by default), and a 12-month cumulative SVG curve.
- New endpoint `GET /api/reports/{id}/projection`. Math lives in
  `src/token_compare/projection.py`.
- **History walker** (`src/token_compare/history.py`): walks
  finalized reports in time order, extracts per-scenario per-metric
  series, surfaces "change markers" when a config tweak shifts the
  curve. Backs `/api/history` and the new `/history` page (2×2 chart
  grid).
- **Regression sparklines** on the scenario detail view (4 KPI
  cards: cost / cache / success / p95 wall-clock). Hide gracefully
  when there's no recent history.
- `@media print` rules for the projection panel + sparklines so the
  PDF export keeps the new charts.

### Changed

- `state.scenarioResults[sid][model]` cube replaces the old flat
  per-scenario result map; SPA components updated.
- Reports analytics list gained a Models column and a model filter.

---

## Tier A · 2026-05-07

**Analytics page + variance/cache/duration/outcomes rollups.** No
release tag (work shipped continuously into the Heroku port).

### Added

- Reports analytics page (`/` setup-view → "View previous runs"
  card): KPI strip, sortable table, kind / model / search filters,
  per-row Actions menu.
- Variance, cache-hit ratio, wall-clock p95, and outcome-type
  rollups computed from `runs` rows.
- Markdown + PDF download per report.
- Login splash + admin scenario CRUD page (gated on the SF OAuth
  session, not a separate token).
- Three-path landing chooser ("Run benchmark / Free-format /
  View previous runs").
- Pretty `/admin` URL (307 → `/admin.html`).

### Fixed

- SF OAuth tokens now persist through dyno restarts (pending logins
  written to Postgres, not in-memory).
- API tolerates null/NaN numeric fields, missing operator/org_name
  on `RunRequest`, empty SPA select values.
- SSE + polling fallback dedupes duplicate `run_complete` events.
- Expired SF tokens refreshed before each run.
- Path-traversal test obsoleted — report ids are opaque
  DB-generated `rpt_<hex>`, not filesystem paths.

---

## Heroku port — `heroku-port` · 2026-05-07

**Move from local CLI tool to Heroku web app.** Foundational shift —
adds OAuth, Postgres, multi-user sessions, and the SPA frontend. Pre-tier
A baseline.

### Added

- **FastAPI** web app (`uvicorn token_compare.api:app`) with SSE
  streaming, OAuth callback, and the original CLI's runner / parser
  / pricing / scenarios modules unchanged.
- **OAuth 2.1 + PKCE** Salesforce login flow
  (`src/token_compare/sf_auth.py`).
- **Postgres-backed sessions** — SF tokens live in the `sessions`
  table keyed by an HTTP-only signed cookie. No filesystem token
  cache.
- **Postgres-backed reports** — every benchmark writes a `reports`
  row keyed by an opaque `rpt_<hex>` id; per-turn rows in `runs`.
- **Heroku Inference** addon used for all model calls (haiku /
  sonnet / opus). `inference_client.py` factory sets up the
  Anthropic SDK against the addon's URL + key.
- **Vanilla-JS SPA** (`static/index.html` + `static/app.js` +
  `static/styles.css`) — no framework. Hard discipline: no
  `innerHTML` with interpolated content, every DOM node built via
  `createElement` + `textContent`.
- Loaders for legacy local-tool reports (`.md` + `.json`) so older
  reports can still be viewed.
- 80-test pytest suite.

---

## Original local tool · 2026-05-04

**Initial release** — single-user CLI that drove Claude Code through
the catalog and produced a markdown report. Replaced wholesale by the
Heroku port; kept here for historical context.

### Added

- 6-scenario YAML catalog covering Sales Cloud SOQL through
  multi-DMO Customer 360 joins.
- `messages_runner.py` driving the Anthropic Messages API with
  Native (REST tools) and MCP (`mcp_servers=[...]`) paths.
- Editorial markdown report with verdict bar, three stat cards,
  per-scenario cost bars, and "When Native wins / When MCP wins"
  framework prose.
- Tokens cached at `.cache/sf-token.json` (gitignored, 0o600) —
  retired in the Heroku port.
