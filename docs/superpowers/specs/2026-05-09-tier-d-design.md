# Tier D Design — Public Share Links + Two-Report Comparison

**Status:** Approved design, awaiting implementation plan
**Date:** 2026-05-09
**Owner:** josers18 + Claude
**Builds on:** Tier C (failure replay + diff explainer) — already shipped at v44.

---

## Goal

Make benchmark results easy to share and easy to compare. Two audiences:

1. **Recipient with no SF login** — "Open this link, see the report read-only."
2. **Operator chasing a regression** — "Side-by-side what changed between Tuesday's run and today's."

## Non-goals

- N-way comparison (only 2 reports). Tier B's history page already does time series.
- Tool-call-detail diffing (per-run-instance data, not aggregable cleanly across reports).
- Per-link revocation (let-it-expire matches the threat model).
- Password-protect on top of HMAC tokens (overkill for an internal tool).
- View-count tracking (would need DB writes; out of scope).
- Sharing a `/history` view (sparklines would leak names of other reports the recipient shouldn't see).
- Sharing a `/compare` view (sharing comparison results is future tier).
- PDF export of the `/compare` page.

---

## Architectural shift: stateless tokens, pure-compute compare

Tier D adds two top-level routes (`/share/<token>`, `/compare`) backed by two new modules (`share_token.py`, `compare.py`). No DB migration, no changes to existing `RunResult` / `BenchmarkResult` shapes. Both features ride on the cube data model from Tier B.

### Stateless share tokens

A token is `urlsafe_base64(report_id) "." urlsafe_base64(iso_expires_at) "." urlsafe_base64(hmac_sha256(report_id || "." || iso_expires_at, SESSION_SECRET))`. ~120 chars, fits in a Slack message line, no URL-unsafe characters.

Verification: re-compute the HMAC and check expiry. No DB writes, no revocation table.

`SESSION_SECRET` is read at verification time from the environment — same source the existing session middleware uses. Documented consequence: if `SESSION_SECRET` rotates, all outstanding share tokens fail.

### Read-only renderer reuses existing SPA

`static/share.html` loads `app.js` in **guest mode**. The SPA detects guest mode via `window.__SHARE_TOKEN__` set in an inline `<script>` block before `app.js` loads. In guest mode, `app.js`:

- Skips the SF login flow (preflight bypassed).
- Redirects all `/api/...` calls to their `/api/share/<token>/...` equivalents through a single `apiPath(path)` helper.
- Hides the header's Admin / History / Compare links.
- Hides run / freeform / compare buttons throughout.
- Hides sparklines (`renderSparklines` early-return when `state.guestMode`).
- Hides the share button itself.
- Renders a footer banner: "Read-only shared view · expires <date>".

### Compare endpoint is pure compute

`GET /api/reports/compare?a=<id>&b=<id>` loads both reports through `_normalize_to_cube`, runs `compare_reports(a, b)` from `compare.py`, returns a JSON `ReportComparison`. Auth: SF login required.

---

## D1: Public Read-Only Share Links

### Module: `src/token_compare/share_token.py`

```python
class ShareTokenError(Exception): ...

def issue(report_id: str, *, ttl_days: int = 30) -> tuple[str, datetime]:
    """Returns (token_string, expires_at_utc)."""

def verify(token: str) -> str:
    """Returns the report_id if valid; raises ShareTokenError on
    expired / tampered / malformed."""
```

Implementation: `hmac.new(SESSION_SECRET, msg=f"{report_id}.{iso}".encode(), digestmod="sha256").digest()`, base64-url-encoded.

### Endpoints

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/reports/{id}/share` | SF login | Body: `{ttl_days?: 30}`. Returns `{url, token, expires_at}`. |
| `GET /share/{token}` | none | Pretty redirect to `/share.html?token=<token>`. |
| `GET /api/share/{token}/data` | none | Read-only `BenchmarkResult` JSON. Verifies token → loads report → returns. |
| `GET /api/share/{token}/projection` | none | Mirror of `/api/reports/{id}/projection`. Same query params. |
| `GET /api/share/{token}/scenarios/{sid}/trace` | none | Mirror of `/api/scenarios/{sid}/trace` so the share view renders the trace card. |

The `/api/share/...` routes intentionally do not expose `/history` (sparklines would leak names of other reports).

### Share view UI: `static/share.html`

Standalone page like `static/admin.html`. Loads `app.js?v=<ts>` after setting `window.__SHARE_TOKEN__`, `window.__SHARE_EXPIRES_AT__`, and `window.__SHARE_GUEST__ = true`.

Header: brand "Token Comparison Tool" wordmark links to nothing (no home for guest). Right side shows expiry date "shared · expires May 8 2026" — no nav links.

Below: a stripped scenario-view + summary-view exactly as the logged-in user sees, minus run controls / share / compare. The model pill row stays (multi-model reports stay browsable).

Footer banner: small muted text "Read-only shared view · expires <date> · ask the owner for a new link to extend access".

### Share modal on the existing SPA

Each scenario-view and summary-view gets a "Share" button alongside the existing "Export PDF" / "Download report" buttons. Click opens a small modal:

```
  ╔═══════════════════════════════════════════╗
  ║  Share this report                         ║
  ║                                            ║
  ║  https://token-comparison-tool-cb60c8f1...║
  ║                              [ Copy link ] ║
  ║                                            ║
  ║  Expires: May 8 2026  ▼ 30 days           ║
  ║                                            ║
  ║  [ Done ]    [ Regenerate ]               ║
  ╚═══════════════════════════════════════════╝
```

`POST /api/reports/{id}/share` is called when the modal opens (or on Regenerate); the URL is rendered into the readonly input.

### Edge cases

- *Expired token*: `/share/<token>` and the `/api/share/...` endpoints return 410 Gone with `{"error": "expired", "expired_at": "..."}`. The HTML page renders "This link expired on <date>. Ask the owner for a new one."
- *Tampered token*: same 410 Gone treatment. We don't distinguish from "expired" in the user-facing message.
- *Report deleted after token issued*: 404 Not Found.
- *SESSION_SECRET rotated*: all outstanding tokens fail verification (documented).
- *Tool I/O privacy*: tool_call_details ARE rendered in the share view's per-run breakdown if the recipient expands a row. Operator's responsibility to know what scenarios touch sensitive data.

### Tests

`tests/test_share_token.py`:
- Round-trip: `issue(report_id)` → `verify(token)` returns the same id.
- Expired token: TTL=0 days → `verify` raises ShareTokenError.
- Tampered token: flip a bit in the HMAC → raises.
- Malformed token: missing dots → raises.

`tests/test_api.py`:
- `POST /api/reports/{id}/share` returns a URL containing the token.
- `GET /api/share/<token>/data` returns the report payload (with `_normalize_to_cube` applied).
- `GET /api/share/<bad>/data` returns 410.
- `GET /api/share/<expired>/data` returns 410.
- Guest mode is correctly detected: rendering uses `apiPath` indirection.

---

## D2: Two-Report Comparison

### Module: `src/token_compare/compare.py`

```python
class MetricDelta(BaseModel):
    a: float
    b: float
    delta_abs: float           # b - a
    delta_pct: Optional[float] # None when a == 0


class ScenarioCompare(BaseModel):
    scenario_id: str
    title: str
    presence: Literal["both", "added_in_b", "removed_in_b"]
    native_cost: Optional[MetricDelta] = None
    mcp_cost: Optional[MetricDelta] = None
    success_rate: Optional[MetricDelta] = None
    cost_multiplier: Optional[MetricDelta] = None
    p95_duration_ms: Optional[MetricDelta] = None
    regressed: bool = False


class ReportSummary(BaseModel):
    id: str
    started_at: str
    model: str
    operator: str
    org_name: str


class ReportComparison(BaseModel):
    report_a: ReportSummary
    report_b: ReportSummary
    model_used: str
    incompatible: bool = False
    incompatibility_reason: Optional[str] = None
    scope: dict[str, list[str]]  # {added, removed, shared}
    scenarios: list[ScenarioCompare]


def compare_reports(
    a: BenchmarkResult, b: BenchmarkResult,
    *, model: Optional[str] = None,
) -> ReportComparison: ...
```

### Math

Common-model selection: `model = model or _default_model([m for m in a.models if m in b.models])`. Empty intersection → `incompatible=True, scenarios=[]`.

For each `scenario_id` in `union(a.scenarios, b.scenarios)`:

- **Native cost / MCP cost**: median of successful runs' `total_cost_usd` for the chosen model slice. Same rule projection.py uses. None when N=0 successful.
- **Success rate**: `(native_succ + mcp_succ) / (native_total + mcp_total)`. `delta_abs` is in fractional units (0.0–1.0); JS renders as percentage points.
- **Cost multiplier**: `mcp_median / native_median`. None if either is 0.
- **p95 wall-clock**: `max(native_p95_duration_ms, mcp_p95_duration_ms)` per side (the slowest path is the user-visible latency).

### Regression heuristic

```python
regressed = (
    (native_cost.delta_pct is not None and native_cost.delta_pct > 10) or
    (mcp_cost.delta_pct is not None and mcp_cost.delta_pct > 10) or
    (success_rate.delta_abs is not None and success_rate.delta_abs < -0.05)
)
```

10% cost-up threshold; 5 percentage-point success-down threshold. Both are constants at the top of `compare.py` for easy tuning.

### Sort order

Regressed scenarios first; within each group, sort by `abs(native_cost.delta_pct or 0)` descending. `presence != "both"` (added/removed) sort to the end after regressed and non-regressed shared ones.

### Endpoint

```
GET /api/reports/compare?a=<id>&b=<id>&model=<optional>
```

Auth: SF login required. Loads both reports through `_normalize_to_cube`, calls `compare_reports`, returns the JSON.

Errors:
- Either id missing → 422 (FastAPI auto-validates).
- Either report not found → 404.
- Same id for a and b → 400 ("comparing a report to itself").

### SPA — analytics entry point

The reports analytics table gets a new column with a per-row "Compare" button. State:

| State | A's row | Other rows |
|---|---|---|
| Idle | "Compare" | "Compare" |
| A selected | "✓ Selected (cancel)" | "vs A" |
| User clicks B | navigate `/compare?a=<A>&b=<B>` | — |

State is in-memory only (`state.compareSelected`). Cleared on navigation.

### `/compare` page: `static/compare.html`

Standalone page like `/admin` and `/history`. Layout:

```
[ Header — brand · History · Admin · Compare (active) ]
─────────────────────────────────────────────
  Compare two reports

  Report A:  [ rpt_xxx · 2026-05-01 · sonnet · 5 scenarios ▼ ]
  Report B:  [ rpt_yyy · 2026-05-08 · sonnet · 5 scenarios ▼ ]
  Model:     [ sonnet ▼ ]   (only common-to-both)

  [ Run comparison → ]
─────────────────────────────────────────────
  Started:  2026-05-01      2026-05-08
  Model:    sonnet           sonnet (same)
  Operator: me               me     (same)
  Scope:    5 scenarios      5 scenarios   +1 added · 0 removed

  ⚠ Regressions (2)
   ┌───────────────────────────────────────────────────────────┐
   │ s01_top_accounts                                           │
   │ Native    $0.011 → $0.026   +135% ⚠                       │
   │ MCP       $0.024 → $0.058   +141% ⚠                       │
   │ Success   100% → 80%        −20pp ⚠                       │
   │ Ratio     2.18× → 2.23×     +2.3%                          │
   │ p95       1,200ms → 1,800ms +50%   ⚠                      │
   └───────────────────────────────────────────────────────────┘

  Other scenarios (3)
   ...

  Scope changes (1)
   • s06_new_scenario  added in B

  [ Open Report A ]   [ Open Report B ]
```

Each scenario card shows the 5 metric rows. Per metric: green when delta improved, red+⚠ when regressed beyond threshold, neutral gray when within ±5%. Click the scenario card title to open scenario-view in a new tab for either report.

URL pre-filling: `/compare?a=<id>&b=<id>&model=<optional>` pre-selects dropdowns and auto-runs the comparison.

### Edge cases

- *No common models*: page renders "These reports share no common model — comparison requires runs on the same model" with both reports' model lists.
- *Scenario only in B*: card shows "Added in Report B" badge, B values populated, A values render as "—". `presence=added_in_b`.
- *Scenario only in A*: same, mirrored. `presence=removed_in_b`.
- *N=1 on either side*: each scenario card shows a "low confidence — N=1 on side A" badge (reuses Tier A's confidence chip styling).
- *delta_pct = None* (a was 0): render "— → $X.XX (new)" instead of a percentage.
- *Same report on both sides*: 400 Bad Request from the endpoint.

### Tests

`tests/test_compare.py`:
- Identical reports → all deltas 0%, regressed=[].
- Cost regression: native cost up 15% → `regressed=True`, sorted first.
- Success regression: success rate down 6pp → `regressed=True`.
- Added scenario: present in B not A → `presence=added_in_b`, A values None.
- Removed scenario: present in A not B → `presence=removed_in_b`, B values None.
- Model intersection: A=[sonnet, opus], B=[sonnet, haiku] → defaults to sonnet.
- Empty model intersection: A=[opus], B=[sonnet] → `incompatible=True, scenarios=[]`.
- Sort: 1 regression + 2 non-regressions, the regression sorts first.

`tests/test_api.py`:
- `GET /api/reports/compare?a=...&b=...` happy path returns `ReportComparison`.
- Missing id → 404.
- Same a and b → 400.

---

## Cross-cutting

### PDF export

Share view's PDF export uses the same client-side print path the existing SPA uses. The compare page does NOT have a PDF export (out of scope per spec).

### Backwards compatibility

- All Tier C reports work unchanged.
- All pre-Tier-C reports load via `_normalize_to_cube` as before.
- The new analytics "Compare" column is additive — clients that ignore it keep working.
- `app.js` guest-mode detection is feature-additive; logged-in flow unchanged.

### Tests summary

| File | Coverage |
|---|---|
| `tests/test_share_token.py` (NEW) | Round-trip, expiry, tampered, malformed. |
| `tests/test_compare.py` (NEW) | 8 cases for compare math + sort. |
| `tests/test_api.py` | New routes for share + compare. |

---

## Open questions for implementation

None blocking. Two implementation-time decisions to surface:

1. The "regressed" threshold constants (10% cost, 5pp success) live at the top of `compare.py`. If real data shows them too tight or too loose, tune in one place.
2. Share view's footer banner copy. Will surface a draft during implementation; happy to iterate.

## Out of scope (future tiers)

- N-way comparison.
- Tool-call-detail diff.
- Per-link revocation.
- Password protection on share links.
- View-count tracking on shares.
- Shared `/history` view.
- Shared `/compare` view.
- PDF export of `/compare`.
- Slack/email integration to push shares automatically.
