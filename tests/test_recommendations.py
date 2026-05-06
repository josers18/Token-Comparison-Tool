from token_compare.models import (
    BenchmarkResult, PathName, RunResult, ScenarioResult,
)
from token_compare.recommendations import generate


def _run(path: PathName, cost: float, inp: int = 500) -> RunResult:
    return RunResult(path=path, input_tokens=inp, output_tokens=50,
                     cache_read_input_tokens=0, total_cost_usd=cost,
                     num_turns=2, duration_ms=100, tool_calls=[],
                     succeeded=True, error=None)


def _scenario_result(sid: str, native_cost: float, mcp_cost: float) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=sid,
        native_runs=[_run(PathName.NATIVE, native_cost) for _ in range(3)],
        mcp_runs=[_run(PathName.MCP, mcp_cost) for _ in range(3)],
    )


def _benchmark(scenarios):
    return BenchmarkResult(
        started_at="2026-05-04T14:00:00+00:00",
        finished_at="2026-05-04T14:15:00+00:00",
        operator="me", model="m", org_name="org",
        tool_commit="abc", runs_per_path=3, scenarios=scenarios,
    )


def test_generate_mentions_overall_multiplier():
    b = _benchmark([
        _scenario_result("s01", 0.01, 0.04),
        _scenario_result("s02", 0.02, 0.06),
    ])
    lines = generate(b)
    joined = " ".join(lines).lower()
    assert "cheaper" in joined or "more expensive" in joined
    assert "%" in joined or "×" in joined


def test_generate_when_mcp_wins_some_scenarios():
    b = _benchmark([
        _scenario_result("s01", 0.01, 0.04),
        _scenario_result("s02", 0.05, 0.02),
    ])
    lines = generate(b)
    assert any("mcp" in line.lower() for line in lines)


def test_generate_handles_empty_benchmark():
    b = _benchmark([])
    lines = generate(b)
    assert lines == [] or all(isinstance(line, str) for line in lines)


def test_generate_with_difficulty_map():
    b = _benchmark([
        _scenario_result("s01", 0.01, 0.06),
        _scenario_result("s02", 0.05, 0.06),
    ])
    lines = generate(b, scenarios_by_id={"s01": "simple", "s02": "complex"})
    joined = " ".join(lines).lower()
    assert "simple" in joined or "complex" in joined or "schema" in joined
