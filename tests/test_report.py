from pathlib import Path

from token_compare.models import (
    BenchmarkResult, PathName, RunResult, Scenario, ScenarioResult,
    SuccessCriteria,
)
from token_compare.report import write_markdown, default_report_path


def _run(path: PathName, cost: float) -> RunResult:
    return RunResult(path=path, input_tokens=1000, output_tokens=100,
                     cache_read_input_tokens=0, total_cost_usd=cost,
                     num_turns=2, duration_ms=1000,
                     tool_calls=["Bash"] if path == PathName.NATIVE else ["mcp__x"],
                     succeeded=True, error=None,
                     raw_json={"usage": {"input_tokens": 1000}})


def _benchmark() -> BenchmarkResult:
    return BenchmarkResult(
        started_at="2026-05-04T14:00:00+00:00",
        finished_at="2026-05-04T14:15:00+00:00",
        operator="user@example.com",
        model="claude-opus-4-7",
        org_name="MyOrg",
        tool_commit="abc1234",
        runs_per_path=3,
        scenarios=[
            ScenarioResult(
                scenario_id="s01_soql_top_accounts",
                native_runs=[_run(PathName.NATIVE, 0.01) for _ in range(3)],
                mcp_runs=[_run(PathName.MCP, 0.04) for _ in range(3)],
            ),
        ],
    )


def test_write_markdown_creates_file(tmp_path):
    b = _benchmark()
    scenarios = [Scenario(id="s01_soql_top_accounts", title="Top accounts",
                          category="core-crm", difficulty="simple",
                          prompt="List accounts.", expected_operations=[],
                          success_criteria=SuccessCriteria(must_contain=[]),
                          notes="")]
    out = tmp_path / "report.md"
    write_markdown(b, out, scenarios=scenarios)
    text = out.read_text()

    assert "# Token Comparison Benchmark" in text
    assert "## Executive Summary" in text
    assert "## Per-Scenario Comparisons" in text
    assert "## Methodology" in text
    assert "## Appendix — Raw Data" in text
    assert "claude-opus-4-7" in text
    assert "abc1234" in text
    assert "s01_soql_top_accounts" in text
    assert "Top accounts" in text
    assert "Native" in text and "MCP" in text

    # Appendix now shows per-run status line
    assert "**succeeded:** True" in text or "**succeeded:** False" in text
    assert "**path:** native" in text or "**path:** mcp" in text
    assert "**tool_calls:**" in text


def test_default_report_path_format(tmp_path):
    p = default_report_path(tmp_path, started_at="2026-05-04T14:32:00+00:00")
    assert p.parent == tmp_path
    assert p.name.startswith("benchmark-2026-05-04-1432")
    assert p.suffix == ".md"
