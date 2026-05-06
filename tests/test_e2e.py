from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from token_compare.api import AppConfig, create_app
from token_compare.models import PathName, RunResult


@pytest.fixture
def e2e_client(tmp_path):
    scen_dir = tmp_path / "scenarios"; scen_dir.mkdir()
    (scen_dir / "sA.yaml").write_text(
        "id: sA\ntitle: A\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    (tmp_path / "sf-mcp.json").write_text("{}")
    reports = tmp_path / "reports"; reports.mkdir()
    cfg = AppConfig(
        scenarios_dir=scen_dir, mcp_config_path=tmp_path / "sf-mcp.json",
        reports_dir=reports, static_dir=None,
    )
    return TestClient(create_app(cfg)), reports


def _mk_run(path):
    return RunResult(path=path, input_tokens=500, output_tokens=50,
                     cache_read_input_tokens=0, total_cost_usd=0.02,
                     num_turns=1, duration_ms=100, tool_calls=["x"],
                     succeeded=True, error=None, raw_json={})


def test_full_run_writes_one_report(e2e_client):
    client, reports = e2e_client

    with patch("token_compare.benchmark.run_once", side_effect=lambda s, p, **k: _mk_run(p)), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream(
            "POST", "/api/run",
            json={"scenario_ids": ["sA"], "runs_per_path": 2,
                  "model": "claude-opus-4-7", "operator": "me", "org_name": "o"},
        ) as resp:
            list(resp.iter_text())

    md_files = list(reports.glob("benchmark-*.md"))
    assert len(md_files) == 1
    content = md_files[0].read_text()
    assert "# Token Comparison Benchmark" in content
    assert "sA" in content
    assert "## Appendix — Raw Data" in content


def test_reports_retention_prunes_old(e2e_client):
    client, reports = e2e_client
    for i in range(12):
        day = str(i + 1).zfill(2)
        (reports / f"benchmark-2026-04-{day}-{day}{day}00.md").write_text("old")
    assert len(list(reports.glob("benchmark-*.md"))) == 12

    with patch("token_compare.benchmark.run_once", side_effect=lambda s, p, **k: _mk_run(p)), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream(
            "POST", "/api/run",
            json={"scenario_ids": ["sA"], "runs_per_path": 1,
                  "model": "m", "operator": "me", "org_name": "o"},
        ) as resp:
            list(resp.iter_text())

    assert len(list(reports.glob("benchmark-*.md"))) <= 10
