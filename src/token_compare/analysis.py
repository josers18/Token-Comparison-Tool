"""Turn-by-turn trace and explanation generator for benchmark comparisons.

Operates on the raw_json captured per run. Produces a list of TurnEntries
that the UI can render side-by-side, plus a Pattern-based paragraph
explaining why two runs differ in cost.

Pure functions — no LLM calls, no I/O. Deterministic.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from token_compare.models import RunResult


class TurnEntry(BaseModel):
    """One assistant message turn from claude -p output."""
    turn_index: int
    input_new: int           # usage.input_tokens — new tokens this turn
    input_cache_read: int    # usage.cache_read_input_tokens
    input_cache_create: int  # usage.cache_creation_input_tokens
    output_tokens: int       # usage.output_tokens
    text_snippet: Optional[str] = None    # first text content block, truncated
    tool_calls: list[str] = []            # names of tool_use blocks this turn
    tool_inputs: list[str] = []           # truncated JSON of each tool's input
    tool_results: list[str] = []          # truncated text of each tool_result that
                                           # follows this turn (joined to next user msg)
    tool_errors: list[bool] = []          # parallel to tool_results: was this an error?


class RunTrace(BaseModel):
    """Compact representation of a single claude -p run for UI display."""
    init_tools: list[str] = []           # from system.init.tools
    init_mcp_servers: list[str] = []     # from system.init.mcp_servers[*].name
    turns: list[TurnEntry] = []
    final_text: Optional[str] = None     # what Claude returned at the end
    succeeded: bool = False
    error: Optional[str] = None
    total_cost_usd: float = 0.0
    duration_ms: int = 0


def extract_trace(run: RunResult) -> RunTrace:
    """Pull a structured per-turn trace out of a RunResult.raw_json."""
    raw = run.raw_json
    trace = RunTrace(
        succeeded=run.succeeded,
        error=run.error,
        total_cost_usd=run.total_cost_usd,
        duration_ms=run.duration_ms,
    )
    if not isinstance(raw, list):
        return trace

    # init event
    for ev in raw:
        if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "init":
            trace.init_tools = list(ev.get("tools") or [])
            trace.init_mcp_servers = [
                m.get("name", "") for m in (ev.get("mcp_servers") or [])
            ]
            break

    # Walk events; pair assistant-with-tool-use to subsequent user-with-tool-result
    turn_idx = 0
    pending_turns: list[TurnEntry] = []  # turns awaiting tool_result data

    import json as _json
    for ev in raw:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            turn_idx += 1
            msg = ev.get("message") or {}
            usage = msg.get("usage") or {}
            content = msg.get("content") or []
            entry = TurnEntry(
                turn_index=turn_idx,
                input_new=int(usage.get("input_tokens", 0)),
                input_cache_read=int(usage.get("cache_read_input_tokens", 0)),
                input_cache_create=int(usage.get("cache_creation_input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
            )
            text_pieces: list[str] = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text":
                    text_pieces.append(c.get("text", ""))
                elif c.get("type") == "tool_use":
                    entry.tool_calls.append(c.get("name", ""))
                    try:
                        inp_str = _json.dumps(c.get("input") or {}, ensure_ascii=False)
                    except (TypeError, ValueError):
                        inp_str = "{...}"
                    entry.tool_inputs.append(inp_str[:400])
            if text_pieces:
                entry.text_snippet = (" ".join(text_pieces).strip())[:300]
            trace.turns.append(entry)
            if entry.tool_calls:
                pending_turns.append(entry)
        elif ev.get("type") == "user":
            msg = ev.get("message") or {}
            content = msg.get("content") or []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    if not pending_turns:
                        continue
                    target = pending_turns[0]
                    pending_turns.pop(0)
                    t = c.get("content")
                    if isinstance(t, list) and t and isinstance(t[0], dict):
                        t = t[0].get("text", "")
                    target.tool_results.append(str(t)[:400])
                    target.tool_errors.append(bool(c.get("is_error")))
        elif ev.get("type") == "result":
            trace.final_text = (ev.get("result") or "")[:600]

    return trace


# ----------------------------------------------------------------------------
# Pattern detection + explanation
# ----------------------------------------------------------------------------

class ScenarioComparison(BaseModel):
    """Aggregates one path's runs into headline numbers for explanation."""
    label: str                    # "Native" or "MCP"
    median_cost: float
    median_input_total: int       # input_tokens + cache_read + cache_create across all turns
    median_turns: int
    succeeded_count: int
    total_runs: int
    init_tool_count: int          # from first run's trace
    init_mcp_server_count: int
    median_tool_calls: int
    distinct_tool_calls: list[str]
    avg_tool_errors: float        # avg error count across runs
    cache_create_first_turn: int  # this is the schema-tax indicator


