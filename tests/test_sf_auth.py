import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from token_compare.sf_auth import (
    OAuthCredentials, AccessToken, SfAuthError,
    _generate_pkce, _build_authorize_url,
    clear_cached_token, fetch_access_token, load_credentials_from_env,
    run_interactive_login,
    CACHE_PATH, SF_OAUTH_SCOPES,
)


def test_load_credentials_from_env_all_present(monkeypatch):
    monkeypatch.setenv("SF_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SF_LOGIN_URL", "https://example.com")
    monkeypatch.setenv("SF_REDIRECT_URI", "http://localhost:8000/callback")
    c = load_credentials_from_env()
    assert c is not None
    assert c.client_id == "cid"
    assert c.redirect_uri == "http://localhost:8000/callback"


def test_load_credentials_defaults_redirect(monkeypatch):
    monkeypatch.setenv("SF_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SF_LOGIN_URL", "https://example.com")
    monkeypatch.delenv("SF_REDIRECT_URI", raising=False)
    c = load_credentials_from_env()
    assert c is not None
    assert c.redirect_uri == "http://localhost:8000/callback"


def test_load_credentials_missing(monkeypatch):
    monkeypatch.delenv("SF_CLIENT_ID", raising=False)
    monkeypatch.delenv("SF_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SF_LOGIN_URL", raising=False)
    assert load_credentials_from_env() is None


def test_pkce_shape():
    v, c = _generate_pkce()
    # verifier: base64url 43 chars, no padding
    assert 43 <= len(v) <= 128
    assert "=" not in v
    # challenge: base64url of SHA256 — always 43 chars
    assert len(c) == 43
    assert "=" not in c


def test_authorize_url_contains_required_params():
    creds = OAuthCredentials(
        client_id="CID", client_secret="S", login_url="https://login.salesforce.com",
        redirect_uri="http://localhost:8000/callback",
    )
    url = _build_authorize_url(creds, state="STATE", challenge="CHAL")
    assert "response_type=code" in url
    assert "client_id=CID" in url
    assert "code_challenge=CHAL" in url
    assert "code_challenge_method=S256" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fcallback" in url
    assert "scope=mcp_api+cdp_api+refresh_token" in url
    assert "state=STATE" in url


def test_fetch_access_token_returns_cached_when_fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # isolate .cache/
    cache = tmp_path / ".cache" / "sf-token.json"
    cache.parent.mkdir()
    now = time.time()
    tok = AccessToken(
        access_token="CACHED_AT", instance_url="https://x",
        scope="mcp_api cdp_api", refresh_token="RT",
        issued_at=now, expires_at=now + 1800,
    )
    cache.write_text(tok.model_dump_json())

    creds = OAuthCredentials(
        client_id="CID", client_secret="S", login_url="https://login.salesforce.com",
    )
    got = fetch_access_token(creds)
    assert got.access_token == "CACHED_AT"


def test_fetch_access_token_refreshes_when_stale(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / ".cache" / "sf-token.json"
    cache.parent.mkdir()
    now = time.time()
    tok = AccessToken(
        access_token="OLD_AT", instance_url="https://x",
        scope="mcp_api cdp_api", refresh_token="RT",
        issued_at=now - 3600, expires_at=now - 100,  # stale
    )
    cache.write_text(tok.model_dump_json())

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "NEW_AT",
        "instance_url": "https://x.my.salesforce.com",
        "scope": "mcp_api cdp_api refresh_token",
        # Note: no "refresh_token" in the response — we keep the old one.
    }
    mock_client.post.return_value = mock_resp

    creds = OAuthCredentials(
        client_id="CID", client_secret="S", login_url="https://login.salesforce.com",
    )
    got = fetch_access_token(creds, client=mock_client)
    assert got.access_token == "NEW_AT"
    # Cache should now hold NEW_AT
    reloaded = AccessToken.model_validate_json(cache.read_text())
    assert reloaded.access_token == "NEW_AT"
    assert reloaded.refresh_token == "RT"  # preserved from old


def test_fetch_access_token_raises_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    creds = OAuthCredentials(
        client_id="CID", client_secret="S", login_url="https://login.salesforce.com",
    )
    with pytest.raises(SfAuthError, match="No cached"):
        fetch_access_token(creds)


def test_clear_cached_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / ".cache" / "sf-token.json"
    cache.parent.mkdir()
    cache.write_text('{"access_token":"x","instance_url":"","issued_at":0,"expires_at":0}')
    assert cache.exists()
    clear_cached_token()
    assert not cache.exists()


def test_complete_pending_login_exchanges_and_signals(tmp_path, monkeypatch):
    """complete_pending_login should exchange the code, cache the token, and set the event."""
    monkeypatch.chdir(tmp_path)
    creds = OAuthCredentials(
        client_id="CID", client_secret="S",
        login_url="https://x.my.salesforce.com",
    )
    # Register a pending login manually (normally done by run_interactive_login).
    from token_compare.sf_auth import (
        _register_pending, complete_pending_login, AccessToken,
    )
    pending = _register_pending("STATE_X", creds, "VERIFIER_X")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "AT",
        "instance_url": "https://x.my.salesforce.com",
        "scope": "mcp_api cdp_api refresh_token",
        "refresh_token": "RT",
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp

    # Patch httpx.Client to return our mock (since _exchange_code constructs one internally)
    with patch("token_compare.sf_auth.httpx.Client", return_value=mock_client):
        mock_client.__enter__ = lambda self: mock_client
        mock_client.__exit__ = lambda self, *a: None
        result = complete_pending_login("STATE_X", "CODE_X")

    assert result.token is not None
    assert result.token.access_token == "AT"
    assert pending.event.is_set()


def test_complete_pending_login_unknown_state_raises():
    from token_compare.sf_auth import SfAuthError, complete_pending_login
    with pytest.raises(SfAuthError, match="no pending login"):
        complete_pending_login("UNKNOWN_STATE", "CODE")


def test_interactive_login_accepts_heroku_redirect(monkeypatch):
    creds = OAuthCredentials(
        client_id="cid", client_secret="csec",
        login_url="https://login.salesforce.com",
        redirect_uri="https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com/callback",
    )
    # We won't actually open a browser; verify only that the host check passes.
    # Stub _register_pending so the function gets past the guard.
    called = {}
    def fake_register(state, c, v):
        called["ok"] = True
        class _P:
            event = type("E", (), {"wait": lambda self, timeout=None: True})()
            error = "stubbed"
            token = None
        return _P()
    monkeypatch.setattr("token_compare.sf_auth._register_pending", fake_register)
    with pytest.raises(SfAuthError, match="stubbed"):
        run_interactive_login(creds, open_browser=False, timeout_s=0.1)
    assert called.get("ok") is True


def test_interactive_login_rejects_random_https(monkeypatch):
    creds = OAuthCredentials(
        client_id="cid", client_secret="csec",
        login_url="https://login.salesforce.com",
        redirect_uri="https://example.com/callback",
    )
    with pytest.raises(SfAuthError, match="not localhost or"):
        run_interactive_login(creds, open_browser=False, timeout_s=0.1)
