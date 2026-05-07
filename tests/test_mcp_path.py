import json
import pytest
from pathlib import Path

from token_compare.mcp_path import build_mcp_servers


def _write_template(tmp_path: Path) -> Path:
    cfg = {
        "mcpServers": {
            "salesforce_crm": {
                "type": "http",
                "url": "https://api.salesforce.com/platform/mcp/v1/platform/sobject-all",
                "headers": {"Authorization": "Bearer ${SF_ACCESS_TOKEN}"},
            },
            "data_cloud_queries": {
                "type": "http",
                "url": "https://api.salesforce.com/platform/mcp/v1/data/data-cloud-queries",
                "headers": {"Authorization": "Bearer ${SF_ACCESS_TOKEN}"},
            },
        }
    }
    p = tmp_path / "sf-mcp.json"
    p.write_text(json.dumps(cfg))
    return p


def test_build_injects_bearer(tmp_path):
    cfg = _write_template(tmp_path)
    out = build_mcp_servers(cfg, sf_access_token="TOK123")
    assert isinstance(out, list)
    assert len(out) == 2
    by_name = {s["name"]: s for s in out}
    assert by_name["salesforce_crm"]["authorization_token"] == "TOK123"
    assert by_name["salesforce_crm"]["url"].startswith("https://")
    assert by_name["data_cloud_queries"]["type"] == "url"


def test_missing_template_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_mcp_servers(tmp_path / "missing.json", sf_access_token="X")
