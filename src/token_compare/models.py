from __future__ import annotations

from enum import Enum
from statistics import median
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


def _median_int(values: list[int]) -> int:
    return int(median(values)) if values else 0


def _median_float(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


class ScenarioResult(BaseModel):
    scenario_id: str
    native_runs: list[RunResult]
    mcp_runs: list[RunResult]

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


class BenchmarkResult(BaseModel):
    started_at: str
    finished_at: str
    operator: str
    model: str
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
