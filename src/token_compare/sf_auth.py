from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel


# Salesforce ECA scope ceiling is ~5; keep this string short.
# mcp_api        — required for Platform MCP gateway acceptance.
# cdp_api        — blanket Data Cloud access (covers cdp_query_api, cdp_profile_api).
# refresh_token  — needed to mint refreshable access tokens.
SF_OAUTH_SCOPES = "mcp_api cdp_api refresh_token"

# ECA-registered redirect URI. Must match exactly.
DEFAULT_REDIRECT_URI = "http://localhost:8000/callback"

# Where we cache the refresh + access token between runs.
CACHE_PATH = Path(".cache/sf-token.json")

# Access tokens don't include an explicit expiry in PKCE responses. Be conservative.
ACCESS_TOKEN_TTL_SECONDS = 90 * 60  # 90 minutes (real tokens typically last ~2h)


class OAuthCredentials(BaseModel):
    client_id: str
    client_secret: str
    login_url: str
    redirect_uri: str = DEFAULT_REDIRECT_URI


class AccessToken(BaseModel):
    access_token: str
    instance_url: str
    scope: Optional[str] = None
    refresh_token: Optional[str] = None
    issued_at: float = 0.0  # epoch seconds
    expires_at: float = 0.0  # epoch seconds

    @property
    def is_fresh(self) -> bool:
        return time.time() < (self.expires_at - 60)  # 60s safety margin


class SfAuthError(RuntimeError):
    """Raised when OAuth operations fail."""


def load_credentials_from_env() -> Optional[OAuthCredentials]:
    cid = os.environ.get("SF_CLIENT_ID")
    csec = os.environ.get("SF_CLIENT_SECRET")
    url = os.environ.get("SF_LOGIN_URL")
    if not (cid and csec and url):
        return None
    redirect = os.environ.get("SF_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    return OAuthCredentials(
        client_id=cid, client_secret=csec, login_url=url, redirect_uri=redirect,
    )


def _generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge). verifier is 43-char base64url; challenge is S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _save_cache(token: AccessToken) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(token.model_dump_json(), encoding="utf-8")
    try:
        os.chmod(CACHE_PATH, 0o600)
    except OSError:
        pass


def _load_cache() -> Optional[AccessToken]:
    if not CACHE_PATH.is_file():
        return None
    try:
        return AccessToken.model_validate_json(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_cached_token() -> None:
    """Delete the cached token. Useful when the user wants to log in again."""
    try:
        CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _refresh_access_token(
    creds: OAuthCredentials,
    refresh_token: str,
    *,
    client: Optional[httpx.Client] = None,
) -> AccessToken:
    url = creds.login_url.rstrip("/") + "/services/oauth2/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
    }
    owned = client is None
    if owned:
        client = httpx.Client(timeout=15.0)
    try:
        resp = client.post(url, data=data)
    except httpx.RequestError as e:
        raise SfAuthError(f"network error refreshing token: {e}") from e
    finally:
        if owned:
            client.close()

    if resp.status_code >= 400:
        try:
            body = resp.json()
            msg = body.get("error_description") or body.get("error") or resp.text
        except Exception:
            msg = resp.text[:300]
        raise SfAuthError(f"OAuth refresh {resp.status_code}: {msg}")

    body = resp.json()
    now = time.time()
    return AccessToken(
        access_token=body["access_token"],
        instance_url=body.get("instance_url", ""),
        scope=body.get("scope"),
        # Salesforce may or may not return a new refresh_token on refresh; keep old if absent.
        refresh_token=body.get("refresh_token", refresh_token),
        issued_at=now,
        expires_at=now + ACCESS_TOKEN_TTL_SECONDS,
    )


def fetch_access_token(
    creds: OAuthCredentials,
    *,
    timeout_s: float = 15.0,  # kept for API compatibility; no longer used here
    client: Optional[httpx.Client] = None,
) -> AccessToken:
    """
    Return a usable access token. Strategy:
    1. Load cached token. If fresh, return it as-is.
    2. If stale but has refresh_token, refresh and cache.
    3. Otherwise raise SfAuthError("login required") — caller should run run_interactive_login.

    NEVER silently falls back to client_credentials. Salesforce Platform MCP
    endpoints reject client_credentials-minted tokens.
    """
    cached = _load_cache()
    if cached and cached.is_fresh:
        return cached
    if cached and cached.refresh_token:
        refreshed = _refresh_access_token(creds, cached.refresh_token, client=client)
        _save_cache(refreshed)
        return refreshed
    raise SfAuthError(
        "No cached Salesforce token. Run interactive login from the UI "
        "(POST /api/sf/login) to authorize via browser."
    )


# ---------------------------------------------------------------------------
# Interactive login (PKCE)
# ---------------------------------------------------------------------------

