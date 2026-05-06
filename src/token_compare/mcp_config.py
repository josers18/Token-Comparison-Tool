from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path


def resolve_template(template_path: Path, substitutions: dict[str, str]) -> Path:
    """Read a JSON MCP config template, substitute ${VAR} placeholders, write
    a secure temp file, and return its path. Caller is responsible for deleting
    the returned file when done.

    substitutions: {"SF_ACCESS_TOKEN": "<real token>", ...}
    """
    raw = Path(template_path).read_text(encoding="utf-8")
    resolved = raw
    for key, value in substitutions.items():
        resolved = resolved.replace("${" + key + "}", value)

    # Validate the resolved content is still valid JSON
    json.loads(resolved)  # raises JSONDecodeError if invalid

    fd, tmp_path = tempfile.mkstemp(prefix="sf-mcp-resolved-", suffix=".json")
    try:
        # Restrict permissions — file contains a live access token
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        with os.fdopen(fd, "w") as fh:
            fh.write(resolved)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return Path(tmp_path)
