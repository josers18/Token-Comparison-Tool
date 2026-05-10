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
    monkeypatch.setattr(db, "list_recent_reports", _empty_list)
    monkeypatch.setattr(db, "get_report", _none)
    monkeypatch.setattr(db, "create_report", _create_report)
    monkeypatch.setattr(db, "finalize_report", _noop)
    monkeypatch.setattr(db, "insert_run", _noop)
    monkeypatch.setattr(db, "put_pending_login", _noop)
    monkeypatch.setattr(db, "pop_pending_login", _none)
    monkeypatch.setattr(db, "prune_pending_logins", _noop)
    # Scenarios DAOs. By default the stub list is empty; tests that
    # need scenarios populate _SCENARIO_STUB.scenarios at fixture setup
    # time so /api/scenarios returns realistic data.
    async def _count_scenarios(): return 99  # pretend already seeded
    async def _list_scenarios(*, include_inactive=False):
        return list(_SCENARIO_STUB.get("scenarios") or [])
    async def _get_scenario(sid):
        for s in (_SCENARIO_STUB.get("scenarios") or []):
            if s["id"] == sid:
                return s
        return None
    monkeypatch.setattr(db, "list_scenarios", _list_scenarios)
    monkeypatch.setattr(db, "count_scenarios", _count_scenarios)
    monkeypatch.setattr(db, "get_scenario", _get_scenario)
    monkeypatch.setattr(db, "upsert_scenario", _noop)
    monkeypatch.setattr(db, "set_scenario_active", _noop)


# Module-level dict the _stub_db fixture's list_scenarios stub reads from.
# Tests that need a scenario in /api/scenarios populate this in their
# `client` fixture so each test owns its own scenario set.
_SCENARIO_STUB: dict = {"scenarios": []}


