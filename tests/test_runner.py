import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from token_compare.models import PathName, Scenario, SuccessCriteria
from token_compare.runner import run_once, build_command, parse_claude_json


FIXTURES = Path(__file__).parent / "fixtures"


def _mock_popen_run(monkeypatch, returncode: int, stdout: str, stderr: str = ""):
    """Patch Popen so run_once sees a subprocess that writes stdout/stderr
    to the temp files it opened, then exits with `returncode`."""
    captured: dict = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            # kwargs["stdout"] and ["stderr"] are open file handles (the temp
            # files opened by run_once). Write our fixtures into them so the
            # re-read on disk sees the data we want.
            out_f = kwargs["stdout"]
            err_f = kwargs["stderr"]
            out_f.write(stdout.encode("utf-8"))
            err_f.write(stderr.encode("utf-8"))
            out_f.flush()
            err_f.flush()
            self.returncode = returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr("token_compare.runner.subprocess.Popen", FakePopen)
    return captured


def _mock_popen_timeout(monkeypatch):
    """Patch Popen such that wait(timeout=...) raises subprocess.TimeoutExpired."""
    class FakePopenTimeout:
        def __init__(self, cmd, **kwargs):
            pass
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        def kill(self):
            pass
    monkeypatch.setattr("token_compare.runner.subprocess.Popen", FakePopenTimeout)


@pytest.fixture
def scenario() -> Scenario:
    return Scenario(
        id="s99", title="t", category="c", difficulty="simple",
        prompt="List 5 accounts.", expected_operations=[],
        success_criteria=SuccessCriteria(must_contain=["AnnualRevenue"]),
        notes="",
    )


def test_build_command_native(scenario):
    cmd = build_command(scenario, PathName.NATIVE, model="claude-opus-4-7",
                        max_turns=15, mcp_config_path=Path("config/sf-mcp.json"))
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert "--bare" in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "Bash" in allowed
    assert "--mcp-config" not in cmd


def test_build_command_mcp(scenario):
    cmd = build_command(scenario, PathName.MCP, model="claude-opus-4-7",
                        max_turns=15, mcp_config_path=Path("config/sf-mcp.json"))
    assert "--bare" in cmd
    assert "--mcp-config" in cmd
    assert "config/sf-mcp.json" in " ".join(cmd)
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "Bash" not in allowed


def test_parse_claude_json_success():
    raw = json.loads((FIXTURES / "claude_json_success.json").read_text())
    parsed = parse_claude_json(
        raw, path=PathName.NATIVE,
        success_criteria=SuccessCriteria(must_contain=["AnnualRevenue"]),
    )
    assert parsed.input_tokens == 1204
    assert parsed.output_tokens == 118
    assert parsed.total_cost_usd == 0.021
    assert parsed.num_turns == 2
    assert parsed.tool_calls == ["Bash"]
    assert parsed.cache_creation_input_tokens == 0
    assert parsed.succeeded is True


def test_parse_claude_json_ignores_success_criteria_now():
    """must_contain is no longer enforced — is_error is the sole success signal."""
    raw = json.loads((FIXTURES / "claude_json_success.json").read_text())
    parsed = parse_claude_json(
        raw, path=PathName.NATIVE,
        success_criteria=SuccessCriteria(must_contain=["NotInResult"]),
    )
    # Previously we'd see succeeded=False; now succeeded=True because is_error=false.
    assert parsed.succeeded is True
    assert parsed.error is None


def test_parse_claude_json_is_error_flag():
    raw = json.loads((FIXTURES / "claude_json_failure.json").read_text())
    parsed = parse_claude_json(raw, path=PathName.MCP,
                               success_criteria=SuccessCriteria(must_contain=["x"]))
    assert parsed.succeeded is False
    assert "Tool execution failed" in (parsed.error or "")


def test_run_once_invokes_claude(scenario, tmp_path, monkeypatch):
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    fixture_json = (FIXTURES / "claude_json_success.json").read_text()
    _mock_popen_run(monkeypatch, returncode=0, stdout=fixture_json, stderr="")
    r = run_once(scenario, PathName.NATIVE, model="claude-opus-4-7",
                 max_turns=15, timeout_s=90, mcp_config_path=mcp_cfg)

    assert r.path == PathName.NATIVE
    assert r.input_tokens == 1204
    assert r.succeeded is True


def test_run_once_handles_timeout(scenario, tmp_path, monkeypatch):
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    _mock_popen_timeout(monkeypatch)
    r = run_once(scenario, PathName.MCP, model="claude-opus-4-7",
                 max_turns=15, timeout_s=90, mcp_config_path=mcp_cfg)

    assert r.succeeded is False
    assert "timeout" in (r.error or "").lower()


