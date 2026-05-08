import pytest
from pydantic import ValidationError
from token_compare.models import (
    Scenario, SuccessCriteria, RunResult, PathName,
    ScenarioResult, BenchmarkResult,
)


def test_scenario_loads_from_dict():
    s = Scenario.model_validate({
        "id": "s01",
        "title": "Basic",
        "category": "core-crm",
        "difficulty": "simple",
        "prompt": "List 5 accounts.",
        "expected_operations": ["sf data query"],
        "success_criteria": {"must_contain": ["account"]},
        "notes": "",
    })
    assert s.id == "s01"
    assert s.difficulty == "simple"
    assert s.success_criteria.must_contain == ["account"]


def test_scenario_rejects_bad_difficulty():
    with pytest.raises(ValidationError):
        Scenario.model_validate({
            "id": "s01",
            "title": "Basic",
            "category": "core-crm",
            "difficulty": "trivial",
            "prompt": "x",
            "expected_operations": [],
            "success_criteria": {"must_contain": []},
        })


def test_run_result_defaults():
    r = RunResult(
        path=PathName.NATIVE,
        input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
        total_cost_usd=0.01, num_turns=2, duration_ms=500,
        tool_calls=["sf data query"], succeeded=True, error=None,
    )
    assert r.succeeded is True
    assert r.path == PathName.NATIVE


def test_scenario_result_median_cost():
    runs = [
        RunResult(path=PathName.NATIVE, input_tokens=100, output_tokens=10,
                  cache_read_input_tokens=0, total_cost_usd=0.01, num_turns=1,
                  duration_ms=100, tool_calls=[], succeeded=True, error=None),
        RunResult(path=PathName.NATIVE, input_tokens=200, output_tokens=20,
                  cache_read_input_tokens=0, total_cost_usd=0.02, num_turns=2,
                  duration_ms=200, tool_calls=[], succeeded=True, error=None),
        RunResult(path=PathName.NATIVE, input_tokens=300, output_tokens=30,
                  cache_read_input_tokens=0, total_cost_usd=0.03, num_turns=3,
                  duration_ms=300, tool_calls=[], succeeded=True, error=None),
    ]
    sr = ScenarioResult(scenario_id="s01", native_runs=runs, mcp_runs=runs)
    assert sr.native_median_cost == 0.02
    assert sr.native_median_input_tokens == 200
    assert sr.succeeded_native == 3


def test_scenario_result_median_total_input_tokens():
    # input_tokens=100, cache_read=200, cache_creation=300 → total=600 per run
    runs = [
        RunResult(path=PathName.NATIVE, input_tokens=100, output_tokens=10,
                  cache_read_input_tokens=200, cache_creation_input_tokens=300,
                  total_cost_usd=0.01, num_turns=1, duration_ms=100,
                  tool_calls=[], succeeded=True, error=None)
        for _ in range(3)
    ]
    sr = ScenarioResult(scenario_id="s", native_runs=runs, mcp_runs=runs)
    assert sr.native_median_total_input_tokens == 600
    assert sr.mcp_median_total_input_tokens == 600


# ─── Tier A: variance + cache + duration + outcomes ────────────────────


def _r(cost: float, in_tok: int = 100, cache_read: int = 0,
       cache_create: int = 0, duration_ms: int = 100,
       succeeded: bool = True, error: str | None = None) -> RunResult:
    return RunResult(
        path=PathName.NATIVE,
        input_tokens=in_tok, output_tokens=20,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_create,
        total_cost_usd=cost, num_turns=1, duration_ms=duration_ms,
        tool_calls=[], succeeded=succeeded, error=error,
    )


def test_p95_cost_picks_high_value_with_small_n():
    # 5 runs: $0.01, $0.02, $0.03, $0.04, $0.10 → p95 nearest-rank picks
    # ceil(0.95 * 5) = 5 → index 4 → 0.10
    runs = [_r(c) for c in [0.01, 0.02, 0.03, 0.04, 0.10]]
    sr = ScenarioResult(scenario_id="s", native_runs=runs, mcp_runs=[])
    assert sr.native_p95_cost == 0.10


def test_p95_handles_empty():
    sr = ScenarioResult(scenario_id="s", native_runs=[], mcp_runs=[])
    assert sr.native_p95_cost == 0.0
    assert sr.mcp_p95_cost == 0.0


def test_stddev_zero_for_identical_runs():
    runs = [_r(0.05) for _ in range(4)]
    sr = ScenarioResult(scenario_id="s", native_runs=runs, mcp_runs=[])
    assert sr.native_stddev_cost == 0.0


def test_stddev_nonzero_with_spread():
    # Population stddev of [0.01, 0.05, 0.09] is √((0.04² + 0 + 0.04²)/3)
    # ≈ 0.0327. Just verify it's > 0 and reasonable.
    runs = [_r(c) for c in [0.01, 0.05, 0.09]]
    sr = ScenarioResult(scenario_id="s", native_runs=runs, mcp_runs=[])
    assert 0.025 < sr.native_stddev_cost < 0.04


def test_stddev_n_lt_2_returns_zero():
    # pstdev would error on empty / be undefined for N=1
    sr_empty = ScenarioResult(scenario_id="s", native_runs=[], mcp_runs=[])
    assert sr_empty.native_stddev_cost == 0.0
    sr_one = ScenarioResult(scenario_id="s", native_runs=[_r(0.01)], mcp_runs=[])
    assert sr_one.native_stddev_cost == 0.0


def test_cache_hit_ratio():
    # input=100, cache_read=300, cache_creation=100 per run → total_in=500
    # cache_read share = 300/500 = 0.6
    runs = [_r(0.01, in_tok=100, cache_read=300, cache_create=100) for _ in range(3)]
    sr = ScenarioResult(scenario_id="s", native_runs=runs, mcp_runs=[])
    assert sr.native_cache_hit_ratio == pytest.approx(0.6)


def test_cache_hit_ratio_zero_when_no_cache():
    runs = [_r(0.01, in_tok=100, cache_read=0, cache_create=0) for _ in range(2)]
    sr = ScenarioResult(scenario_id="s", native_runs=runs, mcp_runs=[])
    assert sr.native_cache_hit_ratio == 0.0


def test_cache_hit_ratio_zero_for_empty_runs():
    sr = ScenarioResult(scenario_id="s", native_runs=[], mcp_runs=[])
    assert sr.native_cache_hit_ratio == 0.0
    assert sr.mcp_cache_hit_ratio == 0.0


def test_duration_medians_p95():
    runs = [_r(0.01, duration_ms=d) for d in [100, 200, 300, 400, 1000]]
    sr = ScenarioResult(scenario_id="s", native_runs=runs, mcp_runs=[])
    assert sr.native_median_duration_ms == 300
    assert sr.native_p95_duration_ms == 1000


def test_outcomes_per_path():
    runs = [
        _r(0.01, succeeded=True),
        _r(0.01, succeeded=True),
        _r(0.0, succeeded=False, error="terminal_reason=max_turns"),
        _r(0.0, succeeded=False, error="HTTP 401: bad scope"),
    ]
    sr = ScenarioResult(scenario_id="s", native_runs=runs, mcp_runs=[])
    out = sr.native_outcomes
    assert out["succeeded"] == 2
    assert out["max_turns"] == 1
    assert out["tool_auth_error"] == 1
    # Empty mcp_runs produces all-zero outcomes dict.
    assert all(v == 0 for v in sr.mcp_outcomes.values())
