from token_compare.analysis import (
    extract_trace, build_comparison, explain_comparison,
    TurnEntry, RunTrace, ScenarioComparison,
)
from token_compare.models import PathName, RunResult, SuccessCriteria


def _fake_raw_native(num_turns=2):
    """Fake raw_json shaped like a real claude -p output: native, 1 Bash call."""
    return [
        {"type": "system", "subtype": "init", "tools": ["Bash", "Edit", "Read"],
         "mcp_servers": []},
        {"type": "assistant",
         "message": {"usage": {"input_tokens": 9, "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 1820, "output_tokens": 3},
                     "content": [{"type": "text", "text": "I'll run a query"}]}},
        {"type": "assistant",
         "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0, "output_tokens": 50},
                     "content": [{"type": "tool_use", "name": "Bash",
                                  "input": {"command": "sf data query ..."}}]}},
        {"type": "user",
         "message": {"content": [{"type": "tool_result",
                                   "is_error": False,
                                   "content": [{"type": "text",
                                                "text": "{\"records\":[...]}"}]}]}},
        {"type": "assistant",
         "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 1110,
                                "cache_creation_input_tokens": 1146, "output_tokens": 200},
                     "content": [{"type": "text", "text": "Top 5 accounts: ..."}]}},
        {"type": "result", "result": "Top 5 accounts: ...",
         "usage": {"input_tokens": 5, "output_tokens": 200,
                    "cache_read_input_tokens": 1110, "cache_creation_input_tokens": 1146},
         "is_error": False, "num_turns": num_turns,
         "duration_ms": 15000, "total_cost_usd": 0.021,
         "terminal_reason": "completed"},
    ]


def _fake_raw_mcp():
    """MCP version: 13 extra tools at init, 1 mcp tool call."""
    return [
        {"type": "system", "subtype": "init",
         "tools": ["Bash", "Edit", "Read"] + [f"mcp__t__{i}" for i in range(13)],
         "mcp_servers": [{"name": "salesforce_crm"},
                          {"name": "data_cloud_queries"}]},
        {"type": "assistant",
         "message": {"usage": {"input_tokens": 9, "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 4761, "output_tokens": 4},
                     "content": [{"type": "text", "text": "Calling MCP"}]}},
        {"type": "assistant",
         "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0, "output_tokens": 60},
                     "content": [{"type": "tool_use",
                                  "name": "mcp__salesforce_crm__soqlQuery",
                                  "input": {"query": "SELECT Name FROM Account"}}]}},
        {"type": "user",
         "message": {"content": [{"type": "tool_result",
                                   "is_error": False,
                                   "content": [{"type": "text",
                                                "text": "{\"records\":[...]}"}]}]}},
        {"type": "assistant",
         "message": {"usage": {"input_tokens": 6, "cache_read_input_tokens": 4051,
                                "cache_creation_input_tokens": 952, "output_tokens": 150},
                     "content": [{"type": "text", "text": "Top 5 accounts: ..."}]}},
        {"type": "result", "result": "Top 5: ...", "is_error": False,
         "num_turns": 2, "duration_ms": 9000, "total_cost_usd": 0.030,
         "terminal_reason": "completed",
         "usage": {"input_tokens": 6, "output_tokens": 150,
                    "cache_read_input_tokens": 4051, "cache_creation_input_tokens": 952}},
    ]


def _native_run():
    return RunResult(
        path=PathName.NATIVE, input_tokens=14, output_tokens=253,
        cache_read_input_tokens=1110, cache_creation_input_tokens=2966,
        total_cost_usd=0.021, num_turns=2, duration_ms=15000,
        tool_calls=["Bash"], succeeded=True, error=None,
        raw_json=_fake_raw_native(),
    )


def _mcp_run():
    return RunResult(
        path=PathName.MCP, input_tokens=15, output_tokens=214,
        cache_read_input_tokens=4051, cache_creation_input_tokens=5713,
        total_cost_usd=0.030, num_turns=2, duration_ms=9000,
        tool_calls=["mcp__salesforce_crm__soqlQuery"],
        succeeded=True, error=None, raw_json=_fake_raw_mcp(),
    )


def test_extract_trace_native():
    trace = extract_trace(_native_run())
    assert trace.init_tools == ["Bash", "Edit", "Read"]
    assert trace.init_mcp_servers == []
    assert len(trace.turns) == 3   # 3 assistant messages
    # Tool call attached to second turn
    tool_turns = [t for t in trace.turns if t.tool_calls]
    assert len(tool_turns) == 1
    assert tool_turns[0].tool_calls == ["Bash"]
    assert tool_turns[0].tool_results
    assert "records" in tool_turns[0].tool_results[0]


