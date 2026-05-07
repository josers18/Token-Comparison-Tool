from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import anthropic

from token_compare.inference_client import get_client_for_model
from token_compare.mcp_path import build_mcp_servers
from token_compare.models import PathName, RunResult, Scenario, SuccessCriteria
from token_compare.native_tools import NATIVE_TOOL_DEFS, dispatch_native_tool
from token_compare.pricing import compute_cost_usd


SHARED_PREAMBLE = (
    "You have access to tools for querying Salesforce and Data Cloud. "
    "Data Cloud Data Model Objects (DMOs, typically ending in __dlm) are "
    "queryable as regular sObjects in this org. "
    "Before querying, use your available tools to discover the correct "
    "object, field, and table names — do not guess schema. "
    "This org has thousands of sObjects; when discovering, narrow results "
    "with filters or grep rather than scanning full lists. "
    "\n\nComplete the user's request and return a concise answer."
)


def _build_prompt(scenario: Scenario) -> str:
    return f"{SHARED_PREAMBLE}\n\n{scenario.prompt}"


def _accumulate_usage(acc: dict[str, int], u) -> None:
    acc["input_tokens"] += getattr(u, "input_tokens", 0) or 0
    acc["output_tokens"] += getattr(u, "output_tokens", 0) or 0
    acc["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
    acc["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0


def _create_with_retry(client, kwargs, *, retries: int = 1):
    """One retry on APIError / RateLimitError. Honors Retry-After if present."""
    attempt = 0
    while True:
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            if attempt >= retries:
                raise
            ra = float(getattr(e, "retry_after", 5) or 5)
            time.sleep(min(ra, 10.0))
            attempt += 1
        except anthropic.APIError:
            if attempt >= retries:
                raise
            time.sleep(1.0)
            attempt += 1


def _native_tool_blocks_to_results(content_blocks, sf_token) -> list[dict]:
    """For each tool_use block, dispatch to the local Native tool and
    return the tool_result blocks Claude expects on the next turn."""
    out = []
    for blk in content_blocks:
        if getattr(blk, "type", None) != "tool_use":
            continue
        try:
            result = dispatch_native_tool(blk.name, blk.input or {}, sf_token)
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        out.append({
            "type": "tool_result",
            "tool_use_id": blk.id,
            "content": str(result)[:50_000],  # guard against runaway sizes
        })
    return out


def run_once(
    scenario: Scenario,
    path: PathName,
    *,
    model: str,
    max_turns: int,
    timeout_s: int,
    mcp_template_path: Path,
    sf_token: dict,
) -> RunResult:
    """Run one scenario through one path. Returns a RunResult with tokens
    aggregated across all turns."""
    started = time.time()
    client = get_client_for_model(model)

    prompt = _build_prompt(scenario)
    messages: list[dict] = [{"role": "user", "content": prompt}]

    base_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 4096,
    }
    if path == PathName.NATIVE:
        base_kwargs["tools"] = NATIVE_TOOL_DEFS
    else:
        base_kwargs["mcp_servers"] = build_mcp_servers(
            mcp_template_path, sf_access_token=sf_token["access_token"],
        )

    usage_acc = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    tool_calls: list[str] = []
    num_turns = 0
    final_text: str = ""
    error: Optional[str] = None
    last_stop: Optional[str] = None

    try:
        while num_turns < max_turns:
            num_turns += 1
            kwargs = {**base_kwargs, "messages": messages}
            try:
                resp = _create_with_retry(client, kwargs)
            except anthropic.APIError as e:
                error = f"inference error: {e}"
                break

            _accumulate_usage(usage_acc, resp.usage)
            last_stop = resp.stop_reason

            # Record tool calls + capture any final text
            for blk in (resp.content or []):
                btype = getattr(blk, "type", None)
                if btype == "tool_use":
                    tool_calls.append(getattr(blk, "name", ""))
                elif btype == "text":
                    final_text = getattr(blk, "text", "") or final_text

            if resp.stop_reason != "tool_use":
                break

            # Native path needs to dispatch tools locally and append tool_result.
            # MCP path: Inference resolves tools server-side, so stop_reason should
            # not normally come back as "tool_use" — but if it does, we have
            # nothing to append, so we treat it as a stuck state and stop.
            if path == PathName.NATIVE:
                tool_results = _native_tool_blocks_to_results(resp.content, sf_token)
                if not tool_results:
                    break
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                break

        if error is None and last_stop == "tool_use" and num_turns >= max_turns:
            error = "terminal_reason=max_turns: tool-use loop did not terminate"
    finally:
        duration_ms = int((time.time() - started) * 1000)

    cost = compute_cost_usd(
        model=model,
        input_tokens=usage_acc["input_tokens"],
        output_tokens=usage_acc["output_tokens"],
        cache_read_input_tokens=usage_acc["cache_read_input_tokens"],
        cache_creation_input_tokens=usage_acc["cache_creation_input_tokens"],
    )

    return RunResult(
        path=path,
        input_tokens=usage_acc["input_tokens"],
        output_tokens=usage_acc["output_tokens"],
        cache_read_input_tokens=usage_acc["cache_read_input_tokens"],
        cache_creation_input_tokens=usage_acc["cache_creation_input_tokens"],
        total_cost_usd=cost,
        num_turns=num_turns,
        duration_ms=duration_ms,
        tool_calls=tool_calls,
        succeeded=(error is None),
        error=error,
        raw_json=None,
    )
