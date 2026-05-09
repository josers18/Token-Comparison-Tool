from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel

from token_compare.models import BenchmarkResult, _default_model


class PerScenarioCost(BaseModel):
    scenario_id: str
    native: float
    mcp: float
    delta: float


class Breakeven(BaseModel):
    scenario_id: str
    threshold_usd: float
    runs_to_breakeven: Optional[int]
    frame: Literal["mcp_more_expensive", "native_more_expensive",
                    "near_break_even", "single_path_failed"]


class CurvePoint(BaseModel):
    month: int
    native_cum: float
    mcp_cum: float


class ScaleProjection(BaseModel):
    model_used: str
    native_total: float
    mcp_total: float
    delta: float
    multiplier: Optional[float]
    per_scenario: list[PerScenarioCost]
    breakevens: list[Breakeven]
    curve: list[CurvePoint]


def project_at_scale(
    bench: BenchmarkResult,
    *,
    runs_per_scenario_per_period: int,
    period: Literal["day", "month", "year"],
    growth_rate_pct: float = 0.0,
    breakeven_thresholds_usd: Optional[list[float]] = None,
    model: Optional[str] = None,
) -> ScaleProjection:
    if breakeven_thresholds_usd is None:
        breakeven_thresholds_usd = [1_000.0, 10_000.0, 100_000.0]

    chosen = model or _default_model(bench.models or [bench.model])

    per_scenario: list[PerScenarioCost] = []
    breakevens: list[Breakeven] = []

    from statistics import median
    for sr in bench.scenarios:
        bucket = sr.runs_by_model.get(chosen)
        if bucket is None:
            continue
        nv = [r.total_cost_usd for r in bucket.native_runs if r.succeeded]
        mv = [r.total_cost_usd for r in bucket.mcp_runs if r.succeeded]
        n_per_run = float(median(nv)) if nv else 0.0
        m_per_run = float(median(mv)) if mv else 0.0
        n_total = n_per_run * runs_per_scenario_per_period
        m_total = m_per_run * runs_per_scenario_per_period
        per_scenario.append(PerScenarioCost(
            scenario_id=sr.scenario_id, native=n_total,
            mcp=m_total, delta=m_total - n_total,
        ))

        for th in breakeven_thresholds_usd:
            if not nv or not mv:
                frame, runs = "single_path_failed", None
            else:
                ratio_diff = abs(m_per_run - n_per_run) / max(m_per_run, n_per_run, 1e-12)
                if ratio_diff < 0.05:
                    frame, runs = "near_break_even", None
                elif m_per_run > n_per_run:
                    frame = "mcp_more_expensive"
                    runs = int(round(th / (m_per_run - n_per_run)))
                else:
                    frame = "native_more_expensive"
                    runs = int(round(th / (n_per_run - m_per_run)))
            breakevens.append(Breakeven(
                scenario_id=sr.scenario_id, threshold_usd=th,
                runs_to_breakeven=runs, frame=frame,  # type: ignore[arg-type]
            ))

    native_total = sum(p.native for p in per_scenario)
    mcp_total = sum(p.mcp for p in per_scenario)
    delta = mcp_total - native_total
    multiplier = (mcp_total / native_total) if native_total > 0 else None

    curve: list[CurvePoint] = []
    g = growth_rate_pct / 100.0
    for n in range(1, 13):
        if g == 0:
            n_cum = native_total * n
            m_cum = mcp_total * n
        else:
            factor = ((1 + g) ** n - 1) / g
            n_cum = native_total * factor
            m_cum = mcp_total * factor
        curve.append(CurvePoint(month=n, native_cum=n_cum, mcp_cum=m_cum))

    return ScaleProjection(
        model_used=chosen, native_total=native_total, mcp_total=mcp_total,
        delta=delta, multiplier=multiplier,
        per_scenario=per_scenario, breakevens=breakevens, curve=curve,
    )
