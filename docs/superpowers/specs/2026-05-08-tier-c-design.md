# Tier C Design — Per-Turn Token Diff + Failed-Run Replay

**Status:** Approved design, awaiting implementation plan
**Date:** 2026-05-08
**Owner:** josers18 + Claude
**Builds on:** Tier B (cube data model + cost-at-scale + multi-model sweep + history) — already shipped.

---

## Goal

Make the app self-explanatory when MCP misbehaves. Two audiences:

1. **Operator debugging a failure** — "MCP failed; what was the actual error response from the gateway?"
2. **Architect reading a run** — "MCP burned 2× the tokens; what was the per-turn breakdown and which turn was the culprit?"

Both today require tailing Heroku logs or eyeballing the raw trace JSON. Tier C surfaces both inline.

## Non-goals

- "Re-run this failure" button (would need to capture full request bodies for replay; defer).
- Cross-model diff (sonnet vs opus per-turn). Tier B already exposes per-model slices.
- Syntax highlighting on tool I/O.
- Search/filter inside tool I/O.
- Persisting raw HTTP request bodies (only response bodies for failures).

---

## Architectural shift: enrichment, not migration

Tier C does not change any existing data shape, endpoint, or DB column. It *adds* optional fields to `RunResult` that get populated by the runner where failures (and tool calls) happen, and renders them in surfaces that already exist.

### `RunResult` gains four optional fields

```python
class ErrorResponse(BaseModel):
    status_code: int
    body_excerpt: str           # first ~500 chars
    headers: dict[str, str]     # selected useful ones (mcp-session-id, retry-after, etc.)


class InferenceError(BaseModel):
    type: str                   # 'rate_limit_error', 'invalid_request_error', etc.
    message: str
    body_excerpt: str           # first ~500 chars


class ToolCallDetail(BaseModel):
    name: str                   # already in tool_calls[]; included for self-contained rendering
    input_excerpt: str          # 2KB cap
    output_excerpt: str         # 2KB cap
    truncated: bool             # True if either was capped
    error: Optional[str] = None # if the tool call itself failed


class RunResult(BaseModel):
    # ... existing fields unchanged ...
    error_response: Optional[ErrorResponse] = None
    inference_error: Optional[InferenceError] = None
    runner_traceback: Optional[str] = None
    tool_call_details: list[ToolCallDetail] = []
```

All four are Optional / default-empty, so legacy reports still pass `model_validate`.

### Storage impact

A typical 5-run benchmark with 3 tool calls per run, all successful, all under 2KB: roughly 30 KB of tool-call detail added to `payload_json`. A 3-scenario × 3-model × 5-run sweep: roughly 270 KB total tool-call data, vs the ~50 KB the rest of the payload weighs. Heroku Postgres essential-0 + JSONB compression handles this comfortably.

### No migration

Existing rows in `reports.payload_json` stay valid. The new fields default to None / [] when absent. No DB schema change; no `_normalize_to_cube`-style shim needed (Pydantic Optional handles it).

### Privacy note

Tool inputs and outputs may contain customer data when scenarios query Salesforce. The captured I/O is persisted in `payload_json` for as long as the report exists. Acceptable for an internal benchmarking tool; documented here so a future operator deciding what to retain has the context.

---

## C1: Per-Turn Token Diff

### Heuristic engine

New `src/token_compare/diff_explainer.py`:

```python
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel


class TurnDiff(BaseModel):
    turn: int
    input_delta: int            # mcp - native, signed
    output_delta: int
    total_delta: int
    reason: Optional[Literal[
        "tool_list_reload",
        "larger_tool_response",
        "extra_turn",
        "model_verbosity",
    ]] = None
    hint: Optional[str] = None  # human-readable one-liner


def explain_turn(
    native_turn: dict | None,
    mcp_turn: dict | None,
    *,
    prior_native_turn: dict | None = None,
    prior_mcp_turn: dict | None = None,
) -> TurnDiff: ...


def diff_traces(
    native_traces: list[dict],
    mcp_traces: list[dict],
) -> list[TurnDiff]: ...
```

`diff_traces` zips by index, calling `explain_turn` for each position. Asymmetric trace lengths produce `TurnDiff` rows where one side is None.

### Reason classifier

Heuristics in priority order (first match wins):

| reason | trigger |
|---|---|
| `extra_turn` | One side has a turn at index N where the other path finished at N-1. |
| `tool_list_reload` | MCP `cache_creation_input_tokens` exceeds native `cache_creation_input_tokens` by >500 AND turn index ≤ 1. |
| `larger_tool_response` | MCP `input_tokens` > native `input_tokens` by >300 AND prior turn made a tool call (detected via `tool_calls` count change). |
| `model_verbosity` | MCP `output_tokens` > native `output_tokens` by >2× AND \|input_delta\| < 200. |
| `(none)` | No detectable pattern — just show the raw delta. |

