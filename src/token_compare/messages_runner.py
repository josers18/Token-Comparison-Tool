from __future__ import annotations

import json
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
    # Per-call `input_tokens` includes the entire growing message history,
    # so summing across turns produces the *billed* total — same metric the
    # legacy `claude -p` runner aggregated from `modelUsage`. This is the
    # number we want for cost comparisons; do not "fix" by subtracting prior
    # turns' totals.
    acc["input_tokens"] += getattr(u, "input_tokens", 0) or 0
    acc["output_tokens"] += getattr(u, "output_tokens", 0) or 0
    acc["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
    acc["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0


def _create_with_retry(create_fn, kwargs, *, retries: int = 1):
    """One retry on APIError / RateLimitError. Honors Retry-After if present.

    `create_fn` is the bound `client.messages.create` or
    `client.beta.messages.create` callable — passed in so the caller can
    pick which endpoint shape to hit.
    """
    attempt = 0
    while True:
        try:
            return create_fn(**kwargs)
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
            # JSON, not Python repr — Anthropic's tool_result content
            # accepts strings; passing valid JSON keeps downstream parsers
            # (and the model itself) happier than `{'records': [...]}`.
            "content": json.dumps(result, default=str)[:50_000],
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
    # The MCP connector lives on `client.beta.messages.create` and requires
    # an explicit beta opt-in. Native path stays on the GA `client.messages`
    # endpoint where `tools` is the standard parameter.
    if path == PathName.NATIVE:
        base_kwargs["tools"] = NATIVE_TOOL_DEFS
        messages_create = client.messages.create
    else:
        base_kwargs["mcp_servers"] = build_mcp_servers(
            mcp_template_path, sf_access_token=sf_token["access_token"],
        )
        base_kwargs["betas"] = ["mcp-client-2025-04-04"]
        messages_create = client.beta.messages.create

    usage_acc = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    tool_calls: list[str] = []
    num_turns = 0
    final_text: str = ""
    error: Optional[str] = None
    last_stop: Optional[str] = None
    # Build raw_json in the legacy `claude -p` event-array shape so the
    # existing trace UI / extract_trace() in analysis.py keeps working
    # without changes. Schema: list of dicts; "system"/init seeds the
    # tools+mcp_servers panel, "assistant"/"user" pairs feed the per-turn
    # trace, "result" carries the final text.
    raw_events: list[dict[str, Any]] = [{
        "type": "system",
        "subtype": "init",
        "tools": [t["name"] for t in NATIVE_TOOL_DEFS] if path == PathName.NATIVE else [],
        "mcp_servers": (
            [{"name": s["name"]} for s in base_kwargs.get("mcp_servers", [])]
            if path == PathName.MCP else []
        ),
    }]

    def _content_blocks_to_event_dicts(blocks) -> list[dict]:
        """Translate the SDK's content-block objects into the dict shape
        the legacy trace expects."""
        out: list[dict] = []
        for blk in blocks or []:
            btype = getattr(blk, "type", None)
            if btype == "text":
                out.append({"type": "text", "text": getattr(blk, "text", "") or ""})
            elif btype == "tool_use":
                out.append({
                    "type": "tool_use",
                    "name": getattr(blk, "name", ""),
                    "input": getattr(blk, "input", {}) or {},
                })
        return out

    try:
        while num_turns < max_turns:
            num_turns += 1
            kwargs = {**base_kwargs, "messages": messages}
            try:
                resp = _create_with_retry(messages_create, kwargs)
            except anthropic.APIError as e:
                error = f"inference error: {e}"
                break
            except Exception as e:
                # Catch-all so structural bugs (TypeError on bad kwargs,
                # network exceptions outside the SDK's APIError hierarchy,
                # etc.) surface as a recorded run failure instead of
                # silently terminating the benchmark.
                error = f"inference call failed ({type(e).__name__}): {e}"
                break

            _accumulate_usage(usage_acc, resp.usage)
            last_stop = resp.stop_reason

            for blk in (resp.content or []):
                btype = getattr(blk, "type", None)
                if btype == "tool_use":
                    tool_calls.append(getattr(blk, "name", ""))
                elif btype == "text":
                    final_text = getattr(blk, "text", "") or final_text

            # Persist this assistant turn into raw_events for the trace UI.
            raw_events.append({
                "type": "assistant",
                "message": {
                    "content": _content_blocks_to_event_dicts(resp.content),
                    "usage": {
                        "input_tokens": getattr(resp.usage, "input_tokens", 0) or 0,
                        "output_tokens": getattr(resp.usage, "output_tokens", 0) or 0,
                        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
                    },
                },
            })

            if resp.stop_reason != "tool_use":
                break

            # Native path: dispatch tools locally and feed tool_result back.
            # MCP path: Inference resolves tools server-side, so a final
            # response should arrive with stop_reason="end_turn". If we
            # *do* see stop_reason="tool_use" on the MCP path, the
            # connector did not resolve the call — record it as an error
            # rather than silently truncating the conversation.
            if path == PathName.NATIVE:
                tool_results = _native_tool_blocks_to_results(resp.content, sf_token)
                if not tool_results:
                    break
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": tool_results})
                raw_events.append({
                    "type": "user",
                    "message": {"content": tool_results},
                })
            else:
                error = (
                    "mcp_unresolved_tool_use: inference returned stop_reason='tool_use' "
                    "on the MCP path — connector did not resolve the tool call"
                )
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

    raw_events.append({"type": "result", "result": final_text[:2000]})

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
        raw_json=raw_events,
    )