def test_extract_trace_mcp_has_mcp_servers():
    trace = extract_trace(_mcp_run())
    assert "salesforce_crm" in trace.init_mcp_servers
    assert "data_cloud_queries" in trace.init_mcp_servers
    assert len(trace.init_tools) == 16  # 3 + 13 mcp tools


def test_explanation_detects_schema_tax():
    nat = build_comparison("Native", [_native_run()], [extract_trace(_native_run())])
    mcp = build_comparison("MCP", [_mcp_run()], [extract_trace(_mcp_run())])
    text = explain_comparison(nat, mcp)
    # Should detect the cache-create token gap (4761 vs 1820 = 2941 delta)
    assert "schema" in text.lower() or "cache-create" in text.lower() or "tool" in text.lower()
    # Should include the headline summary
    assert "cheaper on this scenario" in text.lower() or "structural" in text.lower()


def test_explanation_handles_failure_asymmetry():
    # Native succeeds, MCP fails
    failing_mcp = _mcp_run()
    failing_mcp.succeeded = False
    nat = build_comparison("Native", [_native_run()], [extract_trace(_native_run())])
    mcp = build_comparison("MCP", [failing_mcp], [extract_trace(failing_mcp)])
    text = explain_comparison(nat, mcp)
    assert "1/1" in text or "0/1" in text or "succeeded" in text.lower()


def test_extract_trace_handles_dict_raw_json():
    """Some failed runs have raw_json as a dict (early-fail). Don't crash."""
    r = RunResult(
        path=PathName.NATIVE, input_tokens=0, output_tokens=0,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
        total_cost_usd=0.0, num_turns=0, duration_ms=0,
        tool_calls=[], succeeded=False, error="bad json",
        raw_json={"_bad_json_stdout_tail": "..."},
    )
    trace = extract_trace(r)
    assert trace.turns == []
    assert trace.succeeded is False
    assert trace.error == "bad json"


def test_build_summary_analysis_native_dominant():
    """When Native wins most scenarios, summary should reflect that."""
    from token_compare.analysis import build_summary_analysis

    result_data = {
        "started_at": "2026-05-06T15:59:00+00:00",
        "finished_at": "2026-05-06T16:09:00+00:00",
        "operator": "me", "model": "sonnet", "org_name": "org",
        "tool_commit": "abc", "runs_per_path": 1,
        "scenarios": [
            {
                "scenario_id": "s01", "native_runs": [_minimal_run("native", 0.02)],
                "mcp_runs": [_minimal_run("mcp", 0.04)],
            },
            {
                "scenario_id": "s05", "native_runs": [_minimal_run("native", 0.10)],
                "mcp_runs": [_minimal_run("mcp", 0.20)],
            },
        ],
    }
    meta = {
        "s01": {"title": "Top accounts", "category": "core-crm", "difficulty": "simple"},
        "s05": {"title": "Pipeline", "category": "core-crm", "difficulty": "complex"},
    }
    summary = build_summary_analysis(result_data, meta)
    assert summary.avg_multiplier > 1
    assert "native" in summary.headline.lower() or "cheaper" in summary.headline.lower()
    assert len(summary.scenarios) == 2
    assert all(s.winner == "native" for s in summary.scenarios)
    assert len(summary.framework_native_wins) == 2
    assert summary.framework_native_pattern is not None  # all core-crm
    assert "sales" in summary.framework_native_pattern.lower() or "soql" in summary.framework_native_pattern.lower()


def test_build_summary_analysis_includes_caveats():
    from token_compare.analysis import build_summary_analysis
    result_data = {
        "started_at": "2026-05-06T16:00:00+00:00",
        "finished_at": "2026-05-06T16:10:00+00:00",
        "operator": "me", "model": "sonnet", "org_name": "org",
        "tool_commit": "abc", "runs_per_path": 1,
        "scenarios": [{
            "scenario_id": "s01",
            "native_runs": [_minimal_run("native", 0.02)],
            "mcp_runs": [_minimal_run("mcp", 0.04)],
        }],
    }
    summary = build_summary_analysis(result_data, {})
    assert any("single-run" in c.lower() or "rerun" in c.lower() for c in summary.caveats)
    assert any("token cost" in c.lower() for c in summary.caveats)


def _minimal_run(path, cost):
    return {
        "path": path,
        "input_tokens": 100, "output_tokens": 50,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "total_cost_usd": cost, "num_turns": 2, "duration_ms": 1000,
        "tool_calls": ["Bash"] if path == "native" else ["mcp__x"],
        "succeeded": True, "error": None, "raw_json": None,
    }
