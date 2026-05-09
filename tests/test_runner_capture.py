from unittest.mock import MagicMock


def _make_response(status, text="", headers=None):
    """Build a minimal stand-in for the httpx response shape used by mcp_proxy."""
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.headers = headers or {}
    return r


def test_mcp_session_captures_http_error_response():
    """A non-2xx response from the gateway populates last_error_response on the session."""
    from token_compare.mcp_proxy import McpServerSession, McpServerSpec, McpError

    spec = McpServerSpec(name="test", url="https://x", transport="http")
    sess = McpServerSession(spec=spec, sf_access_token="token")

    # Stub the http client so _rpc gets a 401 back.
    sess._client = MagicMock()
    sess._client.post.return_value = _make_response(
        status=401,
        text='{"error":"Invalid token","error_description":"Session expired"}',
        headers={"mcp-session-id": "abc123",
                 "retry-after": "30",
                 "content-type": "application/json",
                 "authorization": "Bearer secret"},  # this should NOT be captured
    )

    raised = False
    try:
        sess._rpc("initialize", {})
    except McpError:
        raised = True
    assert raised, "expected McpError"

    cap = sess.last_error_response
    assert cap is not None
    assert cap["status_code"] == 401
    assert "Invalid token" in cap["body_excerpt"]
    # Allowed headers
    assert cap["headers"].get("mcp-session-id") == "abc123"
    assert cap["headers"].get("retry-after") == "30"
    assert cap["headers"].get("content-type") == "application/json"
    # Auth header should NOT leak into the capture.
    assert "authorization" not in {k.lower() for k in cap["headers"]}


def test_mcp_proxy_aggregates_last_error_response():
    """McpProxy.last_error_response returns the most recent session capture."""
    from token_compare.mcp_proxy import McpProxy, McpServerSession, McpServerSpec

    spec = McpServerSpec(name="test", url="https://x", transport="http")
    sess = McpServerSession(spec=spec, sf_access_token="token")
    sess.last_error_response = {
        "status_code": 401,
        "body_excerpt": "Invalid token",
        "headers": {"mcp-session-id": "abc"},
    }
    proxy = McpProxy(sessions=[sess])
    cap = proxy.last_error_response
    assert cap is not None
    assert cap["status_code"] == 401


def test_runner_captures_inference_error(monkeypatch, tmp_path):
    """When the Anthropic SDK raises APIError, RunResult.inference_error is populated."""
    from token_compare.messages_runner import run_once
    from token_compare.models import Scenario, SuccessCriteria, PathName
    from unittest.mock import MagicMock
    import anthropic

    # Build an APIError-like exception via the SDK's actual class. The
    # SDK constructor wants (message, request, *, body) — we bypass with
    # a subclass so the test stays insulated from constructor changes.
    class FakeAPIError(anthropic.APIError):
        def __init__(self):
            self.message = "rate limit hit"
            self.body = {"type": "error",
                          "error": {"type": "rate_limit_error",
                                    "message": "Number of request tokens exceeded"}}
            self.status_code = 429
            self.request = None

    def fake_create(**kwargs):
        raise FakeAPIError()

    fake_messages = MagicMock()
    fake_messages.create = fake_create
    fake_client = MagicMock()
    fake_client.messages = fake_messages
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda model: fake_client,
    )

    s = Scenario(id="sA", title="A", category="c", difficulty="simple",
                 prompt="x", success_criteria=SuccessCriteria())
    r = run_once(
        s, PathName.NATIVE, model="claude-4-5-sonnet",
        max_turns=5, timeout_s=30, mcp_template_path=tmp_path / "x.json",
        sf_token={"access_token": "t", "instance_url": "https://x"},
    )
    assert r.succeeded is False
    assert r.inference_error is not None
    assert r.inference_error.type == "rate_limit_error"
    assert "exceeded" in r.inference_error.message.lower() or \
           "rate limit" in r.inference_error.message.lower()
    # Body excerpt should contain JSON of the error.
    assert "rate_limit_error" in r.inference_error.body_excerpt


