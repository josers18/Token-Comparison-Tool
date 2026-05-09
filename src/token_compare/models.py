from __future__ import annotations

import math
from enum import Enum
from statistics import median, pstdev
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PathName(str, Enum):
    NATIVE = "native"
    MCP = "mcp"


class SuccessCriteria(BaseModel):
    must_contain: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    id: str
    title: str
    category: str
    difficulty: Literal["simple", "medium", "complex"]
    prompt: str
    expected_operations: list[str] = Field(default_factory=list)
    success_criteria: SuccessCriteria
    notes: str = ""


class ErrorResponse(BaseModel):
    status_code: int
    body_excerpt: str
    headers: dict[str, str] = Field(default_factory=dict)


class InferenceError(BaseModel):
    type: str
    message: str
    body_excerpt: str = ""


class ToolCallDetail(BaseModel):
    name: str
    input_excerpt: str
    output_excerpt: str
    truncated: bool = False
    error: Optional[str] = None


class RunResult(BaseModel):
    path: PathName
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int = 0
    total_cost_usd: float
    num_turns: int
    duration_ms: int
    tool_calls: list[str]
    succeeded: bool
    error: Optional[str] = None
    raw_json: Optional[dict | list] = None
    error_response: Optional[ErrorResponse] = None
    inference_error: Optional[InferenceError] = None
    runner_traceback: Optional[str] = None
    tool_call_details: list[ToolCallDetail] = Field(default_factory=list)


def _median_int(values: list[int]) -> int:
    return int(median(values)) if values else 0


