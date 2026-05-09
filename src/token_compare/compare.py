from __future__ import annotations

from statistics import median
from typing import Literal, Optional

from pydantic import BaseModel

from token_compare.models import (
    BenchmarkResult, ModelRunBucket, _default_model, _percentile_int,
)


COST_REGRESSION_THRESHOLD_PCT = 10.0
SUCCESS_REGRESSION_THRESHOLD_ABS = -0.05  # -5pp


class MetricDelta(BaseModel):
    a: float
    b: float
    delta_abs: float
    delta_pct: Optional[float]


class ScenarioCompare(BaseModel):
    scenario_id: str
    title: str = ""
    presence: Literal["both", "added_in_b", "removed_in_b"]
    native_cost: Optional[MetricDelta] = None
    mcp_cost: Optional[MetricDelta] = None
    success_rate: Optional[MetricDelta] = None
    cost_multiplier: Optional[MetricDelta] = None
    p95_duration_ms: Optional[MetricDelta] = None
    regressed: bool = False


class ReportSummary(BaseModel):
    id: str = ""
    started_at: str = ""
    model: str = ""
    operator: str = ""
    org_name: str = ""


class ReportComparison(BaseModel):
    report_a: ReportSummary
    report_b: ReportSummary
    model_used: str
    incompatible: bool = False
    incompatibility_reason: Optional[str] = None
    scope: dict[str, list[str]]
    scenarios: list[ScenarioCompare]


def _metric_delta(a: float, b: float) -> MetricDelta:
    delta_abs = b - a
    delta_pct = ((b - a) / a * 100.0) if a != 0 else None
    return MetricDelta(a=a, b=b, delta_abs=delta_abs, delta_pct=delta_pct)


def _bucket_metrics(bucket: Optional[ModelRunBucket]) -> dict:
    if bucket is None:
        return {}
    n_succ = [r.total_cost_usd for r in bucket.native_runs if r.succeeded]
    m_succ = [r.total_cost_usd for r in bucket.mcp_runs if r.succeeded]
    n_dur = [r.duration_ms for r in bucket.native_runs]
    m_dur = [r.duration_ms for r in bucket.mcp_runs]
    return {
        "native_cost": float(median(n_succ)) if n_succ else 0.0,
        "mcp_cost": float(median(m_succ)) if m_succ else 0.0,
        "native_total": len(bucket.native_runs),
        "native_succ": len(n_succ),
        "mcp_total": len(bucket.mcp_runs),
        "mcp_succ": len(m_succ),
        "native_p95": _percentile_int(n_dur, 0.95) if n_dur else 0,
        "mcp_p95": _percentile_int(m_dur, 0.95) if m_dur else 0,
    }


def compare_reports(
    a: BenchmarkResult, b: BenchmarkResult,
    *, model: Optional[str] = None,
) -> ReportComparison:
    common = [m for m in (a.models or [a.model]) if m in (b.models or [b.model])]
    chosen = model or _default_model(common)
    summary_a = ReportSummary(model=a.model, operator=a.operator,
                                org_name=a.org_name, started_at=a.started_at)
    summary_b = ReportSummary(model=b.model, operator=b.operator,
                                org_name=b.org_name, started_at=b.started_at)
    if not chosen:
        return ReportComparison(
            report_a=summary_a, report_b=summary_b,
            model_used="", incompatible=True,
            incompatibility_reason="no common model",
            scope={"added": [], "removed": [], "shared": []},
            scenarios=[],
        )

    a_by_id = {sr.scenario_id: sr for sr in a.scenarios}
    b_by_id = {sr.scenario_id: sr for sr in b.scenarios}
    all_ids = sorted(set(a_by_id) | set(b_by_id))
    added = [i for i in all_ids if i not in a_by_id]
    removed = [i for i in all_ids if i not in b_by_id]
    shared = [i for i in all_ids if i in a_by_id and i in b_by_id]

    scenarios: list[ScenarioCompare] = []
    for sid in all_ids:
        sr_a = a_by_id.get(sid)
        sr_b = b_by_id.get(sid)
        presence = ("both" if sr_a and sr_b
                    else "added_in_b" if sr_b else "removed_in_b")
        bucket_a = sr_a.runs_by_model.get(chosen) if sr_a else None
        bucket_b = sr_b.runs_by_model.get(chosen) if sr_b else None
        m_a = _bucket_metrics(bucket_a)
        m_b = _bucket_metrics(bucket_b)

        sc = ScenarioCompare(scenario_id=sid, presence=presence)
        if presence == "both" and bucket_a is not None and bucket_b is not None:
            sc.native_cost = _metric_delta(m_a["native_cost"], m_b["native_cost"])
            sc.mcp_cost = _metric_delta(m_a["mcp_cost"], m_b["mcp_cost"])
            succ_a = (m_a["native_succ"] + m_a["mcp_succ"]) / max(m_a["native_total"] + m_a["mcp_total"], 1)
            succ_b = (m_b["native_succ"] + m_b["mcp_succ"]) / max(m_b["native_total"] + m_b["mcp_total"], 1)
            sc.success_rate = _metric_delta(succ_a, succ_b)
            ratio_a = (m_a["mcp_cost"] / m_a["native_cost"]) if m_a["native_cost"] > 0 else 0.0
            ratio_b = (m_b["mcp_cost"] / m_b["native_cost"]) if m_b["native_cost"] > 0 else 0.0
            sc.cost_multiplier = _metric_delta(ratio_a, ratio_b)
            p95_a = float(max(m_a["native_p95"], m_a["mcp_p95"]))
            p95_b = float(max(m_b["native_p95"], m_b["mcp_p95"]))
            sc.p95_duration_ms = _metric_delta(p95_a, p95_b)
            sc.regressed = (
                (sc.native_cost.delta_pct is not None and sc.native_cost.delta_pct > COST_REGRESSION_THRESHOLD_PCT)
                or (sc.mcp_cost.delta_pct is not None and sc.mcp_cost.delta_pct > COST_REGRESSION_THRESHOLD_PCT)
                or (sc.success_rate.delta_abs is not None and sc.success_rate.delta_abs < SUCCESS_REGRESSION_THRESHOLD_ABS)
            )
        scenarios.append(sc)

    def _sort_key(sc: ScenarioCompare):
        kind = (0 if sc.regressed else 1 if sc.presence == "both" else 2)
        cost_pct = abs(sc.native_cost.delta_pct or 0.0) if sc.native_cost else 0.0
        return (kind, -cost_pct)
    scenarios.sort(key=_sort_key)

    return ReportComparison(
        report_a=summary_a, report_b=summary_b,
        model_used=chosen,
        scope={"added": added, "removed": removed, "shared": shared},
        scenarios=scenarios,
    )
