from token_compare.models import PathName, RunResult
from token_compare.outcomes import OutcomeKind, aggregate, classify


def _run(succeeded: bool = True, error: str | None = None) -> RunResult:
    return RunResult(
        path=PathName.NATIVE,
        input_tokens=10, output_tokens=2,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
        total_cost_usd=0.001, num_turns=1, duration_ms=100,
        tool_calls=[], succeeded=succeeded, error=error, raw_json=None,
    )


def test_succeeded():
    assert classify(_run(succeeded=True)) == OutcomeKind.SUCCEEDED


def test_max_turns():
    r = _run(succeeded=False, error="terminal_reason=max_turns: tool-use loop did not terminate")
    assert classify(r) == OutcomeKind.MAX_TURNS


def test_inference_error_apierror():
    r = _run(succeeded=False, error="inference error: 503 Service Unavailable")
    assert classify(r) == OutcomeKind.INFERENCE_ERROR


def test_inference_error_typeerror_path():
    r = _run(succeeded=False, error="inference call failed (TypeError): unexpected keyword argument 'mcp_servers'")
    assert classify(r) == OutcomeKind.INFERENCE_ERROR


def test_inference_error_mcp_unresolved():
    r = _run(succeeded=False, error="mcp_unresolved_tool_use: connector did not resolve the call")
    assert classify(r) == OutcomeKind.INFERENCE_ERROR


def test_mcp_init_failed():
    r = _run(succeeded=False, error="mcp_init_failed (McpError): failed to open MCP session")
    assert classify(r) == OutcomeKind.MCP_INIT_FAILED


def test_tool_auth_error_invalid_scopes():
    r = _run(succeeded=False, error='HTTP 401: [{"message":"INVALID_SCOPES","errorCode":"INVALID_AUTH_HEADER"}]')
    assert classify(r) == OutcomeKind.TOOL_AUTH_ERROR


def test_tool_auth_error_403():
    r = _run(succeeded=False, error="HTTP 403: forbidden")
    assert classify(r) == OutcomeKind.TOOL_AUTH_ERROR


def test_no_tool_calls():
    r = _run(succeeded=False, error="no tool calls (model declined the task)")
    assert classify(r) == OutcomeKind.NO_TOOL_CALLS


def test_other_fallback():
    r = _run(succeeded=False, error="something nobody expected")
    assert classify(r) == OutcomeKind.OTHER


def test_other_with_no_error_string():
    # succeeded=false but error is empty/None — still 'other', not a crash
    assert classify(_run(succeeded=False, error=None)) == OutcomeKind.OTHER
    assert classify(_run(succeeded=False, error="")) == OutcomeKind.OTHER


def test_specific_patterns_win_over_generic():
    # 'mcp_init_failed' contains the substring 'failed' but should map to
    # MCP_INIT_FAILED, not OTHER. This is the priority-order test.
    r = _run(succeeded=False, error="mcp_init_failed: gateway 401")
    assert classify(r) == OutcomeKind.MCP_INIT_FAILED


def test_aggregate_counts_per_kind():
    runs = [
        _run(succeeded=True),
        _run(succeeded=True),
        _run(succeeded=False, error="terminal_reason=max_turns"),
        _run(succeeded=False, error="HTTP 401: bad auth"),
        _run(succeeded=False, error="something else"),
    ]
    counts = aggregate(runs)
    assert counts[OutcomeKind.SUCCEEDED.value] == 2
    assert counts[OutcomeKind.MAX_TURNS.value] == 1
    assert counts[OutcomeKind.TOOL_AUTH_ERROR.value] == 1
    assert counts[OutcomeKind.OTHER.value] == 1
    # Every kind appears in the dict, even when zero.
    assert sum(counts.values()) == len(runs)
    assert all(k.value in counts for k in OutcomeKind)


def test_aggregate_empty():
    counts = aggregate([])
    assert all(v == 0 for v in counts.values())
    assert OutcomeKind.SUCCEEDED.value in counts  # all keys still present
