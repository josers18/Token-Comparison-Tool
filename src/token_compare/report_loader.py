"""Reverse parser for benchmark reports.

Two entry points:
- `load_json_report(text)` — parse a JSON sidecar (clean path)
- `load_markdown_report(text)` — parse a `.md` report's appendix
  raw_json blocks back into a `BenchmarkResult`. Used for older
  reports that pre-date the JSON sidecar.

The markdown parser tolerates minor formatting changes — it walks the
Appendix section and pairs `<details><summary>...</summary>` blocks
with their `path:` field and embedded ```json``` block, then re-runs
`parse_claude_json` to produce identical `RunResult`s to a live run.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from token_compare.models import (
    BenchmarkResult,
    PathName,
    RunResult,
    ScenarioResult,
    SuccessCriteria,
)
from token_compare.legacy_parser import parse_claude_json


# Lines like:
#   **Date:** 2026-05-06T16:38:51+00:00 → 2026-05-06T16:39:41+00:00
_DATE_RE = re.compile(
    r"\*\*Date:\*\*\s*(\S+)\s*→\s*(\S+)"
)
_OPERATOR_RE = re.compile(r"\*\*Operator:\*\*\s*([^\n]+?)\s*$", re.MULTILINE)
_MODEL_RE = re.compile(
    r"\*\*Model:\*\*\s*([^\s·]+).*?\*\*Runs per path:\*\*\s*(\d+)"
)
_ORG_RE = re.compile(
    r"\*\*Salesforce org:\*\*\s*([^·\n]+?)\s*·\s*\*\*Tool commit:\*\*\s*([^\s\n]+)"
)
_APPENDIX_RE = re.compile(r"##\s+Appendix\s+—\s+Raw\s+Data\s*\n", re.IGNORECASE)
# `### s01_soql_top_accounts` (scenario header in appendix)
_SCENARIO_HEADER_RE = re.compile(r"^###\s+([^\n—]+?)\s*$", re.MULTILINE)
_DETAILS_BLOCK_RE = re.compile(
    r"<details>.*?</details>",
    re.DOTALL,
)
_PATH_RE = re.compile(r"-\s+\*\*path:\*\*\s+(\w+)")
_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def load_json_report(text: str) -> BenchmarkResult:
    """Validate a JSON sidecar back into a BenchmarkResult."""
    data = json.loads(text)
    return BenchmarkResult.model_validate(data)


def load_markdown_report(text: str) -> BenchmarkResult:
    """Reverse-engineer a `BenchmarkResult` from a markdown report.

    Reads the header for metadata, then walks the Appendix scanning for
    `<details>` blocks. Each block contains a `- **path:**` line and an
    embedded ```json``` payload that we feed back through
    `parse_claude_json` to recover a `RunResult`.
    """
    # Header metadata (best-effort; defaults preserved if missing).
    started_at, finished_at = "", ""
    operator, model, org, tool_commit = "unknown", "unknown", "unknown", "unknown"
    runs_per_path = 1

    if m := _DATE_RE.search(text):
        started_at, finished_at = m.group(1), m.group(2)
    if m := _OPERATOR_RE.search(text):
        operator = m.group(1).strip()
    if m := _MODEL_RE.search(text):
        model = m.group(1).strip()
        runs_per_path = int(m.group(2))
    if m := _ORG_RE.search(text):
        org = m.group(1).strip()
        tool_commit = m.group(2).strip()

    # Find the Appendix and split per-scenario.
    appendix_match = _APPENDIX_RE.search(text)
    if not appendix_match:
        raise ValueError(
            "report appears to be missing the Appendix section — "
            "cannot recover per-run telemetry from it"
        )
    appendix = text[appendix_match.end():]

    # Walk scenario sections (### scenario_id) within the appendix.
    scenario_results: list[ScenarioResult] = []
    headers = list(_SCENARIO_HEADER_RE.finditer(appendix))
    for i, h in enumerate(headers):
        sid = h.group(1).strip()
        section_start = h.end()
        section_end = headers[i + 1].start() if i + 1 < len(headers) else len(appendix)
        section = appendix[section_start:section_end]

        native_runs: list[RunResult] = []
        mcp_runs: list[RunResult] = []
        for block in _DETAILS_BLOCK_RE.finditer(section):
            run = _parse_details_block(block.group(0))
            if run is None:
                continue
            if run.path == PathName.NATIVE:
                native_runs.append(run)
            else:
                mcp_runs.append(run)

        if native_runs or mcp_runs:
            scenario_results.append(ScenarioResult(
                scenario_id=sid,
                native_runs=native_runs,
                mcp_runs=mcp_runs,
            ))

    if not scenario_results:
        raise ValueError(
            "report's Appendix had no parseable per-run blocks; "
            "the file may be incomplete or in an unexpected format"
        )

    return BenchmarkResult(
        started_at=started_at or "1970-01-01T00:00:00+00:00",
        finished_at=finished_at or "1970-01-01T00:00:00+00:00",
        operator=operator,
        model=model,
        org_name=org,
        tool_commit=tool_commit,
        runs_per_path=runs_per_path,
        scenarios=scenario_results,
    )


def _parse_details_block(block: str) -> Optional[RunResult]:
    """Pull (path, raw_json) out of one <details>...</details> block and
    re-run `parse_claude_json` to produce an identical RunResult."""
    path_match = _PATH_RE.search(block)
    json_match = _JSON_BLOCK_RE.search(block)
    if not path_match or not json_match:
        return None
    path_str = path_match.group(1).strip().lower()
    try:
        path = PathName.NATIVE if path_str == "native" else PathName.MCP
    except ValueError:
        return None
    raw_text = json_match.group(1).strip()
    if not raw_text or raw_text == "{}":
        # Empty raw_json (rare; happens when the run failed before any
        # claude output was captured). Skip it — better to omit than to
        # invent zeros that misrepresent the spend.
        return None
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    return parse_claude_json(
        raw, path=path, success_criteria=SuccessCriteria(),
    )


def list_reports(reports_dir) -> list[dict]:
    """Return [{name, mtime_iso, size_bytes, has_json}, ...] sorted newest first."""
    from datetime import datetime, timezone
    from pathlib import Path

    reports_dir = Path(reports_dir)
    if not reports_dir.is_dir():
        return []
    md_files = sorted(
        reports_dir.glob("benchmark-*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    out = []
    for md in md_files:
        json_sidecar = md.with_suffix(".json")
        st = md.stat()
        out.append({
            "name": md.name,
            "mtime_iso": datetime.fromtimestamp(
                st.st_mtime, tz=timezone.utc,
            ).isoformat(timespec="seconds"),
            "size_bytes": st.st_size,
            "has_json": json_sidecar.is_file(),
        })
    return out
