from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from fastapi import UploadFile, File

from token_compare.analysis import build_comparison, explain_comparison, extract_trace
from token_compare.benchmark import BenchmarkOptions, ProgressEvent, run_benchmark
from token_compare.preflight import check_environment
from token_compare.report import default_report_path, write_markdown
from token_compare.report_loader import (
    list_reports, load_json_report, load_markdown_report,
)
from token_compare.models import Scenario, SuccessCriteria
from token_compare.scenarios import load_all, load_all_from_db, seed_from_yaml_if_empty


# Pending OAuth round-trips (state → session_id, PKCE verifier) are
# persisted in Postgres via db.put_pending_login / db.pop_pending_login,
# so a dyno restart between /api/sf/login and /callback doesn't drop
# the verifier. The previous in-memory dict is intentionally gone.


def _load_dotenv_if_present() -> None:
    """Best-effort load of .env.local into os.environ, if present.

    No external dependency — simple parser supporting KEY=value and
    KEY='value' / KEY="value". Ignores blank lines and lines starting with #.
    Does not overwrite variables already set in the environment.
    """
    candidate = Path(".env.local")
    if not candidate.is_file():
        return
    for line in candidate.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith("'") and value.endswith("'")) or \
           (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


class AppConfig(BaseModel):
    scenarios_dir: Path = Path("scenarios")
    mcp_config_path: Path = Path("config/sf-mcp.json")
    reports_dir: Path = Path("reports")
    static_dir: Optional[Path] = Path("static")
    reports_retain: int = 10


def _coerce_int(v, default: int) -> int:
    """Pydantic 422s on null/NaN, but the SPA's parseInt() can produce
    those when an input field is empty or missing. Treat anything
    non-positive-integer as the default so the run starts."""
    try:
        n = int(v)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


class RunRequest(BaseModel):
    # The SPA's select-all logic can include the master checkbox in its
    # query, which has no data-sid → serializes as null. Accept None
    # entries here and filter them out in the handler.
    scenario_ids: list[Optional[str]] = []
    runs_per_path: Optional[int] = None
    model: Optional[str] = None
    # Operator + org_name are descriptive labels stored on the report row
    # only — no semantic dependence. Default to placeholders so a
    # request that forgets to include them doesn't 422; the SPA sends
    # real values but we shouldn't punish a stale frontend that doesn't.
    operator: str = "(unknown)"
    org_name: str = "(unknown)"
    max_turns: Optional[int] = None
    timeout_s: Optional[int] = None

    def resolved_runs_per_path(self) -> int:
        return _coerce_int(self.runs_per_path, 3)

    def resolved_max_turns(self) -> int:
        return _coerce_int(self.max_turns, 15)

    def resolved_timeout_s(self) -> int:
        return _coerce_int(self.timeout_s, 300)

    def resolved_model(self) -> str:
        return self.model or "claude-4-5-sonnet"


class ScenarioPayload(BaseModel):
    """Body of POST/PUT /api/admin/scenarios. Defined at module scope
    (not inside create_app) so Pydantic v2's TypeAdapter can resolve
    the forward-reference annotations FastAPI generates when the
    `from __future__ import annotations` style is in use."""
    id: str
    title: str
    category: str
    difficulty: str = "medium"  # simple | medium | complex
    prompt: str
    expected_operations: list[str] = []
    success_criteria: dict = {"must_contain": []}
    notes: str = ""
    is_active: bool = True


class FreeformRunRequest(BaseModel):
    prompt: str
    title: Optional[str] = None
    # Optional client-provided scenario id — lets the frontend show the
    # stepper dot as soon as the user clicks Run, before the POST returns.
    # Sanitized server-side so we don't accept arbitrary IDs.
    scenario_id: Optional[str] = None
    runs_per_path: Optional[int] = None
    model: Optional[str] = None
    operator: str = "local user"
    org_name: str = "(local org)"
    max_turns: Optional[int] = None
    timeout_s: Optional[int] = None

    def resolved_runs_per_path(self) -> int:
        return _coerce_int(self.runs_per_path, 1)

    def resolved_model(self) -> str:
        return self.model or "claude-4-5-sonnet"

    def resolved_max_turns(self) -> int:
        return _coerce_int(self.max_turns, 30)

    def resolved_timeout_s(self) -> int:
        return _coerce_int(self.timeout_s, 600)


def _event_to_dict(e: ProgressEvent) -> dict:
    d: dict = {"kind": e.kind}
    if e.scenario_id: d["scenario_id"] = e.scenario_id
    if e.path: d["path"] = e.path.value
    if e.run_index is not None: d["run_index"] = e.run_index
    if e.total_runs is not None: d["total_runs"] = e.total_runs
    if e.run_result is not None:
        d["run_result"] = e.run_result.model_dump(exclude={"raw_json"})
    return d


def _payload_to_stats(payload: dict) -> dict:
    """Derive the per-row analytics stats the reports table renders:
    kind, scenario_count, runs_per_path, native_cost, mcp_cost,
    mcp_native_ratio. Tier A also surfaces success rates + aggregate
    cache-hit ratios so the table can flag noisy runs at a glance.
    Robust against partial / older payload shapes (returns zeros where
    the data isn't there)."""
    if not isinstance(payload, dict):
        return {}
    scenarios = payload.get("scenarios") or []
    runs_per_path = payload.get("runs_per_path") or 0
    native_cost = 0.0
    mcp_cost = 0.0
    has_freeform = False
    nat_ok = nat_total = mcp_ok = mcp_total = 0
    nat_in_total = nat_cache_read = 0
    mcp_in_total = mcp_cache_read = 0
    for s in scenarios:
        if not isinstance(s, dict):
            continue
        sid = s.get("scenario_id") or ""
        if sid.startswith("freeform_"):
            has_freeform = True
        for r in (s.get("native_runs") or []):
            nat_total += 1
            if r.get("succeeded"):
                nat_ok += 1
            try:
                native_cost += float(r.get("total_cost_usd") or 0)
            except (TypeError, ValueError):
                pass
            nat_in_total += int(r.get("input_tokens") or 0) \
                + int(r.get("cache_read_input_tokens") or 0) \
                + int(r.get("cache_creation_input_tokens") or 0)
            nat_cache_read += int(r.get("cache_read_input_tokens") or 0)
        for r in (s.get("mcp_runs") or []):
            mcp_total += 1
            if r.get("succeeded"):
                mcp_ok += 1
            try:
                mcp_cost += float(r.get("total_cost_usd") or 0)
            except (TypeError, ValueError):
                pass
            mcp_in_total += int(r.get("input_tokens") or 0) \
                + int(r.get("cache_read_input_tokens") or 0) \
                + int(r.get("cache_creation_input_tokens") or 0)
            mcp_cache_read += int(r.get("cache_read_input_tokens") or 0)
    ratio = (mcp_cost / native_cost) if native_cost > 0 else None
    # 'freeform' if every scenario in the payload is a freeform_*; 'mixed'
    # if both kinds present; 'catalog' otherwise. The common case is
    # one or the other.
    only_freeform = has_freeform and all(
        (s.get("scenario_id") or "").startswith("freeform_")
        for s in scenarios if isinstance(s, dict)
    )
    if only_freeform:
        kind = "freeform"
    elif has_freeform:
        kind = "mixed"
    else:
        kind = "catalog"
    nat_cache = (nat_cache_read / nat_in_total) if nat_in_total > 0 else 0.0
    mcp_cache = (mcp_cache_read / mcp_in_total) if mcp_in_total > 0 else 0.0
    return {
        "kind": kind,
        "scenario_count": len(scenarios),
        "runs_per_path": runs_per_path,
        "native_cost": round(native_cost, 6),
        "mcp_cost": round(mcp_cost, 6),
        "mcp_native_ratio": round(ratio, 3) if ratio is not None else None,
        # Tier A — success rate + cache-hit ratio per path.
        "native_success": nat_ok,
        "native_total": nat_total,
        "mcp_success": mcp_ok,
        "mcp_total": mcp_total,
        "native_cache_hit_ratio": round(nat_cache, 3),
        "mcp_cache_hit_ratio": round(mcp_cache, 3),
    }


def _prune_reports(reports_dir: Path, retain: int) -> None:
    files = sorted(reports_dir.glob("benchmark-*.md"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[retain:]:
        old.unlink(missing_ok=True)
        # Also drop the JSON sidecar so we don't leak orphaned reload data.
        old.with_suffix(".json").unlink(missing_ok=True)


def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI(title="Token Comparison Tool")

    @app.on_event("startup")
    async def _startup() -> None:
        from token_compare import db
        await db.connect()
        await db.migrate()
        # On first boot the scenarios table is empty — import the
        # YAML catalog so the existing 6 scenarios are immediately
        # available. Idempotent: no-op once the table has rows.
        try:
            inserted = await seed_from_yaml_if_empty(config.scenarios_dir)
            if inserted:
                import logging
                logging.getLogger(__name__).info(
                    "seeded %d scenarios from %s into the scenarios table",
                    inserted, config.scenarios_dir,
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "scenario seed failed (will retry next boot): %s", e,
            )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        from token_compare import db
        await db.close()

    # Log the raw body of any 422 so we can see exactly which field
    # the SPA mis-shaped. Uvicorn's default access log doesn't show
    # request bodies, which makes triaging client-side serialization
    # bugs guesswork. Always-on, but the response shape is unchanged.
    from fastapi.exceptions import RequestValidationError
    import logging
    _validation_log = logging.getLogger("token_compare.validation")

    @app.exception_handler(RequestValidationError)
    async def _log_validation_error(request: Request, exc: RequestValidationError):
        try:
            body = await request.body()
            preview = body.decode("utf-8", errors="replace")[:1000]
        except Exception:
            preview = "(could not read body)"
        _validation_log.warning(
            "422 on %s %s | errors=%s | body=%s",
            request.method, request.url.path, exc.errors(), preview,
        )
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    async def _get_or_create_sid_with_cookie(request: Request) -> tuple[str, Optional[tuple[str, str]]]:
        """Return (session_id, cookie_to_set_or_None).

        If the request already carries a valid signed cookie, returns
        (verified_sid, None). Otherwise creates a new session row and
        returns the new sid plus the (cookie_name, signed_value) the
        caller should attach to their response.
        """
        from token_compare import db
        from token_compare.sessions import (
            COOKIE_NAME, sign_session_id, verify_session_id, BadSignature,
        )
        signed = request.cookies.get(COOKIE_NAME)
        if signed:
            try:
                return verify_session_id(signed), None
            except BadSignature:
                pass
        sid = await db.create_session()
        return sid, (COOKIE_NAME, sign_session_id(sid))

    # Track in-memory benchmark state for polling fallback
    _current_run: dict = {"active": False, "events": [], "started_at": None, "report_path": None}

    # Browsers will happily cache JSON GETs that don't say otherwise, and
    # the SPA polls these on every page load — including across deploys
    # that may have changed the response shape (e.g. when preflight.py
    # was rewritten for Heroku). Always send no-store on the introspective
    # endpoints so stale dyno state never sticks in a client.
    _NO_STORE = {"Cache-Control": "no-store"}

    @app.get("/api/preflight")
    def preflight() -> JSONResponse:
        body = check_environment(mcp_config_path=config.mcp_config_path).model_dump()
        return JSONResponse(body, headers=_NO_STORE)

    @app.get("/api/sf/status")
    async def sf_status(request: Request) -> JSONResponse:
        """Has the current browser session completed the SF OAuth flow?
        The SPA hits this on load to decide whether to show the login
        splash or the home chooser. {logged_in: bool, instance_url?: str}."""
        from token_compare import db
        from token_compare.sessions import (
            COOKIE_NAME, verify_session_id, BadSignature,
        )
        signed = request.cookies.get(COOKIE_NAME)
        if not signed:
            return JSONResponse({"logged_in": False}, headers=_NO_STORE)
        try:
            sid = verify_session_id(signed)
        except BadSignature:
            return JSONResponse({"logged_in": False}, headers=_NO_STORE)
        token = await db.get_sf_token(sid)
        if not token:
            return JSONResponse({"logged_in": False}, headers=_NO_STORE)
        return JSONResponse(
            {"logged_in": True, "instance_url": token.get("instance_url", "")},
            headers=_NO_STORE,
        )

    @app.get("/api/scenarios")
    async def list_scenarios() -> JSONResponse:
        body = [s.model_dump() for s in await load_all_from_db()]
        return JSONResponse(body, headers=_NO_STORE)

    @app.get("/api/models")
    def list_models() -> JSONResponse:
        from token_compare.inference_client import discover_models
        body = {"models": [m.model_id for m in discover_models()]}
        return JSONResponse(body, headers=_NO_STORE)

    # ─── Admin endpoints (gated by SF OAuth session) ───────────────────
    #
    # These power the /admin scenario CRUD UI. Auth model: any browser
    # session that has completed the Salesforce OAuth flow (i.e. has an
    # SF token in its sessions row) is allowed to call admin endpoints.
    # Same gate as the rest of the app — login = full access.

    async def _get_fresh_sf_token(sid: str) -> Optional[dict]:
        """Look up the SF token for `sid`, refresh it if expired, write
        the refreshed token back to the session row, and return the dict.

        Returns None if no token exists or refresh fails. Callers that
        need the token (run, freeform, MCP gateway init) all go through
        this helper so a stale token doesn't reach the runner — the MCP
        gateway is stricter than the Data API and 401s on expired tokens
        even when /services/data/vXX would still accept them."""
        from token_compare import db
        from token_compare.sf_auth import (
            AccessToken, SfAuthError, _refresh_access_token,
            load_credentials_from_env,
        )
        token = await db.get_sf_token(sid)
        if not token:
            return None
        try:
            access = AccessToken.model_validate(token)
        except Exception:
            # Old/garbled session row — treat as missing.
            return None
        if access.is_fresh:
            return token
        if not access.refresh_token:
            return None  # nothing to refresh with → caller must re-auth
        creds = load_credentials_from_env()
        if creds is None:
            return None
        try:
            refreshed = _refresh_access_token(creds, access.refresh_token)
        except SfAuthError:
            return None
        refreshed_dict = refreshed.model_dump()
        await db.put_sf_token(sid, refreshed_dict)
        return refreshed_dict

    async def _require_sf_session(request: Request) -> Optional[JSONResponse]:
        """Returns None if the request carries a logged-in SF session
        cookie; otherwise an error JSONResponse the caller should return
        immediately."""
        from token_compare import db
        from token_compare.sessions import (
            COOKIE_NAME, verify_session_id, BadSignature,
        )
        signed = request.cookies.get(COOKIE_NAME)
        if not signed:
            return JSONResponse(
                {"error": "Salesforce login required"}, status_code=401,
            )
        try:
            sid = verify_session_id(signed)
        except BadSignature:
            return JSONResponse(
                {"error": "Salesforce login required"}, status_code=401,
            )
        token = await db.get_sf_token(sid)
        if not token:
            return JSONResponse(
                {"error": "Salesforce login required"}, status_code=401,
            )
        return None

    @app.get("/api/admin/scenarios")
    async def admin_list_scenarios(request: Request):
        guard = await _require_sf_session(request)
        if guard is not None:
            return guard
        from token_compare import db
        rows = await db.list_scenarios(include_inactive=True)
        # Convert datetimes for JSON.
        from datetime import datetime as _dt
        for r in rows:
            for k in ("created_at", "updated_at"):
                if isinstance(r.get(k), _dt):
                    r[k] = r[k].isoformat()
        return JSONResponse({"scenarios": rows}, headers=_NO_STORE)

    @app.post("/api/admin/scenarios")
    async def admin_create_scenario(
        request: Request,
        payload: ScenarioPayload = Body(...),
    ):
        guard = await _require_sf_session(request)
        if guard is not None:
            return guard
        from token_compare import db
        # Reject if id already exists — admin should use PUT to edit.
        existing = await db.get_scenario(payload.id)
        if existing:
            return JSONResponse(
                {"error": f"scenario {payload.id!r} already exists; use PUT to edit"},
                status_code=409,
            )
        await db.upsert_scenario(
            id=payload.id, title=payload.title, category=payload.category,
            difficulty=payload.difficulty, prompt=payload.prompt,
            expected_operations=payload.expected_operations,
            success_criteria=payload.success_criteria,
            notes=payload.notes, is_active=payload.is_active,
        )
        return {"ok": True, "id": payload.id}

    @app.put("/api/admin/scenarios/{scenario_id}")
    async def admin_update_scenario(
        scenario_id: str,
        request: Request,
        payload: ScenarioPayload = Body(...),
    ):
        guard = await _require_sf_session(request)
        if guard is not None:
            return guard
        # The path id wins over the body id so the URL is the canonical
        # reference. The body id (if different) is silently ignored.
        from token_compare import db
        existing = await db.get_scenario(scenario_id)
        if not existing:
            return JSONResponse(
                {"error": f"scenario {scenario_id!r} not found"},
                status_code=404,
            )
        await db.upsert_scenario(
            id=scenario_id, title=payload.title, category=payload.category,
            difficulty=payload.difficulty, prompt=payload.prompt,
            expected_operations=payload.expected_operations,
            success_criteria=payload.success_criteria,
            notes=payload.notes, is_active=payload.is_active,
        )
        return {"ok": True, "id": scenario_id}

    @app.delete("/api/admin/scenarios/{scenario_id}")
    async def admin_soft_delete_scenario(scenario_id: str, request: Request):
        """Soft-delete: set is_active=false. Historical reports
        referencing this scenario_id still resolve title/category."""
        guard = await _require_sf_session(request)
        if guard is not None:
            return guard
        from token_compare import db
        ok = await db.set_scenario_active(scenario_id, is_active=False)
        if not ok:
            return JSONResponse(
                {"error": f"scenario {scenario_id!r} not found"},
                status_code=404,
            )
        return {"ok": True, "id": scenario_id, "is_active": False}

    @app.post("/api/admin/scenarios/{scenario_id}/restore")
    async def admin_restore_scenario(scenario_id: str, request: Request):
        guard = await _require_sf_session(request)
        if guard is not None:
            return guard
        from token_compare import db
        ok = await db.set_scenario_active(scenario_id, is_active=True)
        if not ok:
            return JSONResponse(
                {"error": f"scenario {scenario_id!r} not found"},
                status_code=404,
            )
        return {"ok": True, "id": scenario_id, "is_active": True}

    def _start_benchmark_stream(
        picked_scenarios: list[Scenario],
        options: BenchmarkOptions,
        *,
        report_id: str,
        freeform_scenario: Optional[Scenario] = None,
    ) -> StreamingResponse:
        """Run a benchmark over `picked_scenarios` and stream progress as SSE.

        Used by both /api/run (catalog scenarios) and /api/run/freeform
        (single ad-hoc scenario). Caches the result so the same trace /
        summary / report endpoints work for either entry point.
        """
        queue: asyncio.Queue = asyncio.Queue()

        _current_run["active"] = True
        _current_run["events"] = []
        _current_run["started_at"] = datetime.now(timezone.utc).isoformat()
        _current_run["report_path"] = None
        _current_run["result_data"] = None
        # Freeform scenarios aren't on disk, so stash the synthesized
        # Scenario so /trace and /summary can resolve title/category.
        _current_run["freeform_scenario"] = (
            freeform_scenario.model_dump() if freeform_scenario else None
        )

        def on_progress(e: ProgressEvent) -> None:
            evt = _event_to_dict(e)
            queue.put_nowait(evt)
            _current_run["events"].append(evt)

        async def runner_task():
            try:
                loop = asyncio.get_running_loop()
                from token_compare import db as _db

                # Wrap on_progress to also INSERT each run row as it completes.
                original_on_progress = on_progress

                def db_on_progress(e):
                    original_on_progress(e)
                    if e.kind == "run_complete" and e.run_result is not None:
                        # Best-effort fire-and-forget insert. We schedule it on
                        # the loop because run_benchmark is in an executor.
                        asyncio.run_coroutine_threadsafe(
                            _db.insert_run(
                                report_id=report_id,
                                scenario_id=e.scenario_id,
                                path=e.path.value,
                                run_index=e.run_index,
                                result=e.run_result.model_dump(),
                            ),
                            loop,
                        )

                result = await loop.run_in_executor(
                    None, lambda: run_benchmark(picked_scenarios, options, db_on_progress),
                )

                await _db.finalize_report(report_id, payload=result.model_dump())
                _current_run["result_data"] = result.model_dump()
                _current_run["report_path"] = report_id
                queue.put_nowait({"kind": "report_written", "report_id": report_id})
            except Exception as e:
                err_evt = {
                    "kind": "error",
                    "message": f"{type(e).__name__}: {e}",
                }
                queue.put_nowait(err_evt)
                # Also stash on the polling cache so /api/run/status surfaces
                # the failure to clients that lost the SSE stream.
                _current_run["events"].append(err_evt)
            finally:
                _current_run["active"] = False
                queue.put_nowait(None)

        async def event_stream():
            task = asyncio.create_task(runner_task())
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
            await task

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/run")
    async def run(req: RunRequest, request: Request) -> Response:
        from token_compare import db
        sid, cookie = await _get_or_create_sid_with_cookie(request)
        # Refresh the SF token if it's expired before handing it to the
        # runner — the MCP gateway 401s on stale tokens that the Data
        # API would still accept, and that asymmetry caused the
        # mcp_init_failed errors users were seeing 13+ hours after login.
        sf_token = await _get_fresh_sf_token(sid)
        if not sf_token:
            resp = JSONResponse(
                {"error": "Salesforce login required"}, status_code=401,
            )
            if cookie:
                resp.set_cookie(cookie[0], cookie[1], httponly=True,
                                secure=True, samesite="lax",
                                max_age=60 * 60 * 24 * 30)
            return resp

        all_scenarios = await load_all_from_db()
        # Filter out None/empty entries the SPA may have submitted (e.g. a
        # master checkbox without data-sid).
        wanted = {sid for sid in (req.scenario_ids or []) if sid}
        picked = [s for s in all_scenarios if s.id in wanted]
        if not picked:
            picked = all_scenarios

        model = req.resolved_model()
        report_id = await db.create_report(
            model=model, operator=req.operator, org_name=req.org_name,
        )

        options = BenchmarkOptions(
            model=model,
            max_turns=req.resolved_max_turns(),
            timeout_s=req.resolved_timeout_s(),
            runs_per_path=req.resolved_runs_per_path(),
            mcp_template_path=config.mcp_config_path,
            operator=req.operator, org_name=req.org_name,
            sf_token=sf_token,
        )
        stream = _start_benchmark_stream(picked, options, report_id=report_id)
        if cookie:
            stream.set_cookie(cookie[0], cookie[1], httponly=True,
                              secure=True, samesite="lax",
                              max_age=60 * 60 * 24 * 30)
        return stream

    @app.post("/api/run/freeform")
    async def run_freeform(req: FreeformRunRequest, request: Request) -> Response:
        from token_compare import db
        sid, cookie = await _get_or_create_sid_with_cookie(request)
        sf_token = await _get_fresh_sf_token(sid)
        if not sf_token:
            resp = JSONResponse(
                {"error": "Salesforce login required"}, status_code=401,
            )
            if cookie:
                resp.set_cookie(cookie[0], cookie[1], httponly=True,
                                secure=True, samesite="lax",
                                max_age=60 * 60 * 24 * 30)
            return resp

        prompt = (req.prompt or "").strip()
        if not prompt:
            return JSONResponse({"error": "prompt is required"}, status_code=400)
        # Sanitize a client-provided id; otherwise generate from timestamp.
        # Pattern: must start with "freeform_" and contain only safe chars,
        # so we can use it in URL paths and filenames without escaping.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        sid_param = (req.scenario_id or "").strip()
        import re as _re
        if sid_param and _re.fullmatch(r"freeform_[A-Za-z0-9_\-]{1,64}", sid_param):
            scenario_id = sid_param
        else:
            scenario_id = f"freeform_{ts}"
        scenario = Scenario(
            id=scenario_id,
            title=(req.title or "Free-format scenario").strip(),
            category="freeform",
            difficulty="medium",
            prompt=prompt,
            success_criteria=SuccessCriteria(),
        )

        model = req.resolved_model()
        report_id = await db.create_report(
            model=model, operator=req.operator, org_name=req.org_name,
        )

        options = BenchmarkOptions(
            model=model,
            max_turns=req.resolved_max_turns(),
            timeout_s=req.resolved_timeout_s(),
            runs_per_path=req.resolved_runs_per_path(),
            mcp_template_path=config.mcp_config_path,
            operator=req.operator, org_name=req.org_name,
            sf_token=sf_token,
        )
        stream = _start_benchmark_stream(
            [scenario], options, report_id=report_id, freeform_scenario=scenario,
        )
        if cookie:
            stream.set_cookie(cookie[0], cookie[1], httponly=True,
                              secure=True, samesite="lax",
                              max_age=60 * 60 * 24 * 30)
        return stream

    @app.get("/api/run/status")
    def run_status() -> JSONResponse:
        body = {
            "active": _current_run["active"],
            "started_at": _current_run["started_at"],
            "events": _current_run["events"],
            "report_path": _current_run["report_path"],
            "freeform_scenario": _current_run.get("freeform_scenario"),
        }
        return JSONResponse(body, headers=_NO_STORE)

    @app.api_route("/api/reports/latest", methods=["GET", "HEAD"])
    def latest_report(request: Request):
        files = sorted(config.reports_dir.glob("benchmark-*.md"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return JSONResponse({"path": None, "content": None})
        latest = files[0]
        return FileResponse(latest, media_type="text/markdown", filename=latest.name)

    async def _ensure_latest_in_cache() -> Optional[dict]:
        """Return the latest run's BenchmarkResult dict, populating the
        in-memory cache from Postgres if a dyno restart wiped it.
        Skips in-progress / aborted rows (payload_json IS NULL).
        None if no finalized report exists yet."""
        if _current_run.get("result_data"):
            return _current_run["result_data"]
        from token_compare import db
        rows = await db.list_reports(limit=1, finalized_only=True)
        if not rows:
            return None
        rec = await db.get_report(rows[0]["id"])
        if not rec or not rec.get("payload_json"):
            return None
        from token_compare.models import BenchmarkResult
        result = BenchmarkResult.model_validate(rec["payload_json"])
        _current_run["active"] = False
        _current_run["events"] = []
        _current_run["started_at"] = result.started_at
        _current_run["report_path"] = rows[0]["id"]
        _current_run["result_data"] = result.model_dump()
        _current_run.setdefault("freeform_scenario", None)
        return _current_run["result_data"]

    @app.get("/api/reports/latest/data")
    async def latest_report_data():
        """Return the latest benchmark's data as JSON. Reads the in-memory
        cache first; falls back to Postgres for runs that completed before
        a dyno restart wiped the cache."""
        result_data = await _ensure_latest_in_cache()
        if result_data is None:
            return JSONResponse({"scenarios": []}, status_code=404)
        data = dict(result_data)
        data["freeform_scenario"] = _current_run.get("freeform_scenario")
        return JSONResponse(data, headers=_NO_STORE)

    @app.get("/api/reports/latest/summary")
    async def latest_summary():
        from token_compare.analysis import build_summary_analysis
        result_data = await _ensure_latest_in_cache()
        if not result_data:
            return JSONResponse({"error": "no benchmark cached"}, status_code=404)
        # Pull from DB (active + inactive both — historical reports may
        # reference soft-deleted scenarios and we still want their title).
        from token_compare import db as _db
        all_db_scenarios = await _db.list_scenarios(include_inactive=True)
        scenarios_meta = {
            s["id"]: {"title": s["title"], "category": s["category"], "difficulty": s["difficulty"]}
            for s in all_db_scenarios
        }
        # Freeform scenarios aren't on disk; merge their meta from the run cache.
        ff = _current_run.get("freeform_scenario")
        if ff:
            scenarios_meta[ff["id"]] = {
                "title": ff.get("title"),
                "category": ff.get("category"),
                "difficulty": ff.get("difficulty"),
            }
        analysis = build_summary_analysis(result_data, scenarios_meta)
        return JSONResponse(analysis.model_dump(), headers=_NO_STORE)

    @app.get("/api/reports")
    async def list_saved_reports(limit: int = 50) -> JSONResponse:
        """List saved reports newest-first with derived per-row stats
        the analytics view needs: kind, scenario_count, runs_per_path,
        native_cost, mcp_cost, mcp_native_ratio. Pulls payload_json once
        per row server-side instead of forcing the SPA to do N+1 fetches."""
        from token_compare import db
        from datetime import datetime
        # Cap so a runaway client can't blow up the dyno.
        limit = max(1, min(int(limit or 50), 200))
        rows = await db.list_reports(limit=limit)
        # We need payload_json for the stats; pull each by id. The
        # full-list query intentionally doesn't include the JSONB to
        # keep that endpoint cheap, so we hydrate here. Cheap given the
        # cap above and Postgres' jsonb storage.
        reports = []
        for r in rows:
            started_at = r.get("started_at")
            if isinstance(started_at, datetime):
                started_at = started_at.isoformat()
            elif not isinstance(started_at, str):
                started_at = None
            finished_at = r.get("finished_at")
            if isinstance(finished_at, datetime):
                finished_at = finished_at.isoformat()
            elif not isinstance(finished_at, str):
                finished_at = None

            full = await db.get_report(r["id"])
            payload = (full or {}).get("payload_json")
            stats = _payload_to_stats(payload) if payload else {}
            reports.append({
                "name": r["id"],
                "started_at": started_at,
                "finished_at": finished_at,
                "finalized": bool(payload),
                "model": r.get("model"),
                "operator": r.get("operator"),
                "org_name": r.get("org_name"),
                **stats,
            })
        # Aggregate KPIs across the returned set so the SPA's tile
        # strip doesn't have to recompute over every row.
        finalized = [r for r in reports if r["finalized"]]
        kpis = {
            "total_runs": len(reports),
            "total_finalized": len(finalized),
            "total_native_cost": round(sum(r.get("native_cost") or 0 for r in finalized), 6),
            "total_mcp_cost": round(sum(r.get("mcp_cost") or 0 for r in finalized), 6),
            "avg_ratio": (
                round(
                    sum(r["mcp_native_ratio"] for r in finalized
                        if r.get("mcp_native_ratio") is not None) /
                    max(1, sum(1 for r in finalized if r.get("mcp_native_ratio") is not None)),
                    3,
                )
                if any(r.get("mcp_native_ratio") is not None for r in finalized)
                else None
            ),
        }
        return JSONResponse({"reports": reports, "kpis": kpis}, headers=_NO_STORE)

    @app.get("/api/reports/{report_id}/data")
    async def load_saved_report(report_id: str):
        from token_compare import db
        rec = await db.get_report(report_id)
        if not rec or not rec.get("payload_json"):
            return JSONResponse({"error": "report not found"}, status_code=404)
        from token_compare.models import BenchmarkResult
        result = BenchmarkResult.model_validate(rec["payload_json"])
        return _hydrate_from_result(result, source=report_id)

    @app.get("/api/reports/{report_id}/markdown")
    async def download_report_markdown(report_id: str):
        """Render a saved report as Markdown and stream it as a download.
        Used by the reports table's 'Download Markdown' action."""
        from token_compare import db
        from io import StringIO
        rec = await db.get_report(report_id)
        if not rec or not rec.get("payload_json"):
            return JSONResponse({"error": "report not found"}, status_code=404)
        from token_compare.models import BenchmarkResult
        result = BenchmarkResult.model_validate(rec["payload_json"])
        # Resolve scenario metadata (title/category/difficulty) for the
        # writer. Same path as the live-run report writer uses.
        all_db_scenarios = await db.list_scenarios(include_inactive=True)
        all_scenarios = [
            Scenario(
                id=s["id"], title=s["title"], category=s["category"],
                difficulty=s["difficulty"], prompt=s["prompt"],
                expected_operations=s.get("expected_operations") or [],
                success_criteria=SuccessCriteria.model_validate(
                    s.get("success_criteria_json") or {"must_contain": []}
                ),
                notes=s.get("notes", "") or "",
            )
            for s in all_db_scenarios
        ]
        # write_markdown writes to a Path; render to a StringIO buffer
        # via the same internal helpers.
        from token_compare.report import _render_markdown
        text = _render_markdown(result, scenarios=all_scenarios)
        filename = f"{report_id}.md"
        return Response(
            content=text,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                **_NO_STORE,
            },
        )

    @app.post("/api/reports/load")
    async def upload_and_load_report(file: UploadFile = File(...)) -> dict:
        """Upload a .md or .json report file and hydrate it into _current_run.
        File contents are not persisted to disk — this is read-only viewing."""
        contents = (await file.read()).decode("utf-8", errors="replace")
        try:
            if (file.filename or "").endswith(".json"):
                result = load_json_report(contents)
            else:
                result = load_markdown_report(contents)
        except Exception as e:
            return JSONResponse(
                {"error": f"failed to parse report: {e}"},
                status_code=400,
            )
        return _hydrate_from_result(result, source=f"upload:{file.filename}")

    def _hydrate_from_files(md_path: Path) -> dict:
        """Prefer the JSON sidecar, fall back to parsing the markdown."""
        json_path = md_path.with_suffix(".json")
        try:
            if json_path.is_file():
                result = load_json_report(json_path.read_text(encoding="utf-8"))
            else:
                result = load_markdown_report(md_path.read_text(encoding="utf-8"))
        except Exception as e:
            return JSONResponse(
                {"error": f"failed to parse {md_path.name}: {e}"},
                status_code=400,
            )
        return _hydrate_from_result(result, source=str(md_path))

    def _hydrate_from_result(result, *, source: str) -> dict:
        """Stuff a `BenchmarkResult` into the _current_run cache so the
        existing /trace, /summary, /data endpoints serve this report."""
        _current_run["active"] = False
        _current_run["events"] = []
        _current_run["started_at"] = result.started_at
        _current_run["report_path"] = source
        _current_run["result_data"] = result.model_dump()
        _current_run["freeform_scenario"] = None
        return {
            "ok": True,
            "scenario_count": len(result.scenarios),
            "started_at": result.started_at,
            "model": result.model,
            "runs_per_path": result.runs_per_path,
            "source": source,
            # Echo the scenario IDs so the frontend can register them in
            # state.scenarios for the stepper.
            "scenario_ids": [s.scenario_id for s in result.scenarios],
        }

    @app.post("/api/sf/login")
    async def sf_login(request: Request):
        from token_compare import db
        from token_compare.sessions import COOKIE_NAME, sign_session_id
        from token_compare.sf_auth import (
            load_credentials_from_env,
            _generate_pkce, _build_authorize_url,
        )
        import secrets as _secrets

        creds = load_credentials_from_env()
        if creds is None:
            return JSONResponse(
                {"ok": False, "error": "SF_CLIENT_ID/SECRET/LOGIN_URL not set"},
                status_code=400,
            )
        verifier, challenge = _generate_pkce()
        state = _secrets.token_urlsafe(24)
        auth_url = _build_authorize_url(creds, state, challenge)

        sid, _ = await _get_or_create_sid_with_cookie(request)
        # Persist (state → session_id, verifier) in Postgres so a dyno
        # restart between this call and /callback doesn't drop the
        # PKCE verifier the way the in-memory dict used to.
        await db.put_pending_login(
            state=state, session_id=sid, verifier=verifier,
        )

        resp = JSONResponse({"ok": True, "authorize_url": auth_url})
        resp.set_cookie(
            COOKIE_NAME, sign_session_id(sid),
            httponly=True, secure=True, samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
        return resp

    @app.post("/api/sf/logout")
    async def sf_logout(request: Request) -> dict:
        from token_compare import db
        sid, _ = await _get_or_create_sid_with_cookie(request)
        await db.delete_sf_token(sid)
        return {"ok": True}

    @app.get("/api/scenarios/{scenario_id}/trace")
    async def scenario_trace(scenario_id: str):
        """Return per-turn traces and an explanation paragraph for the
        most recent benchmark run's data on this scenario. Falls back to
        Postgres if the in-memory cache was wiped by a dyno restart."""
        result_data = await _ensure_latest_in_cache()
        if not result_data:
            return JSONResponse(
                {"error": "no benchmark result cached; run a benchmark first"},
                status_code=404,
            )
        from token_compare.models import BenchmarkResult
        result = BenchmarkResult.model_validate(result_data)
        sr = next((s for s in result.scenarios if s.scenario_id == scenario_id), None)
        if not sr:
            return JSONResponse(
                {"error": f"scenario {scenario_id} not in latest run"},
                status_code=404,
            )

        native_traces = [extract_trace(r) for r in sr.native_runs]
        mcp_traces = [extract_trace(r) for r in sr.mcp_runs]
        native_summary = build_comparison("Native", sr.native_runs, native_traces)
        mcp_summary = build_comparison("MCP", sr.mcp_runs, mcp_traces)

        return JSONResponse({
            "scenario_id": scenario_id,
            "native_traces": [t.model_dump() for t in native_traces],
            "mcp_traces": [t.model_dump() for t in mcp_traces],
            "native_summary": native_summary.model_dump(),
            "mcp_summary": mcp_summary.model_dump(),
            "explanation": explain_comparison(native_summary, mcp_summary),
        }, headers=_NO_STORE)

    @app.get("/callback")
    async def oauth_callback(
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
        error_description: Optional[str] = None,
    ):
        import html
        from token_compare import db
        from token_compare.sf_auth import (
            SfAuthError, _exchange_code, load_credentials_from_env,
        )

        def page(title: str, body: str, status: int) -> HTMLResponse:
            html_content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:-apple-system,Inter,sans-serif;max-width:640px;margin:80px auto;padding:24px;color:#181818}}
h1{{font-size:24px}}p{{color:#747474}}</style></head><body>
<h1>{html.escape(title)}</h1><p>{html.escape(body)}</p></body></html>"""
            return HTMLResponse(html_content, status_code=status)

        if error:
            error_msg = f"{error}: {error_description or ''}"
            if state:
                await db.pop_pending_login(state)  # clean up
            return page(
                "Salesforce login failed",
                f"{error_msg}. You can close this tab.",
                400,
            )
        if not code or not state:
            if state:
                await db.pop_pending_login(state)
            return page("Invalid callback", "Missing code or state.", 400)

        # Atomically claim the pending-login row from Postgres. This is
        # restart-safe: even if the dyno that registered the state has
        # been replaced (e.g. config-var change), the new dyno can pick
        # up where the old one left off.
        pending = await db.pop_pending_login(state)
        if pending is None:
            return page(
                "Salesforce login failed",
                f"no pending login for state {state!r} — it may have expired "
                "(15-min limit) or been consumed already. Click Connect "
                "Salesforce again to start a fresh login.",
                400,
            )

        creds = load_credentials_from_env()
        if creds is None:
            return page(
                "Salesforce login failed",
                "SF_CLIENT_ID / SF_CLIENT_SECRET / SF_LOGIN_URL not set on the server.",
                500,
            )

        try:
            tok = _exchange_code(creds, code, pending["verifier"])
        except SfAuthError as e:
            return page("Salesforce login failed", str(e), 400)
        except Exception as e:
            return page("Salesforce login failed", f"unexpected error: {e}", 500)

        sid = pending["session_id"]
        await db.put_sf_token(sid, tok.model_dump())
        # Carry the session cookie through, in case the user landed on
        # /callback in a tab that didn't have one.
        from fastapi.responses import RedirectResponse
        from token_compare.sessions import COOKIE_NAME, sign_session_id
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(
            COOKIE_NAME, sign_session_id(sid),
            httponly=True, secure=True, samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
        return resp

    @app.get("/admin")
    async def admin_redirect():
        """Pretty URL for the admin page — redirects to the static HTML."""
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/admin.html", status_code=307)

    if config.static_dir and config.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(config.static_dir), html=True), name="static")

    return app


def main() -> None:
    _load_dotenv_if_present()
    cfg = AppConfig()
    app = create_app(cfg)
    uvicorn.run(app, host="127.0.0.1", port=8000)


def _bootstrap_app() -> FastAPI:
    """Module-level entry point for `uvicorn token_compare.api:app`."""
    _load_dotenv_if_present()
    return create_app(AppConfig())


app = _bootstrap_app()


if __name__ == "__main__":
    main()