def explain_comparison(
    native: ScenarioComparison,
    mcp: ScenarioComparison,
) -> str:
    """Generate a plain-English paragraph explaining why these two paths
    produced different costs on this scenario.

    Detectors run in priority order. The first matching detector's text is
    appended; multiple detectors can fire (concatenated). At least one
    sentence is always produced.
    """
    parts: list[str] = []

    # Detector: failure asymmetry
    if native.succeeded_count > mcp.succeeded_count:
        parts.append(
            f"Native succeeded {native.succeeded_count}/{native.total_runs} runs, "
            f"MCP only {mcp.succeeded_count}/{mcp.total_runs}."
        )
    elif mcp.succeeded_count > native.succeeded_count:
        parts.append(
            f"MCP succeeded {mcp.succeeded_count}/{mcp.total_runs} runs, "
            f"Native only {native.succeeded_count}/{native.total_runs}."
        )

    # Detector: schema tax (MCP only)
    if mcp.cache_create_first_turn > native.cache_create_first_turn + 500:
        delta = mcp.cache_create_first_turn - native.cache_create_first_turn
        unused = max(0, mcp.init_tool_count - mcp.median_tool_calls)
        if unused > 0:
            parts.append(
                f"MCP loaded {mcp.init_tool_count} tools at startup (~{delta:,} extra "
                f"cache-create tokens vs Native) but Claude only invoked "
                f"{mcp.median_tool_calls} tool call(s) per run — {unused} tool "
                f"schemas were carried as context tax without ever being used."
            )
        else:
            parts.append(
                f"MCP loaded {mcp.init_tool_count} tool schemas at startup, "
                f"adding ~{delta:,} cache-create tokens vs Native's lighter "
                f"{native.init_tool_count}-tool init."
            )

    # Detector: schema-discovery loop on Native
    native_bash = native.distinct_tool_calls.count("Bash") if "Bash" in native.distinct_tool_calls else 0
    if native.median_turns > mcp.median_turns + 3 and native.median_tool_calls > mcp.median_tool_calls + 2:
        parts.append(
            f"Native used {native.median_turns} turns vs MCP's {mcp.median_turns} — "
            f"the extra turns reflect Native discovering schema and field names via "
            f"shell calls (sf sobject describe, grep), while MCP's purpose-built "
            f"tools returned the same information in fewer steps."
        )

    # Detector: tool errors / recovery
    if mcp.avg_tool_errors > 2 and mcp.avg_tool_errors > native.avg_tool_errors + 1:
        parts.append(
            f"MCP encountered ~{mcp.avg_tool_errors:.1f} tool errors per run "
            f"(vs Native's ~{native.avg_tool_errors:.1f}); each retry replays "
            f"the full tool-schema cache, compounding cost."
        )
    elif native.avg_tool_errors > 2 and native.avg_tool_errors > mcp.avg_tool_errors + 1:
        parts.append(
            f"Native hit ~{native.avg_tool_errors:.1f} tool errors per run "
            f"(typically wrong field/object guesses) before recovering."
        )

    # Detector: input volume gap that's not explained by schema
    in_delta = mcp.median_input_total - native.median_input_total
    if in_delta > 5000 and mcp.median_turns <= native.median_turns:
        parts.append(
            f"MCP processed ~{in_delta:,} more total input tokens than Native "
            f"despite the same or fewer turns — its tool result payloads tend "
            f"to be larger (verbose JSON with metadata)."
        )

    # Detector: identical tool count
    if (native.median_turns == mcp.median_turns
        and native.median_tool_calls == mcp.median_tool_calls
        and abs(native.median_cost - mcp.median_cost) > 0.005):
        cheaper, dearer = (("Native", "MCP") if native.median_cost < mcp.median_cost
                          else ("MCP", "Native"))
        parts.append(
            f"Both paths completed in {native.median_turns} turns with the same "
            f"number of tool calls. The cost gap is structural — {dearer}'s "
            f"prompt overhead per turn is higher."
        )

    # Headline summary always last
    if native.median_cost > 0 and mcp.median_cost > 0:
        if native.median_cost < mcp.median_cost:
            mult = mcp.median_cost / native.median_cost
            parts.append(
                f"Net: Native ${native.median_cost:.3f} vs MCP ${mcp.median_cost:.3f} "
                f"— Native {mult:.1f}× cheaper on this scenario."
            )
        else:
            mult = native.median_cost / mcp.median_cost
            parts.append(
                f"Net: MCP ${mcp.median_cost:.3f} vs Native ${native.median_cost:.3f} "
                f"— MCP {mult:.1f}× cheaper on this scenario."
            )

    if not parts:
        return "Both paths completed similarly with no distinguishing pattern detected."
    return " ".join(parts)


