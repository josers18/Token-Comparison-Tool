from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_mcp_servers(template_path: Path, *, sf_access_token: str) -> list[dict[str, Any]]:
    """Read the sf-mcp.json template and return the list of MCP server
    descriptors expected by anthropic.messages.create(mcp_servers=...).

    The Anthropic SDK's MCP-connector shape is:
        [{"type": "url", "url": "...", "name": "...", "authorization_token": "..."}]
    """
    template_path = Path(template_path)
    if not template_path.is_file():
        raise FileNotFoundError(template_path)
    raw = json.loads(template_path.read_text(encoding="utf-8"))
    servers = raw.get("mcpServers", {}) or {}
    out: list[dict[str, Any]] = []
    for name, spec in servers.items():
        out.append({
            "type": "url",
            "url": spec["url"],
            "name": name,
            "authorization_token": sf_access_token,
        })
    return out