`hint` is a one-line string templated from the reason and the actual numbers, e.g. `"MCP reloaded the tool list — +1,247 cache-creation tokens"`.

### Tests

`tests/test_diff_explainer.py`:
- One test per reason category with a hand-crafted minimal trace pair.
- One test for asymmetric trace lengths.
- One test for the "no detectable reason, just show delta" path.
- One test for the noise threshold: small deltas (<100 tokens) emit no reason.

### Endpoint

Existing `GET /api/scenarios/{id}/trace` is enriched. Today it returns:

```json
{
  "scenario_id": "...",
  "explanation": "...",
  "native_traces": [...],
  "mcp_traces": [...]
}
```

C1 adds:
```json
{
  "turn_diffs": [{"turn": 0, "input_delta": 0, "output_delta": 0, "total_delta": 0, "reason": null, "hint": null}, ...]
}
```

### SPA rendering

The trace card's existing table has 3 columns: Turn | Native | MCP. C1 adds a 4th: Δ.

- Empty when `|total_delta| < 100` (noise threshold).
- `+1,247` (red) when MCP > native; `−824` (green) when MCP < native.
- A small reason chip below the number when `reason != None`. Hover shows the `hint`.
- Reason chip styled subtly (small, low-contrast) so the table still reads as data, not editorial.

Responsive: on narrow screens (<720px) the Δ wraps under the MCP cell rather than fighting for column width.

### Edge cases

- One path failed all runs: trace endpoint returns no traces for that path; the trace card already handles this. Δ column shows "—".
- Symmetric runs (small deltas everywhere): Δ column blank, no reason chips. Don't punish quiet runs.
- Reason heuristic wrong: `(?)` tooltip explains "best guess based on token shape, may be inaccurate." Better to be modest than confidently wrong.

---

## C2: Failed-Run Replay

### Capture points

#### MCP gateway HTTP errors → `error_response`

In `src/token_compare/mcp_proxy.py`, the existing JSON-RPC client raises on non-2xx responses. Before raising, capture `(status_code, body[:500], selected_headers)` and attach to a thread-local or pass through the call stack to the runner.

The runner's outer try/except for the MCP path lifts that captured data onto `RunResult.error_response` when the run fails.