def test_parse_claude_json_unexpected_shape():
    # Empty array
    parsed = parse_claude_json(
        [], path=PathName.NATIVE,
        success_criteria=SuccessCriteria(must_contain=[])
    )
    assert parsed.succeeded is False
    assert "empty" in (parsed.error or "").lower()

    # Array where last element is not type=result
    parsed = parse_claude_json(
        [{"type": "system"}], path=PathName.NATIVE,
        success_criteria=SuccessCriteria(must_contain=[])
    )
    assert parsed.succeeded is False
    assert "unexpected" in (parsed.error or "").lower()


def test_run_once_parses_valid_json_even_on_nonzero_exit(scenario, tmp_path, monkeypatch):
    """claude -p exits 1 when hitting max_turns but still emits complete JSON.
    The runner should parse that JSON rather than discarding it."""
    mcp_cfg = tmp_path / "sf-mcp.json"
    mcp_cfg.write_text("{}")
    fixture_json = (FIXTURES / "claude_json_success.json").read_text()
    _mock_popen_run(monkeypatch, returncode=1, stdout=fixture_json)
    r = run_once(scenario, PathName.NATIVE, model="sonnet",
                 max_turns=15, timeout_s=90, mcp_config_path=mcp_cfg)

    # We should still have extracted the token counts from the JSON
    assert r.input_tokens == 1204  # same as the fixture's usage.input_tokens
    assert r.succeeded is True


def test_parse_claude_json_flags_max_turns_as_failure():
    """terminal_reason != 'completed' should set succeeded=False with a descriptive error."""
    raw = [
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "subtype": "max_turns",
            "is_error": False,
            "result": "partial answer",
            "num_turns": 15,
            "duration_ms": 100000,
            "total_cost_usd": 0.12,
            "usage": {"input_tokens": 80, "output_tokens": 2710,
                      "cache_read_input_tokens": 92737, "cache_creation_input_tokens": 13970},
            "terminal_reason": "max_turns",
            "errors": ["Reached maximum number of turns (15)"],
        },
    ]
    parsed = parse_claude_json(
        raw, path=PathName.NATIVE,
        success_criteria=SuccessCriteria(must_contain=[]),
    )
    assert parsed.succeeded is False
    assert "max_turns" in (parsed.error or "").lower()
    assert parsed.input_tokens == 80
    assert parsed.total_cost_usd == 0.12


def test_run_once_preserves_bad_stdout_on_json_error(scenario, tmp_path, monkeypatch):
    """When stdout is unparseable, preserve a tail of it on the RunResult."""
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    bad_stdout = '[{"type":"system"},{"type":"result","usage":{"input_tokens":100,' + 'x' * 20000
    # ^ Looks like an array that starts valid but runs off the end (unterminated).
    _mock_popen_run(monkeypatch, returncode=0, stdout=bad_stdout)
    r = run_once(scenario, PathName.NATIVE, model="sonnet",
                 max_turns=15, timeout_s=90, mcp_config_path=mcp_cfg)

    assert r.succeeded is False
    assert "bad json" in (r.error or "").lower()
    # The tail of the stdout should be preserved so users can diagnose.
    assert isinstance(r.raw_json, dict)
    assert "_bad_json_stdout_tail" in r.raw_json
    assert "_stdout_total_bytes" in r.raw_json
    assert r.raw_json["_stdout_total_bytes"] == len(bad_stdout)


def test_run_once_notes_signal_death_in_bad_json_error(scenario, tmp_path, monkeypatch):
    """When subprocess died by signal (timeout/OOM) AND stdout was truncated,
    the error message should say so."""
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    _mock_popen_run(monkeypatch, returncode=-15, stdout='[{"partial', stderr="killed")
    r = run_once(scenario, PathName.NATIVE, model="sonnet",
                 max_turns=15, timeout_s=90, mcp_config_path=mcp_cfg)

    assert r.succeeded is False
    assert "killed by signal 15" in (r.error or "")


