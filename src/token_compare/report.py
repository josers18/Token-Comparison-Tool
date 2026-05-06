from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from token_compare.models import BenchmarkResult, Scenario, ScenarioResult
from token_compare.recommendations import generate as generate_recs


def default_report_path(directory: Path, started_at: str) -> Path:
    dt = datetime.fromisoformat(started_at)
    stamp = dt.strftime("%Y-%m-%d-%H%M")
    return Path(directory) / f"benchmark-{stamp}.md"


def write_markdown(
    result: BenchmarkResult,
    path: Path,
    *,
    scenarios: list[Scenario] | None = None,
) -> None:
    scenarios = scenarios or []
    by_id = {s.id: s for s in scenarios}
    difficulty_by_id = {s.id: s.difficulty for s in scenarios}

    out: list[str] = []
    out.append("# Token Comparison Benchmark")
    out.append("")
    out.append(f"**Date:** {result.started_at} → {result.finished_at}  ")
    out.append(f"**Operator:** {result.operator}  ")
    out.append(f"**Model:** {result.model}  ·  **Runs per path:** {result.runs_per_path}  ")
    out.append(f"**Salesforce org:** {result.org_name}  ·  **Tool commit:** {result.tool_commit}")
    out.append("")
    out.append("---")
    out.append("")

    # Executive Summary
    out.append("## Executive Summary")
    out.append("")
    mult = result.average_multiplier
    if mult is not None:
        if mult > 1:
            out.append(
                f"Across **{len(result.scenarios)} scenarios**, native was "
                f"**{mult:.1f}× cheaper on average** than the Salesforce-hosted MCP equivalent."
            )
        else:
            out.append(
                f"Across **{len(result.scenarios)} scenarios**, MCP was "
                f"**{(1/mult):.1f}× cheaper on average** than the native equivalent."
            )
        out.append("")
    out.append("|                   | Native   | MCP      |")
    out.append("|-------------------|----------|----------|")
    out.append(f"| Total cost        | ${result.total_native_cost:.2f}    | ${result.total_mcp_cost:.2f}    |")
    out.append(f"| Total input tok   | {result.total_native_total_input_tokens:,}    | {result.total_mcp_total_input_tokens:,}    |")
    succ_native = sum(s.succeeded_native for s in result.scenarios)
    succ_mcp = sum(s.succeeded_mcp for s in result.scenarios)
    total = result.runs_per_path * len(result.scenarios)
    out.append(f"| Success rate      | {succ_native}/{total}     | {succ_mcp}/{total}     |")
    out.append("")

    # Recommendations
    out.append("### Recommendations")
    out.append("")
    for line in generate_recs(result, scenarios_by_id=difficulty_by_id):
        out.append(f"- {line}")
    out.append("")
    out.append("---")
    out.append("")

    # Per-Scenario
    out.append("## Per-Scenario Comparisons")
    out.append("")
    for sr in result.scenarios:
        sc = by_id.get(sr.scenario_id)
        title = sc.title if sc else sr.scenario_id
        cat_diff = f"{sc.category} · {sc.difficulty}" if sc else ""
        out.append(f"### {sr.scenario_id} — {title}  ({cat_diff})")
        out.append("")
        if sc:
            out.append(f"**Prompt:** {sc.prompt.strip()}")
            out.append("")
        out.append("|              | Native (median) | MCP (median) |")
        out.append("|--------------|-----------------|---------------|")
        # All-runs medians so failed runs' real spend shows up in the table.
        # The Succeeded row below conveys pass/fail context.
        out.append(f"| Input tok    | {sr.native_median_total_input_tokens_all:,}           | {sr.mcp_median_total_input_tokens_all:,}          |")
        out.append(f"| Output tok   | {sr.native_median_output_tokens_all:,}           | {sr.mcp_median_output_tokens_all:,}          |")
        out.append(f"| Cost         | ${sr.native_median_cost_all:.3f}         | ${sr.mcp_median_cost_all:.3f}        |")
        out.append(f"| Turns        | {sr.native_median_turns_all}              | {sr.mcp_median_turns_all}             |")
        out.append(f"| Succeeded    | {sr.succeeded_native}/{len(sr.native_runs)}             | {sr.succeeded_mcp}/{len(sr.mcp_runs)}            |")
        out.append("")
        out.append(f"**Tool calls — Native:** {_summarize_tool_calls(sr.native_runs)}  ")
        out.append(f"**Tool calls — MCP:** {_summarize_tool_calls(sr.mcp_runs)}")
        out.append("")
        out.append(_outcome_sentence(sr))
        out.append("")

    out.append("---")
    out.append("")

    # Methodology
    out.append("## Methodology")
    out.append("")
    out.append("- **Measurement source:** `claude -p --output-format json`; every number is extracted directly from the per-run `usage` block.")
    out.append("- **Held constant:** same prompt, model, org, machine, `--max-turns` cap. Path order randomized per scenario.")
    out.append("- **One axis of variance:** tool provider only (native `sf` CLI vs Salesforce-hosted MCP server).")
    out.append("- **Stats:** medians reported in tables; full per-run data in Appendix.")
    out.append("- **Out of scope:** Salesforce API consumption, semantic accuracy beyond `must_contain`, MCP server startup time.")
    out.append("")
    out.append("---")
    out.append("")

    # Appendix
    out.append("## Appendix — Raw Data")
    out.append("")
    for sr in result.scenarios:
        out.append(f"### {sr.scenario_id}")
        out.append("")
        for i, r in enumerate(sr.native_runs, start=1):
            out.extend(_render_run_details("Native", i, r))
        for i, r in enumerate(sr.mcp_runs, start=1):
            out.extend(_render_run_details("MCP", i, r))

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(out))


def _summarize_tool_calls(runs) -> str:
    if not runs:
        return "(none)"
    for r in runs:
        if r.succeeded and r.tool_calls:
            return ", ".join(r.tool_calls)
    return "(no tool calls)"


def _render_run_details(label: str, i: int, r) -> list[str]:
    """Render one run as a collapsible appendix block including error/status."""
    status_icon = "✓" if r.succeeded else "✗"
    summary = (
        f"{status_icon} {label} run {i} — ${r.total_cost_usd:.3f}, "
        f"{r.num_turns} turns, {r.duration_ms} ms"
    )
    lines: list[str] = []
    lines.append(f"<details><summary>{summary}</summary>")
    lines.append("")
    lines.append(f"- **succeeded:** {r.succeeded}")
    if r.error:
        lines.append(f"- **error:** `{r.error}`")
    lines.append(f"- **path:** {r.path.value}")
    lines.append(f"- **tool_calls:** {', '.join(r.tool_calls) if r.tool_calls else '(none)'}")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(r.raw_json or {}, indent=2))
    lines.append("```")
    lines.append("</details>")
    lines.append("")
    return lines


def _outcome_sentence(sr: ScenarioResult) -> str:
    m = sr.cheaper_multiplier
    if m is None:
        return "**Outcome:** inconclusive — one or both paths had no successful runs."
    if m > 1.05:
        return f"**Outcome:** Native {m:.1f}× cheaper on this scenario."
    if m < 0.95:
        return f"**Outcome:** MCP {(1/m):.1f}× cheaper on this scenario."
    return "**Outcome:** effectively tied on token cost."
