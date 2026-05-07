from __future__ import annotations

from typing import Any
from urllib.parse import quote
import httpx

# Pinned version — bump when we want new SOQL/REST features.
_API_VERSION = "v60.0"
_REST_TIMEOUT_S = 30.0


NATIVE_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "execute_soql",
        "description": (
            "Run a SOQL query against the connected Salesforce org and "
            "return the raw query result. Use SOQL syntax — single-line "
            "queries are easiest. Returns {records, totalSize, done}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "SOQL query to execute"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "describe_object",
        "description": (
            "Return the field list and metadata for a given sObject. Use "
            "this to discover the correct field API names before composing "
            "SOQL. Argument: sObject API name (e.g. 'Account', 'Contact', "
            "'UnifiedssotAccountAcc__dlm')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "sObject API name"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_sobjects",
        "description": (
            "Return the list of available sObjects in the org, optionally "
            "filtered by a substring match against the API name. This org "
            "has thousands of sObjects — always pass a `filter` to keep the "
            "response small."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": "Substring to match against sObject names",
                },
            },
            "required": ["filter"],
        },
    },
    {
        "name": "run_dc_query",
        "description": (
            "Run a Data Cloud SQL query against the connected Data Cloud "
            "instance. Argument: a SQL string. Returns {data, metadata}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "Data Cloud SQL"},
            },
            "required": ["sql"],
        },
    },
]


def _headers(token: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token['access_token']}",
        "Content-Type": "application/json",
    }


def _err(e: Exception) -> dict[str, Any]:
    if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    return {"error": f"{type(e).__name__}: {str(e)[:300]}"}


def _execute_soql(args: dict, token: dict) -> dict:
    base = token["instance_url"].rstrip("/")
    url = f"{base}/services/data/{_API_VERSION}/query"
    try:
        resp = httpx.get(
            url, headers=_headers(token),
            params={"q": args["query"]}, timeout=_REST_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return _err(e)


def _describe_object(args: dict, token: dict) -> dict:
    base = token["instance_url"].rstrip("/")
    # Percent-escape the name so a confused model can't slip ".." or "?"
    # into the path and reach a sibling REST endpoint.
    safe_name = quote(args["name"], safe="")
    url = f"{base}/services/data/{_API_VERSION}/sobjects/{safe_name}/describe"
    try:
        resp = httpx.get(url, headers=_headers(token), params={}, timeout=_REST_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return _err(e)


def _list_sobjects(args: dict, token: dict) -> dict:
    base = token["instance_url"].rstrip("/")
    url = f"{base}/services/data/{_API_VERSION}/sobjects"
    try:
        resp = httpx.get(url, headers=_headers(token), params={}, timeout=_REST_TIMEOUT_S)
        resp.raise_for_status()
        body = resp.json()
        f = args["filter"].lower()
        # Trim to matching names so we don't blow the context window
        names = [
            o.get("name") for o in body.get("sobjects", [])
            if f in (o.get("name", "").lower())
        ]
        return {"matches": names[:200], "total": len(names)}
    except Exception as e:
        return _err(e)


def _run_dc_query(args: dict, token: dict) -> dict:
    base = token["instance_url"].rstrip("/")
    # Salesforce Data Cloud query API path. The Heroku-hosted MCP server
    # uses the same endpoint shape; this is the direct REST equivalent.
    url = f"{base}/services/data/{_API_VERSION}/ssot/query-sql"
    try:
        resp = httpx.post(
            url, headers=_headers(token),
            json={"sql": args["sql"]}, timeout=_REST_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return _err(e)


_DISPATCH = {
    "execute_soql": _execute_soql,
    "describe_object": _describe_object,
    "list_sobjects": _list_sobjects,
    "run_dc_query": _run_dc_query,
}


def dispatch_native_tool(name: str, args: dict, token: dict) -> dict:
    if name not in _DISPATCH:
        raise KeyError(f"unknown native tool: {name}")
    return _DISPATCH[name](args, token)
