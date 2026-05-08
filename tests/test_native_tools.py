import pytest
import httpx
from unittest.mock import patch, MagicMock

from token_compare.native_tools import (
    NATIVE_TOOL_DEFS, dispatch_native_tool,
)


def _mock_token():
    return {"access_token": "TOK", "instance_url": "https://my.salesforce.com"}


def test_native_tool_defs_have_required_fields():
    names = {t["name"] for t in NATIVE_TOOL_DEFS}
    assert "execute_soql" in names
    assert "describe_object" in names
    assert "list_sobjects" in names
    assert "run_dc_query" in names
    for t in NATIVE_TOOL_DEFS:
        assert "input_schema" in t and t["input_schema"]["type"] == "object"
        assert "description" in t


def test_execute_soql_hits_query_endpoint():
    captured = {}
    def fake_get(url, headers, params, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"records": [{"Id": "001"}], "done": True}
        resp.raise_for_status = lambda: None
        return resp
    with patch.object(httpx, "get", fake_get):
        out = dispatch_native_tool(
            "execute_soql",
            {"query": "SELECT Id FROM Account LIMIT 1"},
            _mock_token(),
        )
    assert "/services/data/" in captured["url"]
    assert captured["url"].endswith("/query")
    assert captured["headers"]["Authorization"] == "Bearer TOK"
    assert captured["params"]["q"] == "SELECT Id FROM Account LIMIT 1"
    assert out["records"][0]["Id"] == "001"


def test_describe_object_hits_sobject_describe():
    captured = {}
    def fake_get(url, headers, params, timeout):
        captured["url"] = url
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"fields": [{"name": "Id"}]}
        resp.raise_for_status = lambda: None
        return resp
    with patch.object(httpx, "get", fake_get):
        dispatch_native_tool("describe_object", {"name": "Account"}, _mock_token())
    assert "/sobjects/Account/describe" in captured["url"]


def test_run_dc_query_posts_to_data_cloud_query_api():
    captured = {}
    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["body"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": []}
        resp.raise_for_status = lambda: None
        return resp
    with patch.object(httpx, "post", fake_post):
        dispatch_native_tool("run_dc_query", {"sql": "SELECT 1"}, _mock_token())
    assert "/services/data/" in captured["url"]
    assert "data-cloud" in captured["url"].lower() or "ssot" in captured["url"].lower()
    assert captured["body"]["sql"] == "SELECT 1"


def test_unknown_tool_raises():
    with pytest.raises(KeyError):
        dispatch_native_tool("nonexistent", {}, _mock_token())


def test_http_error_returned_as_error_payload():
    def fake_get(*a, **kw):
        resp = MagicMock()
        resp.status_code = 400
        resp.text = "MALFORMED_QUERY"
        def raise_():
            raise httpx.HTTPStatusError("400", request=None, response=resp)
        resp.raise_for_status = raise_
        return resp
    with patch.object(httpx, "get", fake_get):
        out = dispatch_native_tool(
            "execute_soql", {"query": "garbage"}, _mock_token(),
        )
    assert out.get("error")
    assert "400" in out["error"] or "MALFORMED" in out["error"]
