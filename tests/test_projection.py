from token_compare.models import (
    BenchmarkResult, ScenarioResult, ModelRunBucket, RunResult, PathName,
)
from token_compare.projection import project_at_scale


def _bench(native_per_run: float, mcp_per_run: float, model: str = "sonnet"
           ) -> BenchmarkResult:
    n = RunResult(path=PathName.NATIVE, input_tokens=10, output_tokens=5,
                  cache_read_input_tokens=0, total_cost_usd=native_per_run,
                  num_turns=1, duration_ms=100, tool_calls=[],
                  succeeded=True, raw_json={})
    m = RunResult(path=PathName.MCP, input_tokens=10, output_tokens=5,
                  cache_read_input_tokens=0, total_cost_usd=mcp_per_run,
                  num_turns=1, duration_ms=100, tool_calls=[],
                  succeeded=True, raw_json={})
    return BenchmarkResult(
        started_at="x", finished_at="y", operator="me", model=model,
        models=[model], org_name="o", tool_commit="abc", runs_per_path=1,
        scenarios=[ScenarioResult(
            scenario_id="s1", native_runs=[n], mcp_runs=[m],
            runs_by_model={model: ModelRunBucket(native_runs=[n], mcp_runs=[m])},
        )],
    )


def test_projection_monthly_volume():
    bench = _bench(0.01, 0.02)
    p = project_at_scale(bench, runs_per_scenario_per_period=10000,
                         period="month", growth_rate_pct=0.0)
    assert p.native_total == 100.0
    assert p.mcp_total == 200.0
    assert p.delta == 100.0


def test_projection_period_scaling():
    bench = _bench(0.01, 0.02)
    daily = project_at_scale(bench, runs_per_scenario_per_period=100,
                             period="day", growth_rate_pct=0.0)
    annual = project_at_scale(bench, runs_per_scenario_per_period=100,
                              period="year", growth_rate_pct=0.0)
    # Period only labels the volume; headline math is volume * per-run cost
    # regardless of period. Both should be 1.0.
    assert daily.native_total == 1.0
    assert annual.native_total == 1.0


def test_projection_growth_zero_is_linear_curve():
    bench = _bench(0.01, 0.02)
    p = project_at_scale(bench, runs_per_scenario_per_period=10000,
                         period="month", growth_rate_pct=0.0)
    # Linear: month n cumulative = native_total * n
    assert p.curve[0].native_cum == 100.0
    assert p.curve[5].native_cum == 600.0
    assert p.curve[11].native_cum == 1200.0


def test_projection_growth_rate_compounds():
    bench = _bench(0.01, 0.02)
    p = project_at_scale(bench, runs_per_scenario_per_period=10000,
                         period="month", growth_rate_pct=10.0)
    # Geometric sum at g=0.10: cum_n = 100 * ((1.1^n - 1) / 0.1)
    # n=1: 100. n=2: 100 + 110 = 210. n=12: ~2138.
    assert abs(p.curve[0].native_cum - 100.0) < 0.01
    assert abs(p.curve[1].native_cum - 210.0) < 0.01


def test_projection_breakeven_thresholds():
    bench = _bench(0.01, 0.02)  # delta = 0.01 per run
    p = project_at_scale(bench, runs_per_scenario_per_period=10000,
                         period="month", growth_rate_pct=0.0,
                         breakeven_thresholds_usd=[1000, 10000, 100000])
    by_threshold = {b.threshold_usd: b for b in p.breakevens}
    # $1K threshold: at delta $0.01/run, need 100K runs.
    assert by_threshold[1000].runs_to_breakeven == 100000
    # $10K threshold: 1M runs.
    assert by_threshold[10000].runs_to_breakeven == 1000000


def test_projection_breakeven_near_equal_renders_label():
    bench = _bench(0.01, 0.0102)  # within 5%
    p = project_at_scale(bench, runs_per_scenario_per_period=10000,
                         period="month", growth_rate_pct=0.0)
    # All breakevens collapse to the "near break-even" frame.
    for b in p.breakevens:
        assert b.frame == "near_break_even"


def test_projection_native_more_expensive_inverts_frame():
    bench = _bench(0.05, 0.01)  # native loses
    p = project_at_scale(bench, runs_per_scenario_per_period=10000,
                         period="month", growth_rate_pct=0.0)
    assert p.delta < 0
    for b in p.breakevens:
        assert b.frame == "native_more_expensive"


def test_projection_picks_default_model_when_omitted():
    n = RunResult(path=PathName.NATIVE, input_tokens=10, output_tokens=5,
                  cache_read_input_tokens=0, total_cost_usd=0.05,
                  num_turns=1, duration_ms=100, tool_calls=[],
                  succeeded=True, raw_json={})
    m = RunResult(path=PathName.MCP, input_tokens=10, output_tokens=5,
                  cache_read_input_tokens=0, total_cost_usd=0.10,
                  num_turns=1, duration_ms=100, tool_calls=[],
                  succeeded=True, raw_json={})
    bench = BenchmarkResult(
        started_at="x", finished_at="y", operator="me", model="opus",
        models=["opus", "claude-4-5-sonnet"],
        org_name="o", tool_commit="abc", runs_per_path=1,
        scenarios=[ScenarioResult(
            scenario_id="s1", native_runs=[n], mcp_runs=[m],
            runs_by_model={
                "opus": ModelRunBucket(native_runs=[n], mcp_runs=[m]),
                "claude-4-5-sonnet": ModelRunBucket(native_runs=[n], mcp_runs=[m]),
            },
        )],
    )
    p = project_at_scale(bench, runs_per_scenario_per_period=1, period="month",
                         growth_rate_pct=0.0)
    assert p.model_used == "claude-4-5-sonnet"  # _default_model picks sonnet
