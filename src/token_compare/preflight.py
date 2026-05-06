from __future__ import annotations

import json as _json  # module-local alias so we don't collide with a caller's `json`
import subprocess
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from token_compare.sf_auth import SfAuthError, fetch_access_token, load_credentials_from_env


class PreflightResult(BaseModel):
    ok: bool
    checks: dict[str, bool]
    remediation: list[str]
    details: dict[str, str]


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return p.returncode, p.stdout, p.stderr


def _is_claude_logged_in(rc: int, out: str) -> bool:
    if rc != 0:
        return False
    text = out.strip()
    # New CLI: JSON output with {"loggedIn": true, ...}
    try:
        data = _json.loads(text)
        if isinstance(data, dict) and data.get("loggedIn") is True:
            return True
    except ValueError:
        pass
    # Older CLI: human-readable "Logged in as ..." line
    return "logged in" in text.lower()


def _summarize_claude_auth(out: str) -> str:
    text = out.strip()
    try:
        data = _json.loads(text)
        if isinstance(data, dict):
            # Keep only non-sensitive fields
            return _json.dumps({
                k: v for k, v in data.items()
                if k in {"loggedIn", "authMethod", "apiProvider", "account", "email"}
            })
    except ValueError:
        pass
    # Fallback: truncate but never include auth tokens (defensive — claude auth status
    # shouldn't include them, but truncation alone is not enough).
    return text[:200]


def _sf_has_org(rc: int, out: str) -> bool:
    if rc != 0:
        return False
    text = out.strip()
    if text in ("", "[]"):
        return False
    try:
        data = _json.loads(text)
    except ValueError:
        return False
    if isinstance(data, list):
        return len(data) > 0
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict):
            # sf v2: {"result": {"nonScratchOrgs": [...], "scratchOrgs": [...], "other": [...]}}
            return any(isinstance(v, list) and len(v) > 0 for v in result.values())
        if isinstance(result, list):
            return len(result) > 0
    return False


# Fields we refuse to surface to callers. Anything matching is dropped or redacted.
_SENSITIVE_ORG_FIELDS = {
    "accessToken", "refreshToken", "clientSecret", "privateKey",
    "password", "oauthToken", "sfdxAuthUrl",
}


def _redact_org(org: dict) -> dict:
    """Return a copy of an sf org record with sensitive credentials removed."""
    return {k: v for k, v in org.items() if k not in _SENSITIVE_ORG_FIELDS}


def _summarize_sf_orgs(out: str) -> str:
    """Return a short, non-sensitive string describing available orgs."""
    text = out.strip()
    if not text or text == "[]":
        return "none"
    try:
        data = _json.loads(text)
    except ValueError:
        return "parse error"

    orgs: list[dict] = []
    if isinstance(data, list):
        orgs = [o for o in data if isinstance(o, dict)]
    elif isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict):
            for v in result.values():
                if isinstance(v, list):
                    orgs.extend(o for o in v if isinstance(o, dict))
        elif isinstance(result, list):
            orgs = [o for o in result if isinstance(o, dict)]

    if not orgs:
        return "none"

    redacted = [_redact_org(o) for o in orgs]
    # Show just alias/username pairs — keep it short, non-sensitive.
    summaries = [
        f"{o.get('alias') or '(no alias)'} [{o.get('username', '?')}]"
        for o in redacted[:5]
    ]
    suffix = f" (+{len(redacted) - 5} more)" if len(redacted) > 5 else ""
    return ", ".join(summaries) + suffix


def check_environment(mcp_config_path: Optional[Path] = None) -> PreflightResult:
    checks: dict[str, bool] = {}
    remediation: list[str] = []
    details: dict[str, str] = {}

    # claude installed
    try:
        rc, out, _ = _run(["claude", "--version"])
        checks["claude_installed"] = rc == 0
        details["claude_version"] = out.strip()
        if rc != 0:
            remediation.append("Install Claude Code (https://claude.ai/code).")
    except FileNotFoundError:
        checks["claude_installed"] = False
        remediation.append("Install Claude Code: `claude` was not found on PATH.")

    # claude logged in
    if checks.get("claude_installed"):
        try:
            rc, out, _ = _run(["claude", "auth", "status"])
            logged_in = _is_claude_logged_in(rc, out)
            checks["claude_logged_in"] = logged_in
            details["claude_account"] = _summarize_claude_auth(out)
            if not logged_in:
                remediation.append("Run `claude login` to authenticate Claude Code.")
        except FileNotFoundError:
            checks["claude_logged_in"] = False
    else:
        checks["claude_logged_in"] = False

    # sf authenticated
    try:
        rc, out, _ = _run(["sf", "org", "list", "--json"])
        checks["sf_authenticated"] = _sf_has_org(rc, out)
        details["sf_orgs"] = _summarize_sf_orgs(out)
        if not checks["sf_authenticated"]:
            remediation.append("Authenticate Salesforce CLI: `sf org login web`.")
    except FileNotFoundError:
        checks["sf_authenticated"] = False
        remediation.append("Install Salesforce CLI (`sf`): https://developer.salesforce.com/tools/salesforcecli")

    # mcp config present
    mcp_cfg = Path(mcp_config_path) if mcp_config_path else Path("config/sf-mcp.json")
    checks["mcp_config_present"] = mcp_cfg.is_file()
    details["mcp_config_path"] = str(mcp_cfg)
    if not checks["mcp_config_present"]:
        remediation.append(
            f"Create MCP config at {mcp_cfg} pointing to the Salesforce-hosted MCP server."
        )

    # sf_oauth_reachable: verifies cached token or refresh token exists
    creds = load_credentials_from_env()
    if creds is None:
        checks["sf_oauth_reachable"] = False
        details["sf_oauth"] = "SF_CLIENT_ID / SF_CLIENT_SECRET / SF_LOGIN_URL not set"
        remediation.append(
            "Set SF_CLIENT_ID, SF_CLIENT_SECRET, SF_LOGIN_URL in .env.local "
            "so Path B (MCP) can authenticate."
        )
    else:
        try:
            tok = fetch_access_token(creds, timeout_s=10.0)
            checks["sf_oauth_reachable"] = True
            scope_str = tok.scope or "unknown"
            details["sf_oauth"] = f"cached token ok, scope={scope_str}"
        except SfAuthError as e:
            checks["sf_oauth_reachable"] = False
            details["sf_oauth"] = str(e)[:200]
            remediation.append(
                "No Salesforce access token. Click 'Connect Salesforce' in the UI "
                "(or POST /api/sf/login) to run the browser-based login flow."
            )

    return PreflightResult(
        ok=all(checks.values()),
        checks=checks,
        remediation=remediation,
        details=details,
    )
