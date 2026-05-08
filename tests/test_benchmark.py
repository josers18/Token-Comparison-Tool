from pathlib import Path
from unittest.mock import patch

from token_compare.benchmark import run_benchmark, BenchmarkOptions, ProgressEvent
from token_compare.models import PathName, RunResult, Scenario, SuccessCriteria


def _mk_scenario(sid: str) -> Scenario:
    return Scenario(id=sid, title=sid, category="c", difficulty="simple",
                    prompt="x", expected_operations=[],
                    success_criteria=SuccessCriteria(must_contain=[]), notes="")


def _fake_run(cost: float) -> RunResult:
    return RunResult(path=PathName.NATIVE, input_tokens=100, output_tokens=10,
                     cache_read_input_tokens=0, total_cost_usd=cost, num_turns=1,
                     duration_ms=100, tool_calls=[], succeeded=True, error=None)


def test_run_benchmark_shape(tmp_path):
    scenarios = [_mk_scenario("s01"), _mk_scenario("s02")]
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")

    def fake_run_once(scenario, path, **kwargs):
        r = _fake_run(0.01 if path == PathName.NATIVE else 0.03)
        r.path = path
        return r

    opts = BenchmarkOptions(model="m", max_turns=5, timeout_s=5, runs_per_path=2,
                            mcp_template_path=mcp_cfg, operator="me", org_name="org",
                            sf_token={"access_token": "T", "instance_url": "https://x"})

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc123"):
        result = run_benchmark(scenarios, opts)

    assert len(result.scenarios) == 2
    for s in result.scenarios:
        assert len(s.native_runs) == 2
        assert len(s.mcp_runs) == 2
    assert result.runs_per_path == 2
    assert result.operator == "me"
    assert result.tool_commit == "abc123"


def test_run_benchmark_emits_progress(tmp_path):
    scenarios = [_mk_scenario("s01")]
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    events: list[ProgressEvent] = []

    def fake_run_once(scenario, path, **kwargs):
        return _fake_run(0.01)

    opts = BenchmarkOptions(model="m", max_turns=5, timeout_s=5, runs_per_path=1,
                            mcp_template_path=mcp_cfg, operator="me", org_name="org",
                            sf_token={"access_token": "T", "instance_url": "https://x"})

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        run_benchmark(scenarios, opts, on_progress=events.append)

    kinds = [e.kind for e in events]
    assert "benchmark_start" in kinds
    assert "scenario_start" in kinds
    assert "run_complete" in kinds
    assert "benchmark_complete" in kinds


def test_run_benchmark_randomizes_path_order(tmp_path):
    scenarios = [_mk_scenario("s01")]
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    observed_order: list[PathName] = []

    def fake_run_once(scenario, path, **kwargs):
        observed_order.append(path)
        return _fake_run(0.01)

    opts = BenchmarkOptions(model="m", max_turns=5, timeout_s=5, runs_per_path=3,
                            mcp_template_path=mcp_cfg, operator="me", org_name="org",
                            sf_token={"access_token": "T", "instance_url": "https://x"})

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"), \
         patch("token_compare.benchmark.random.random", return_value=0.1):
        run_benchmark(scenarios, opts)

    assert observed_order == [PathName.MCP, PathName.NATIVE] * 3


# Removed test_run_benchmark_resolves_mcp_config_when_creds_set:
# resolve_template behavior is retired — tokens now flow from api.py → benchmark → messages_runner without temp files.


def test_benchmark_options_models_field():
    from token_compare.benchmark import BenchmarkOptions
    from pathlib import Path
    opts = BenchmarkOptions(
        model="sonnet", models=["sonnet", "opus"],
        max_turns=10, timeout_s=30, runs_per_path=1,
        mcp_template_path=Path("/tmp/x"), operator="me",
        org_name="o", sf_token={},
    )
    assert opts.models == ["sonnet", "opus"]


def test_progress_event_carries_model():
    from token_compare.benchmark import ProgressEvent
    e = ProgressEvent(kind="run_start", model="sonnet")
    assert e.model == "sonnet"
