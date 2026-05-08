from pathlib import Path
from unittest.mock import MagicMock

import pytest

from token_compare.messages_runner import run_once
from token_compare.models import PathName, Scenario, SuccessCriteria


def _scenario():
    return Scenario(
        id="s_test", title="t", category="c", difficulty="simple",
        prompt="Find the top 1 Account",
        success_criteria=SuccessCriteria(),
    )


def _make_msg_response(*, stop_reason, content, usage):
    """Build the shape the Anthropic SDK returns from messages.create()."""
    m = MagicMock()
    m.stop_reason = stop_reason
    m.content = content
    m.usage = MagicMock(
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
    )
    return m


def test_native_single_tool_call_then_text(monkeypatch, tmp_path):
    """Two-turn loop: model calls execute_soql, then returns final text."""
    # Turn 1: tool_use
    tool_use = MagicMock()
    tool_use.type = "tool_use"
    tool_use.id = "tu_1"
    tool_use.name = "execute_soql"
    tool_use.input = {"query": "SELECT Id FROM Account LIMIT 1"}
    r1 = _make_msg_response(
        stop_reason="tool_use", content=[tool_use],
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    # Turn 2: end_turn with final text
    text = MagicMock()
    text.type = "text"
    text.text = "Done. Top account: Acme."
    r2 = _make_msg_response(
        stop_reason="end_turn", content=[text],
        usage={"input_tokens": 200, "output_tokens": 30,
               "cache_read_input_tokens": 80, "cache_creation_input_tokens": 0},
    )

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [r1, r2]
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda model_id: fake_client,
    )
    monkeypatch.setattr(
        "token_compare.messages_runner.dispatch_native_tool",
        lambda name, args, tok: {"records": [{"Id": "001"}]},
    )

    result = run_once(
        _scenario(), PathName.NATIVE,
        model="claude-4-5-sonnet", max_turns=10, timeout_s=60,
        mcp_template_path=tmp_path / "unused.json",
        sf_token={"access_token": "T", "instance_url": "https://x"},
    )

    # Aggregated across both turns
    assert result.input_tokens == 300
    assert result.output_tokens == 80
    assert result.cache_read_input_tokens == 80
    assert result.num_turns == 2
    assert result.tool_calls == ["execute_soql"]
    assert result.succeeded is True
    assert result.path == PathName.NATIVE


def test_max_turns_recorded_as_failure(monkeypatch, tmp_path):
    tool_use = MagicMock()
    tool_use.type = "tool_use"
    tool_use.id = "tu_1"
    tool_use.name = "execute_soql"
    tool_use.input = {"query": "SELECT 1"}
    r = _make_msg_response(
        stop_reason="tool_use", content=[tool_use],
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = r
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda mid: fake_client,
    )
    monkeypatch.setattr(
        "token_compare.messages_runner.dispatch_native_tool",
        lambda *a, **kw: {"records": []},
    )

    result = run_once(
        _scenario(), PathName.NATIVE,
        model="claude-4-5-sonnet", max_turns=2, timeout_s=60,
        mcp_template_path=tmp_path / "x.json",
        sf_token={"access_token": "T", "instance_url": "https://x"},
    )

    assert result.succeeded is False
    assert "max_turns" in (result.error or "")
    assert result.num_turns == 2  # cap honored


def test_mcp_path_passes_mcp_servers_not_tools(monkeypatch, tmp_path):
    cfg = tmp_path / "sf-mcp.json"
    cfg.write_text(
        '{"mcpServers":{"x":{"type":"http","url":"https://example",'
        '"headers":{"Authorization":"Bearer ${SF_ACCESS_TOKEN}"}}}}'
    )
    text = MagicMock()
    text.type = "text"
    text.text = "ok"
    r = _make_msg_response(
        stop_reason="end_turn", content=[text],
        usage={"input_tokens": 10, "output_tokens": 1},
    )
    fake_client = MagicMock()
    # MCP path goes through client.beta.messages.create (mcp_servers param
    # only exists on the beta endpoint in anthropic SDK 0.100+).
    fake_client.beta.messages.create.return_value = r
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda mid: fake_client,
    )

    run_once(
        _scenario(), PathName.MCP,
        model="claude-4-5-sonnet", max_turns=5, timeout_s=60,
        mcp_template_path=cfg,
        sf_token={"access_token": "TOK", "instance_url": "https://x"},
    )

    kwargs = fake_client.beta.messages.create.call_args.kwargs
    assert "mcp_servers" in kwargs
    assert kwargs["mcp_servers"][0]["authorization_token"] == "TOK"
    assert "tools" not in kwargs
    assert kwargs.get("betas") == ["mcp-client-2025-04-04"]
    # Native-path endpoint must NOT have been called for this run.
    fake_client.messages.create.assert_not_called()


def test_mcp_path_flags_unresolved_tool_use(monkeypatch, tmp_path):
    """If Inference returns stop_reason='tool_use' on the MCP path, the
    connector did NOT resolve the call server-side — record an explicit
    error rather than silently truncating the conversation."""
    cfg = tmp_path / "sf-mcp.json"
    cfg.write_text(
        '{"mcpServers":{"x":{"type":"http","url":"https://example",'
        '"headers":{"Authorization":"Bearer ${SF_ACCESS_TOKEN}"}}}}'
    )
    tu = MagicMock()
    tu.type = "tool_use"
    tu.name = "some_mcp_tool"
    tu.id = "tu_1"
    tu.input = {}
    r = _make_msg_response(
        stop_reason="tool_use", content=[tu],
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    fake_client = MagicMock()
    # MCP path → beta.messages.create
    fake_client.beta.messages.create.return_value = r
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda mid: fake_client,
    )
    result = run_once(
        _scenario(), PathName.MCP,
        model="claude-4-5-sonnet", max_turns=5, timeout_s=60,
        mcp_template_path=cfg,
        sf_token={"access_token": "TOK", "instance_url": "https://x"},
    )
    assert result.succeeded is False
    assert "mcp_unresolved_tool_use" in (result.error or "")


def test_raw_json_populated_with_legacy_event_shape(monkeypatch, tmp_path):
    """raw_json must be a list of {type:...} dicts so analysis.extract_trace
    works without code changes from the local-tool era."""
    text = MagicMock()
    text.type = "text"
    text.text = "Done."
    r = _make_msg_response(
        stop_reason="end_turn", content=[text],
        usage={"input_tokens": 5, "output_tokens": 2},
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = r
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda mid: fake_client,
    )
    result = run_once(
        _scenario(), PathName.NATIVE,
        model="claude-4-5-sonnet", max_turns=5, timeout_s=60,
        mcp_template_path=tmp_path / "unused.json",
        sf_token={"access_token": "T", "instance_url": "https://x"},
    )
    assert isinstance(result.raw_json, list)
    types = [ev.get("type") for ev in result.raw_json]
    assert types[0] == "system"  # init seed
    assert "assistant" in types
    assert "result" in types  # final-text terminator


def test_inference_5xx_retried_then_fails(monkeypatch, tmp_path):
    import anthropic
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = anthropic.APIError(
        message="boom", request=None, body=None,
    )
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda mid: fake_client,
    )
    result = run_once(
        _scenario(), PathName.NATIVE,
        model="claude-4-5-sonnet", max_turns=5, timeout_s=60,
        mcp_template_path=tmp_path / "x.json",
        sf_token={"access_token": "T", "instance_url": "https://x"},
    )
    assert result.succeeded is False
    assert "inference" in (result.error or "").lower()
    # Retried at least once
    assert fake_client.messages.create.call_count >= 2