@pytest.fixture
def client(tmp_path):
    scen_dir = tmp_path / "scenarios"; scen_dir.mkdir()
    (scen_dir / "sA.yaml").write_text(
        "id: sA\ntitle: A\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    # Mirror the YAML into the in-memory stub so /api/scenarios returns it.
    _SCENARIO_STUB["scenarios"] = [{
        "id": "sA", "title": "A", "category": "c", "difficulty": "simple",
        "prompt": "x", "expected_operations": [],
        "success_criteria_json": {"must_contain": []}, "notes": "",
        "is_active": True, "created_at": None, "updated_at": None,
    }]
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
        return {"access_token": "T", "instance_url": "https://x", "issued_at": 0, "expires_at": 9999999999}
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
        return {"access_token": "T", "instance_url": "https://x", "issued_at": 0, "expires_at": 9999999999}
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
        return {"access_token": "T", "instance_url": "https://x", "issued_at": 0, "expires_at": 9999999999}
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
        return {"access_token": "T", "instance_url": "https://x", "issued_at": 0, "expires_at": 9999999999}
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
        return {"access_token": "T", "instance_url": "https://x", "issued_at": 0, "expires_at": 9999999999}
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
    body = r.json()
    assert body["reports"] == []
    # The endpoint always emits a KPI strip, even for the empty case;
    # the SPA's analytics view can render zeroes without a special path.
    assert body["kpis"]["total_runs"] == 0
    assert body["kpis"]["total_finalized"] == 0


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
    # The list response includes a models[] field for the analytics table
    # so the SPA can render multi-model sweeps and filter "report contains
    # this model" instead of "report.model == this model".
    assert "models" in body["reports"][0]
    assert isinstance(body["reports"][0]["models"], list)

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


def test_post_run_accepts_models_list(client, monkeypatch):
    from token_compare.models import RunResult, PathName
    from token_compare import db
    async def _mock_get_sf_token(sid):
        return {"access_token": "T", "instance_url": "https://x",
                "issued_at": 0, "expires_at": 9999999999}
    monkeypatch.setattr(db, "get_sf_token", _mock_get_sf_token)

    def fake_run_once(scenario, path, **kw):
        return RunResult(
            path=path, input_tokens=1, output_tokens=1,
            cache_read_input_tokens=0, total_cost_usd=0.01,
            num_turns=1, duration_ms=1, tool_calls=[],
            succeeded=True, raw_json={},
        )

    from unittest.mock import patch
    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream(
            "POST", "/api/run",
            json={"scenario_ids": ["sA"], "runs_per_path": 1,
                  "models": ["claude-4-5-sonnet", "claude-3-haiku"],
                  "operator": "me", "org_name": "o"},
        ) as resp:
            chunks = "".join(list(resp.iter_text()))

    assert "benchmark_complete" in chunks
    assert "claude-4-5-sonnet" in chunks
    assert "claude-3-haiku" in chunks


def test_post_run_legacy_model_string_still_works(client, monkeypatch):
    """Old clients sending model: 'sonnet' should still work."""
    from token_compare.models import RunResult
    from token_compare import db
    async def _mock_get_sf_token(sid):
        return {"access_token": "T", "instance_url": "https://x",
                "issued_at": 0, "expires_at": 9999999999}
    monkeypatch.setattr(db, "get_sf_token", _mock_get_sf_token)

    def fake_run_once(scenario, path, **kw):
        return RunResult(
            path=path, input_tokens=1, output_tokens=1,
            cache_read_input_tokens=0, total_cost_usd=0.01,
            num_turns=1, duration_ms=1, tool_calls=[],
            succeeded=True, raw_json={},
        )
    from unittest.mock import patch
    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream(
            "POST", "/api/run",
            json={"scenario_ids": ["sA"], "runs_per_path": 1,
                  "model": "claude-4-5-sonnet",
                  "operator": "me", "org_name": "o"},
        ) as resp:
            chunks = "".join(list(resp.iter_text()))
    assert "benchmark_complete" in chunks


def test_load_legacy_report_normalizes_to_cube(client, monkeypatch):
    """A legacy DB row without 'models'/'runs_by_model' still loads."""
    from token_compare import db
    legacy_payload = {
        "started_at": "2026-04-01T00:00:00+00:00",
        "finished_at": "2026-04-01T00:00:01+00:00",
        "operator": "me", "org_name": "o", "tool_commit": "abc",
        "model": "claude-4-5-sonnet",
        "runs_per_path": 1,
        "scenarios": [{
            "scenario_id": "sLegacy",
            "native_runs": [{"path": "native", "input_tokens": 10,
                              "output_tokens": 5,
                              "cache_read_input_tokens": 0,
                              "total_cost_usd": 0.01, "num_turns": 1,
                              "duration_ms": 100, "tool_calls": [],
                              "succeeded": True, "raw_json": {}}],
            "mcp_runs": [{"path": "mcp", "input_tokens": 20,
                          "output_tokens": 10,
                          "cache_read_input_tokens": 0,
                          "total_cost_usd": 0.02, "num_turns": 1,
                          "duration_ms": 200, "tool_calls": [],
                          "succeeded": True, "raw_json": {}}],
        }],
    }
    async def _mock_get_report(report_id):
        if report_id == "rpt_legacy":
            return {"id": "rpt_legacy",
                    "started_at": "2026-04-01T00:00:00+00:00",
                    "payload_json": legacy_payload}
        return None
    monkeypatch.setattr(db, "get_report", _mock_get_report)

    r = client.get("/api/reports/rpt_legacy/data")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["scenario_count"] == 1
    assert body["scenario_ids"] == ["sLegacy"]


def test_projection_endpoint(client, monkeypatch):
    from token_compare import db
    payload = {
        "started_at": "2026-04-01T00:00:00+00:00",
        "finished_at": "2026-04-01T00:00:01+00:00",
        "operator": "me", "org_name": "o", "tool_commit": "abc",
        "model": "claude-4-5-sonnet",
        "models": ["claude-4-5-sonnet"],
        "runs_per_path": 1,
        "scenarios": [{
            "scenario_id": "s1",
            "native_runs": [{"path": "native", "input_tokens": 10,
                              "output_tokens": 5,
                              "cache_read_input_tokens": 0,
                              "total_cost_usd": 0.01, "num_turns": 1,
                              "duration_ms": 100, "tool_calls": [],
                              "succeeded": True, "raw_json": {}}],
            "mcp_runs": [{"path": "mcp", "input_tokens": 10,
                          "output_tokens": 5,
                          "cache_read_input_tokens": 0,
                          "total_cost_usd": 0.02, "num_turns": 1,
                          "duration_ms": 100, "tool_calls": [],
                          "succeeded": True, "raw_json": {}}],
            "runs_by_model": {"claude-4-5-sonnet": {
                "native_runs": [{"path": "native", "input_tokens": 10,
                                  "output_tokens": 5,
                                  "cache_read_input_tokens": 0,
                                  "total_cost_usd": 0.01, "num_turns": 1,
                                  "duration_ms": 100, "tool_calls": [],
                                  "succeeded": True, "raw_json": {}}],
                "mcp_runs": [{"path": "mcp", "input_tokens": 10,
                              "output_tokens": 5,
                              "cache_read_input_tokens": 0,
                              "total_cost_usd": 0.02, "num_turns": 1,
                              "duration_ms": 100, "tool_calls": [],
                              "succeeded": True, "raw_json": {}}],
            }},
        }],
    }
    async def _mock_get_report(report_id):
        if report_id == "rpt_proj":
            return {"id": "rpt_proj", "payload_json": payload,
                    "started_at": "2026-04-01T00:00:00+00:00"}
        return None
    monkeypatch.setattr(db, "get_report", _mock_get_report)

    r = client.get("/api/reports/rpt_proj/projection?volume=10000&period=month")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["native_total"] == 100.0
    assert body["mcp_total"] == 200.0
    assert len(body["curve"]) == 12
    assert body["model_used"] == "claude-4-5-sonnet"


def test_projection_endpoint_with_thresholds(client, monkeypatch):
    """Custom breakeven thresholds passed as comma-separated query."""
    from token_compare import db
    payload = {
        "started_at": "2026-04-01T00:00:00+00:00",
        "finished_at": "2026-04-01T00:00:01+00:00",
        "operator": "me", "org_name": "o", "tool_commit": "abc",
        "model": "claude-4-5-sonnet",
        "models": ["claude-4-5-sonnet"],
        "runs_per_path": 1,
        "scenarios": [{
            "scenario_id": "s1",
            "native_runs": [{"path": "native", "input_tokens": 10,
                              "output_tokens": 5,
                              "cache_read_input_tokens": 0,
                              "total_cost_usd": 0.01, "num_turns": 1,
                              "duration_ms": 100, "tool_calls": [],
                              "succeeded": True, "raw_json": {}}],
            "mcp_runs": [{"path": "mcp", "input_tokens": 10,
                          "output_tokens": 5,
                          "cache_read_input_tokens": 0,
                          "total_cost_usd": 0.02, "num_turns": 1,
                          "duration_ms": 100, "tool_calls": [],
                          "succeeded": True, "raw_json": {}}],
            "runs_by_model": {"claude-4-5-sonnet": {
                "native_runs": [{"path": "native", "input_tokens": 10,
                                  "output_tokens": 5,
                                  "cache_read_input_tokens": 0,
                                  "total_cost_usd": 0.01, "num_turns": 1,
                                  "duration_ms": 100, "tool_calls": [],
                                  "succeeded": True, "raw_json": {}}],
                "mcp_runs": [{"path": "mcp", "input_tokens": 10,
                              "output_tokens": 5,
                              "cache_read_input_tokens": 0,
                              "total_cost_usd": 0.02, "num_turns": 1,
                              "duration_ms": 100, "tool_calls": [],
                              "succeeded": True, "raw_json": {}}],
            }},
        }],
    }
    async def _mock_get_report(report_id):
        if report_id == "rpt_proj":
            return {"id": "rpt_proj", "payload_json": payload,
                    "started_at": "2026-04-01T00:00:00+00:00"}
        return None
    monkeypatch.setattr(db, "get_report", _mock_get_report)

    r = client.get(
        "/api/reports/rpt_proj/projection"
        "?volume=10000&period=month&thresholds=500,5000,50000"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    threshold_values = sorted({b["threshold_usd"] for b in body["breakevens"]})
    assert threshold_values == [500.0, 5000.0, 50000.0]


def test_projection_endpoint_404_on_unknown_report(client, monkeypatch):
    from token_compare import db
    async def _mock_get_report(report_id):
        return None
    monkeypatch.setattr(db, "get_report", _mock_get_report)
    r = client.get("/api/reports/rpt_nonexistent/projection?volume=1000&period=month")
    assert r.status_code == 404


def test_projection_endpoint_invalid_thresholds(client, monkeypatch):
    from token_compare import db
    async def _mock_get_report(report_id):
        return {"id": "rpt_x", "payload_json": {"models": ["sonnet"], "model": "sonnet", "scenarios": [], "runs_per_path": 1}, "started_at": "x"}
    monkeypatch.setattr(db, "get_report", _mock_get_report)
    r = client.get("/api/reports/rpt_x/projection?volume=1000&period=month&thresholds=abc,def")
    assert r.status_code == 400


def test_history_endpoint_returns_series(client, monkeypatch):
    from token_compare import db
    from datetime import datetime, timedelta, timezone

    # Use a recent timestamp so the default 30-day since filter doesn't
    # exclude this row (the test originally hardcoded 2026-04-01 which
    # silently drifted out of the window as the calendar moved on).
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    rows = [
        {"id": "r1", "started_at": recent,
         "payload_json": {
            "model": "claude-4-5-sonnet", "models": ["claude-4-5-sonnet"],
            "scenarios": [{
                "scenario_id": "s1",
                "runs_by_model": {"claude-4-5-sonnet": {
                    "native_runs": [{"path": "native", "input_tokens": 1,
                                      "output_tokens": 1,
                                      "cache_read_input_tokens": 0,
                                      "total_cost_usd": 0.01, "num_turns": 1,
                                      "duration_ms": 1, "tool_calls": [],
                                      "succeeded": True, "raw_json": {}}],
                    "mcp_runs": [{"path": "mcp", "input_tokens": 1,
                                   "output_tokens": 1,
                                   "cache_read_input_tokens": 0,
                                   "total_cost_usd": 0.02, "num_turns": 1,
                                   "duration_ms": 1, "tool_calls": [],
                                   "succeeded": True, "raw_json": {}}],
                }},
            }],
        }},
    ]
    async def _mock_history(scenario_id, model):
        return rows
    monkeypatch.setattr(db, "list_finalized_reports_for_history",
                        _mock_history, raising=False)

    r = client.get("/api/history?scenario_id=s1&model=claude-4-5-sonnet&metric=cost")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metric"] == "cost"
    assert len(body["points"]) == 1
    assert body["points"][0]["native"] == 0.01


def test_history_endpoint_invalid_since(client, monkeypatch):
    from token_compare import db
    async def _mock_history(scenario_id, model): return []
    monkeypatch.setattr(db, "list_finalized_reports_for_history",
                        _mock_history, raising=False)
    r = client.get("/api/history?scenario_id=s1&model=sonnet&since=not-a-date")
    assert r.status_code == 400


def test_history_endpoint_default_since_is_30_days(client, monkeypatch):
    """No since param → server defaults to 30 days ago. Older points filtered out."""
    from token_compare import db
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc) - timedelta(days=60)
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    rows = [
        {"id": "old", "started_at": old, "payload_json": {
            "model": "sonnet", "models": ["sonnet"],
            "scenarios": [{"scenario_id": "s1",
                            "runs_by_model": {"sonnet": {
                                "native_runs": [{"path":"native","input_tokens":1,
                                  "output_tokens":1,"cache_read_input_tokens":0,
                                  "total_cost_usd":0.01,"num_turns":1,
                                  "duration_ms":1,"tool_calls":[],
                                  "succeeded":True,"raw_json":{}}],
                                "mcp_runs": []}},
                          }]}},
        {"id": "new", "started_at": recent, "payload_json": {
            "model": "sonnet", "models": ["sonnet"],
            "scenarios": [{"scenario_id": "s1",
                            "runs_by_model": {"sonnet": {
                                "native_runs": [{"path":"native","input_tokens":1,
                                  "output_tokens":1,"cache_read_input_tokens":0,
                                  "total_cost_usd":0.02,"num_turns":1,
                                  "duration_ms":1,"tool_calls":[],
                                  "succeeded":True,"raw_json":{}}],
                                "mcp_runs": []}},
                          }]}},
    ]
    async def _mock_history(scenario_id, model): return rows
    monkeypatch.setattr(db, "list_finalized_reports_for_history",
                        _mock_history, raising=False)
    r = client.get("/api/history?scenario_id=s1&model=sonnet&metric=cost")
    assert r.status_code == 200
    body = r.json()
    # Only the recent row should appear.
    assert len(body["points"]) == 1
    assert body["points"][0]["report_id"] == "new"


def test_scenario_trace_includes_turn_diffs(tmp_path, monkeypatch):
    """After a benchmark run, /api/scenarios/{id}/trace returns turn_diffs."""
    from unittest.mock import patch
    from token_compare.api import AppConfig, create_app
    from fastapi.testclient import TestClient
    from token_compare.models import PathName, RunResult
    from token_compare import db

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

    async def _mock_get_sf_token(sid):
        return {"access_token": "T", "instance_url": "https://x",
                "issued_at": 0, "expires_at": 9999999999}
    monkeypatch.setattr(db, "get_sf_token", _mock_get_sf_token)

    def fake_run_once(scenario, path, **kwargs):
        # Mock raw_json with one assistant turn whose cache_creation differs
        # between paths — exactly the tool_list_reload pattern.
        cache_create = 100 if path == PathName.NATIVE else 1500
        return RunResult(
            path=path, input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=cache_create,
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
                                        "cache_creation_input_tokens": cache_create},
                             "content": [{"type": "text", "text": "ok"}]}},
                {"type": "result", "result": "ok", "is_error": False,
                 "num_turns": 1, "duration_ms": 100, "total_cost_usd": 0.01,
                 "usage": {"input_tokens": 5, "output_tokens": 5,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": cache_create},
                 "terminal_reason": "completed"},
            ],
        )

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream("POST", "/api/run", json={
            "scenario_ids": ["sA"], "runs_per_path": 1,
            "models": ["claude-4-5-sonnet"], "operator": "me", "org_name": "o",
        }) as resp:
            list(resp.iter_text())

    r = client.get("/api/scenarios/sA/trace")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "turn_diffs" in body
    assert isinstance(body["turn_diffs"], list)
    # MCP turn 1 had cache_create=1500 vs native 100 → tool_list_reload
    assert any(d.get("reason") == "tool_list_reload" for d in body["turn_diffs"])


