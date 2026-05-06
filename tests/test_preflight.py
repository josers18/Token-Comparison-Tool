from pathlib import Path
from unittest.mock import patch, MagicMock
import json as _json_for_test

from token_compare.preflight import check_environment, PreflightResult


def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_all_green(tmp_path, monkeypatch):
    monkeypatch.setenv("SF_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SF_LOGIN_URL", "https://x.my.salesforce.com")
    mcp_cfg = tmp_path / "sf-mcp.json"
    mcp_cfg.write_text("{}")

    from token_compare.sf_auth import AccessToken
    with patch("token_compare.preflight.subprocess.run") as run, \
         patch("token_compare.preflight.fetch_access_token") as ft:
        run.side_effect = [
            _fake_proc(0, "claude 1.2.3\n"),
            _fake_proc(0, "Logged in as user@x.com\n"),
            _fake_proc(0, '[{"alias":"me"}]'),
        ]
        ft.return_value = AccessToken(access_token="t", instance_url="https://x", scope="api")
        result = check_environment(mcp_config_path=mcp_cfg)

    assert isinstance(result, PreflightResult)
    assert result.ok is True
    assert result.checks == {
        "claude_installed": True, "claude_logged_in": True,
        "sf_authenticated": True, "mcp_config_present": True,
        "sf_oauth_reachable": True,
    }


def test_missing_mcp_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SF_CLIENT_ID", raising=False)
    monkeypatch.delenv("SF_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SF_LOGIN_URL", raising=False)

    with patch("token_compare.preflight.subprocess.run") as run:
        run.side_effect = [
            _fake_proc(0, "claude 1.2.3\n"),
            _fake_proc(0, "Logged in as user@x.com\n"),
            _fake_proc(0, '[{"alias":"me"}]'),
        ]
        result = check_environment(mcp_config_path=tmp_path / "nope.json")

    assert result.ok is False
    assert result.checks["mcp_config_present"] is False
    assert any("mcp" in r.lower() for r in result.remediation)


def test_claude_not_installed(tmp_path, monkeypatch):
    monkeypatch.delenv("SF_CLIENT_ID", raising=False)
    monkeypatch.delenv("SF_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SF_LOGIN_URL", raising=False)
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    with patch("token_compare.preflight.subprocess.run") as run:
        run.side_effect = FileNotFoundError("claude: not found")
        result = check_environment(mcp_config_path=mcp_cfg)

    assert result.ok is False
    assert result.checks["claude_installed"] is False
    assert any("claude" in r.lower() for r in result.remediation)


def test_claude_logged_in_via_json_output(tmp_path, monkeypatch):
    monkeypatch.setenv("SF_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SF_LOGIN_URL", "https://x.my.salesforce.com")
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    json_status = _json_for_test.dumps({
        "loggedIn": True, "authMethod": "third_party", "apiProvider": "bedrock",
    })

    from token_compare.sf_auth import AccessToken
    with patch("token_compare.preflight.subprocess.run") as run, \
         patch("token_compare.preflight.fetch_access_token") as ft:
        run.side_effect = [
            _fake_proc(0, "claude 2.1.0\n"),
            _fake_proc(0, json_status),
            _fake_proc(0, '{"status":0,"result":{"other":[{"alias":"me","username":"u@x"}]}}'),
        ]
        ft.return_value = AccessToken(access_token="t", instance_url="https://x", scope="api")
        result = check_environment(mcp_config_path=mcp_cfg)

    assert result.checks["claude_logged_in"] is True
    # Non-sensitive claude_account summary — no access tokens
    assert "loggedIn" in result.details["claude_account"]
    assert "accessToken" not in result.details["claude_account"]


def test_sf_orgs_summary_redacts_access_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("SF_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SF_LOGIN_URL", "https://x.my.salesforce.com")
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    sf_json = _json_for_test.dumps({
        "status": 0,
        "result": {
            "other": [
                {"alias": "prod", "username": "admin@example.com",
                 "accessToken": "SECRET_TOKEN", "refreshToken": "SECRET_REFRESH"}
            ],
            "scratchOrgs": [],
        },
    })

    from token_compare.sf_auth import AccessToken
    with patch("token_compare.preflight.subprocess.run") as run, \
         patch("token_compare.preflight.fetch_access_token") as ft:
        run.side_effect = [
            _fake_proc(0, "claude 2.1.0\n"),
            _fake_proc(0, '{"loggedIn": true}'),
            _fake_proc(0, sf_json),
        ]
        ft.return_value = AccessToken(access_token="t", instance_url="https://x", scope="api")
        result = check_environment(mcp_config_path=mcp_cfg)

    assert result.ok is True
    assert result.checks["sf_authenticated"] is True
    # Access tokens must NOT appear anywhere in the returned details
    dumped = _json_for_test.dumps(result.model_dump())
    assert "SECRET_TOKEN" not in dumped
    assert "SECRET_REFRESH" not in dumped
    # But the alias should still be visible for UX
    assert "prod" in result.details["sf_orgs"]
