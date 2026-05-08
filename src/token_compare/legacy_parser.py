"""Legacy parser for old `claude -p` JSON arrays.

Extracted from the retired subprocess runner.py to support backward-compat
deserialization in report_loader.py. All new runs use messages_runner.py.
"""
from __future__ import annotations

import re
from typing import Optional

from token_compare.models import PathName, RunResult, Scenario, SuccessCriteria


# Punt response detection: Claude exited cleanly but declined to attempt the task.
_PUNT_PATTERNS = [
    r"i (?:don't|cannot|can't|do not|am unable to) (?:have access to|access|query|retrieve|find)",
    r"i (?:apologize|am sorry)[^.]*(?:don't|cannot|can't|do not|unable|limited)",
    r"the tools available to me (?:are|include|only)",
    r"i need (?:access to|permission to|you to grant)",
    r"(?:salesforce|data cloud)[^.]{0,100}(?:access|credentials|api)[^.]{0,100}(?:required|needed|necessary)",
]


def _looks_like_punt(text: str) -> bool:
    """Return True if the text contains phrases indicating Claude declined the task."""
    if not text:
        return False
    lower = text.lower()
    for pattern in _PUNT_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


def parse_claude_json(
    raw: dict | list,
    *,
    path: PathName,
    success_criteria: SuccessCriteria,
) -> RunResult:
    # Handle both array (real claude -p output) and dict (legacy/test compatibility)
    if isinstance(raw, list):
        raw_array = raw
        if not raw_array:
            return RunResult(
                path=path, succeeded=False, error="empty JSON array",
                input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                cache_creation_input_tokens=0, total_cost_usd=0.0, num_turns=0,
                duration_ms=0, tool_calls=[], raw_json=None
            )

        result_event = raw_array[-1]
        if result_event.get("type") != "result":
            return RunResult(
                path=path, succeeded=False, error="unexpected claude output shape",
                input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                cache_creation_input_tokens=0, total_cost_usd=0.0, num_turns=0,
                duration_ms=0, tool_calls=[], raw_json=raw_array
            )

        # Extract metrics from result event
        # Prefer modelUsage (aggregate across ALL turns) over usage (final turn only).
        # modelUsage is keyed by model name, e.g.:
        #   "us.anthropic.claude-sonnet-4-5-...": {
        #       "inputTokens": 80, "outputTokens": 2710,
        #       "cacheReadInputTokens": 92737, "cacheCreationInputTokens": 13970,
        #       "costUSD": 0.121, ...
        #   }
        model_usage = result_event.get("modelUsage") or {}
        if model_usage:
            input_tokens = sum(
                int(v.get("inputTokens", 0)) for v in model_usage.values()
            )
            output_tokens = sum(
                int(v.get("outputTokens", 0)) for v in model_usage.values()
            )
            cache_read_input_tokens = sum(
                int(v.get("cacheReadInputTokens", 0)) for v in model_usage.values()
            )
            cache_creation_input_tokens = sum(
                int(v.get("cacheCreationInputTokens", 0)) for v in model_usage.values()
            )
        else:
            usage = result_event.get("usage") or {}
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            cache_read_input_tokens = int(usage.get("cache_read_input_tokens", 0))
            cache_creation_input_tokens = int(usage.get("cache_creation_input_tokens", 0))
        total_cost_usd = float(result_event.get("total_cost_usd", 0.0))
        num_turns = int(result_event.get("num_turns", 0))
        duration_ms = int(result_event.get("duration_ms", 0))
        result_text = result_event.get("result") or ""
        is_error = bool(result_event.get("is_error"))

        # Extract tool calls from assistant message events
        tool_calls = []
        for event in raw_array:
            if event.get("type") == "assistant":
                msg = event.get("message") or {}
                content_list = msg.get("content") or []
                for content_item in content_list:
                    if isinstance(content_item, dict) and content_item.get("type") == "tool_use":
                        tool_calls.append(content_item.get("name", ""))

        raw_json = raw_array
    else:
        # Legacy dict format (backward compatibility)
        usage = raw.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        cache_read_input_tokens = int(usage.get("cache_read_input_tokens", 0))
        cache_creation_input_tokens = int(usage.get("cache_creation_input_tokens", 0))
        total_cost_usd = float(raw.get("total_cost_usd", 0.0))
        num_turns = int(raw.get("num_turns", 0))
        duration_ms = int(raw.get("duration_ms", 0))
        result_text = raw.get("result") or ""
        is_error = bool(raw.get("is_error"))
        tool_calls = [t.get("name", "") for t in (raw.get("tool_uses") or [])]
        raw_json = raw

    # Determine success. Three failure signals from claude:
    #   is_error=true → tool or infrastructure error
    #   terminal_reason != "completed" → e.g., hit max_turns, got cancelled
    #   punt response → no tool calls and text indicates model declined the task
    error: Optional[str] = None
    terminal_reason = (
        result_event.get("terminal_reason") if isinstance(raw, list)
        else raw.get("terminal_reason")
    )
    errors_list = (
        result_event.get("errors", []) if isinstance(raw, list)
        else raw.get("errors", [])
    )
    if is_error:
        error = result_event.get("error") if isinstance(raw, list) else raw.get("error")
        if not error:
            error = "is_error flag set"
    elif terminal_reason and terminal_reason != "completed":
        reason_detail = (errors_list[0] if errors_list else terminal_reason)
        error = f"terminal_reason={terminal_reason}: {reason_detail}"
    # Third failure signal: "punt response" — Claude exited cleanly but didn't
    # actually use any tools and returned a text explaining it can't do the task.
    # A benchmark "success" requires the model to have attempted the work.
    elif not tool_calls and _looks_like_punt(result_text):
        error = "no tool calls (model declined the task)"

    succeeded = (error is None)

    return RunResult(
        path=path,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        total_cost_usd=total_cost_usd,
        num_turns=num_turns,
        duration_ms=duration_ms,
        tool_calls=tool_calls,
        succeeded=succeeded,
        error=error,
        raw_json=raw_json,
    )
