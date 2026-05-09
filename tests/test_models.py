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


def test_scenario_result_runs_by_model_round_trip():
    from token_compare.models import (
        ScenarioResult, ModelRunBucket, RunResult, PathName,
    )
    r_native = RunResult(
        path=PathName.NATIVE, input_tokens=10, output_tokens=5,
        cache_read_input_tokens=0, total_cost_usd=0.01, num_turns=1,
        duration_ms=100, tool_calls=[], succeeded=True, raw_json={},
    )
    r_mcp = RunResult(
        path=PathName.MCP, input_tokens=20, output_tokens=10,
        cache_read_input_tokens=0, total_cost_usd=0.02, num_turns=1,
        duration_ms=100, tool_calls=[], succeeded=True, raw_json={},
    )
    sr = ScenarioResult(
        scenario_id="s1",
        native_runs=[r_native], mcp_runs=[r_mcp],
        runs_by_model={"sonnet": ModelRunBucket(
            native_runs=[r_native], mcp_runs=[r_mcp]
        )},
    )
    dumped = sr.model_dump()
    assert dumped["runs_by_model"]["sonnet"]["native_runs"][0]["input_tokens"] == 10
    rebuilt = ScenarioResult.model_validate(dumped)
    assert rebuilt.runs_by_model["sonnet"].native_runs[0].input_tokens == 10


def test_benchmark_result_models_field():
    from token_compare.models import BenchmarkResult
    payload = {
        "started_at": "x", "finished_at": "y", "operator": "me",
        "model": "claude-4-5-sonnet",
        "models": ["claude-4-5-sonnet"],
        "org_name": "o", "tool_commit": "abc",
        "runs_per_path": 1, "scenarios": [],
    }
    b = BenchmarkResult.model_validate(payload)
    assert b.models == ["claude-4-5-sonnet"]
    assert b.model == "claude-4-5-sonnet"


def test_normalize_to_cube_legacy_payload():
    from token_compare.models import _normalize_to_cube
    legacy = {
        "started_at": "x", "finished_at": "y", "operator": "me",
        "model": "claude-4-5-sonnet", "org_name": "o",
        "tool_commit": "abc", "runs_per_path": 1,
        "scenarios": [{
            "scenario_id": "s1",
            "native_runs": [{"path": "native", "input_tokens": 1,
                              "output_tokens": 1, "cache_read_input_tokens": 0,
                              "total_cost_usd": 0.01, "num_turns": 1,
                              "duration_ms": 1, "tool_calls": [],
                              "succeeded": True, "raw_json": {}}],
            "mcp_runs": [],
        }],
    }
    out = _normalize_to_cube(legacy)
    assert out["models"] == ["claude-4-5-sonnet"]
    sr = out["scenarios"][0]
    assert "claude-4-5-sonnet" in sr["runs_by_model"]
    assert len(sr["runs_by_model"]["claude-4-5-sonnet"]["native_runs"]) == 1
    assert sr["runs_by_model"]["claude-4-5-sonnet"]["mcp_runs"] == []


def test_normalize_to_cube_idempotent():
    """If payload already has models + runs_by_model, leave it alone."""
    from token_compare.models import _normalize_to_cube
    cube = {
        "model": "sonnet", "models": ["sonnet", "opus"],
        "scenarios": [{
            "scenario_id": "s1",
            "native_runs": [], "mcp_runs": [],
            "runs_by_model": {
                "sonnet": {"native_runs": [], "mcp_runs": []},
                "opus": {"native_runs": [], "mcp_runs": []},
            },
        }],
    }
    out = _normalize_to_cube(cube)
    assert out["models"] == ["sonnet", "opus"]
    assert set(out["scenarios"][0]["runs_by_model"].keys()) == {"sonnet", "opus"}


def test_default_model_picks_sonnet():
    from token_compare.models import _default_model
    assert _default_model(["claude-4-5-sonnet"]) == "claude-4-5-sonnet"
    assert _default_model(["claude-3-opus", "claude-4-5-sonnet"]) == "claude-4-5-sonnet"
    assert _default_model(["claude-3-opus", "claude-3-haiku"]) == "claude-3-opus"
    assert _default_model(["CLAUDE-5-SONNET"]) == "CLAUDE-5-SONNET"  # case-insensitive
    assert _default_model([]) == ""  # degenerate input → empty string, no exception


def test_run_result_enrichment_fields_round_trip():
    from token_compare.models import (
        RunResult, PathName, ErrorResponse, InferenceError, ToolCallDetail,
    )
    r = RunResult(
        path=PathName.NATIVE,
        input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
        total_cost_usd=0.01, num_turns=1, duration_ms=100,
        tool_calls=["Bash"], succeeded=False, raw_json={},
        error_response=ErrorResponse(
            status_code=401,
            body_excerpt='{"error":"Invalid token"}',
            headers={"mcp-session-id": "abc123"},
        ),
        tool_call_details=[ToolCallDetail(
            name="Bash", input_excerpt="sf data query 'SELECT ...'",
            output_excerpt="5 rows", truncated=False,
        )],
    )
    dumped = r.model_dump()
    assert dumped["error_response"]["status_code"] == 401
    assert dumped["tool_call_details"][0]["name"] == "Bash"
    rebuilt = RunResult.model_validate(dumped)
    assert rebuilt.error_response.status_code == 401
    assert rebuilt.tool_call_details[0].input_excerpt.startswith("sf data")


def test_run_result_legacy_payload_no_enrichment_fields():
    """Legacy payloads without enrichment fields validate cleanly."""
    from token_compare.models import RunResult, PathName
    legacy = {
        "path": "native", "input_tokens": 10, "output_tokens": 5,
        "cache_read_input_tokens": 0, "total_cost_usd": 0.01,
        "num_turns": 1, "duration_ms": 100, "tool_calls": [],
        "succeeded": True, "raw_json": {},
    }
    r = RunResult.model_validate(legacy)
    assert r.error_response is None
    assert r.inference_error is None
    assert r.runner_traceback is None
    assert r.tool_call_details == []