def _median_float(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


def _percentile_float(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (e.g. pct=0.95 → p95). Returns 0.0 on
    empty input. Conservative choice over linear interpolation: with
    small N (3–5 typical for benchmark runs) interpolation gives the
    *same* answer as nearest-rank but reads as if we have more
    statistical resolution than we do."""
    if not values:
        return 0.0
    s = sorted(values)
    # nearest-rank: ceil(pct * N) - 1, clamped to valid range
    idx = max(0, min(len(s) - 1, math.ceil(pct * len(s)) - 1))
    return float(s[idx])


def _percentile_int(values: list[int], pct: float) -> int:
    return int(_percentile_float([float(v) for v in values], pct))


def _stddev_float(values: list[float]) -> float:
    """Population standard deviation. Returns 0.0 for fewer than 2
    samples (statistics.pstdev would error on an empty list and is
    undefined for N=1; both cases mean 'no spread to report')."""
    if len(values) < 2:
        return 0.0
    return float(pstdev(values))


def _normalize_to_cube(payload: dict) -> dict:
    """In-place-safe shim: ensure `models` and per-scenario `runs_by_model`
    fields are populated, even for reports written before Tier B. Legacy
    reports get models=[model] and runs_by_model={model: {native_runs, mcp_runs}}."""
    primary = payload.get("model")
    if "models" not in payload or not payload.get("models"):
        payload["models"] = [primary] if primary else []
    for sr in payload.get("scenarios", []):
        if not sr.get("runs_by_model"):
            sr["runs_by_model"] = {
                primary: {
                    "native_runs": sr.get("native_runs", []),
                    "mcp_runs": sr.get("mcp_runs", []),
                }
            } if primary else {}
    return payload


def _default_model(models: list[str]) -> str:
    """Pick a default model from a sweep list. Prefer any name containing
    'sonnet' (case-insensitive) so future sonnet versions keep working;
    otherwise return the first. Empty input → empty string."""
    if not models:
        return ""
    for m in models:
        if "sonnet" in m.lower():
            return m
    return models[0]


class ModelRunBucket(BaseModel):
    native_runs: list[RunResult] = Field(default_factory=list)
    mcp_runs: list[RunResult] = Field(default_factory=list)


class ScenarioResult(BaseModel):
    scenario_id: str
    native_runs: list[RunResult]
    mcp_runs: list[RunResult]
    runs_by_model: dict[str, ModelRunBucket] = Field(default_factory=dict)

    @property
    def native_successful(self) -> list[RunResult]:
        return [r for r in self.native_runs if r.succeeded]

    @property
    def mcp_successful(self) -> list[RunResult]:
        return [r for r in self.mcp_runs if r.succeeded]

    @property
    def succeeded_native(self) -> int:
        return len(self.native_successful)

    @property
    def succeeded_mcp(self) -> int:
        return len(self.mcp_successful)

    @property
    def native_median_cost(self) -> float:
        return _median_float([r.total_cost_usd for r in self.native_successful])

    @property
    def mcp_median_cost(self) -> float:
        return _median_float([r.total_cost_usd for r in self.mcp_successful])

    @property
    def native_median_input_tokens(self) -> int:
        return _median_int([r.input_tokens for r in self.native_successful])

    @property
    def mcp_median_input_tokens(self) -> int:
        return _median_int([r.input_tokens for r in self.mcp_successful])

    @property
    def native_median_total_input_tokens(self) -> int:
        vals = [
            r.input_tokens + r.cache_read_input_tokens + r.cache_creation_input_tokens
            for r in self.native_successful
        ]
        return _median_int(vals)

    @property
    def mcp_median_total_input_tokens(self) -> int:
        vals = [
            r.input_tokens + r.cache_read_input_tokens + r.cache_creation_input_tokens
            for r in self.mcp_successful
        ]
        return _median_int(vals)

    # --- "all runs" variants — include failed runs so callers can show
    # actual spend regardless of outcome. The success-vs-fail breakdown is
    # available via succeeded_native / succeeded_mcp. ---

    @property
    def native_median_cost_all(self) -> float:
        return _median_float([r.total_cost_usd for r in self.native_runs])

    @property
    def mcp_median_cost_all(self) -> float:
        return _median_float([r.total_cost_usd for r in self.mcp_runs])

    @property
    def native_median_total_input_tokens_all(self) -> int:
        vals = [
            r.input_tokens + r.cache_read_input_tokens + r.cache_creation_input_tokens
            for r in self.native_runs
        ]
        return _median_int(vals)

    @property
    def mcp_median_total_input_tokens_all(self) -> int:
        vals = [
            r.input_tokens + r.cache_read_input_tokens + r.cache_creation_input_tokens
            for r in self.mcp_runs
        ]
        return _median_int(vals)

    @property
    def native_median_output_tokens_all(self) -> int:
        return _median_int([r.output_tokens for r in self.native_runs])

    @property
    def mcp_median_output_tokens_all(self) -> int:
        return _median_int([r.output_tokens for r in self.mcp_runs])

    @property
    def native_median_turns_all(self) -> int:
        return _median_int([r.num_turns for r in self.native_runs])

    @property
    def mcp_median_turns_all(self) -> int:
        return _median_int([r.num_turns for r in self.mcp_runs])

    @property
    def native_median_output_tokens(self) -> int:
        return _median_int([r.output_tokens for r in self.native_successful])

    @property
    def mcp_median_output_tokens(self) -> int:
        return _median_int([r.output_tokens for r in self.mcp_successful])

    @property
    def native_median_turns(self) -> int:
        return _median_int([r.num_turns for r in self.native_successful])

    @property
    def mcp_median_turns(self) -> int:
        return _median_int([r.num_turns for r in self.mcp_successful])

    @property
    def cheaper_multiplier(self) -> Optional[float]:
        """MCP median / Native median. None if either side has no successes."""
        if not self.native_successful or not self.mcp_successful:
            return None
        if self.native_median_cost <= 0:
            return None
        return self.mcp_median_cost / self.native_median_cost

    # ─── Tier A: variance + cache + duration + outcomes ──────────────────
    # All computed lazily from the existing run lists; no schema migration.

    @property
    def native_p95_cost(self) -> float:
        return _percentile_float([r.total_cost_usd for r in self.native_runs], 0.95)

    @property
    def mcp_p95_cost(self) -> float:
        return _percentile_float([r.total_cost_usd for r in self.mcp_runs], 0.95)

    @property
    def native_stddev_cost(self) -> float:
        return _stddev_float([r.total_cost_usd for r in self.native_runs])

    @property
    def mcp_stddev_cost(self) -> float:
        return _stddev_float([r.total_cost_usd for r in self.mcp_runs])

    @property
    def native_median_duration_ms(self) -> int:
        return _median_int([r.duration_ms for r in self.native_runs])

    @property
    def mcp_median_duration_ms(self) -> int:
        return _median_int([r.duration_ms for r in self.mcp_runs])

    @property
    def native_p95_duration_ms(self) -> int:
        return _percentile_int([r.duration_ms for r in self.native_runs], 0.95)

    @property
    def mcp_p95_duration_ms(self) -> int:
        return _percentile_int([r.duration_ms for r in self.mcp_runs], 0.95)

    def _cache_hit_ratio(self, runs: list[RunResult]) -> float:
        """Fraction of total *input-side* tokens served from cache.
        Denominator = input_tokens + cache_read + cache_creation across
        all runs. cache_creation is *not* a hit (it's the cost of
        seeding cache for next time), so the numerator is just
        cache_read_input_tokens. Returns 0.0 if there's no input data."""
        total_in = 0
        total_cache_read = 0
        for r in runs:
            total_in += (
                r.input_tokens
                + r.cache_read_input_tokens
                + r.cache_creation_input_tokens
            )
            total_cache_read += r.cache_read_input_tokens
        if total_in <= 0:
            return 0.0
        return total_cache_read / total_in

    @property
    def native_cache_hit_ratio(self) -> float:
        return self._cache_hit_ratio(self.native_runs)

    @property
    def mcp_cache_hit_ratio(self) -> float:
        return self._cache_hit_ratio(self.mcp_runs)

    @property
    def native_outcomes(self) -> dict[str, int]:
        """Map of outcome-kind → count for the Native runs.
        Imported lazily so models.py doesn't gain a runtime dep on
        outcomes.py (keeps the dependency graph one-way: outcomes → models)."""
        from token_compare.outcomes import aggregate
        return aggregate(self.native_runs)

    @property
    def mcp_outcomes(self) -> dict[str, int]:
        from token_compare.outcomes import aggregate
        return aggregate(self.mcp_runs)


class BenchmarkResult(BaseModel):
    started_at: str
    finished_at: str
    operator: str
    model: str
    models: list[str] = Field(default_factory=list)
    org_name: str
    tool_commit: str
    runs_per_path: int
    scenarios: list[ScenarioResult]

    @property
    def total_native_cost(self) -> float:
        return sum(r.total_cost_usd for s in self.scenarios for r in s.native_successful)

    @property
    def total_mcp_cost(self) -> float:
        return sum(r.total_cost_usd for s in self.scenarios for r in s.mcp_successful)

    @property
    def total_native_input_tokens(self) -> int:
        return sum(r.input_tokens for s in self.scenarios for r in s.native_successful)

    @property
    def total_mcp_input_tokens(self) -> int:
        return sum(r.input_tokens for s in self.scenarios for r in s.mcp_successful)

    @property
    def total_native_total_input_tokens(self) -> int:
        return sum(
            r.input_tokens + r.cache_read_input_tokens + r.cache_creation_input_tokens
            for s in self.scenarios for r in s.native_successful
        )

    @property
    def total_mcp_total_input_tokens(self) -> int:
        return sum(
            r.input_tokens + r.cache_read_input_tokens + r.cache_creation_input_tokens
            for s in self.scenarios for r in s.mcp_successful
        )

    @property
    def average_multiplier(self) -> Optional[float]:
        mults = [s.cheaper_multiplier for s in self.scenarios if s.cheaper_multiplier is not None]
        return sum(mults) / len(mults) if mults else None
