from __future__ import annotations

import random
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional

from pydantic import BaseModel

from token_compare.mcp_config import resolve_template
from token_compare.models import (
    BenchmarkResult, PathName, RunResult, Scenario, ScenarioResult,
)
from token_compare.runner import run_once
from token_compare.sf_auth import (
    AccessToken, SfAuthError, fetch_access_token, load_credentials_from_env,
)


class BenchmarkOptions(BaseModel):
    model: str
    max_turns: int
    timeout_s: int
    runs_per_path: int
    mcp_config_path: Path
    operator: str
    org_name: str


EventKind = Literal[
    "benchmark_start", "scenario_start", "run_start",
    "run_complete", "scenario_complete", "benchmark_complete",
]


@dataclass
class ProgressEvent:
    kind: EventKind
    scenario_id: Optional[str] = None
    path: Optional[PathName] = None
    run_index: Optional[int] = None
    total_runs: Optional[int] = None
    run_result: Optional[RunResult] = None


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_benchmark(
    scenarios: list[Scenario],
    options: BenchmarkOptions,
    on_progress: Optional[Callable[[ProgressEvent], None]] = None,
) -> BenchmarkResult:
    emit = on_progress or (lambda e: None)
    started_at = _now_iso()
    emit(ProgressEvent(kind="benchmark_start"))

    # For Path B (MCP), fetch a Salesforce access token and write a resolved
    # mcp-config temp file so ${SF_ACCESS_TOKEN} placeholders carry a real
    # bearer. If credentials aren't set OR the exchange fails, fall back to
    # the template file — MCP runs will surface the auth failure per-run.
    resolved_mcp_path: Optional[Path] = None
    effective_mcp_config = options.mcp_config_path
    creds = load_credentials_from_env()
    if creds is not None:
        try:
            token = fetch_access_token(creds)
            resolved_mcp_path = resolve_template(
                options.mcp_config_path,
                {"SF_ACCESS_TOKEN": token.access_token},
            )
            effective_mcp_config = resolved_mcp_path
        except SfAuthError:
            # Leave effective_mcp_config as the template; per-run failures
            # will be captured in each RunResult.error.
            pass

    results: list[ScenarioResult] = []
    total_runs_per_scenario = options.runs_per_path * 2

    for scenario in scenarios:
        emit(ProgressEvent(kind="scenario_start", scenario_id=scenario.id,
                           total_runs=total_runs_per_scenario))

        first_is_mcp = random.random() < 0.5
        order: list[PathName] = []
        for _ in range(options.runs_per_path):
            if first_is_mcp:
                order.extend([PathName.MCP, PathName.NATIVE])
            else:
                order.extend([PathName.NATIVE, PathName.MCP])

        native_runs: list[RunResult] = []
        mcp_runs: list[RunResult] = []

        for i, path in enumerate(order, start=1):
            emit(ProgressEvent(kind="run_start", scenario_id=scenario.id,
                               path=path, run_index=i,
                               total_runs=total_runs_per_scenario))
            r = run_once(
                scenario, path,
                model=options.model,
                max_turns=options.max_turns,
                timeout_s=options.timeout_s,
                mcp_config_path=effective_mcp_config,
            )
            (native_runs if path == PathName.NATIVE else mcp_runs).append(r)
            emit(ProgressEvent(kind="run_complete", scenario_id=scenario.id,
                               path=path, run_index=i,
                               total_runs=total_runs_per_scenario, run_result=r))

        sr = ScenarioResult(scenario_id=scenario.id,
                            native_runs=native_runs, mcp_runs=mcp_runs)
        results.append(sr)
        emit(ProgressEvent(kind="scenario_complete", scenario_id=scenario.id))

    finished_at = _now_iso()
    emit(ProgressEvent(kind="benchmark_complete"))

    if resolved_mcp_path is not None:
        try:
            resolved_mcp_path.unlink(missing_ok=True)
        except OSError:
            pass

    return BenchmarkResult(
        started_at=started_at,
        finished_at=finished_at,
        operator=options.operator,
        model=options.model,
        org_name=options.org_name,
        tool_commit=_git_sha(),
        runs_per_path=options.runs_per_path,
        scenarios=results,
    )
