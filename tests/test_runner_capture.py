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
