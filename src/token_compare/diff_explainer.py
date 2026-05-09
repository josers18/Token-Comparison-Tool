from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel


NOISE_FLOOR = 100  # token diffs below this don't get a reason
TOOL_LIST_RELOAD_THRESHOLD = 500
LARGER_TOOL_RESPONSE_THRESHOLD = 300


class TurnDiff(BaseModel):
    turn: int
    input_delta: int
    output_delta: int
    total_delta: int
    reason: Optional[Literal[
        "tool_list_reload",
        "larger_tool_response",
        "extra_turn",
        "model_verbosity",
    ]] = None
    hint: Optional[str] = None


def _turn_total(t: dict) -> int:
    return (t.get("input_new", 0)
            + t.get("input_cache_read", 0)
            + t.get("input_cache_create", 0)
            + t.get("output_tokens", 0))


def explain_turn(
    native_turn: dict | None,
    mcp_turn: dict | None,
    *,
    prior_native_turn: dict | None = None,
    prior_mcp_turn: dict | None = None,
) -> TurnDiff:
    """Compute a TurnDiff for one logical turn position. Best-guess reason
    classification based on token shape — may be inaccurate; callers
    should surface a (?) tooltip."""
    n_total = _turn_total(native_turn) if native_turn else 0
    m_total = _turn_total(mcp_turn) if mcp_turn else 0
    n_input = ((native_turn or {}).get("input_new", 0))
    m_input = ((mcp_turn or {}).get("input_new", 0))
    n_output = ((native_turn or {}).get("output_tokens", 0))
    m_output = ((mcp_turn or {}).get("output_tokens", 0))
    n_cache_create = ((native_turn or {}).get("input_cache_create", 0))
    m_cache_create = ((mcp_turn or {}).get("input_cache_create", 0))

    input_delta = m_input - n_input
    output_delta = m_output - n_output
    total_delta = m_total - n_total
    turn_idx = ((mcp_turn or native_turn or {}).get("turn_index", 0))

    # 1. extra_turn — one side has nothing
    if native_turn is None or mcp_turn is None:
        side = "MCP" if mcp_turn else "Native"
        return TurnDiff(
            turn=turn_idx,
            input_delta=input_delta,
            output_delta=output_delta,
            total_delta=total_delta,
            reason="extra_turn",
            hint=f"{side} has an extra turn here",
        )

    # 2. tool_list_reload — early turn with large cache_create gap
    if turn_idx <= 1 and (m_cache_create - n_cache_create) > TOOL_LIST_RELOAD_THRESHOLD:
        delta = m_cache_create - n_cache_create
        return TurnDiff(
            turn=turn_idx, input_delta=input_delta, output_delta=output_delta,
            total_delta=total_delta, reason="tool_list_reload",
            hint=f"MCP reloaded the tool list — +{delta:,} cache-creation tokens",
        )

    # 3. larger_tool_response — input gap on a turn following a tool call
    prior_made_tool_call = bool(
        (prior_native_turn or {}).get("tool_calls")
        or (prior_mcp_turn or {}).get("tool_calls")
    )
    if prior_made_tool_call and input_delta > LARGER_TOOL_RESPONSE_THRESHOLD:
        return TurnDiff(
            turn=turn_idx, input_delta=input_delta, output_delta=output_delta,
            total_delta=total_delta, reason="larger_tool_response",
            hint=f"MCP's tool returned more data — +{input_delta:,} input tokens",
        )

    # 4. model_verbosity — output gap with similar inputs
    if (n_output > 0
        and m_output > 2 * n_output
        and abs(input_delta) < 200):
        return TurnDiff(
            turn=turn_idx, input_delta=input_delta, output_delta=output_delta,
            total_delta=total_delta, reason="model_verbosity",
            hint=f"MCP generated {output_delta:,} more output tokens",
        )

    # No detectable reason
    return TurnDiff(
        turn=turn_idx, input_delta=input_delta,
        output_delta=output_delta, total_delta=total_delta,
    )


def diff_traces(
    native_traces: list[dict],
    mcp_traces: list[dict],
) -> list[TurnDiff]:
    """Zip two trace turn lists by index and produce one TurnDiff per
    position. Asymmetric lengths produce extra_turn entries on the long side."""
    diffs: list[TurnDiff] = []
    n = max(len(native_traces), len(mcp_traces))
    for i in range(n):
        nt = native_traces[i] if i < len(native_traces) else None
        mt = mcp_traces[i] if i < len(mcp_traces) else None
        prior_n = native_traces[i - 1] if i > 0 and i - 1 < len(native_traces) else None
        prior_m = mcp_traces[i - 1] if i > 0 and i - 1 < len(mcp_traces) else None
        diffs.append(explain_turn(
            nt, mt,
            prior_native_turn=prior_n, prior_mcp_turn=prior_m,
        ))
    return diffs
