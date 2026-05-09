from datetime import datetime, timedelta, timezone
from token_compare.history import walk_history, METRICS


def _legacy_payload(model: str, native_cost: float, mcp_cost: float,
                    scenario_id: str = "s1", prompt: str = "x") -> dict:
    return {
        "model": model, "models": [model],
        "scenarios": [{
            "scenario_id": scenario_id, "prompt": prompt,
            "native_runs": [{"path": "native", "input_tokens": 10,
                             "output_tokens": 5,
                             "cache_read_input_tokens": 0,
                             "total_cost_usd": native_cost, "num_turns": 1,
                             "duration_ms": 100, "tool_calls": [],
                             "succeeded": True, "raw_json": {}}],
            "mcp_runs": [{"path": "mcp", "input_tokens": 10,
                          "output_tokens": 5,
                          "cache_read_input_tokens": 0,
                          "total_cost_usd": mcp_cost, "num_turns": 1,
                          "duration_ms": 100, "tool_calls": [],
                          "succeeded": True, "raw_json": {}}],
        }],
    }


def test_walk_history_basic():
    rows = [
        {"id": "r1", "started_at": datetime(2026,4,1,tzinfo=timezone.utc),
         "payload_json": _legacy_payload("sonnet", 0.01, 0.02)},
        {"id": "r2", "started_at": datetime(2026,4,2,tzinfo=timezone.utc),
         "payload_json": _legacy_payload("sonnet", 0.015, 0.022)},
    ]
    out = walk_history(rows, scenario_id="s1", model="sonnet", metric="cost")
    assert len(out["points"]) == 2
    assert out["points"][0]["report_id"] == "r1"
    assert out["points"][0]["native"] == 0.01
    assert out["points"][1]["native"] == 0.015


def test_walk_history_skips_other_models():
    rows = [
        {"id": "r1", "started_at": datetime(2026,4,1,tzinfo=timezone.utc),
         "payload_json": _legacy_payload("sonnet", 0.01, 0.02)},
        {"id": "r2", "started_at": datetime(2026,4,2,tzinfo=timezone.utc),
         "payload_json": _legacy_payload("opus", 0.05, 0.10)},
    ]
    out = walk_history(rows, scenario_id="s1", model="sonnet", metric="cost")
    # Only r1 contributed (r2 is opus).
    assert len(out["points"]) == 1
    assert out["points"][0]["report_id"] == "r1"


def test_walk_history_change_marker_for_prompt_edit():
    rows = [
        {"id": "r1", "started_at": datetime(2026,4,1,tzinfo=timezone.utc),
         "payload_json": _legacy_payload("sonnet", 0.01, 0.02, prompt="A")},
        {"id": "r2", "started_at": datetime(2026,4,2,tzinfo=timezone.utc),
         "payload_json": _legacy_payload("sonnet", 0.01, 0.02, prompt="B")},
    ]
    out = walk_history(rows, scenario_id="s1", model="sonnet", metric="cost")
    markers = out["change_markers"]
    assert any(m["kind"] == "prompt_edited" for m in markers)


def test_walk_history_metric_cache():
    rows = [
        {"id": "r1", "started_at": datetime(2026,4,1,tzinfo=timezone.utc),
         "payload_json": _legacy_payload("sonnet", 0.01, 0.02)},
    ]
    out = walk_history(rows, scenario_id="s1", model="sonnet", metric="cache")
    # cache_read_input_tokens=0 in fixture → ratio 0
    assert out["points"][0]["native"] == 0.0


def test_walk_history_filters_since():
    rows = [
        {"id": "r1", "started_at": datetime(2026,3,1,tzinfo=timezone.utc),
         "payload_json": _legacy_payload("sonnet", 0.01, 0.02)},
        {"id": "r2", "started_at": datetime(2026,4,1,tzinfo=timezone.utc),
         "payload_json": _legacy_payload("sonnet", 0.02, 0.03)},
    ]
    since = datetime(2026,3,15,tzinfo=timezone.utc)
    out = walk_history(rows, scenario_id="s1", model="sonnet",
                       metric="cost", since=since)
    assert len(out["points"]) == 1
    assert out["points"][0]["report_id"] == "r2"


def test_walk_history_metric_success():
    """Mix of successful and failed runs returns the success rate."""
    payload = _legacy_payload("sonnet", 0.01, 0.02)
    # Add a second failed native run so success rate = 1/2 = 0.5
    failed_run = {"path": "native", "input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": 0, "total_cost_usd": 0.0,
                  "num_turns": 0, "duration_ms": 1, "tool_calls": [],
                  "succeeded": False, "raw_json": {}}
    payload["scenarios"][0]["native_runs"].append(failed_run)
    rows = [{"id": "r1", "started_at": datetime(2026,4,1,tzinfo=timezone.utc),
             "payload_json": payload}]
    out = walk_history(rows, scenario_id="s1", model="sonnet", metric="success")
    assert out["points"][0]["native"] == 0.5


def test_walk_history_metric_p95_duration():
    """p95 across runs is the nearest-rank percentile."""
    payload = _legacy_payload("sonnet", 0.01, 0.02)
    # Replace the single run with a series of varying durations.
    payload["scenarios"][0]["native_runs"] = [
        {"path": "native", "input_tokens": 10, "output_tokens": 5,
         "cache_read_input_tokens": 0, "total_cost_usd": 0.01,
         "num_turns": 1, "duration_ms": d, "tool_calls": [],
         "succeeded": True, "raw_json": {}}
        for d in [100, 200, 300, 400, 500]
    ]
    rows = [{"id": "r1", "started_at": datetime(2026,4,1,tzinfo=timezone.utc),
             "payload_json": payload}]
    out = walk_history(rows, scenario_id="s1", model="sonnet", metric="p95_duration")
    # p95 of [100,200,300,400,500] nearest-rank = ceil(0.95 * 5) - 1 = 4 → 500
    assert out["points"][0]["native"] == 500.0