def test_runner_captures_traceback_on_unhandled(monkeypatch, tmp_path):
    """An unhandled exception during run_once produces RunResult.runner_traceback."""
    from token_compare.messages_runner import run_once
    from token_compare.models import Scenario, SuccessCriteria, PathName

    def boom(*a, **kw):
        raise RuntimeError("simulated runner blowup")
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model", boom)

    s = Scenario(id="sA", title="A", category="c", difficulty="simple",
                 prompt="x", success_criteria=SuccessCriteria())
    r = run_once(
        s, PathName.NATIVE, model="claude-4-5-sonnet",
        max_turns=5, timeout_s=30, mcp_template_path=tmp_path / "x.json",
        sf_token={"access_token": "t", "instance_url": "https://x"},
    )
    assert r.succeeded is False
    assert r.runner_traceback is not None
    assert "simulated runner blowup" in r.runner_traceback
    assert "RuntimeError" in r.runner_traceback


def test_runner_traceback_none_on_success(monkeypatch, tmp_path):
    """Successful runs leave runner_traceback as None."""
    from token_compare.messages_runner import run_once
    from token_compare.models import Scenario, SuccessCriteria, PathName
    from unittest.mock import MagicMock

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "done"

    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [text_block]
    resp.usage = MagicMock(input_tokens=10, output_tokens=5,
                            cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)

    fake_messages = MagicMock()
    fake_messages.create = MagicMock(return_value=resp)
    fake_client = MagicMock()
    fake_client.messages = fake_messages

    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda m: fake_client)

    s = Scenario(id="sA", title="A", category="c", difficulty="simple",
                 prompt="x", success_criteria=SuccessCriteria())
    r = run_once(
        s, PathName.NATIVE, model="claude-4-5-sonnet",
        max_turns=5, timeout_s=30, mcp_template_path=tmp_path / "x.json",
        sf_token={"access_token": "t", "instance_url": "https://x"},
    )
    assert r.succeeded is True
    assert r.runner_traceback is None


def test_make_tool_call_detail_truncates_when_oversize():
    """Inputs/outputs > 2KB get truncated with a marker; truncated=True."""
    from token_compare.messages_runner import _make_tool_call_detail

    big = "x" * 5000
    d = _make_tool_call_detail(name="Bash", input_obj={"q": big}, output_str=big)
    assert d.truncated is True
    assert "[truncated" in d.input_excerpt
    assert "[truncated" in d.output_excerpt
    assert d.error is None


def test_make_tool_call_detail_under_cap_not_truncated():
    """Small inputs/outputs round-trip without truncation."""
    from token_compare.messages_runner import _make_tool_call_detail

    d = _make_tool_call_detail(
        name="Bash",
        input_obj={"command": "echo hi"},
        output_str="hi\n",
    )
    assert d.truncated is False
    assert "echo hi" in d.input_excerpt
    assert "hi" in d.output_excerpt


def test_make_tool_call_detail_binary_content_guard():
    """Mostly control-byte output is replaced with a binary-content marker."""
    from token_compare.messages_runner import _make_tool_call_detail
    # Realistic binary shape: lots of NUL/control bytes (matches PDF/PNG/zip
    # headers). bytes(range(32)) gives 32 control chars; we repeat to push
    # the ratio decisively over 0.5.
    binary = (b"\x00" * 50 + b"\x89PNG\r\n\x1a\n" + bytes(range(32)) * 5).decode("latin-1")
    d = _make_tool_call_detail(name="Bash", input_obj={}, output_str=binary)
    assert d.output_excerpt.startswith("[binary content")
    assert "bytes" in d.output_excerpt


def test_make_tool_call_detail_unicode_text_not_flagged():
    """CJK / Arabic / accented Latin text passes through as-is — they are
    NOT binary, even though many chars have ord > 126."""
    from token_compare.messages_runner import _make_tool_call_detail
    samples = [
        "résumé café",                              # Latin-1 supplement
        "こんにちは、世界。これは日本語のテストです。",   # Japanese
        "你好世界，这是一个中文测试。",                # Chinese
        "مرحبا بالعالم، هذا اختبار باللغة العربية.",  # Arabic
    ]
    for s in samples:
        d = _make_tool_call_detail(name="Bash", input_obj={}, output_str=s)
        assert not d.output_excerpt.startswith("[binary content"), \
            f"Unicode text misclassified as binary: {s!r}"
        assert s in d.output_excerpt


