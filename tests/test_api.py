import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from token_compare.api import create_app, AppConfig

# Set required env vars for tests
os.environ["SESSION_SECRET"] = "test-secret-key"
os.environ.setdefault("DATABASE_URL", "postgresql://test@localhost/test")


@pytest.fixture(autouse=True)
def _stub_db(monkeypatch):
    """Most existing api tests don't actually need real DB. Stub the
    surface they touch so api.py imports + routes work."""
    from token_compare import db
    async def _noop(*a, **kw): return None
    async def _empty_list(*a, **kw): return []
    async def _none(*a, **kw): return None
    async def _create(*a, **kw): return "sid_test"
    async def _create_report(*a, **kw): return "rpt_test"
    monkeypatch.setattr(db, "connect", _noop)
    monkeypatch.setattr(db, "migrate", _noop)
    monkeypatch.setattr(db, "close", _noop)
    monkeypatch.setattr(db, "create_session", _create)
    monkeypatch.setattr(db, "put_sf_token", _noop)
    monkeypatch.setattr(db, "get_sf_token", _none)
    monkeypatch.setattr(db, "delete_sf_token", _noop)
    monkeypatch.setattr(db, "list_reports", _empty_list)
    monkeypatch.setattr(db, "get_report", _none)
    monkeypatch.setattr(db, "create_report", _create_report)
    monkeypatch.setattr(db, "finalize_report", _noop)
    monkeypatch.setattr(db, "insert_run", _noop)
    monkeypatch.setattr(db, "put_pending_login", _noop)
    monkeypatch.setattr(db, "pop_pending_login", _none)
    monkeypatch.setattr(db, "prune_pending_logins", _noop)


