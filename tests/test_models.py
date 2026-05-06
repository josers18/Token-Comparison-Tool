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
