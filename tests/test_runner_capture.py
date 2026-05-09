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
