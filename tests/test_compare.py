from token_compare.compare import compare_reports, _metric_delta
from token_compare.models import (
    BenchmarkResult, ScenarioResult, ModelRunBucket, RunResult, PathName,
)


def _bench(scenarios_data: list[dict], model: str = "sonnet") -> BenchmarkResult:
    scenarios = []
    for s in scenarios_data:
        n_runs = []
        for i in range(s["native_total"]):
            n_runs.append(RunResult(
                path=PathName.NATIVE, input_tokens=10, output_tokens=5,
                cache_read_input_tokens=0, total_cost_usd=s["native_cost"],
                num_turns=1, duration_ms=s.get("p95", 100), tool_calls=[],
                succeeded=i < s["native_succ"], raw_json={},
            ))
        m_runs = []
        for i in range(s["mcp_total"]):
            m_runs.append(RunResult(
                path=PathName.MCP, input_tokens=10, output_tokens=5,
                cache_read_input_tokens=0, total_cost_usd=s["mcp_cost"],
                num_turns=1, duration_ms=s.get("p95", 100), tool_calls=[],
                succeeded=i < s["mcp_succ"], raw_json={},
            ))
        scenarios.append(ScenarioResult(
            scenario_id=s["id"], native_runs=n_runs, mcp_runs=m_runs,
            runs_by_model={model: ModelRunBucket(native_runs=n_runs, mcp_runs=m_runs)},
        ))
    runs_per_path = max((s["native_total"] for s in scenarios_data), default=1)
    return BenchmarkResult(
        started_at="x", finished_at="y", operator="me", model=model,
        models=[model], org_name="o", tool_commit="abc",
        runs_per_path=runs_per_path, scenarios=scenarios,
    )


def test_identical_reports_no_regressions():
    s = {"id": "s1", "native_cost": 0.01, "mcp_cost": 0.02,
         "native_succ": 3, "native_total": 3, "mcp_succ": 3, "mcp_total": 3}
    a = _bench([s]); b = _bench([s])
    cmp = compare_reports(a, b)
    assert not cmp.incompatible
    assert all(not sc.regressed for sc in cmp.scenarios)
    assert cmp.scenarios[0].native_cost.delta_pct == 0.0


def test_cost_regression_flagged_and_sorted_first():
    s_reg = {"id": "s_reg", "native_cost": 0.01, "mcp_cost": 0.02,
             "native_succ": 3, "native_total": 3, "mcp_succ": 3, "mcp_total": 3}
    s_reg_b = {**s_reg, "native_cost": 0.012}
    s_quiet = {"id": "s_quiet", "native_cost": 0.01, "mcp_cost": 0.02,
               "native_succ": 3, "native_total": 3, "mcp_succ": 3, "mcp_total": 3}
    a = _bench([s_reg, s_quiet]); b = _bench([s_reg_b, s_quiet])
    cmp = compare_reports(a, b)
    assert cmp.scenarios[0].scenario_id == "s_reg"
    assert cmp.scenarios[0].regressed is True
    assert cmp.scenarios[1].regressed is False


def test_success_regression():
    s_a = {"id": "s1", "native_cost": 0.01, "mcp_cost": 0.02,
           "native_succ": 5, "native_total": 5, "mcp_succ": 5, "mcp_total": 5}
    s_b = {**s_a, "native_succ": 4}
    a = _bench([s_a]); b = _bench([s_b])
    cmp = compare_reports(a, b)
    assert cmp.scenarios[0].regressed is True


def test_added_scenario_in_b():
    a_data = [{"id": "s1", "native_cost": 0.01, "mcp_cost": 0.02,
               "native_succ": 1, "native_total": 1, "mcp_succ": 1, "mcp_total": 1}]
    b_data = a_data + [{"id": "s2", "native_cost": 0.03, "mcp_cost": 0.04,
                         "native_succ": 1, "native_total": 1, "mcp_succ": 1, "mcp_total": 1}]
    cmp = compare_reports(_bench(a_data), _bench(b_data))
    added = next((s for s in cmp.scenarios if s.scenario_id == "s2"), None)
    assert added is not None
    assert added.presence == "added_in_b"
    assert "s2" in cmp.scope["added"]


def test_removed_scenario_in_b():
    a_data = [{"id": "s1", "native_cost": 0.01, "mcp_cost": 0.02,
               "native_succ": 1, "native_total": 1, "mcp_succ": 1, "mcp_total": 1},
              {"id": "s2", "native_cost": 0.03, "mcp_cost": 0.04,
               "native_succ": 1, "native_total": 1, "mcp_succ": 1, "mcp_total": 1}]
    b_data = a_data[:1]
    cmp = compare_reports(_bench(a_data), _bench(b_data))
    removed = next((s for s in cmp.scenarios if s.scenario_id == "s2"), None)
    assert removed is not None
    assert removed.presence == "removed_in_b"
    assert "s2" in cmp.scope["removed"]


def test_no_common_model_marks_incompatible():
    a = _bench([], model="claude-3-opus")
    b = _bench([], model="claude-4-5-sonnet")
    cmp = compare_reports(a, b)
    assert cmp.incompatible is True
    assert cmp.scenarios == []


def test_common_sonnet_chosen():
    s = {"id": "s1", "native_cost": 0.01, "mcp_cost": 0.02,
         "native_succ": 1, "native_total": 1, "mcp_succ": 1, "mcp_total": 1}
    a = _bench([s], model="claude-4-5-sonnet")
    b = _bench([s], model="claude-4-5-sonnet")
    b.models = ["claude-3-opus", "claude-4-5-sonnet"]
    cmp = compare_reports(a, b)
    assert cmp.model_used == "claude-4-5-sonnet"
    assert cmp.incompatible is False


def test_delta_pct_none_when_a_is_zero():
    md = _metric_delta(0.0, 5.0)
    assert md.delta_pct is None
    assert md.delta_abs == 5.0
