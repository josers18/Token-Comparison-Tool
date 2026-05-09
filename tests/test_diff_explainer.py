from token_compare.diff_explainer import (
    explain_turn, diff_traces, TurnDiff,
)


def _turn(input_new=0, cache_read=0, cache_create=0, output=0, tools=None,
          turn_index=1):
    return {
        "turn_index": turn_index,
        "input_new": input_new,
        "input_cache_read": cache_read,
        "input_cache_create": cache_create,
        "output_tokens": output,
        "tool_calls": tools or [],
    }


def test_extra_turn_when_one_side_finished():
    diff = explain_turn(None, _turn(input_new=100, output=50))
    assert diff.reason == "extra_turn"
    assert "extra" in (diff.hint or "").lower()


def test_tool_list_reload_first_turn_high_cache_create():
    native = _turn(input_new=200, cache_create=300, turn_index=1)
    mcp = _turn(input_new=200, cache_create=1500, turn_index=1)
    diff = explain_turn(native, mcp)
    assert diff.reason == "tool_list_reload"
    assert diff.input_delta == 0  # input_new is the same; the gap is in cache_create
    assert "cache" in (diff.hint or "").lower()


def test_larger_tool_response_after_tool_call():
    prior_native = _turn(tools=["Bash"])
    prior_mcp = _turn(tools=["mcp__sf"])
    native = _turn(input_new=500, turn_index=2)
    mcp = _turn(input_new=900, turn_index=2)
    diff = explain_turn(native, mcp,
                         prior_native_turn=prior_native,
                         prior_mcp_turn=prior_mcp)
    assert diff.reason == "larger_tool_response"


def test_model_verbosity():
    native = _turn(input_new=200, output=100, turn_index=2)
    mcp = _turn(input_new=200, output=300, turn_index=2)
    diff = explain_turn(native, mcp)
    assert diff.reason == "model_verbosity"


def test_no_reason_when_quiet():
    native = _turn(input_new=200, output=100, turn_index=2)
    mcp = _turn(input_new=205, output=102, turn_index=2)
    diff = explain_turn(native, mcp)
    assert diff.reason is None


def test_diff_traces_zips_by_index():
    native_traces = [
        _turn(input_new=100, turn_index=1),
        _turn(input_new=200, turn_index=2),
    ]
    mcp_traces = [
        _turn(input_new=120, turn_index=1),
        _turn(input_new=210, turn_index=2),
        _turn(input_new=50, turn_index=3),
    ]
    diffs = diff_traces(native_traces, mcp_traces)
    assert len(diffs) == 3
    # Last entry has native=None → reason should be extra_turn
    assert diffs[2].reason == "extra_turn"
