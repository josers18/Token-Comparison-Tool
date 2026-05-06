# Token Comparison Tool — Design Spec

**Date:** 2026-05-04
**Status:** Approved — ready for implementation planning

> **Historical document.** This is the original RFC that initiated the
> project. It captures the design decisions and trade-offs as they
> were made, but the codebase has since grown features beyond this
> spec (free-format mode, load-saved-reports, JSON sidecar, brand-mark
> home affordance, etc.). For current behavior, see the [README](../../../README.md).
> This document is kept as a record of the original design.

---

## 1. Purpose

An internal showcase benchmark that quantifies the token-cost difference between two ways of invoking Salesforce Headless 360 operations from Claude:

- **Path A — Native:** Claude Code invokes Salesforce APIs directly (e.g., via the `sf` CLI / REST endpoints) using hand-rolled tools.
- **Path B — MCP:** Claude Code invokes the same operations through Salesforce-hosted MCP servers.

Both paths execute the same natural-language scenarios against the same org on the same machine. Every token count, dollar amount, and turn count is sourced from `claude -p --output-format json` so every number in the final report is traceable to Claude Code's own telemetry.

The tool is intended for internal comparison and customer/executive showcases.

### Non-goals

- Measuring Salesforce-side API consumption (API calls, query rows, storage). Out of scope.
- Scoring semantic answer quality beyond a pass/fail `success_criteria` check.
- Hosting a public service. The tool runs locally on each user's machine and is distributed via git.
- Write operations against Salesforce. All scenarios are read-only so any contributor can safely run the benchmark against any org.

---

## 2. Distribution & Execution Model

**Distribution:** GitHub repo. Users `git clone`, run a single bootstrap command, and the tool opens at `http://localhost:8000`.

**Execution:** Locally on each user's machine against their own Claude Code login. The FastAPI backend spawns `claude -p` subprocesses to execute each run; tokens are billed to whichever account is logged into Claude Code on that machine. No Anthropic API keys handled by the app.

**Prerequisites on a user's machine:**
- Claude Code installed and logged in.
- Python 3.11+.
- `sf` CLI installed and authenticated to a Salesforce org (for Path A).
- Salesforce-hosted MCP server registration file available (for Path B). A setup script registers it with the user's Claude Code install on first run.

**Why not Heroku / hosted:** The tool must run against the operator's logged-in Claude Code session to bill their own token usage. A hosted instance would be logged into a single account and could not fairly represent "the user's tokens." Distribution via git is the correct model.

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (index.html + vanilla JS + Chart.js)              │
│   - Stepper: one page per scenario + final summary page    │
│   - Per-scenario dual-panel (Native | MCP) layout          │
│   - SLDS-inspired light-mode design, executive-ready       │
└───────────────────▲─────────────────────────────────────────┘
                    │ HTTP + Server-Sent Events (live logs)