Selected headers: `mcp-session-id`, `retry-after`, `x-request-id`, `content-type`. Avoid logging auth headers verbatim (we already don't, but be explicit in the capture allowlist).

#### Anthropic API errors → `inference_error`

In `src/token_compare/messages_runner.py`, `client.messages.create(...)` calls raise `anthropic.APIError` subclasses on failures. The existing `except` block already handles this — we extend it to capture `(error.type, error.message, error.body_excerpt[:500])` onto `RunResult.inference_error`.

The `inference_error` field is set when the Anthropic SDK raises (any path). The `error_response` field is set when the MCP gateway returns a non-2xx. A run that fails will have at most one of them populated (the runner returns at the first failure point, so we never reach a second capture). The schema keeps both `Optional` rather than enforcing exclusivity — simpler than a discriminated union, and a future failure mode that touches both isn't blocked.

#### Uncaught exceptions → `runner_traceback`

The runner's outermost try/except for `run_once` catches everything else. We add `runner_traceback = traceback.format_exc()` and continue (a failed run is still a run; we record it and move on).

#### Tool calls → `tool_call_details`

Inside `messages_runner.py`'s tool-use loop, after each tool execution we already have `tool_use.name`, `tool_use.input`, and the tool's response content. We append a `ToolCallDetail` to a list:

```python
truncated = False
input_str = json.dumps(tool_use.input, indent=2)
if len(input_str) > 2000:
    input_str = input_str[:2000] + f"…[truncated, {len(input_str) - 2000} more chars]"
    truncated = True
output_str = str(tool_response.content)[:2000]
if len(str(tool_response.content)) > 2000:
    output_str += f"…[truncated, {len(str(tool_response.content)) - 2000} more chars]"
    truncated = True

tool_call_details.append(ToolCallDetail(
    name=tool_use.name,
    input_excerpt=input_str,
    output_excerpt=output_str,
    truncated=truncated,
    error=tool_response.error if hasattr(tool_response, "error") else None,
))
```

Truncation is at character boundaries (Python string slicing). Acceptable for forensic reading; JSON inputs that hit the cap may end mid-key, which is fine — the tail is rarely the meaningful part.

When the tool's content contains >50% non-printable chars, replace with `"[binary content, NN bytes]"` to avoid garbage in the rendered table.

### Tests

`tests/test_runner_capture.py` (new):
- Mock the Anthropic client to raise an `APIError` with a fake body. Verify `RunResult.inference_error` is populated.
- Mock the MCP proxy to return an HTTP 401. Verify `RunResult.error_response` is populated and the run is marked failed.
- Mock a tool execution that returns a 5KB string. Verify truncation, `truncated=True`, and the marker.
- Mock a runner that throws `RuntimeError`. Verify `runner_traceback` is populated.
- Verify `tool_call_details` is empty for a run with no tool calls (model didn't act).

### SPA rendering

The per-run breakdown table on scenario-view (already exists) gets a chevron column. Click toggles one expansion row spanning the table width:

```
  [ ▾ ] 1  Native  succeeded   4   2,500   180   60%   12s   $0.011
        └─ Tool calls (3)
           1. Bash: sf data query 'SELECT Name FROM Account LIMIT 5'
              → 5 rows. Account names: Acme Corp, Globex, ...
           2. Bash: sf data query 'SELECT Id, Amount FROM Opportunity ...'
              → 23 rows. Total: $1,247,000 ...
           3. Bash: sf data query 'SELECT TOP 3 ...'
              → 3 rows. ...
```

For failed runs, additional blocks appear above tool calls:

```
  [ ▾ ] 2  MCP  mcp_init_failed  0  —  —  —  —  —
        └─ MCP gateway error
           HTTP 401 Unauthorized
           Body: {"error":"Invalid token","error_description":"Session expired..."}
           Headers: mcp-session-id=abc123, retry-after=30
        └─ Tool calls (0)
```

For `inference_error`:
```
        └─ Inference error
           rate_limit_error: Number of request tokens has exceeded...
           Body: {"type":"error","error":{"type":"rate_limit_error",...}}
```

For `runner_traceback`:
```
        └─ Runner traceback
           Traceback (most recent call last):
             File "messages_runner.py", line 142, in run_once
               ...
```

**Default expansion behavior:** failed rows (any non-`succeeded` outcome) start expanded; successful rows start collapsed. State is per-row, not persisted.

**Tool I/O rendering:** `<pre class="tool-io">` with monospace font, scroll-on-overflow horizontally. No syntax highlighting (plaintext is enough for short SOQL / REST URLs).

### Edge cases

- *Tool input/output is binary*: rendered as `[binary content, NN bytes]`.
- *MCP session timeout mid-run*: captured as `error_response` with the 401 body. Surfaces immediately.
- *Crash before any tool call*: `tool_call_details=[]`, `runner_traceback` populated. Expansion shows just the traceback.
- *Old reports without these fields*: expansion shows "(no detail captured for this run)". Row is still expandable but empty.

---

## Cross-cutting

### PDF export

The expansion state is captured client-side. For PDF:
- Successful runs render collapsed (one row each, keeping the per-run card compact for paper economy).
- Failed runs render expanded (so a printed report includes the failure context).
- Tool I/O is rendered as the same `<pre>` block, but with `page-break-inside: avoid` so a long tool output isn't split across pages mid-line.

### Tests

| Test file | What it covers |
|---|---|
| `tests/test_diff_explainer.py` | All 4 reason categories + asymmetric + noise threshold (NEW). |
| `tests/test_runner_capture.py` | RunResult enrichment paths (NEW). |
| `tests/test_models.py` | Round-trip test for new RunResult Optional fields. |
| `tests/test_api.py` | `/api/scenarios/{id}/trace` returns `turn_diffs`. |

### Backward compatibility

- All new RunResult fields are Optional / default-empty.
- Existing reports load unchanged via Pydantic.
- `_normalize_to_cube` shim is unaffected (it only normalizes Tier B fields).
- The trace endpoint adds `turn_diffs` to its response — clients that ignore it (e.g. a stale cached SPA) keep working.

---

## Open questions for implementation

None blocking. Two implementation-time decisions to surface:

1. The "binary content" detection threshold (50% non-printable chars). May need to be tightened or relaxed once we see real-world tool outputs. Easy to tune later.
2. The 2KB truncation per side. If we see truncation routinely cutting useful info (e.g., long REST responses), we can revisit. The cap is a single constant in `messages_runner.py`.

## Out of scope (future tiers)

- "Re-run this failure" button.
- Public read-only report URLs (Tier D).
- Two-report comparison view (Tier D).
- Tool I/O search / filter.
- Syntax highlighting in tool I/O blocks.