def test_runner_captures_tool_call_details(monkeypatch, tmp_path):
    """A successful run with one tool call populates tool_call_details."""
    from token_compare.messages_runner import run_once
    from token_compare.models import Scenario, SuccessCriteria, PathName
    from unittest.mock import MagicMock

    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.id = "tool_1"
    tool_use_block.name = "Bash"
    tool_use_block.input = {"command": "echo hi"}

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "done"

    resp1 = MagicMock()
    resp1.stop_reason = "tool_use"
    resp1.content = [tool_use_block]
    resp1.usage = MagicMock(input_tokens=10, output_tokens=5,
                             cache_read_input_tokens=0, cache_creation_input_tokens=0)

    resp2 = MagicMock()
    resp2.stop_reason = "end_turn"
    resp2.content = [text_block]
    resp2.usage = MagicMock(input_tokens=20, output_tokens=2,
                             cache_read_input_tokens=0, cache_creation_input_tokens=0)

    responses = iter([resp1, resp2])
    fake_messages = MagicMock()
    fake_messages.create = MagicMock(side_effect=lambda **kw: next(responses))
    fake_client = MagicMock()
    fake_client.messages = fake_messages
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda m: fake_client)
    monkeypatch.setattr(
        "token_compare.messages_runner.dispatch_native_tool",
        lambda name, inp, tok: {"ok": True, "rows": []})

    s = Scenario(id="sA", title="A", category="c", difficulty="simple",
                 prompt="x", success_criteria=SuccessCriteria())
    r = run_once(
        s, PathName.NATIVE, model="claude-4-5-sonnet",
        max_turns=5, timeout_s=30, mcp_template_path=tmp_path / "x.json",
        sf_token={"access_token": "t", "instance_url": "https://x"},
    )
    assert r.succeeded is True
    assert len(r.tool_call_details) == 1
    detail = r.tool_call_details[0]
    assert detail.name == "Bash"
    assert "echo hi" in detail.input_excerpt
    assert "ok" in detail.output_excerpt or "rows" in detail.output_excerpt
    assert detail.truncated is False
    assert detail.error is None


def test_runner_captures_tool_error(monkeypatch, tmp_path):
    """A tool that throws gets recorded with error message in detail.error."""
    from token_compare.messages_runner import run_once
    from token_compare.models import Scenario, SuccessCriteria, PathName
    from unittest.mock import MagicMock

    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.id = "tool_1"
    tool_use_block.name = "Bash"
    tool_use_block.input = {"command": "boom"}

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "ok"

    resp1 = MagicMock()
    resp1.stop_reason = "tool_use"
    resp1.content = [tool_use_block]
    resp1.usage = MagicMock(input_tokens=10, output_tokens=5,
                             cache_read_input_tokens=0, cache_creation_input_tokens=0)
    resp2 = MagicMock()
    resp2.stop_reason = "end_turn"
    resp2.content = [text_block]
    resp2.usage = MagicMock(input_tokens=20, output_tokens=2,
                             cache_read_input_tokens=0, cache_creation_input_tokens=0)

    responses = iter([resp1, resp2])
    fake_messages = MagicMock()
    fake_messages.create = MagicMock(side_effect=lambda **kw: next(responses))
    fake_client = MagicMock()
    fake_client.messages = fake_messages
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda m: fake_client)

    def raising_tool(name, inp, tok):
        raise RuntimeError("tool blew up")
    monkeypatch.setattr(
        "token_compare.messages_runner.dispatch_native_tool", raising_tool)

    s = Scenario(id="sA", title="A", category="c", difficulty="simple",
                 prompt="x", success_criteria=SuccessCriteria())
    r = run_once(
        s, PathName.NATIVE, model="claude-4-5-sonnet",
        max_turns=5, timeout_s=30, mcp_template_path=tmp_path / "x.json",
        sf_token={"access_token": "t", "instance_url": "https://x"},
    )
    # The run itself doesn't fail — the tool error is recorded as a
    # tool_result with ERROR content, and the model gets to react.
    assert len(r.tool_call_details) == 1
    detail = r.tool_call_details[0]
    assert detail.error is not None
    assert "tool blew up" in detail.error
