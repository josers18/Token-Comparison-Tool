# Tier B Design — Cost-at-Scale, Multi-Model Sweep, Regression History

**Status:** Approved design, awaiting implementation plan
**Date:** 2026-05-08
**Owner:** josers18 + Claude
**Builds on:** Tier A (variance + cache + outcomes + duration) — already shipped.

---

## Goal

Make the "MCP costs N× Native" claim defensible to three different audiences:

1. **CFO** — "What does this cost me at production volume?" (B1)
2. **PM / Architect** — "Which model should we use?" (B2)
3. **Ops** — "Did something regress?" (B3)

All three are bundled into one Tier B release.

## Non-goals

- Traffic-mix weighting (uniform volume only).
- Per-org pricing overrides — `pricing.py` stays the source of truth.
- Automatic "best model" recommendations.
- Drift alerting / notifications.
- Statistical significance tests on regression.
- Cross-scenario rollups.

---

## Architectural shift: report-as-cube

**Today:** an active report is a flat list of `(scenario, native_runs[], mcp_runs[])`.

**Tier B:** an active report is a cube indexed by `(scenario, model, path)`. Every existing reduction (cost, cache, success, p95) is a function of that cube. B1 reduces across volume; B2 expands the model axis; B3 reduces across reports over time.

This shape change is what makes B1/B2/B3 mostly UI work on top of one shared data structure.

### Backend data model changes

```python
# src/token_compare/models.py
class BenchmarkResult(BaseModel):
    ...
    model: str            # KEPT for backwards compat; equals models[0] when single-model
    models: list[str]     # NEW; for single-model reports = [model]
    ...

class ScenarioResult(BaseModel):
    scenario_id: str
    native_runs: list[RunResult]    # KEPT, denormalized view of runs_by_model[primary_model]
    mcp_runs: list[RunResult]       # KEPT, same
    runs_by_model: dict[str, ModelRunBucket]  # NEW
    ...

class ModelRunBucket(BaseModel):    # NEW
    native_runs: list[RunResult]
    mcp_runs: list[RunResult]
```

`runs` table gets a `model TEXT NOT NULL` column. Backfill in `db.migrate()`:

```sql
ALTER TABLE runs ADD COLUMN model TEXT;
UPDATE runs SET model = (SELECT model FROM reports WHERE id = runs.report_id) WHERE model IS NULL;
ALTER TABLE runs ALTER COLUMN model SET NOT NULL;
```

### Read-time shim for legacy reports

Reports written before this design ship don't have `models` or `runs_by_model`. A `_normalize_to_cube(payload: dict) -> BenchmarkResult` helper runs in the API read path:

```python
def _normalize_to_cube(payload: dict) -> dict:
    if "models" not in payload:
        payload["models"] = [payload["model"]]
    for sr in payload.get("scenarios", []):
        if "runs_by_model" not in sr:
            sr["runs_by_model"] = {
                payload["model"]: {
                    "native_runs": sr.get("native_runs", []),
                    "mcp_runs": sr.get("mcp_runs", []),
                }
            }
    return payload
```

No destructive migration of `payload_json` rows — old data stays valid, the API normalizes on read. New reports write the cube shape directly.

### Frontend data model

`state.scenarioResults[sid]` becomes `state.scenarioResults[sid][model] = {native: [], mcp: []}`. `state.activeModel` controls which slice the existing scenario-view + summary-view render. When the report has one model, the model selector hides and behavior is unchanged from today.

---

## B1: Cost-at-Scale Projector

### UX surface

Replaces the existing `#summary-scale` + `#summary-bars` block in the summary-view. The "Cost at scale" heading is already there; the section grows from a single number into a real projection panel.

### Inputs