def test_post_share_returns_url(client, monkeypatch):
    from token_compare import db
    async def _get_report(rid):
        if rid == "rpt_share":
            return {"id": "rpt_share", "started_at": "2026-05-01T00:00:00+00:00",
                    "payload_json": {"model": "sonnet", "models": ["sonnet"],
                                       "scenarios": [], "runs_per_path": 1}}
        return None
    monkeypatch.setattr(db, "get_report", _get_report)

    async def _mock_get_sf_token(sid):
        return {"access_token": "T", "instance_url": "https://x", "issued_at": 0, "expires_at": 9999999999}
    monkeypatch.setattr(db, "get_sf_token", _mock_get_sf_token)

    r = client.post("/api/reports/rpt_share/share", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "url" in body and "token" in body and "expires_at" in body
    assert "/share/" in body["url"]


def test_get_share_data_with_valid_token(client, monkeypatch):
    from token_compare import db
    from token_compare.share_token import issue
    async def _get_report(rid):
        if rid == "rpt_share":
            return {"id": "rpt_share", "started_at": "2026-05-01T00:00:00+00:00",
                    "payload_json": {"model": "sonnet", "models": ["sonnet"],
                                       "scenarios": [], "runs_per_path": 1,
                                       "started_at": "x", "finished_at": "y",
                                       "operator": "me", "org_name": "o",
                                       "tool_commit": "abc"}}
        return None
    monkeypatch.setattr(db, "get_report", _get_report)
    token, _ = issue("rpt_share")
    r = client.get(f"/api/share/{token}/data")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "sonnet"


def test_get_share_data_expired_returns_410(client):
    from token_compare.share_token import issue
    import time
    token, _ = issue("rpt_x", ttl_days=0)
    time.sleep(0.01)
    r = client.get(f"/api/share/{token}/data")
    assert r.status_code == 410


def test_get_share_data_malformed_returns_410(client):
    r = client.get("/api/share/not-a-token/data")
    assert r.status_code == 410


def test_compare_endpoint_happy_path(client, monkeypatch):
    from token_compare import db
    payload_a = {"model": "sonnet", "models": ["sonnet"],
                  "started_at": "2026-05-01T00:00:00+00:00",
                  "finished_at": "2026-05-01T00:00:01+00:00",
                  "operator": "me", "org_name": "o", "tool_commit": "abc",
                  "runs_per_path": 1,
                  "scenarios": [{"scenario_id": "s1",
                                  "native_runs": [{"path": "native", "input_tokens": 1,
                                                   "output_tokens": 1,
                                                   "cache_read_input_tokens": 0,
                                                   "total_cost_usd": 0.01,
                                                   "num_turns": 1, "duration_ms": 100,
                                                   "tool_calls": [], "succeeded": True,
                                                   "raw_json": {}}],
                                  "mcp_runs": [{"path": "mcp", "input_tokens": 1,
                                                 "output_tokens": 1,
                                                 "cache_read_input_tokens": 0,
                                                 "total_cost_usd": 0.02,
                                                 "num_turns": 1, "duration_ms": 100,
                                                 "tool_calls": [], "succeeded": True,
                                                 "raw_json": {}}]}]}
    payload_b = {**payload_a, "scenarios": [{**payload_a["scenarios"][0],
                                                "native_runs": [{**payload_a["scenarios"][0]["native_runs"][0],
                                                                  "total_cost_usd": 0.012}]}]}
    async def _get_report(rid):
        if rid == "rpt_a": return {"id": "rpt_a", "payload_json": payload_a, "started_at": "x"}
        if rid == "rpt_b": return {"id": "rpt_b", "payload_json": payload_b, "started_at": "y"}
        return None
    monkeypatch.setattr(db, "get_report", _get_report)
    r = client.get("/api/reports/compare?a=rpt_a&b=rpt_b")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model_used"] == "sonnet"
    assert len(body["scenarios"]) == 1
    assert body["scenarios"][0]["scenario_id"] == "s1"
    # Report ids are backfilled by the endpoint.
    assert body["report_a"]["id"] == "rpt_a"
    assert body["report_b"]["id"] == "rpt_b"


def test_compare_endpoint_404_on_missing(client, monkeypatch):
    from token_compare import db
    async def _get_report(rid): return None
    monkeypatch.setattr(db, "get_report", _get_report)
    r = client.get("/api/reports/compare?a=missing&b=alsomissing")
    assert r.status_code == 404


def test_compare_endpoint_400_on_same_id(client):
    r = client.get("/api/reports/compare?a=same&b=same")
    assert r.status_code == 400


def test_scenarios_sparkline_returns_per_scenario_history(client, monkeypatch):
    from token_compare import db

    async def _list_recent_reports(limit=20):
        return [
            {"id": "rpt_1", "started_at": "2026-05-09T00:00:00+00:00",
             "payload_json": {
                "model": "sonnet", "models": ["sonnet"],
                "started_at": "x", "finished_at": "y", "operator": "me",
                "org_name": "o", "tool_commit": "abc", "runs_per_path": 1,
                "scenarios": [
                    {"scenario_id": "s01",
                     "native_runs": [{"path":"native","input_tokens":1,"output_tokens":1,
                                       "cache_read_input_tokens":0,"total_cost_usd":0.62,
                                       "num_turns":1,"duration_ms":100,"tool_calls":[],
                                       "succeeded":True,"raw_json":{}}],
                     "mcp_runs":    [{"path":"mcp","input_tokens":1,"output_tokens":1,
                                       "cache_read_input_tokens":0,"total_cost_usd":0.93,
                                       "num_turns":1,"duration_ms":100,"tool_calls":[],
                                       "succeeded":True,"raw_json":{}}]},
                ]}},
            {"id": "rpt_2", "started_at": "2026-05-08T00:00:00+00:00",
             "payload_json": {
                "model": "sonnet", "models": ["sonnet"],
                "started_at": "x", "finished_at": "y", "operator": "me",
                "org_name": "o", "tool_commit": "abc", "runs_per_path": 1,
                "scenarios": [
                    {"scenario_id": "s01",
                     "native_runs": [{"path":"native","input_tokens":1,"output_tokens":1,
                                       "cache_read_input_tokens":0,"total_cost_usd":0.65,
                                       "num_turns":1,"duration_ms":100,"tool_calls":[],
                                       "succeeded":True,"raw_json":{}}],
                     "mcp_runs":    [{"path":"mcp","input_tokens":1,"output_tokens":1,
                                       "cache_read_input_tokens":0,"total_cost_usd":0.91,
                                       "num_turns":1,"duration_ms":100,"tool_calls":[],
                                       "succeeded":True,"raw_json":{}}]},
                ]}},
        ]

    monkeypatch.setattr(db, "list_recent_reports", _list_recent_reports)
    r = client.get("/api/scenarios/sparkline?ids=s01,s02")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "s01" in body
    # Most-recent first.
    assert body["s01"]["native"] == [0.62, 0.65]
    assert body["s01"]["mcp"] == [0.93, 0.91]
    # s02 not present in any report — endpoint omits the key.
    assert "s02" not in body


def test_scenarios_sparkline_empty_when_no_reports(client, monkeypatch):
    from token_compare import db
    async def _list_recent_reports(limit=20): return []
    monkeypatch.setattr(db, "list_recent_reports", _list_recent_reports)
    r = client.get("/api/scenarios/sparkline?ids=s01")
    assert r.status_code == 200
    assert r.json() == {}


def test_scenarios_sparkline_400_on_missing_ids(client):
    r = client.get("/api/scenarios/sparkline")
    assert r.status_code == 400
