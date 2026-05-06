"""Round-trip tests for the report loader.

A benchmark result written by `write_markdown` should be recoverable
via `load_markdown_report`. The JSON sidecar path is the easy case;
the markdown parser is the interesting one.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from token_compare.models import (
    BenchmarkResult,
    PathName,
    RunResult,
    ScenarioResult,
)
from token_compare.report import write_markdown
from token_compare.report_loader import (
    list_reports,
    load_json_report,
    load_markdown_report,
)


def _make_run(*, path: PathName, succeeded: bool = True) -> RunResult:
    """Return a RunResult whose raw_json has the same shape claude -p emits.

    The loader feeds raw_json back through `parse_claude_json`, so the
    raw_json must be a list with an init event + a result event.
    """
    raw_json = [
        {
            "type": "system",
            "subtype": "init",
            "tools": ["Bash"] if path == PathName.NATIVE else [],
            "mcp_servers": [] if path == PathName.NATIVE else [
                {"name": "salesforce_crm"},
            ],
        },
        {
            "type": "result",
            "subtype": "success" if succeeded else "error",
            "is_error": not succeeded,
            "duration_ms": 5000,
            "duration_api_ms": 4500,
            "num_turns": 2,
            "result": "ok" if succeeded else "failed",
            "total_cost_usd": 0.025,
            "usage": {
                "input_tokens": 500,
                "cache_creation_input_tokens": 1200,
                "cache_read_input_tokens": 800,
                "output_tokens": 100,
            },
            "modelUsage": {
                "us.anthropic.claude-sonnet-4-5": {
                    "inputTokens": 500,
                    "outputTokens": 100,
                    "cacheReadInputTokens": 800,
                    "cacheCreationInputTokens": 1200,
                    "costUSD": 0.025,
                },
            },
            "terminal_reason": "completed" if succeeded else "error",
        },
    ]
    return RunResult(
        path=path,
        input_tokens=500,
        output_tokens=100,
        cache_read_input_tokens=800,
        cache_creation_input_tokens=1200,
        total_cost_usd=0.025,
        num_turns=2,
        duration_ms=5000,
        tool_calls=["Bash"] if path == PathName.NATIVE else ["mcp__salesforce_crm__soqlQuery"],
        succeeded=succeeded,
        error=None if succeeded else "is_error flag set",
        raw_json=raw_json,
    )


def _make_benchmark() -> BenchmarkResult:
    return BenchmarkResult(
        started_at="2026-05-06T10:00:00+00:00",
        finished_at="2026-05-06T10:05:00+00:00",
        operator="user@example.com",
        model="sonnet",
        org_name="Example Org",
        tool_commit="abc1234",
        runs_per_path=1,
        scenarios=[
            ScenarioResult(
                scenario_id="s01_test_scenario",
                native_runs=[_make_run(path=PathName.NATIVE)],
                mcp_runs=[_make_run(path=PathName.MCP)],
            ),
        ],
    )


def test_markdown_round_trip(tmp_path: Path) -> None:
    """A report written by write_markdown should reload cleanly."""
    bench = _make_benchmark()
    md_path = tmp_path / "benchmark-2026-05-06-1000.md"
    write_markdown(bench, md_path)

    text = md_path.read_text()
    loaded = load_markdown_report(text)

    assert loaded.started_at == bench.started_at
    assert loaded.finished_at == bench.finished_at
    assert loaded.operator == bench.operator
    assert loaded.model == bench.model
    assert loaded.runs_per_path == bench.runs_per_path
    assert len(loaded.scenarios) == 1

    sr = loaded.scenarios[0]
    assert sr.scenario_id == "s01_test_scenario"
    assert len(sr.native_runs) == 1
    assert len(sr.mcp_runs) == 1

    nat = sr.native_runs[0]
    assert nat.path == PathName.NATIVE
    assert nat.succeeded is True
    # Token totals come back through parse_claude_json — should match.
    assert nat.input_tokens == 500
    assert nat.output_tokens == 100
    assert nat.cache_read_input_tokens == 800
    assert nat.cache_creation_input_tokens == 1200
    assert abs(nat.total_cost_usd - 0.025) < 1e-9


def test_json_round_trip(tmp_path: Path) -> None:
    """JSON sidecar should be a clean serialize → parse path."""
    bench = _make_benchmark()
    json_text = json.dumps(bench.model_dump(), default=str)
    loaded = load_json_report(json_text)
    assert loaded.scenarios[0].scenario_id == "s01_test_scenario"
    assert loaded.total_native_cost == 0.025


def test_markdown_parser_rejects_no_appendix() -> None:
    """A markdown blob without the Appendix section is unparseable."""
    text = "# Token Comparison Benchmark\n\n**Date:** 2026-05-06T10:00:00+00:00 → 2026-05-06T10:05:00+00:00\n"
    with pytest.raises(ValueError, match="Appendix"):
        load_markdown_report(text)


def test_list_reports_returns_newest_first(tmp_path: Path) -> None:
    bench = _make_benchmark()
    older = tmp_path / "benchmark-2026-05-01-0900.md"
    newer = tmp_path / "benchmark-2026-05-06-1000.md"
    write_markdown(bench, older)
    write_markdown(bench, newer)
    # Bump newer's mtime so the test isn't flaky on fast filesystems.
    import os, time
    os.utime(newer, (time.time(), time.time() + 60))

    items = list_reports(tmp_path)
    names = [r["name"] for r in items]
    assert names == ["benchmark-2026-05-06-1000.md", "benchmark-2026-05-01-0900.md"]
    assert all("mtime_iso" in r and "size_bytes" in r and "has_json" in r for r in items)


def test_list_reports_detects_json_sidecar(tmp_path: Path) -> None:
    bench = _make_benchmark()
    md_path = tmp_path / "benchmark-2026-05-06-1000.md"
    write_markdown(bench, md_path)
    # Without sidecar
    items = list_reports(tmp_path)
    assert items[0]["has_json"] is False
    # Add sidecar
    md_path.with_suffix(".json").write_text(
        json.dumps(bench.model_dump(), default=str), encoding="utf-8"
    )
    items = list_reports(tmp_path)
    assert items[0]["has_json"] is True
