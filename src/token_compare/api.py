from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from token_compare.analysis import build_comparison, explain_comparison, extract_trace
from token_compare.benchmark import BenchmarkOptions, ProgressEvent, run_benchmark
from token_compare.preflight import check_environment
from token_compare.report import default_report_path, write_markdown
from token_compare.models import Scenario, SuccessCriteria
from token_compare.scenarios import load_all


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


class RunRequest(BaseModel):
    scenario_ids: list[str]
    runs_per_path: int = 3
    model: str = "claude-opus-4-7"
    operator: str
    org_name: str
    max_turns: int = 15
    timeout_s: int = 300


class FreeformRunRequest(BaseModel):
    prompt: str
    title: Optional[str] = None
    runs_per_path: int = 1
    model: str = "claude-opus-4-7"
    operator: str = "local user"
    org_name: str = "(local org)"
    max_turns: int = 30
    timeout_s: int = 600


def _event_to_dict(e: ProgressEvent) -> dict:
    d: dict = {"kind": e.kind}
    if e.scenario_id: d["scenario_id"] = e.scenario_id
    if e.path: d["path"] = e.path.value
    if e.run_index is not None: d["run_index"] = e.run_index
    if e.total_runs is not None: d["total_runs"] = e.total_runs
    if e.run_result is not None:
        d["run_result"] = e.run_result.model_dump(exclude={"raw_json"})
    return d


