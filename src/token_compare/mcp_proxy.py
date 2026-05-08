"""Lightweight MCP-over-HTTP client that lets the messages_runner expose
upstream MCP server tools to a model on platforms that don't support
Anthropic's `mcp_servers` connector beta (e.g. Heroku Inference, which
proxies the request through AWS Bedrock and silently drops mcp_servers).

The flow per benchmark run:
  1. McpServerSession.initialize()  → sends MCP `initialize`, captures the
     mcp-session-id header so subsequent calls can carry it.
  2. McpServerSession.list_tools()  → sends `tools/list`, returns the
     tools as Anthropic-shaped tool defs (prefixed with the server name
     so two servers with the same tool name can co-exist).
  3. McpServerSession.call_tool(name, args) → sends `tools/call`, returns
     the result content the model expects in a `tool_result` block.

The SF Platform MCP gateway uses Streamable HTTP. Responses can be either
`application/json` (single-message replies) or `text/event-stream` (one
or more SSE-framed JSON-RPC messages). We accept both.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


# Newest stable MCP protocol version that's broadly accepted at the time
# of writing. The SF gateway also accepts older revisions; the spec says
# the server may downgrade in its `initialize` response.
_MCP_PROTOCOL_VERSION = "2025-06-18"
_MCP_TIMEOUT_S = 60.0


@dataclass
class McpServerSpec:
    """How to reach one MCP server. Mirrors the shape of the entries in
    config/sf-mcp.json under `mcpServers`."""
    name: str
    url: str


@dataclass
class McpServerSession:
    """A live JSON-RPC-over-HTTP session against one MCP server."""

    spec: McpServerSpec
    sf_access_token: str
    session_id: Optional[str] = None
    _next_id: int = 1
    _client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=_MCP_TIMEOUT_S))

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "McpServerSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- HTTP plumbing -----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.sf_access_token}",
            "Content-Type": "application/json",
            # SF gateway sends back text/event-stream sometimes; advertise both.
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _MCP_PROTOCOL_VERSION,
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _rpc(self, method: str, params: Optional[dict] = None) -> dict[str, Any]:
        """Send one JSON-RPC request and return the parsed response object.

        Raises McpError on protocol-level failure. The server may answer
        as application/json (one message) or text/event-stream (one or
        more SSE-framed messages); we accept both.
        """
        rid = self._next_id
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params

        resp = self._client.post(
            self.spec.url, headers=self._headers(), json=body,
        )
        # Capture the session id from the initialize response header.
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid

        if resp.status_code >= 400:
            raise McpError(
                f"MCP {method} → HTTP {resp.status_code}: {resp.text[:300]}"
            )

        ct = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in ct:
            payload = _parse_sse_response(resp.text, rid)
        else:
            payload = resp.json()

        if "error" in payload:
            raise McpError(
                f"MCP {method} returned error: "
                f"{payload['error'].get('message', payload['error'])}"
            )
        return payload.get("result") or {}

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        """Fire-and-forget JSON-RPC notification (no `id`, no response body)."""
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        self._client.post(self.spec.url, headers=self._headers(), json=body)

    # ---- MCP protocol ------------------------------------------------------

    def initialize(self) -> None:
        """Open an MCP session. Must be called before list_tools/call_tool."""
        self._rpc("initialize", {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": "token-comparison-tool",
                "version": "0.1.0",
            },
        })
        # Per spec the client should send a `notifications/initialized` after
        # the initialize handshake completes. Some servers reject subsequent
        # tools/list if this is skipped.
        try:
            self._notify("notifications/initialized")
        except Exception:
            pass

    def list_tools(self) -> list[dict[str, Any]]:
        """Return Anthropic-shaped tool defs (prefixed with the server name)."""
        result = self._rpc("tools/list", {})
        upstream = result.get("tools") or []
        out: list[dict[str, Any]] = []
        for t in upstream:
            tool_name = t.get("name", "")
            if not tool_name:
                continue
            out.append({
                # Prefix so two servers with overlapping tool names don't clash.
                # Anthropic's tool name regex requires alphanumerics + _ / - only.
                "name": _scope_tool_name(self.spec.name, tool_name),
                "description": t.get("description", ""),
                "input_schema": t.get("inputSchema") or t.get("input_schema") or {
                    "type": "object", "properties": {}, "required": [],
                },
            })
        return out

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Run one upstream tool. Returns a string for the tool_result content."""
        result = self._rpc("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        # tools/call result.content is a list of content blocks ({type, text, ...}).
        content = result.get("content") or []
        is_error = bool(result.get("isError"))
        rendered: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                rendered.append(block.get("text", "") or "")
            elif btype == "image":
                rendered.append("[image content omitted]")
            else:
                # Unknown block type — fall back to JSON so the model
                # at least sees structured data.
                rendered.append(json.dumps(block, default=str))
        prefix = "ERROR: " if is_error else ""
        return prefix + "\n".join(rendered)


class McpError(RuntimeError):
    """Raised when the upstream MCP server returns a protocol-level error."""


@dataclass
class McpProxy:
    """Aggregates one or more McpServerSessions, exposes their tools as a
    single Anthropic tools[] list, and dispatches tool_use blocks back to
    the right server."""

    sessions: list[McpServerSession]
    # Map prefixed (Anthropic-side) tool name → (session, upstream tool name).
    _routing: dict[str, tuple[McpServerSession, str]] = field(default_factory=dict)

    @classmethod
    def from_specs(cls, specs: list[McpServerSpec], *, sf_access_token: str) -> "McpProxy":
        sessions = [
            McpServerSession(spec=s, sf_access_token=sf_access_token)
            for s in specs
        ]
        return cls(sessions=sessions)

    def open(self) -> list[dict[str, Any]]:
        """Initialize every session, list tools, build the routing table.
        Returns the combined Anthropic tools[] list."""
        combined: list[dict[str, Any]] = []
        for s in self.sessions:
            try:
                s.initialize()
                tools = s.list_tools()
            except Exception as e:
                raise McpError(
                    f"failed to open MCP session for {s.spec.name!r}: {e}"
                )
            for t in tools:
                prefixed = t["name"]
                upstream = _unscope_tool_name(prefixed, s.spec.name)
                self._routing[prefixed] = (s, upstream)
            combined.extend(tools)
        return combined

    def call(self, prefixed_tool_name: str, arguments: dict) -> str:
        try:
            session, upstream_name = self._routing[prefixed_tool_name]
        except KeyError:
            return f"ERROR: unknown tool {prefixed_tool_name!r}"
        try:
            return session.call_tool(upstream_name, arguments)
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    def close(self) -> None:
        for s in self.sessions:
            s.close()

    def __enter__(self) -> "McpProxy":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

# Anthropic's tool-name regex requires `^[a-zA-Z0-9_-]{1,64}$`.
_TOOL_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _scope_tool_name(server: str, tool: str) -> str:
    """Prefix the tool name with the server name so collisions across
    multiple MCP servers don't clobber each other. Both halves are
    normalized to the Anthropic-allowed character set."""
    safe_server = _TOOL_NAME_SAFE.sub("_", server)
    safe_tool = _TOOL_NAME_SAFE.sub("_", tool)
    return f"{safe_server}__{safe_tool}"[:64]


def _unscope_tool_name(scoped: str, server: str) -> str:
    """Reverse the scoping done by _scope_tool_name. Returns the upstream
    tool name the MCP server actually expects in tools/call."""
    safe_server = _TOOL_NAME_SAFE.sub("_", server)
    prefix = f"{safe_server}__"
    if scoped.startswith(prefix):
        return scoped[len(prefix):]
    return scoped


def _parse_sse_response(text: str, expected_rpc_id: int) -> dict[str, Any]:
    """Pull the first JSON-RPC payload that matches the expected id out of
    a text/event-stream body. Falls back to the first JSON-RPC envelope if
    no id matches (some servers reply with a 'response' that omits the id
    when there's only one in flight)."""
    fallback: Optional[dict[str, Any]] = None
    for chunk in text.split("\n\n"):
        # Each event has zero-or-more `event:`/`id:` lines and a `data:` line.
        data_lines = [
            line[len("data:"):].lstrip()
            for line in chunk.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        raw = "".join(data_lines)
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("id") == expected_rpc_id:
            return payload
        if fallback is None:
            fallback = payload
    if fallback is not None:
        return fallback
    raise McpError("MCP SSE response did not contain a JSON-RPC payload")


def load_specs_from_template(template_path) -> list[McpServerSpec]:
    """Read config/sf-mcp.json and return the list of upstream MCP servers."""
    from pathlib import Path
    p = Path(template_path)
    if not p.is_file():
        raise FileNotFoundError(p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    out: list[McpServerSpec] = []
    for name, spec in (raw.get("mcpServers") or {}).items():
        url = spec.get("url")
        if not url:
            continue
        out.append(McpServerSpec(name=name, url=url))
    return out