def test_run_once_handles_large_stdout(scenario, tmp_path, monkeypatch):
    """Regression: 250KB of valid JSON must be captured and parsed without truncation."""
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    # Build a 250KB JSON array (3× the macOS pipe-buffer boundary that bit us).
    padding = "x" * 200
    big_result = '[{"type":"system","subtype":"init"},'
    for _ in range(1000):
        big_result += (
            '{"type":"assistant","message":{"content":[{"type":"text","text":"'
            + padding + '"}]}},'
        )
    big_result += (
        '{"type":"result","subtype":"success","is_error":false,"result":"done",'
        '"num_turns":3,"duration_ms":1000,"total_cost_usd":0.01,'
        '"usage":{"input_tokens":100,"output_tokens":50,"cache_read_input_tokens":0,'
        '"cache_creation_input_tokens":0},"terminal_reason":"completed"}]'
    )
    assert len(big_result) > 250_000
    _mock_popen_run(monkeypatch, returncode=0, stdout=big_result)
    r = run_once(scenario, PathName.NATIVE, model="sonnet",
                 max_turns=15, timeout_s=90, mcp_config_path=mcp_cfg)

    assert r.succeeded is True
    assert r.input_tokens == 100
    assert r.total_cost_usd == 0.01


def test_parse_claude_json_prefers_modelUsage_over_usage():
    """modelUsage is the aggregate across all turns; usage is only the last turn.
    When both are present, parser should use modelUsage."""
    raw = [
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "the answer",
            "num_turns": 3,
            "duration_ms": 5000,
            "total_cost_usd": 0.05,
            # usage on the result event is ONLY the last turn:
            "usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            },
            # modelUsage is the aggregate across the whole run:
            "modelUsage": {
                "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {
                    "inputTokens": 500,
                    "outputTokens": 200,
                    "cacheReadInputTokens": 8000,
                    "cacheCreationInputTokens": 3000,
                    "costUSD": 0.05,
                },
            },
            "terminal_reason": "completed",
        },
    ]
    parsed = parse_claude_json(
        raw, path=PathName.NATIVE,
        success_criteria=SuccessCriteria(must_contain=[]),
    )
    assert parsed.input_tokens == 500    # not 10 (from usage)
    assert parsed.output_tokens == 200
    assert parsed.cache_read_input_tokens == 8000
    assert parsed.cache_creation_input_tokens == 3000


def test_parse_claude_json_falls_back_to_usage_when_modelUsage_missing():
    """Some very short runs don't include modelUsage — fall back gracefully."""
    raw = [
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "num_turns": 1,
            "duration_ms": 500,
            "total_cost_usd": 0.001,
            "usage": {
                "input_tokens": 42, "output_tokens": 3,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            },
            "terminal_reason": "completed",
        },
    ]
    parsed = parse_claude_json(
        raw, path=PathName.NATIVE,
        success_criteria=SuccessCriteria(must_contain=[]),
    )
    assert parsed.input_tokens == 42


def test_parse_claude_json_flags_punt_response_as_failure():
    """When tool_calls is empty and the model returned a refusal, mark as failure."""
    raw = [
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": (
                "I apologize, but I don't have access to Salesforce-specific tools "
                "to query Data Cloud profiles. The tools available to me are limited to..."
            ),
            "num_turns": 1,
            "duration_ms": 9000,
            "total_cost_usd": 0.011,
            "usage": {
                "input_tokens": 9, "output_tokens": 486,
                "cache_read_input_tokens": 1110, "cache_creation_input_tokens": 631,
            },
            "modelUsage": {
                "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {
                    "inputTokens": 1750, "outputTokens": 486,
                    "cacheReadInputTokens": 1110, "cacheCreationInputTokens": 631,
                    "costUSD": 0.011,
                },
            },
            "terminal_reason": "completed",
        },
    ]
    parsed = parse_claude_json(
        raw, path=PathName.NATIVE,
        success_criteria=SuccessCriteria(must_contain=[]),
    )
    assert parsed.succeeded is False
    assert "declined" in (parsed.error or "").lower() or "no tool calls" in (parsed.error or "").lower()
    # But we still captured usage data — good for the appendix
    assert parsed.input_tokens == 1750


def test_parse_claude_json_does_not_flag_genuine_answer_without_tools_as_punt():
    """If the model successfully answered without needing tools (unlikely but possible),
    don't mark as punt. Only flag when punt phrases are present."""
    raw = [
        {"type": "system", "subtype": "init"},
        {
            "type": "result", "subtype": "success", "is_error": False,
            "result": "The top 5 accounts by revenue are UnitedTech, Assurity, B2B Commerce, and others.",
            "num_turns": 1, "duration_ms": 1000, "total_cost_usd": 0.01,
            "usage": {"input_tokens": 10, "output_tokens": 40,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "terminal_reason": "completed",
        },
    ]
    parsed = parse_claude_json(
        raw, path=PathName.NATIVE,
        success_criteria=SuccessCriteria(must_contain=[]),
    )
    assert parsed.succeeded is True
