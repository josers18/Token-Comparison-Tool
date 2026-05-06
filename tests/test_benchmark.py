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
                            mcp_config_path=mcp_cfg, operator="me", org_name="org")

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
                            mcp_config_path=mcp_cfg, operator="me", org_name="org")

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
                            mcp_config_path=mcp_cfg, operator="me", org_name="org")

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"), \
         patch("token_compare.benchmark.random.random", return_value=0.1):
        run_benchmark(scenarios, opts)

    assert observed_order == [PathName.MCP, PathName.NATIVE] * 3


def test_run_benchmark_resolves_mcp_config_when_creds_set(tmp_path, monkeypatch):
    """When SF creds are set, effective mcp_config_path should be the resolved temp file."""
    monkeypatch.setenv("SF_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SF_LOGIN_URL", "https://x.my.salesforce.com")

    scenarios = [_mk_scenario("s01")]
    mcp_cfg = tmp_path / "sf-mcp.json"
    mcp_cfg.write_text('{"mcpServers":{"x":{"url":"h","headers":{"Authorization":"Bearer ${SF_ACCESS_TOKEN}"}}}}')

    captured_paths = []

    def fake_run_once(scenario, path, *, mcp_config_path, **kwargs):
        captured_paths.append((path, str(mcp_config_path)))
        return _fake_run(0.01)

    def fake_fetch(creds, **kw):
        from token_compare.sf_auth import AccessToken
        return AccessToken(access_token="LIVE_TOK", instance_url="https://x", scope="api")

    opts = BenchmarkOptions(model="m", max_turns=5, timeout_s=5, runs_per_path=1,
                            mcp_config_path=mcp_cfg, operator="me", org_name="org")

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark.fetch_access_token", side_effect=fake_fetch), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        run_benchmark(scenarios, opts)

    # MCP run should have received a temp path (NOT the original mcp_cfg)
    mcp_paths = [p for kind, p in captured_paths if kind.value == "mcp"]
    assert len(mcp_paths) == 1
    assert mcp_paths[0] != str(mcp_cfg)
    # The resolved file was deleted at the end
    assert not Path(mcp_paths[0]).exists()