┌───────────────────┴─────────────────────────────────────────┐
│  FastAPI backend (Python 3.11+)                              │
│   ├── GET  /api/preflight      → check claude, sf, MCP reg  │
│   ├── GET  /api/scenarios      → list catalog               │
│   ├── POST /api/run            → execute benchmark (SSE)    │
│   └── GET  /api/reports/latest → fetch current report       │
│                                                              │
│  Runner (single code path, two invocation modes)             │
│   ├── NativeRunner  → claude -p --allowedTools "Bash"        │
│   │                  (preamble instructs sf CLI usage)       │
│   └── McpRunner     → claude -p --mcp-config <sf-mcp.json>   │
│                                                              │
│  Scenario catalog (scenarios/*.yaml)                         │
│   - 5 YAML files, each defining one scenario                 │
│                                                              │
│  Report writer → reports/benchmark-YYYY-MM-DD-HHmm.md        │
└──────────────────────────────────────────────────────────────┘
```

**Key architectural property:** both paths flow through a single `claude -p` invocation helper. The *only* thing that differs between Path A and Path B is the tool-provider flags passed to `claude -p`. This makes the comparison apples-to-apples by construction.

**Scenarios as config, not code:** adding a scenario means adding one YAML file, no code changes.

---

## 4. Scenario Catalog

Five scenarios, one YAML file per scenario under `scenarios/`. All read-only.

| # | ID | Category | Difficulty | Purpose |
|---|----|----------|------------|---------|
| 1 | `s01_soql_top_accounts` | Core CRM | simple | Basic SOQL read — baseline per-turn overhead |
| 2 | `s02_unified_profile_lookup` | Data 360 | simple | Single Data Cloud lookup — exposes schema-tax on short tasks |
| 3 | `s03_segment_publish_check` | Data 360 | medium | Multi-tool chain — list segments → inspect one |
| 4 | `s04_agent_session_trace` | Agentforce | medium | STDM session query — exercises less-trodden MCP area |
| 5 | `s05_opportunity_pipeline_report` | Core CRM + Data 360 | complex | Multi-source join — where MCP's richer schemas may offset overhead |

### Scenario YAML shape

```yaml
id: s02_unified_profile_lookup
title: "Retrieve unified customer profile by email"
category: data-cloud
difficulty: simple   # simple | medium | complex
prompt: |
  Find the unified profile for the customer with email
  "jane.doe@example.com" and return their lifetime value,
  most recent purchase date, and segment memberships.
expected_operations:
  - query_data_cloud
  - get_unified_profile
success_criteria:
  must_contain: ["lifetime value", "segment"]
notes: |
  Tests the schema-overhead hypothesis on a short task.
```

### Execution defaults

- **Runs per path per scenario:** 3 (configurable, UI default 3).
- **Max turns per run:** 15 (hard stop).
- **Per-run timeout:** 90 seconds.
- **Path order randomized** per scenario (coin flip: A-B-A-B-A-B or B-A-B-A-B-A) to avoid systematic time-of-day bias.
- **Each of the 3 runs is an independent `claude -p` invocation** — no shared context between runs.

---

## 5. Measurement Methodology

### Source of truth

Every metric comes directly from `claude -p --output-format json`. The relevant fields:

```json
{
  "result": "...model output...",
  "usage": {
    "input_tokens": 2481,
    "output_tokens": 318,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  },
  "total_cost_usd": 0.0429,
  "duration_ms": 8432,
  "num_turns": 4
}
```

### Per-run metrics captured

| Metric | Source | Rationale |
|---|---|---|
| `input_tokens` | JSON `usage` | Where MCP schema tax manifests |
| `output_tokens` | JSON `usage` | Reasoning + tool calls + final answer |
| `cache_read_input_tokens` | JSON `usage` | Reported separately; caching may favor one path |
| `total_cost_usd` | JSON root | Authoritative; already pricing-aware |
| `num_turns` | JSON root | More turns = more schema re-hydration |
| `duration_ms` | JSON root | Wall-clock latency |
| `tool_calls[]` | Parsed from `--verbose` stream | Sequence of tool names invoked |
| `scenario_succeeded` | Checked against YAML `success_criteria` | Cheap-but-failed run is not a win |

### Held constant between Path A and Path B

- Same scenario prompt, verbatim from YAML.
- Same model (default `claude-opus-4-7`; configurable).
- Same `--max-turns` cap.
- Same machine, same time window (A and B runs alternate).
- Same minimal system-prompt wrapper: *"You have access to tools for querying Salesforce. Complete the user's request and return a concise answer."*

### The one axis of variance

- **Path A (Native):** `claude -p --allowedTools "Bash" ...` plus a preamble telling Claude to use `sf` CLI for Salesforce. No MCP servers enabled for this invocation.
- **Path B (MCP):** `claude -p --mcp-config path/to/sf-mcp.json ... --allowedTools "<mcp tools only>"`. Bash is disallowed so the model must route through MCP.

### Statistical handling

- 3 runs per path per scenario.
- Headline number: **median**. Min/max shown in raw-data appendix.
- Failed runs are **not excluded** — they're reported ("2/3 succeeded, median cost $0.04 across successful runs"). Hiding failures would bias the comparison.

### Reproducibility

- Full Claude Code JSON for every run is embedded in the report's "Appendix — Raw Data" section.
- Every headline number in the report can be recomputed from the appendix by any reader.

---

## 6. User Interface

### Design language

- **Light mode default.** SLDS-inspired tokens, not the full SLDS framework.
- **Typography:** Salesforce Sans (self-hosted) or Inter fallback. 14px base, 32px+ for hero numbers.
- **Colors:** Neutral canvas `#F3F3F3`, cards `#FFFFFF`, text `#181818`, Native accent `#0176D3`, MCP accent `#5867E8`, status `#2E844A / #FE9339 / #BA0517`.
- **Layout:** 8px grid, 1px `#C9C9C9` borders, subtle shadows `0 2px 4px rgba(0,0,0,0.04)`.
- **No external CSS framework.** One `styles.css` with CSS custom properties mapped to SLDS tokens.

### Screen flow — single-page app with JS-driven stepper

**Step indicator (always visible):**
```
●───●───●───●───●───○
s01  s02  s03  s04  s05  Summary
```
- Spinner on the currently-running scenario; check on completed; gray on unstarted.
- Users can click any completed step to view its page while others continue running.

**Preflight banner (top of screen, on load):**
- Verifies `claude` on PATH + logged in, `sf` CLI authenticated, MCP registration file present.
- On failure, banner turns red with remediation text and "Run Full Benchmark" is disabled.

**Setup (initial view):**
- Checklist of the 5 scenarios (all checked by default; uncheck to skip).
- Runs-per-path selector (default 3).
- Model selector (default `claude-opus-4-7`).
- Single "Run Full Benchmark" button.

**Per-scenario page (one per scenario):**

```
╭─ Scenario ──────────────────────────────────────────────╮
│  Unified customer profile lookup                         │
│  Data 360 · Simple · 3 runs per path                     │
│  "Find the unified profile for the customer with..."     │
╰──────────────────────────────────────────────────────────╯

╭─ Native (Path A) ────────╮  ╭─ MCP (Path B) ────────────╮
│  ✓ Complete · 3/3 runs    │  │  ✓ Complete · 3/3 runs    │
│                           │  │                            │
│     1,204                 │  │     4,871                  │
│     input tokens          │  │     input tokens           │
│                           │  │                            │
│  Turns 2   Cost $0.021    │  │  Turns 3   Cost $0.067     │
│  ──────────────────────   │  │  ─────────────────────     │
│  ▸ sf data query Account  │  │  ▸ query_data_cloud (...)  │
│  ▸ sf data query Contact  │  │  ▸ get_unified_profile     │
╰───────────────────────────╯  ╰────────────────────────────╯

╭─ Results (this scenario) ───────────────────────────────╮
│         Native was 3.2× cheaper on this scenario         │
│         $0.021  vs  $0.067                               │
│         Native median    MCP median                      │
│   [horizontal bar chart — tokens in/out, cost, turns]    │
╰──────────────────────────────────────────────────────────╯
```

**Summary page (final step in stepper):**

- Hero summary card: average multiplier, total cost both paths, total tokens, success rates.
- Per-scenario overview: one line per scenario with a horizontal bar showing the cheapness multiplier.
- **Recommendations card:** template-driven text assembled from actual result deltas (e.g., "Native was X% cheaper on simple scenarios, Y% on complex"). Not AI-generated — every sentence traces to measured data.
- Download full report + "Run again" buttons.

### Executive-facing polish

- One hero number per card (input-token count, 32px+).
- Dynamic headline sentence per scenario ("Native was 3.2× cheaper").
- Chart.js restrained styling: no gridlines, labels inline, brand colors, 200ms fade-in only.
- Every visible number has a tooltip citing its source field in the Claude Code JSON.

### Error visibility

- Failed `claude -p` runs turn the relevant panel yellow and show the error inline. Not silently dropped.
- Salesforce-side errors (org rejects a call) surface in the same panel. Distinguishes "MCP was expensive" from "MCP was broken."

### What the UI does NOT include (intentional YAGNI)

- No authentication. Localhost-only.
- No history view across benchmark runs. One run, one set of results per page load. Older reports live in `reports/` as files.
- No per-run replay / transcript view.
- No theming / branding customization.

---

## 7. Report Output

### One artifact per benchmark run

**File:** `reports/benchmark-YYYY-MM-DD-HHmm.md` — one file covering every scenario + summary + recommendations + raw-data appendix. No side files, no per-scenario splits on disk.

### Structure

```markdown
# Token Comparison Benchmark
Date · Operator · Model · Runs per path · Org · Tool commit SHA

## Executive Summary
- Headline sentence
- Totals table (cost, tokens, success rate)
- Recommendations (template-driven from measured deltas)

## Per-Scenario Comparisons
For each of s01..s05:
  - Prompt
  - Native vs MCP table (median of 3)
  - Tool-call sequences for each path
  - One-sentence outcome

## Methodology
- Source of truth (claude -p JSON)
- Held constant / one axis of variance
- Statistical handling
- Out-of-scope items

## Appendix — Raw Data
- Full claude -p JSON for every run, grouped by scenario
- Embedded inline; not separate files
```

### Export formats

- **Markdown** (default): the source-of-truth file, git-friendly.
- **PDF** (optional, on-demand): rendered using the UI's stylesheet so the PDF visually matches the in-browser report. Renderer (WeasyPrint vs headless Chromium) chosen during implementation — see §9. For emailing to execs/customers.

### Retention

- `reports/` keeps the last 10 benchmark reports; rolling cleanup on each new run.
- `reports/.gitignore` excludes `*.md` and `*.pdf` by default. Committing a report is an explicit user action (used to pin a canonical benchmark for reference).

### Privacy / data-hygiene rules for the report

- **Salesforce record data is summarized, not dumped.** Tool outputs showing customer records become counts or redacted summaries ("returned 5 Account records"), not the records themselves.
- **No API keys, tokens, or org URLs** (beyond the org's display name).
- **No full per-turn transcripts.** The report records the sequence of tool names invoked, not full message history. Full transcripts are verbose and often leak scenario-specific data.

---

## 8. Units / Components

Each unit is small, has one responsibility, and communicates through a narrow interface.

| Unit | Responsibility | Interface |
|---|---|---|
| `preflight.py` | Verify `claude`, `sf` CLI, MCP registration file are usable | `check_environment() -> PreflightResult` |
| `scenarios.py` | Load + validate YAML scenario files | `load_all() -> list[Scenario]` |
| `runner.py` | Invoke `claude -p` for a given scenario + path; parse JSON output | `run_once(scenario, path) -> RunResult` |
| `benchmark.py` | Orchestrate N runs × 5 scenarios × 2 paths; aggregate medians; track progress | `run_benchmark(scenarios, n_runs) -> BenchmarkResult` |
| `report.py` | Render `BenchmarkResult` to markdown (+ optional PDF) | `write_markdown(result, path) -> None` |
| `recommendations.py` | Generate summary sentences from measured deltas (template-driven, deterministic) | `generate(result) -> list[str]` |
| `api.py` | FastAPI endpoints + SSE streaming during a run | — |
| `static/index.html` + `static/app.js` + `static/styles.css` | Stepper UI, per-scenario pages, summary page | — |

This split keeps every Python file focused and lets tests exercise each unit without spinning up the FastAPI server.

---

## 9. Open Questions Deferred to Implementation

- Exact `--mcp-config` JSON format for the Salesforce-hosted MCP server — confirmed during setup-script authoring.
- Whether PDF export uses WeasyPrint vs headless Chromium — decided when benchmarking render fidelity against the UI stylesheet.
- Exact Salesforce-authenticated org/user setup steps in the README — documented after a dry run on a clean machine.

---

## 10. Success Criteria for the Project

The tool is considered done when:

1. A user can `git clone` the repo and, following the README, run a full benchmark on their own machine in under 30 minutes of setup + run.
2. A single benchmark run produces exactly one markdown report file covering all 5 scenarios with a summary and recommendations.
3. The UI visibly renders each scenario's dual-panel comparison and the final summary in the light-mode SLDS-inspired design.
4. Every headline number in the report can be recomputed by a reader from the embedded raw-data appendix.
5. The tool runs against the user's logged-in Claude Code account — no Anthropic API key configuration is ever required.