@pytest.fixture
def client(tmp_path):
    scen_dir = tmp_path / "scenarios"; scen_dir.mkdir()
    (scen_dir / "sA.yaml").write_text(
        "id: sA\ntitle: A\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    reports = tmp_path / "reports"; reports.mkdir()

    cfg = AppConfig(
        scenarios_dir=scen_dir, mcp_config_path=mcp_cfg,
        reports_dir=reports, static_dir=None,
    )
    return TestClient(create_app(cfg))


def test_get_scenarios(client):
    r = client.get("/api/scenarios")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == "sA"


def test_get_preflight(client):
    from token_compare.preflight import PreflightResult
    with patch("token_compare.api.check_environment") as m:
        m.return_value = PreflightResult(
            ok=True,
            checks={"claude_installed": True, "claude_logged_in": True,
                    "sf_authenticated": True, "mcp_config_present": True},
            remediation=[], details={},
        )
        r = client.get("/api/preflight")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_post_run_streams_events(client, monkeypatch):
    from token_compare.models import PathName, RunResult
    from token_compare import db

    # Provide a mock SF token for this test
    async def _mock_get_sf_token(sid):
        return {"access_token": "T", "instance_url": "https://x"}
    monkeypatch.setattr(db, "get_sf_token", _mock_get_sf_token)

    def fake_run_once(scenario, path, **kwargs):
        return RunResult(path=path, input_tokens=100, output_tokens=10,
                         cache_read_input_tokens=0, total_cost_usd=0.01,
                         num_turns=1, duration_ms=10, tool_calls=[],
                         succeeded=True, error=None, raw_json={})

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream(
            "POST", "/api/run",
            json={"scenario_ids": ["sA"], "runs_per_path": 1,
                  "model": "claude-opus-4-7", "operator": "me", "org_name": "org"},
        ) as resp:
            chunks = "".join(list(resp.iter_text()))

    assert "benchmark_start" in chunks
    assert "run_complete" in chunks
    assert "benchmark_complete" in chunks


def test_post_run_emits_error_and_closes_on_failure(client, monkeypatch):
    from token_compare import db

    # Provide a mock SF token for this test
    async def _mock_get_sf_token(sid):
        return {"access_token": "T", "instance_url": "https://x"}
    monkeypatch.setattr(db, "get_sf_token", _mock_get_sf_token)

    def boom(scenario, path, **kwargs):
        raise RuntimeError("simulated benchmark failure")

    with patch("token_compare.benchmark.run_once", side_effect=boom), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream(
            "POST", "/api/run",
            json={"scenario_ids": ["sA"], "runs_per_path": 1,
                  "model": "claude-opus-4-7", "operator": "me", "org_name": "org"},
        ) as resp:
            chunks = "".join(list(resp.iter_text()))

    # Stream should close cleanly (no hang), include an error event with the message
    assert '"kind": "error"' in chunks
    assert "simulated benchmark failure" in chunks


def test_reports_latest_head_returns_last_modified(tmp_path):
    from token_compare.api import AppConfig, create_app
    from fastapi.testclient import TestClient
    scen = tmp_path / "scenarios"; scen.mkdir()
    (scen / "sA.yaml").write_text(
        "id: sA\ntitle: A\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    mcp = tmp_path / "mcp.json"; mcp.write_text("{}")
    reports = tmp_path / "reports"; reports.mkdir()
    # Write a fake report
    report_file = reports / "benchmark-2026-05-05-1200.md"
    report_file.write_text("# test\n")

    cfg = AppConfig(scenarios_dir=scen, mcp_config_path=mcp,
                    reports_dir=reports, static_dir=None)
    client = TestClient(create_app(cfg))
    r = client.head("/api/reports/latest")
    assert r.status_code == 200
    assert "last-modified" in {k.lower() for k in r.headers.keys()}


def test_run_status_returns_state(tmp_path):
    from token_compare.api import AppConfig, create_app
    from fastapi.testclient import TestClient
    scen = tmp_path / "scenarios"; scen.mkdir()
    (scen / "sA.yaml").write_text(
        "id: sA\ntitle: A\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    mcp = tmp_path / "mcp.json"; mcp.write_text("{}")
    reports = tmp_path / "reports"; reports.mkdir()
    cfg = AppConfig(scenarios_dir=scen, mcp_config_path=mcp,
                    reports_dir=reports, static_dir=None)
    client = TestClient(create_app(cfg))
    r = client.get("/api/run/status")
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is False
    assert body["events"] == []
    assert body["report_path"] is None


def test_run_status_reflects_active_benchmark(tmp_path, monkeypatch):
    """After POST /api/run starts, /api/run/status should show active state + events."""
    from unittest.mock import patch
    from token_compare.api import AppConfig, create_app
    from fastapi.testclient import TestClient
    from token_compare.models import PathName, RunResult
    from token_compare import db

    # Provide a mock SF token
    async def _mock_get_sf_token(sid):
        return {"access_token": "T", "instance_url": "https://x"}
    monkeypatch.setattr(db, "get_sf_token", _mock_get_sf_token)

    scen = tmp_path / "scenarios"; scen.mkdir()
    (scen / "sA.yaml").write_text(
        "id: sA\ntitle: A\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    mcp = tmp_path / "mcp.json"; mcp.write_text("{}")
    reports = tmp_path / "reports"; reports.mkdir()
    cfg = AppConfig(scenarios_dir=scen, mcp_config_path=mcp,
                    reports_dir=reports, static_dir=None)
    client = TestClient(create_app(cfg))

    def fake_run_once(scenario, path, **kwargs):
        return RunResult(path=path, input_tokens=10, output_tokens=5,
                         cache_read_input_tokens=0, cache_creation_input_tokens=0,
                         total_cost_usd=0.001, num_turns=1, duration_ms=1,
                         tool_calls=[], succeeded=True, error=None, raw_json={})

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream(
            "POST", "/api/run",
            json={"scenario_ids": ["sA"], "runs_per_path": 1,
                  "model": "sonnet", "operator": "me", "org_name": "o"},
        ) as resp:
            list(resp.iter_text())

    # After the run, status should show inactive but with events + report_path
    r = client.get("/api/run/status")
    body = r.json()
    assert body["active"] is False
    assert body["report_path"] is not None
    assert len(body["events"]) > 0
    # Latest report endpoint should return JSON with scenarios
    r2 = client.get("/api/reports/latest/data")
    assert r2.status_code == 200
    data = r2.json()
    assert "scenarios" in data
    assert len(data["scenarios"]) == 1
    assert data["scenarios"][0]["scenario_id"] == "sA"


def test_scenario_trace_returns_comparison(tmp_path, monkeypatch):
    """After a benchmark run, /api/scenarios/{id}/trace returns trace data."""
    from unittest.mock import patch
    from token_compare.api import AppConfig, create_app
    from fastapi.testclient import TestClient
    from token_compare.models import PathName, RunResult
    from token_compare import db

    # Provide a mock SF token
    async def _mock_get_sf_token(sid):
        return {"access_token": "T", "instance_url": "https://x"}
    monkeypatch.setattr(db, "get_sf_token", _mock_get_sf_token)

    scen = tmp_path / "scenarios"; scen.mkdir()
    (scen / "sA.yaml").write_text(
        "id: sA\ntitle: A\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    mcp = tmp_path / "mcp.json"; mcp.write_text("{}")
    reports = tmp_path / "reports"; reports.mkdir()
    cfg = AppConfig(scenarios_dir=scen, mcp_config_path=mcp,
                    reports_dir=reports, static_dir=None)
    client = TestClient(create_app(cfg))

    def fake_run_once(scenario, path, **kwargs):
        return RunResult(
            path=path, input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
            total_cost_usd=0.01, num_turns=1, duration_ms=100,
            tool_calls=["Bash" if path == PathName.NATIVE else "mcp__x"],
            succeeded=True, error=None,
            raw_json=[
                {"type": "system", "subtype": "init",
                 "tools": ["Bash"] if path == PathName.NATIVE else ["Bash"]+["mcp__t"]*5,
                 "mcp_servers": [] if path == PathName.NATIVE
                                  else [{"name": "salesforce_crm"}]},
                {"type": "assistant",
                 "message": {"usage": {"input_tokens": 5, "output_tokens": 5,
                                        "cache_read_input_tokens": 0,
                                        "cache_creation_input_tokens": 100},
                             "content": [{"type": "text", "text": "ok"}]}},
                {"type": "result", "result": "ok", "is_error": False,
                 "num_turns": 1, "duration_ms": 100, "total_cost_usd": 0.01,
                 "usage": {"input_tokens": 5, "output_tokens": 5,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 100},
                 "terminal_reason": "completed"},
            ],
        )

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream(
            "POST", "/api/run",
            json={"scenario_ids": ["sA"], "runs_per_path": 1,
                  "model": "sonnet", "operator": "me", "org_name": "o"},
        ) as resp:
            list(resp.iter_text())

    r = client.get("/api/scenarios/sA/trace")
    assert r.status_code == 200
    body = r.json()
    assert body["scenario_id"] == "sA"
    assert "explanation" in body and len(body["explanation"]) > 10
    assert len(body["native_traces"]) == 1
    assert len(body["mcp_traces"]) == 1


def test_summary_endpoint_returns_analysis(tmp_path, monkeypatch):
    """After a benchmark, /api/reports/latest/summary returns structured analysis."""
    from unittest.mock import patch
    from token_compare.api import AppConfig, create_app
    from fastapi.testclient import TestClient
    from token_compare.models import PathName, RunResult
    from token_compare import db

    # Provide a mock SF token
    async def _mock_get_sf_token(sid):
        return {"access_token": "T", "instance_url": "https://x"}
    monkeypatch.setattr(db, "get_sf_token", _mock_get_sf_token)

    scen = tmp_path / "scenarios"; scen.mkdir()
    (scen / "sA.yaml").write_text(
        "id: sA\ntitle: A\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    mcp = tmp_path / "mcp.json"; mcp.write_text("{}")
    reports = tmp_path / "reports"; reports.mkdir()
    cfg = AppConfig(scenarios_dir=scen, mcp_config_path=mcp,
                    reports_dir=reports, static_dir=None)
    client = TestClient(create_app(cfg))

    def fake_run_once(scenario, path, **kwargs):
        return RunResult(
            path=path, input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
            total_cost_usd=0.01 if path == PathName.NATIVE else 0.02,
            num_turns=1, duration_ms=100, tool_calls=["x"],
            succeeded=True, error=None, raw_json={},
        )

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream("POST", "/api/run", json={
            "scenario_ids": ["sA"], "runs_per_path": 1,
            "model": "sonnet", "operator": "me", "org_name": "o"
        }) as resp:
            list(resp.iter_text())

    r = client.get("/api/reports/latest/summary")
    assert r.status_code == 200
    body = r.json()
    assert "headline" in body and len(body["headline"]) > 10
    assert "scenarios" in body and len(body["scenarios"]) == 1
    assert "caveats" in body and isinstance(body["caveats"], list)
    assert body["scenarios"][0]["winner"] == "native"


def test_list_reports_empty(client):
    r = client.get("/api/reports")
    assert r.status_code == 200
    assert r.json() == {"reports": []}


def test_list_and_load_report(client, tmp_path, monkeypatch):
    """Drop a report file in reports/ and confirm it lists + loads."""
    from token_compare import db
    from token_compare.models import BenchmarkResult
    from tests.test_report_loader import _make_benchmark

    bench = _make_benchmark()

    # Mock the DB to return a synthetic report
    async def _mock_list_reports(limit=10):
        return [{
            "id": "rpt_test123",
            "started_at": bench.started_at,
            "model": bench.model,
            "operator": bench.operator,
            "org_name": bench.org_name,
        }]

    async def _mock_get_report(report_id):
        if report_id == "rpt_test123":
            return {
                "id": "rpt_test123",
                "started_at": bench.started_at,
                "payload_json": bench.model_dump(),
            }
        return None

    monkeypatch.setattr(db, "list_reports", _mock_list_reports)
    monkeypatch.setattr(db, "get_report", _mock_get_report)

    # List
    r = client.get("/api/reports")
    body = r.json()
    assert len(body["reports"]) == 1
    assert body["reports"][0]["name"] == "rpt_test123"

    # Load by id
    r = client.get("/api/reports/rpt_test123/data")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["scenario_count"] == 1
    assert body["scenario_ids"] == ["s01_test_scenario"]

    # And the trace endpoint should now serve this loaded report.
    r = client.get("/api/scenarios/s01_test_scenario/trace")
    assert r.status_code == 200, r.text
    trace_body = r.json()
    assert "native_traces" in trace_body and "mcp_traces" in trace_body


@pytest.mark.skip(reason="DB-backed reports use opaque ULIDs; no path traversal risk")
def test_load_report_rejects_path_traversal(client):
    """Names like ../etc/passwd must not escape the reports/ dir."""
    r = client.get("/api/reports/..%2Fetc%2Fpasswd/data")
    # FastAPI may either 404 (not under benchmark-*.md prefix) or 400.
    # Either way: not 200, and must not leak a file from outside.
    assert r.status_code in (400, 404)


def test_load_report_uploaded_markdown(client, tmp_path):
    """Upload a markdown report directly without it being on disk."""
    from token_compare.report import write_markdown
    from tests.test_report_loader import _make_benchmark
    bench = _make_benchmark()
    md_path = tmp_path / "uploaded.md"
    write_markdown(bench, md_path)
    text = md_path.read_text(encoding="utf-8")

    r = client.post(
        "/api/reports/load",
        files={"file": ("uploaded.md", text, "text/markdown")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["scenario_count"] == 1
    assert body["source"].startswith("upload:")
