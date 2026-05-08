from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from token_compare.sf_auth import load_credentials_from_env


class PreflightResult(BaseModel):
    ok: bool
    checks: dict[str, bool]
    remediation: list[str]
    details: dict[str, str]


def check_environment(mcp_config_path: Optional[Path] = None) -> PreflightResult:
    import os
    checks: dict[str, bool] = {}
    remediation: list[str] = []
    details: dict[str, str] = {}

    # Inference addon env present?
    from token_compare.inference_client import discover_models
    models = discover_models()
    checks["inference_models_present"] = len(models) >= 1
    details["inference_models"] = ", ".join(m.model_id for m in models) or "none"
    if not checks["inference_models_present"]:
        remediation.append(
            "No Heroku Inference addons detected. Attach at least one "
            "heroku-inference:claude-* addon to the app."
        )

    # Postgres reachable?
    try:
        from token_compare import db
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(db.connect())
            checks["postgres_reachable"] = True
            details["postgres"] = "ok"
        finally:
            loop.close()
    except Exception as e:
        checks["postgres_reachable"] = False
        details["postgres"] = str(e)[:200]
        remediation.append("Set DATABASE_URL (heroku-postgresql:essential-0).")

    # ECA env vars set?
    creds = load_credentials_from_env()
    if creds is None:
        checks["sf_eca_configured"] = False
        details["sf_eca"] = "SF_CLIENT_ID / SF_CLIENT_SECRET / SF_LOGIN_URL not set"
        remediation.append(
            "Set SF_CLIENT_ID, SF_CLIENT_SECRET, SF_LOGIN_URL as Heroku config vars."
        )
    else:
        checks["sf_eca_configured"] = True
        details["sf_eca"] = f"login_url={creds.login_url}"

    # mcp template present?
    mcp_cfg = Path(mcp_config_path) if mcp_config_path else Path("config/sf-mcp.json")
    checks["mcp_template_present"] = mcp_cfg.is_file()
    details["mcp_template_path"] = str(mcp_cfg)
    if not checks["mcp_template_present"]:
        remediation.append(f"Create MCP template at {mcp_cfg}.")

    # SESSION_SECRET set?
    checks["session_secret_set"] = bool(os.environ.get("SESSION_SECRET"))
    if not checks["session_secret_set"]:
        remediation.append("Set SESSION_SECRET to a 32-byte hex via heroku config:set.")

    return PreflightResult(
        ok=all(checks.values()),
        checks=checks,
        remediation=remediation,
        details=details,
    )