def build_comparison(
    label: str,
    runs: list[RunResult],
    traces: list[RunTrace],
) -> ScenarioComparison:
    """Aggregate runs + traces into a ScenarioComparison summary."""
    from statistics import median
    successful = [r for r in runs if r.succeeded]
    succ_traces = [t for t, r in zip(traces, runs) if r.succeeded]
    if successful:
        median_cost = median(r.total_cost_usd for r in successful)
        median_turns = int(median(r.num_turns for r in successful))
        median_tool_calls = int(median(len(r.tool_calls) for r in successful))
        # input total = new + cache_read + cache_create across all turns
        per_run_totals = [
            sum(t.input_new + t.input_cache_read + t.input_cache_create for t in tr.turns)
            for tr in succ_traces if tr.turns
        ]
        median_input_total = int(median(per_run_totals)) if per_run_totals else 0
    else:
        median_cost = 0.0
        median_turns = 0
        median_tool_calls = 0
        median_input_total = 0

    distinct_tools: list[str] = []
    for r in runs:
        for name in r.tool_calls or []:
            if name not in distinct_tools:
                distinct_tools.append(name)

    if traces and traces[0].turns:
        cache_create_first = traces[0].turns[0].input_cache_create
    else:
        cache_create_first = 0

    avg_errors = (
        sum(sum(t.tool_errors.count(True) for t in tr.turns) for tr in traces)
        / max(len(traces), 1)
    )

    return ScenarioComparison(
        label=label,
        median_cost=median_cost,
        median_input_total=median_input_total,
        median_turns=median_turns,
        succeeded_count=len(successful),
        total_runs=len(runs),
        init_tool_count=traces[0].init_tools.__len__() if traces else 0,
        init_mcp_server_count=traces[0].init_mcp_servers.__len__() if traces else 0,
        median_tool_calls=median_tool_calls,
        distinct_tool_calls=distinct_tools,
        avg_tool_errors=avg_errors,
        cache_create_first_turn=cache_create_first,
    )


# ----------------------------------------------------------------------------
# Summary analysis for the Summary page
# ----------------------------------------------------------------------------

class ScenarioWinner(BaseModel):
    scenario_id: str
    title: Optional[str] = None
    winner: str                  # "native", "mcp", "tied", "inconclusive"
    multiplier: Optional[float] = None
    native_cost: float
    mcp_cost: float
    native_succeeded: int
    mcp_succeeded: int


class SummaryAnalysis(BaseModel):
    scenarios: list[ScenarioWinner] = []
    total_native_cost: float = 0.0
    total_mcp_cost: float = 0.0
    avg_multiplier: Optional[float] = None
    headline: str = ""
    caveats: list[str] = []
    framework_native_wins: list[str] = []      # bullets describing scenarios Native won
    framework_mcp_wins: list[str] = []         # bullets describing scenarios MCP won
    framework_native_pattern: Optional[str] = None  # one-line generalization
    framework_mcp_pattern: Optional[str] = None     # one-line generalization
    runs_per_path: int = 1