| Input | Type | Default | Notes |
|---|---|---|---|
| Volume | number | 10000 | "runs per scenario per period" |
| Period | segmented control | Monthly | Daily / Monthly / Annual |
| Growth rate % | number | 0 | Monthly growth applied to the 12-month curve only, NOT to headline totals |
| Breakeven thresholds | 3 number inputs | $1K / $10K / $100K | Persisted to localStorage |
| Model | dropdown | sonnet (or report's only model) | Hidden when report has 1 model. See B2 default rule. |

### Outputs

1. **Headline totals** — three big numbers: Native total, MCP total, Delta with multiplier. Recomputes live (debounced 300ms) on volume/period/growth/model changes.
2. **Per-scenario bars** — keeps `#summary-bars` shape; bars now show projected spend at chosen volume + period. Sorted by absolute delta descending.
3. **Breakeven table** — for each scenario, computes "MCP becomes ≥$X more expensive at N runs/scenario/period" for each of the three editable thresholds. Edge cases:
   - Native > MCP: row flips to "Native catches up at N."
   - Costs within 5% of each other: render "≈ break-even" instead of a misleading huge number.
4. **12-month cumulative spend curve** — inline SVG, two lines (Native, MCP), x-axis months 1–12, y-axis cumulative dollars. Hover shows running totals + delta. Renders as inline SVG so it survives PDF export.

### Math

All server-side, in new `src/token_compare/projection.py`:

```python
def project_at_scale(
    bench: BenchmarkResult,
    *,
    runs_per_scenario_per_period: int,
    period: Literal["day", "month", "year"],
    growth_rate_pct: float = 0.0,
    breakeven_thresholds_usd: list[float] = [1_000, 10_000, 100_000],
    model: str | None = None,
) -> ScaleProjection: ...
```

**Growth rate math (12-month curve only):**
- If `g == 0`: `month_n_native = native_per_run * volume * n` (linear)
- If `g != 0`: geometric series `month_n_native = native_per_run * volume * ((1+g)^n - 1) / g`

Headline totals do NOT apply growth — they're the clean per-period baseline. Growth only shapes the curve.

**Breakeven math:**
- `breakeven_volume = threshold / (mcp_per_run - native_per_run)` when MCP > Native
- `breakeven_volume = threshold / (native_per_run - mcp_per_run)` when Native > MCP
- `|mcp_per_run - native_per_run| / max(mcp_per_run, native_per_run) < 0.05` → render "≈ break-even"

### Endpoint

```
GET /api/reports/{id}/projection
  ?volume=10000
  &period=month
  &growth_rate_pct=0
  &thresholds=1000,10000,100000
  &model=claude-4-5-sonnet
```

Returns `ScaleProjection`:
```json
{
  "native_total": 1234.56, "mcp_total": 2345.67, "delta": 1111.11, "multiplier": 1.9,
  "per_scenario": [{"scenario_id": "...", "native": 12.3, "mcp": 23.4, "delta": 11.1}],
  "breakevens": [{"scenario_id": "...", "threshold_usd": 1000, "runs_to_breakeven": 12345, "frame": "mcp_more_expensive"}],
  "curve": [{"month": 1, "native_cum": 100, "mcp_cum": 190}, ...]
}
```

### Edge cases

- Single-run scenarios (N=1): project anyway, propagate Tier A confidence chip into projection panel as "low-confidence projection" badge.
- Path failed all runs: that path shows "—", per-scenario bar still renders, headline excludes it; caveat string calls out by name.
- Negative deltas (Native more expensive): coloring inverts; headline reads "Native costs X% more at this volume."

---

## B2: Multi-Model Sweep

### UX surface

Model `<select>` on benchmark setup card → multi-checkbox list. Default: only sonnet checked (matches today's behavior). Selecting 2+ checkboxes turns it into a sweep automatically; no separate button. Same swap on the freeform setup card.

### Default model resolution

Helper used by SPA initial render, `/api/reports/{id}/projection` when `model` omitted, and PDF export:

```python
def _default_model(models: list[str]) -> str:
    for m in models:
        if "sonnet" in m.lower():
            return m
    return models[0]
```

Case-insensitive substring match keeps future sonnet versions working.

### Wire format

`POST /api/run` and `/api/run/freeform`:
- Accept `models: list[str]` (new, preferred).
- Accept `model: str` (legacy, promoted server-side to `models=[model]`).
- SPA always sends `models`.

### Server-side execution

`run_benchmark` gains an outer loop over models. Existing scenario × runs_per_path loop is unchanged inside each model's iteration. Random path interleaving stays per-`(scenario, model)` so cache effects are still controlled.

For 3 scenarios × 3 models × 5 runs/path × 2 paths = 90 runs total (vs 30 today). SSE progress bar's `totalRuns` denominator multiplies by `models.length`. Progress text format: `"Scenario X · sonnet · MCP run 2/5"`.

### Storage

One report per sweep:
- `BenchmarkResult.models: list[str]`
- `ScenarioResult.runs_by_model: dict[str, ModelRunBucket]`
- `runs.model` column

### SPA changes

- Model selector pill row above the verdict block in scenario-view, above the headline in summary-view. Hidden when `len(models) === 1`.
- Clicking a pill swaps `state.activeModel` and re-renders without refetching.
- Reports analytics table gets a "Models" column (count or comma list, abbreviated). The existing model filter dropdown matches if the report contains the chosen model — i.e., a sweep of opus+sonnet shows up under both "opus" and "sonnet" filters.

### Edge cases

- One model fails entirely (e.g., haiku rate-limited): that model's slice shows the failure outcomes from Tier A; other models render normally. Sweep still completes.
- Models with very different prices (opus 5× sonnet): the model pill row provides the context label so a viewer doesn't misread the data as MCP regressing.
- Mid-sweep abort: `_current_run` cache and `runs` table get partial data. Report row stays unfinalized so analytics filters it out via `finalized_only=True`. Same handling as today.

---

## B3: Regression History

### Identity

A history line is one `(scenario_id, model)` tuple over time. Native and MCP get separate lines on the same chart so drift on each path is independently visible.

### UX surfaces

#### Sparklines on scenario-view

Tucked under the verdict block, before the dual native/MCP panels. Four small SVG sparklines side-by-side: median cost, cache hit %, success %, p95 duration. ~80×30px each, no axes, just the line + last-value label. The "<2 points" rule is per-metric — if cost has 5 points but p95 has 1 (e.g., older reports didn't track p95), only the p95 slot collapses to "(needs ≥2 reports for trend)." If all four metrics have <2 points, the entire sparkline row hides rather than showing four dead slots. Hover shows a tooltip with underlying data points + dates.

#### `/history` page

New top-level route. Header link added next to "Admin." Layout:

- **Top filter bar:** scenario picker (from `db.list_scenarios`) + model picker (distinct models in `runs`) + date range (last 7d / 30d / 90d / all, default 30d) + metric checkboxes (cost / cache / success / p95, default all four checked) + "Show change markers" toggle (default off).
- **2×2 chart grid:** one full-size chart per checked metric. Two lines per chart (Native, MCP). Proper axes, gridlines, tooltips with report_id.
- **Data table:** rows of (date, report_id, model, native median, MCP median, % delta from previous). Clicking a row opens that report in scenario-view.

### Backend

New `src/token_compare/history.py` with metric extractors. Endpoint:

```
GET /api/history?scenario_id=X&model=Y&metric=cost&since=2026-04-08
```

Metrics: `cost | cache | success | p95_duration`. `since` defaults to 30 days ago.

Returns:
```json
{
  "scenario_id": "...",
  "model": "...",
  "metric": "cost",
  "points": [{"report_id": "rpt_...", "started_at": "...", "native": 0.012, "mcp": 0.023}],
  "change_markers": [{"report_id": "rpt_...", "kind": "prompt_edited", "detail": "..."}]
}
```

### Implementation

Walks finalized reports newest→oldest, pulls `payload_json`, normalizes via `_normalize_to_cube`, projects out the `(scenario, model)` slice, computes the requested metric. Stops at `since`. Expected ~50ms for 100 reports.

If measured to be slow, add a Postgres view materializing `(report_id, scenario_id, model, native_median_cost, ...)`. Defer until measured.

### Caching

`Cache-Control: max-age=60` + ETag derived from `latest_finalized_report.started_at + scenario_id + model`. Sparklines hit the endpoint once per scenario-view render; reopens are instant. /history page hits on filter change.

### Change markers

Server-side detection in the same response. For each consecutive report pair in the series, compare `payload.scenarios[i].prompt` and `payload.model`/`payload.models`. Emit `change_markers` for prompt edits or model swaps.

Toggle state persisted to localStorage. When ON, charts render vertical dashed reference lines at marker x-coords with hover tooltips ("prompt edited" / "model swapped opus→sonnet"). Sparklines never render markers (no room).

### Edge cases

- Same scenario re-run within minutes: each report is a point; close-together x-coords are visible noise. Don't dedupe.
- Missing model in older reports (pre-B2): walker reads `payload.model` or `payload.models`; old reports without filter model contribute zero points (gap = discontinuous line, not fake zero).
- Scenario renamed/deleted: history keys on `scenario_id`, so renames don't break. Deletes leave history intact (payload still has data).
- First report after a prompt edit: data point is genuinely incomparable. Surfaced via change marker (when toggle is on) and "% delta from previous" column flagged.

### Privacy

Per-Heroku-instance, behind SF login like everything else. No external publication.

---

## Cross-cutting

### PDF export

Updated to render:
- Active model selector state (which model is being projected).
- Cost-at-scale projection panel (headline + breakeven table + 12-month curve as inline SVG).
- Sparklines (skipped if <2 history points).

### Tests

- Projection math: linear (`g=0`), geometric (`g>0`), breakeven thresholds, edge cases (Native > MCP, ≈ break-even).
- History walker: single-model legacy reports, multi-model reports, mixed timeline, change marker detection.
- Multi-model report round-trip: write cube → read → assert `runs_by_model` equality.
- Default model resolver: sonnet present, sonnet absent, sonnet-variant ('claude-5-sonnet'), case variations.
- Backwards compat: legacy `model: str` request body still accepted.
- Endpoint: `/api/reports/{id}/projection` with all combinations of inputs.
- Endpoint: `/api/history` with all 4 metrics, `since` filtering, ETag.

---

## Open questions for implementation

None blocking. Will surface during implementation if any.

## Out of scope (future tiers)

- Tier C: per-turn token diff, failed-run replay debugging.
- Tier D: public read-only report URLs, two-report comparison view.
- Drift alerting on history.
- Per-org or per-user pricing overrides.
- Best-model auto-recommendations.
