from __future__ import annotations

from token_compare.models import BenchmarkResult


def generate(
    result: BenchmarkResult,
    scenarios_by_id: dict[str, str] | None = None,
) -> list[str]:
    """
    scenarios_by_id: optional map of scenario_id -> difficulty
                     ("simple" | "medium" | "complex"). When omitted,
                     difficulty-specific lines are skipped.
    """
    if not result.scenarios:
        return []

    lines: list[str] = []
    mult = result.average_multiplier
    if mult is not None:
        if mult > 1.05:
            pct = int(round((1 - 1 / mult) * 100))
            lines.append(
                f"Across {len(result.scenarios)} scenarios, native integrations "
                f"cost ~{pct}% less per task than the Salesforce-hosted MCP "
                f"equivalent (average {mult:.1f}× cheaper)."
            )
        elif mult < 0.95:
            pct = int(round((1 / mult - 1) * 100))
            lines.append(
                f"Across {len(result.scenarios)} scenarios, MCP averaged "
                f"~{pct}% cheaper than the native equivalent."
            )
        else:
            lines.append(
                "Across these scenarios, native and MCP paths were "
                "effectively tied on token cost."
            )

    if scenarios_by_id:
        simple = [s for s in result.scenarios
                  if scenarios_by_id.get(s.scenario_id) == "simple"]
        complex_ = [s for s in result.scenarios
                    if scenarios_by_id.get(s.scenario_id) == "complex"]

        simple_mults = [s.cheaper_multiplier for s in simple if s.cheaper_multiplier]
        if simple_mults and sum(simple_mults) / len(simple_mults) > 1.5:
            lines.append(
                "The gap is widest on simple queries, where MCP tool-schema "
                "overhead dominates the prompt."
            )

        complex_mults = [s.cheaper_multiplier for s in complex_ if s.cheaper_multiplier]
        if complex_mults and min(complex_mults) < 1.2:
            lines.append(
                "MCP closes the gap on complex, multi-step scenarios — richer "
                "tool schemas can reduce the number of turns required."
            )

    mcp_wins = [s for s in result.scenarios
                if s.cheaper_multiplier is not None and s.cheaper_multiplier < 1.0]
    if mcp_wins:
        ids = ", ".join(s.scenario_id for s in mcp_wins)
        lines.append(f"MCP was cheaper on: {ids}. Worth investigating why.")

    lines.append(
        "Recommendation: prefer native for read-heavy, well-scoped workflows; "
        "reconsider MCP where schema richness demonstrably reduces turn count "
        "enough to offset input-token overhead."
    )
    return lines
