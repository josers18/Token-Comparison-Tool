from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Iterable, Literal, Optional

from token_compare.models import _normalize_to_cube


METRICS = ("cost", "cache", "success", "p95_duration")


def _metric_for_runs(runs: list[dict], metric: str) -> float:
    if metric == "cost":
        succ = [r["total_cost_usd"] for r in runs if r.get("succeeded")]
        return float(median(succ)) if succ else 0.0
    if metric == "cache":
        total_in = 0
        total_cache = 0
        for r in runs:
            total_in += (r.get("input_tokens", 0)
                         + r.get("cache_read_input_tokens", 0)
                         + r.get("cache_creation_input_tokens", 0))
            total_cache += r.get("cache_read_input_tokens", 0)
        return total_cache / total_in if total_in > 0 else 0.0
    if metric == "success":
        if not runs:
            return 0.0
        succ = sum(1 for r in runs if r.get("succeeded"))
        return succ / len(runs)
    if metric == "p95_duration":
        durations = sorted(r.get("duration_ms", 0) for r in runs)
        if not durations:
            return 0.0
        import math
        idx = max(0, min(len(durations) - 1,
                          math.ceil(0.95 * len(durations)) - 1))
        return float(durations[idx])
    raise ValueError(f"unknown metric: {metric}")


def walk_history(
    rows: Iterable[dict],
    *,
    scenario_id: str,
    model: str,
    metric: Literal["cost", "cache", "success", "p95_duration"] = "cost",
    since: Optional[datetime] = None,
) -> dict:
    rows_sorted = sorted(rows, key=lambda r: r["started_at"])
    points = []
    last_prompt: Optional[str] = None
    last_models: Optional[list[str]] = None
    change_markers = []

    for row in rows_sorted:
        if since is not None and row["started_at"] < since:
            continue
        payload = _normalize_to_cube(dict(row["payload_json"]))
        if model not in (payload.get("models") or []):
            continue

        scenario_dict = None
        for sr in payload.get("scenarios", []):
            if sr.get("scenario_id") == scenario_id:
                scenario_dict = sr
                break
        if scenario_dict is None:
            continue

        bucket = scenario_dict.get("runs_by_model", {}).get(model)
        if not bucket:
            continue

        native_val = _metric_for_runs(bucket.get("native_runs", []), metric)
        mcp_val = _metric_for_runs(bucket.get("mcp_runs", []), metric)
        points.append({
            "report_id": row["id"],
            "started_at": row["started_at"].isoformat()
                          if isinstance(row["started_at"], datetime)
                          else str(row["started_at"]),
            "native": native_val,
            "mcp": mcp_val,
        })

        prompt = scenario_dict.get("prompt") or ""
        models_in_payload = payload.get("models") or []
        if last_prompt is not None and prompt != last_prompt:
            change_markers.append({
                "report_id": row["id"], "kind": "prompt_edited",
                "detail": "scenario prompt changed",
            })
        if last_models is not None and set(models_in_payload) != set(last_models):
            change_markers.append({
                "report_id": row["id"], "kind": "models_changed",
                "detail": f"models changed to {models_in_payload}",
            })
        last_prompt = prompt
        last_models = models_in_payload

    return {
        "scenario_id": scenario_id,
        "model": model,
        "metric": metric,
        "points": points,
        "change_markers": change_markers,
    }