def _prune_reports(reports_dir: Path, retain: int) -> None:
    files = sorted(reports_dir.glob("benchmark-*.md"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[retain:]:
        old.unlink(missing_ok=True)


def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI(title="Token Comparison Tool")

    # Track in-memory benchmark state for polling fallback
    _current_run: dict = {"active": False, "events": [], "started_at": None, "report_path": None}

    @app.get("/api/preflight")
    def preflight() -> dict:
        return check_environment(mcp_config_path=config.mcp_config_path).model_dump()

    @app.get("/api/scenarios")
    def list_scenarios() -> list[dict]:
        return [s.model_dump() for s in load_all(config.scenarios_dir)]

    def _start_benchmark_stream(
        picked_scenarios: list[Scenario],
        options: BenchmarkOptions,
        *,
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
                result = await loop.run_in_executor(
                    None, lambda: run_benchmark(picked_scenarios, options, on_progress),
                )
                config.reports_dir.mkdir(parents=True, exist_ok=True)
                out = default_report_path(config.reports_dir, result.started_at)
                write_markdown(result, out, scenarios=picked_scenarios)
                _prune_reports(config.reports_dir, config.reports_retain)
                _current_run["result_data"] = result.model_dump()
                _current_run["report_path"] = str(out)
                queue.put_nowait({"kind": "report_written", "path": str(out)})
            except Exception as e:
                queue.put_nowait({"kind": "error", "message": str(e)})
            finally:
                _current_run["active"] = False
                queue.put_nowait(None)

        async def event_stream():
            task = asyncio.create_task(runner_task())
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
            await task

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/run")
    async def run(req: RunRequest) -> StreamingResponse:
        all_scenarios = load_all(config.scenarios_dir)
        picked = [s for s in all_scenarios if s.id in set(req.scenario_ids)]
        if not picked:
            picked = all_scenarios

        options = BenchmarkOptions(
            model=req.model, max_turns=req.max_turns, timeout_s=req.timeout_s,
            runs_per_path=req.runs_per_path,
            mcp_config_path=config.mcp_config_path,
            operator=req.operator, org_name=req.org_name,
        )
        return _start_benchmark_stream(picked, options)

    @app.post("/api/run/freeform")
    async def run_freeform(req: FreeformRunRequest) -> StreamingResponse:
        prompt = (req.prompt or "").strip()
        if not prompt:
            return JSONResponse({"error": "prompt is required"}, status_code=400)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        scenario = Scenario(
            id=f"freeform_{ts}",
            title=(req.title or "Free-format scenario").strip(),
            category="freeform",
            difficulty="medium",
            prompt=prompt,
            success_criteria=SuccessCriteria(),
        )
        options = BenchmarkOptions(
            model=req.model, max_turns=req.max_turns, timeout_s=req.timeout_s,
            runs_per_path=req.runs_per_path,
            mcp_config_path=config.mcp_config_path,
            operator=req.operator, org_name=req.org_name,
        )
        return _start_benchmark_stream(
            [scenario], options, freeform_scenario=scenario,
        )

    @app.get("/api/run/status")
    def run_status() -> dict:
        return {
            "active": _current_run["active"],
            "started_at": _current_run["started_at"],
            "events": _current_run["events"],
            "report_path": _current_run["report_path"],
            "freeform_scenario": _current_run.get("freeform_scenario"),
        }

    @app.api_route("/api/reports/latest", methods=["GET", "HEAD"])
    def latest_report(request: Request):
        files = sorted(config.reports_dir.glob("benchmark-*.md"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return JSONResponse({"path": None, "content": None})
        latest = files[0]
        return FileResponse(latest, media_type="text/markdown", filename=latest.name)

    @app.get("/api/reports/latest/data")
    def latest_report_data() -> dict:
        """Return the latest benchmark's data as JSON (parses from memory if
        active run ended recently, otherwise re-reads the report file's
        embedded raw_json appendix)."""
        if _current_run.get("result_data"):
            data = dict(_current_run["result_data"])
            data["freeform_scenario"] = _current_run.get("freeform_scenario")
            return data
        return JSONResponse({"scenarios": []}, status_code=404)

    @app.get("/api/reports/latest/summary")
    def latest_summary() -> dict:
        from token_compare.analysis import build_summary_analysis
        result_data = _current_run.get("result_data")
        if not result_data:
            return JSONResponse({"error": "no benchmark cached"}, status_code=404)
        scenarios_meta = {
            s.id: {"title": s.title, "category": s.category, "difficulty": s.difficulty}
            for s in load_all(config.scenarios_dir)
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
        return analysis.model_dump()

    @app.post("/api/sf/login")
    async def sf_login() -> dict:
        from token_compare.sf_auth import (
            SfAuthError, load_credentials_from_env, run_interactive_login,
        )
        creds = load_credentials_from_env()
        if creds is None:
            return JSONResponse(
                {"ok": False, "error": "SF_CLIENT_ID/SECRET/LOGIN_URL not set"},
                status_code=400,
            )
        try:
            # This BLOCKS the event loop while waiting for the browser callback.
            # Run in executor so uvicorn stays responsive to other requests.
            import asyncio
            loop = asyncio.get_running_loop()
            tok = await loop.run_in_executor(None, lambda: run_interactive_login(creds))
            return {"ok": True, "scope": tok.scope, "instance_url": tok.instance_url}
        except SfAuthError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.post("/api/sf/logout")
    def sf_logout() -> dict:
        from token_compare.sf_auth import clear_cached_token
        clear_cached_token()
        return {"ok": True}

    @app.get("/api/scenarios/{scenario_id}/trace")
    def scenario_trace(scenario_id: str) -> dict:
        """Return per-turn traces and an explanation paragraph for the
        most recent benchmark run's data on this scenario."""
        # Use the in-memory cached result if available (most recent run).
        result_data = _current_run.get("result_data")
        if not result_data:
            return JSONResponse(
                {"error": "no benchmark result cached; run a benchmark first"},
                status_code=404,
            )
        # Find the scenario
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

        return {
            "scenario_id": scenario_id,
            "native_traces": [t.model_dump() for t in native_traces],
            "mcp_traces": [t.model_dump() for t in mcp_traces],
            "native_summary": native_summary.model_dump(),
            "mcp_summary": mcp_summary.model_dump(),
            "explanation": explain_comparison(native_summary, mcp_summary),
        }

    @app.get("/callback")
    def oauth_callback(
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
        error_description: Optional[str] = None,
    ):
        import html
        from token_compare.sf_auth import (
            SfAuthError, complete_pending_login, complete_pending_login_error,
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
                complete_pending_login_error(state, error_msg)
            return page(
                "Salesforce login failed",
                f"{error_msg}. You can close this tab.",
                400,
            )
        if not code or not state:
            return page("Invalid callback", "Missing code or state.", 400)
        try:
            complete_pending_login(state, code)
        except SfAuthError as e:
            return page("Salesforce login failed", str(e), 400)
        except Exception as e:
            return page("Salesforce login failed", f"unexpected error: {e}", 500)
        return page(
            "Salesforce login complete",
            "You can close this tab and return to the benchmark tool.",
            200,
        )

    if config.static_dir and config.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(config.static_dir), html=True), name="static")

    return app


def main() -> None:
    _load_dotenv_if_present()
    cfg = AppConfig()
    app = create_app(cfg)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
