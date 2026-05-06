import json
import os
import stat
from pathlib import Path

import pytest

from token_compare.mcp_config import resolve_template


def test_resolve_template_substitutes_token(tmp_path):
    tmpl = tmp_path / "sf-mcp.json"
    tmpl.write_text(json.dumps({
        "mcpServers": {"x": {"url": "https://x", "headers": {"Authorization": "Bearer ${SF_ACCESS_TOKEN}"}}}
    }))
    out = resolve_template(tmpl, {"SF_ACCESS_TOKEN": "REAL_TOKEN"})
    try:
        data = json.loads(out.read_text())
        assert data["mcpServers"]["x"]["headers"]["Authorization"] == "Bearer REAL_TOKEN"
        # Permissions are 0600 (owner rw only)
        mode = out.stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        out.unlink(missing_ok=True)


def test_resolve_template_rejects_invalid_result(tmp_path):
    tmpl = tmp_path / "bad.json"
    tmpl.write_text("{invalid json ${X}")
    with pytest.raises(Exception):  # json.JSONDecodeError
        resolve_template(tmpl, {"X": "value"})