# Module-level: pending OAuth login attempts keyed by state.
# Each entry holds the creds+verifier needed to complete the exchange when the
# browser redirects back to /callback, and an Event the waiter blocks on.
_pending_logins: dict[str, "_PendingLogin"] = {}
_pending_lock = threading.Lock()


class _PendingLogin:
    def __init__(self, creds: OAuthCredentials, verifier: str) -> None:
        self.creds = creds
        self.verifier = verifier
        self.token: Optional[AccessToken] = None
        self.error: Optional[str] = None
        self.event = threading.Event()


def _register_pending(state: str, creds: OAuthCredentials, verifier: str) -> _PendingLogin:
    pending = _PendingLogin(creds, verifier)
    with _pending_lock:
        _pending_logins[state] = pending
    return pending


def _pop_pending(state: str) -> Optional[_PendingLogin]:
    with _pending_lock:
        return _pending_logins.pop(state, None)


def complete_pending_login(state: str, code: str) -> _PendingLogin:
    """
    Called by the /callback route after the browser redirect completes.
    Exchanges `code` for tokens, writes the cache, signals the waiter.
    Returns the _PendingLogin (so the route can render a success/failure page).
    """
    pending = _pop_pending(state)
    if pending is None:
        raise SfAuthError(f"no pending login for state {state!r}")
    try:
        tok = _exchange_code(pending.creds, code, pending.verifier)
        _save_cache(tok)
        pending.token = tok
    except SfAuthError as e:
        pending.error = str(e)
        raise
    finally:
        pending.event.set()
    return pending


def complete_pending_login_error(state: str, error: str) -> None:
    """Called by the /callback route when the provider returned an error."""
    pending = _pop_pending(state)
    if pending is None:
        return
    pending.error = error
    pending.event.set()


def _build_authorize_url(creds: OAuthCredentials, state: str, challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": creds.client_id,
        "redirect_uri": creds.redirect_uri,
        "state": state,
        "scope": SF_OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
    }
    return (
        creds.login_url.rstrip("/")
        + "/services/oauth2/authorize?"
        + urllib.parse.urlencode(params)
    )


def _exchange_code(
    creds: OAuthCredentials,
    code: str,
    verifier: str,
    *,
    client: Optional[httpx.Client] = None,
) -> AccessToken:
    url = creds.login_url.rstrip("/") + "/services/oauth2/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "redirect_uri": creds.redirect_uri,
        "code_verifier": verifier,
    }
    owned = client is None
    if owned:
        client = httpx.Client(timeout=15.0)
    try:
        resp = client.post(url, data=data)
    except httpx.RequestError as e:
        raise SfAuthError(f"network error exchanging code: {e}") from e
    finally:
        if owned:
            client.close()

    if resp.status_code >= 400:
        try:
            body = resp.json()
            msg = body.get("error_description") or body.get("error") or resp.text
        except Exception:
            msg = resp.text[:300]
        raise SfAuthError(f"OAuth code exchange {resp.status_code}: {msg}")

    body = resp.json()
    now = time.time()
    return AccessToken(
        access_token=body["access_token"],
        instance_url=body.get("instance_url", ""),
        scope=body.get("scope"),
        refresh_token=body.get("refresh_token"),
        issued_at=now,
        expires_at=now + ACCESS_TOKEN_TTL_SECONDS,
    )


def run_interactive_login(
    creds: OAuthCredentials,
    *,
    open_browser: bool = True,
    timeout_s: float = 180.0,
) -> AccessToken:
    """
    Run OAuth 2.1 + PKCE authorization_code flow.

    The /callback route on the FastAPI server completes the flow when the
    browser redirects back — this function just generates the state, opens
    the browser, and blocks waiting for the route to signal completion.
    """
    redirect = urllib.parse.urlparse(creds.redirect_uri)
    if redirect.hostname not in {"localhost", "127.0.0.1"}:
        raise SfAuthError(
            f"redirect_uri {creds.redirect_uri} is not localhost; refusing to "
            "run interactive login (callback cannot be served)."
        )

    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(24)
    auth_url = _build_authorize_url(creds, state, challenge)
    pending = _register_pending(state, creds, verifier)

    if open_browser:
        webbrowser.open(auth_url)

    completed = pending.event.wait(timeout=timeout_s)
    if not completed:
        # Best-effort cleanup
        _pop_pending(state)
        raise SfAuthError(
            f"timed out waiting for OAuth callback after {timeout_s}s; "
            "did you complete the browser login?"
        )
    if pending.error:
        raise SfAuthError(f"OAuth callback error: {pending.error}")
    if not pending.token:
        raise SfAuthError("OAuth callback completed without a token")
    return pending.token
