"""Classify a RunResult into one of seven outcome categories.

Today the SPA collapses every failure into 'failed', which hides
qualitatively different failure modes — a model that decided not to
call any tools is a different signal than a 401 from the underlying
REST endpoint, which is different again from running out of turns.

Tier A surfaces these distinctly so the user can tell whether MCP and
Native are failing in similar or different ways. Pure function of the
existing RunResult fields — no schema migration, old reports get the
new classification automatically when re-displayed.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from token_compare.models import RunResult


class OutcomeKind(str, Enum):
    SUCCEEDED = "succeeded"
    MAX_TURNS = "max_turns"
    INFERENCE_ERROR = "inference_error"
    MCP_INIT_FAILED = "mcp_init_failed"
    NO_TOOL_CALLS = "no_tool_calls"
    TOOL_AUTH_ERROR = "tool_auth_error"
    OTHER = "other"


# Human-readable labels for the SPA. Kept short — these render as pills.
OUTCOME_LABELS: dict[str, str] = {
    OutcomeKind.SUCCEEDED.value: "succeeded",
    OutcomeKind.MAX_TURNS.value: "max turns",
    OutcomeKind.INFERENCE_ERROR.value: "inference error",
    OutcomeKind.MCP_INIT_FAILED.value: "mcp init failed",
    OutcomeKind.NO_TOOL_CALLS.value: "no tool calls",
    OutcomeKind.TOOL_AUTH_ERROR.value: "tool auth error",
    OutcomeKind.OTHER.value: "other failure",
}


def classify(run: RunResult) -> OutcomeKind:
    """Return the outcome category for a single run.

    Order of checks matters: more-specific patterns win over the
    catch-all 'other'.
    """
    if run.succeeded:
        return OutcomeKind.SUCCEEDED

    err = (run.error or "").lower()

    # The runner emits this exact prefix for the Phase 8 risk
    # (MCP gateway didn't initialize → Salesforce token rejected
    # before tools/list could run).
    if "mcp_init_failed" in err:
        return OutcomeKind.MCP_INIT_FAILED

    # max_turns exhaustion. The runner currently writes
    # "terminal_reason=max_turns:..." for this case.
    if "max_turns" in err or "max turns" in err:
        return OutcomeKind.MAX_TURNS

    # 'inference call failed' / 'inference error' / 'mcp_unresolved_tool_use'
    # are all surfaced by the runner when the SDK call raises or returns
    # a non-progressing stop_reason.
    if (
        "inference error" in err
        or "inference call failed" in err
        or "mcp_unresolved_tool_use" in err
        or "anthropic" in err
    ):
        return OutcomeKind.INFERENCE_ERROR

    # 401/403 from the underlying REST tool dispatch. The native_tools
    # error helper writes "HTTP 401: ..." / "HTTP 403: ..." into the
    # tool_result payload, but the run-level error only contains the
    # last tool's error if the model gave up. Catch the common shapes.
    if (
        "http 401" in err
        or "http 403" in err
        or "invalid_scopes" in err
        or "invalid_auth_header" in err
        or "unauthorized" in err
    ):
        return OutcomeKind.TOOL_AUTH_ERROR

    # Punt response — the legacy parser flagged "no tool calls (model
    # declined the task)" for this case.
    if "no tool calls" in err or "model declined" in err:
        return OutcomeKind.NO_TOOL_CALLS

    return OutcomeKind.OTHER


def aggregate(runs: list[RunResult]) -> dict[str, int]:
    """Count outcomes across a list of runs. Returns a dict keyed by
    the OutcomeKind string values, so the SPA can render it as JSON
    without re-hydrating the enum."""
    out: dict[str, int] = {k.value: 0 for k in OutcomeKind}
    for r in runs:
        out[classify(r).value] += 1
    return out
