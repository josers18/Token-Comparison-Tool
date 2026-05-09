from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import anthropic

from token_compare.inference_client import get_client_for_model
from token_compare.mcp_proxy import McpProxy, load_specs_from_template
from token_compare.models import ErrorResponse, InferenceError, PathName, RunResult, Scenario, SuccessCriteria
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


def _tool_blocks_to_results(
    content_blocks,
    *,
    sf_token: dict,
    mcp_proxy: Optional[McpProxy] = None,
) -> list[dict]:
    """Translate every tool_use block in the assistant turn into a
    tool_result block for the next user turn. Dispatches to the Native
    tool registry by default; if `mcp_proxy` is given, uses that instead
    (the MCP path).
    """
    out = []
    for blk in content_blocks:
        if getattr(blk, "type", None) != "tool_use":
            continue
        try:
            if mcp_proxy is not None:
                # mcp_proxy.call already returns a string (text content
                # rendered from upstream tool_result.content blocks).
                result_str = mcp_proxy.call(blk.name, blk.input or {})
            else:
                native_out = dispatch_native_tool(blk.name, blk.input or {}, sf_token)
                # Anthropic accepts strings; JSON is more parser-friendly
                # than Python repr.
                result_str = json.dumps(native_out, default=str)
        except Exception as e:
            result_str = f"ERROR: {type(e).__name__}: {e}"
        out.append({
            "type": "tool_result",
            "tool_use_id": blk.id,
            "content": result_str[:50_000],
        })
    return out


def _capture_mcp_error(mcp_proxy: Optional[McpProxy]) -> Optional[ErrorResponse]:
    """Lift the proxy's most recent HTTP error capture (if any) onto the
    typed `ErrorResponse` shape used by RunResult.error_response. Returns
    None when the proxy is missing or never saw a non-2xx response — the
    RunResult field stays None for clean (or non-MCP-related) failures.

    Defensive on shape: tests stub the proxy with a MagicMock whose
    attribute access auto-generates child mocks; treat anything that
    isn't a real dict (or that fails to validate) as "no capture".
    """
    if mcp_proxy is None:
        return None
    try:
        cap = mcp_proxy.last_error_response
    except Exception:
        return None
    if not isinstance(cap, dict) or not cap:
        return None
    try:
        return ErrorResponse(**cap)
    except Exception:
        return None


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
    messages_create = client.messages.create

    prompt = _build_prompt(scenario)
    messages: list[dict] = [{"role": "user", "content": prompt}]

    base_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 4096,
    }

    # Both paths now use the GA `client.messages.create` endpoint with
    # `tools=[...]`. The Native path's tool list comes from native_tools.py;
    # the MCP path opens an upstream MCP session against the SF gateway,
    # asks for its tools/list, and exposes those tool defs to the model.
    # Heroku Inference is Bedrock-backed and silently drops the
    # mcp_servers parameter on beta.messages.create, so we run the
    # connector loop ourselves instead.
    mcp_proxy: Optional[McpProxy] = None
    mcp_init_error: Optional[str] = None
    # Captured below as soon as the MCP gateway returns a non-2xx, before
    # the proxy is closed in the finally block (close() doesn't drop the
    # capture today, but reading it eagerly keeps us robust to that
    # changing).
    error_response: Optional[ErrorResponse] = None
    inference_error: Optional[InferenceError] = None
    if path == PathName.NATIVE:
        base_kwargs["tools"] = NATIVE_TOOL_DEFS
    else:
        try:
            specs = load_specs_from_template(mcp_template_path)
            mcp_proxy = McpProxy.from_specs(specs, sf_access_token=sf_token["access_token"])
            base_kwargs["tools"] = mcp_proxy.open()
        except Exception as e:
            # Couldn't even reach the MCP gateway — record this as the
            # run's error and skip the model call entirely.
            mcp_init_error = f"mcp_init_failed ({type(e).__name__}): {e}"
            error_response = _capture_mcp_error(mcp_proxy)
            if mcp_proxy is not None:
                mcp_proxy.close()
                mcp_proxy = None
            base_kwargs["tools"] = []  # so the model call doesn't 400

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
        "tools": [t["name"] for t in (base_kwargs.get("tools") or [])],
        "mcp_servers": (
            [{"name": s.spec.name} for s in mcp_proxy.sessions]
            if mcp_proxy is not None else []
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
        if mcp_init_error is not None:
            # The MCP gateway didn't even initialize; don't waste an inference
            # call. Record the error and short-circuit.
            error = mcp_init_error
        else:
            while num_turns < max_turns:
                num_turns += 1
                kwargs = {**base_kwargs, "messages": messages}
                try:
                    resp = _create_with_retry(messages_create, kwargs)
                except anthropic.APIError as e:
                    error = f"inference error: {e}"
                    # Capture structured pieces for the SPA's failed-run replay panel.
                    err_type = "unknown"
                    err_message = str(e)
                    body_str = ""
                    body = getattr(e, "body", None)
                    if isinstance(body, dict):
                        sub = body.get("error") or {}
                        if isinstance(sub, dict):
                            err_type = sub.get("type", err_type)
                            err_message = sub.get("message", err_message)
                        try:
                            import json as _json
                            body_str = _json.dumps(body)[:500]
                        except (TypeError, ValueError):
                            body_str = str(body)[:500]
                    inference_error = InferenceError(
                        type=err_type, message=err_message, body_excerpt=body_str,
                    )
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

                # Both paths now do their tool dispatch locally. Native uses
                # native_tools; MCP uses the McpProxy aggregating upstream
                # MCP servers.
                tool_results = _tool_blocks_to_results(
                    resp.content, sf_token=sf_token, mcp_proxy=mcp_proxy,
                )
                if not tool_results:
                    break
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": tool_results})
                raw_events.append({
                    "type": "user",
                    "message": {"content": tool_results},
                })

            if error is None and last_stop == "tool_use" and num_turns >= max_turns:
                error = "terminal_reason=max_turns: tool-use loop did not terminate"
    finally:
        if mcp_proxy is not None:
            # Pick up any HTTP error captured during the message loop
            # (initialize-time failures were already grabbed above) before
            # tearing the sessions down.
            if error_response is None:
                error_response = _capture_mcp_error(mcp_proxy)
            mcp_proxy.close()
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
        # Only attached on failures — `error_response` stays None on a
        # clean run. Surfacing it on RunResult lets the failed-run replay
        # UI show the gateway status, body, and safe headers without
        # storing secrets.
        error_response=error_response if error is not None else None,
        inference_error=inference_error if error is not None else None,
    )