def build_summary_analysis(
    result_data: dict,
    scenarios_meta: dict[str, dict],  # scenario_id -> {"title", "category", "difficulty"}
) -> SummaryAnalysis:
    """Synthesize a deck-ready summary from a BenchmarkResult dict."""
    from token_compare.models import BenchmarkResult
    result = BenchmarkResult.model_validate(result_data)

    summary = SummaryAnalysis(runs_per_path=result.runs_per_path)
    winners: list[ScenarioWinner] = []
    native_wins: list[ScenarioWinner] = []
    mcp_wins: list[ScenarioWinner] = []

    for sr in result.scenarios:
        meta = scenarios_meta.get(sr.scenario_id) or {}
        nat_succ = sr.succeeded_native
        mcp_succ = sr.succeeded_mcp
        nat_cost = sr.native_median_cost  # success-only, intentional for comparison
        mcp_cost = sr.mcp_median_cost
        mult = sr.cheaper_multiplier  # None if either side fully failed

        winner = "inconclusive"
        if mult is None:
            if nat_succ > 0 and mcp_succ == 0:
                winner = "native"
            elif mcp_succ > 0 and nat_succ == 0:
                winner = "mcp"
        elif mult > 1.05:
            winner = "native"
        elif mult < 0.95:
            winner = "mcp"
        else:
            winner = "tied"

        sw = ScenarioWinner(
            scenario_id=sr.scenario_id,
            title=meta.get("title"),
            winner=winner,
            multiplier=mult,
            native_cost=nat_cost,
            mcp_cost=mcp_cost,
            native_succeeded=nat_succ,
            mcp_succeeded=mcp_succ,
        )
        winners.append(sw)
        if winner == "native":
            native_wins.append(sw)
        elif winner == "mcp":
            mcp_wins.append(sw)

    summary.scenarios = winners
    summary.total_native_cost = result.total_native_cost
    summary.total_mcp_cost = result.total_mcp_cost
    summary.avg_multiplier = result.average_multiplier

    # Headline
    if summary.avg_multiplier is None:
        summary.headline = (
            f"Across {len(winners)} scenarios: insufficient data "
            f"(too many failures on at least one path)."
        )
    elif summary.avg_multiplier > 1.05:
        pct = int(round((1 - 1 / summary.avg_multiplier) * 100))
        summary.headline = (
            f"Across {len(winners)} scenarios, native integrations cost ~{pct}% less "
            f"per task than the Salesforce-hosted MCP equivalent (average "
            f"{summary.avg_multiplier:.1f}× cheaper)."
        )
    elif summary.avg_multiplier < 0.95:
        pct = int(round((1 / summary.avg_multiplier - 1) * 100))
        summary.headline = (
            f"Across {len(winners)} scenarios, MCP cost ~{pct}% less per task "
            f"than native integrations (average {1/summary.avg_multiplier:.1f}× "
            f"cheaper)."
        )
    else:
        summary.headline = (
            f"Across {len(winners)} scenarios, native and MCP totals are within "
            f"~5% of each other — effectively tied."
        )

    # Caveats
    n = result.runs_per_path
    if n == 1:
        summary.caveats.append(
            "Single-run measurements; rerun with 3+ runs per path for stable medians."
        )
    summary.caveats.append(
        "Compares token cost only — does not measure latency, success quality, "
        "or operational reliability."
    )
    summary.caveats.append(
        f"Results are specific to this org's data shape and Claude {result.model}; "
        "generalization to other orgs / other models requires repeating the benchmark there."
    )

    # Framework bullets per side
    summary.framework_native_wins = [
        _winner_bullet(w, scenarios_meta) for w in native_wins
    ]
    summary.framework_mcp_wins = [
        _winner_bullet(w, scenarios_meta) for w in mcp_wins
    ]
    summary.framework_native_pattern = _generalize(native_wins, scenarios_meta)
    summary.framework_mcp_pattern = _generalize(mcp_wins, scenarios_meta)

    return summary


def _winner_bullet(w: ScenarioWinner, scenarios_meta: dict) -> str:
    title = w.title or w.scenario_id
    if w.multiplier is None:
        return f"{title} — only this path succeeded ({w.native_succeeded if w.winner=='native' else w.mcp_succeeded}/1)."
    if w.winner == "native":
        return f"{title} — Native ${w.native_cost:.3f} vs MCP ${w.mcp_cost:.3f} ({w.multiplier:.1f}× cheaper)."
    return f"{title} — MCP ${w.mcp_cost:.3f} vs Native ${w.native_cost:.3f} ({(1/w.multiplier):.1f}× cheaper)."


def _generalize(wins: list[ScenarioWinner], scenarios_meta: dict) -> Optional[str]:
    """Detect a common pattern in the scenarios won and produce a single
    explanatory sentence. Conservative — returns None if the wins don't share
    a clear theme."""
    if not wins:
        return None

    # Collect categories + difficulties
    cats: list[str] = []
    diffs: list[str] = []
    for w in wins:
        meta = scenarios_meta.get(w.scenario_id) or {}
        if meta.get("category"): cats.append(meta["category"])
        if meta.get("difficulty"): diffs.append(meta["difficulty"])

    # Check for pure category dominance
    if cats and all(c == cats[0] for c in cats):
        cat = cats[0]
        if cat == "core-crm":
            return ("Wins concentrate on Sales/Service Cloud (SOQL) workloads — "
                    "well-known objects, no schema discovery needed.")
        if cat in ("data-360", "data-cloud"):
            return ("Wins concentrate on Data Cloud workloads — multi-DMO "
                    "traversal and schema-aware aggregation benefit from the "
                    "tooling here.")

    # Check for difficulty mix
    if "complex" in diffs and "simple" not in diffs:
        return "Wins concentrate on complex multi-step scenarios."
    if "simple" in diffs and "complex" not in diffs:
        return "Wins concentrate on simple single-query scenarios."

    if len(wins) >= 3:
        return f"Wins span {len(wins)} scenarios with no single dominant pattern."
    return None
