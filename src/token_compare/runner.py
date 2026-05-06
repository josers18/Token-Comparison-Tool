from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from token_compare.models import PathName, RunResult, Scenario, SuccessCriteria


# Audit log for every claude -p invocation. Path is resolved lazily from cwd so
# tests can isolate via tmp_path. Disabled when TOKEN_COMPARE_AUDIT_LOG=0.
_AUDIT_LOG_ENV = "TOKEN_COMPARE_AUDIT_LOG"
_DEFAULT_AUDIT_LOG_PATH = Path("reports/commands.log")
# A token is an opaque credential ≥ this many chars; we redact anything matching.
_TOKEN_REDACT_PREFIXES = ("Bearer ",)

# Punt response detection: Claude exited cleanly but declined to attempt the task.
_PUNT_PATTERNS = [
    r"i (?:don't|cannot|can't|do not|am unable to) (?:have access to|access|query|retrieve|find)",
    r"i (?:apologize|am sorry)[^.]*(?:don't|cannot|can't|do not|unable|limited)",
    r"the tools available to me (?:are|include|only)",
    r"i need (?:access to|permission to|you to grant)",
    r"(?:salesforce|data cloud)[^.]{0,100}(?:access|credentials|api)[^.]{0,100}(?:required|needed|necessary)",
]


def _looks_like_punt(text: str) -> bool:
    """Return True if the text contains phrases indicating Claude declined the task."""
    if not text:
        return False
    lower = text.lower()
    for pattern in _PUNT_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


def _redact_cmd_for_log(cmd: list[str]) -> list[str]:
    """Copy cmd, replacing any Authorization-header-looking values with [REDACTED]."""
    # The prompt is the 3rd arg (claude -p <prompt> ...), which may contain
    # customer data from the scenario YAML but not secrets. mcp-config path is
    # fine — it's just a filesystem path. We only need to redact if somehow a
    # Bearer token ended up inline in the command (shouldn't happen today since
    # tokens live in the mcp-config temp file, but guard anyway).
    out = []
    for part in cmd:
        redacted = part
        for prefix in _TOKEN_REDACT_PREFIXES:
            if prefix in redacted:
                redacted = redacted.split(prefix)[0] + prefix + "[REDACTED]"
        out.append(redacted)
    return out


def _audit_log_path() -> Optional[Path]:
    if os.environ.get(_AUDIT_LOG_ENV, "1") == "0":
        return None
    return _DEFAULT_AUDIT_LOG_PATH


def _write_audit_entry(scenario: Scenario, path: PathName, cmd: list[str]) -> None:
    log_path = _audit_log_path()
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Show the prompt separately so the reader can eyeball what was sent.
        # The prompt is always cmd[2] given our build_command shape: [claude, -p, <prompt>, ...]
        prompt = cmd[2] if len(cmd) > 2 else ""
        flag_args = _redact_cmd_for_log(cmd[:2] + cmd[3:])  # [claude, -p] + flags
        entry = (
            f"\n=== {ts} · scenario={scenario.id} · path={path.value} ===\n"
            f"prompt:\n{prompt}\n\n"
            f"flags: {' '.join(flag_args)}\n"
        )
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(entry)
    except OSError:
        # Never fail a benchmark run because the audit log can't be written.
        pass


SHARED_PREAMBLE = (
    "You have access to tools for querying Salesforce and Data Cloud. "
    "Data Cloud Data Model Objects (DMOs, typically ending in __dlm) are "
    "queryable as regular sObjects in this org. "
    "Before querying, use your available tools to discover the correct "
    "object, field, and table names — do not guess schema. "
    "This org has thousands of sObjects; when discovering, narrow results "
    "with filters or grep rather than scanning full lists. "
    "\n\n"
    "Schema-discovery hints (apply whichever match the tools you have):\n"
    "- For Salesforce sObjects: prefer `sf sobject list --sobject all` "
    "(piped to grep) or `sf sobject describe <Name>` over EntityDefinition / "
    "FieldDefinition SOQL — those metadata catalogs reject disjunctions and "
    "require specific filters that are easy to get wrong.\n"
    "- For Data Cloud DMOs: prefer a metadata-listing tool (e.g., "
    "`get_dc_metadata`) or describe the candidate DMO directly. Common naming "
    "patterns: unified DMOs are `Unifiedssot<Entity><Suffix>__dlm` "
    "(e.g., `UnifiedssotAccountAcc__dlm`), source DMOs are `ssot__<Entity>__dlm`.\n"
    "- For piped JSON output: use `--json` flags and pipe to `jq` or pure "
    "`python3 -c 'import json,sys; ...'`. Don't grep raw JSON for field names "
    "since CLI banners and color codes break parsing.\n"
    "\n"
    "Complete the user's request and return a concise answer."
)

MCP_ALLOWED_TOOLS = "mcp__*"


def build_command(
    scenario: Scenario,
    path: PathName,
    *,
    model: str,
    max_turns: int,
    mcp_config_path: Path,
) -> list[str]:
    prompt = f"{SHARED_PREAMBLE}\n\n{scenario.prompt}"
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--max-turns", str(max_turns),
        "--output-format", "json",
        "--bare",
        "--permission-mode", "bypassPermissions",
    ]
    if path == PathName.NATIVE:
        cmd += ["--allowedTools", "Bash"]
    else:
        cmd += ["--mcp-config", str(mcp_config_path)]
        cmd += ["--allowedTools", MCP_ALLOWED_TOOLS]
    return cmd


def parse_claude_json(
    raw: dict | list,
    *,
    path: PathName,
    success_criteria: SuccessCriteria,
) -> RunResult:
    # Handle both array (real claude -p output) and dict (legacy/test compatibility)
    if isinstance(raw, list):
        raw_array = raw
        if not raw_array:
            return RunResult(
                path=path, succeeded=False, error="empty JSON array",
                input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                cache_creation_input_tokens=0, total_cost_usd=0.0, num_turns=0,
                duration_ms=0, tool_calls=[], raw_json=None
            )

        result_event = raw_array[-1]
        if result_event.get("type") != "result":
            return RunResult(
                path=path, succeeded=False, error="unexpected claude output shape",
                input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                cache_creation_input_tokens=0, total_cost_usd=0.0, num_turns=0,
                duration_ms=0, tool_calls=[], raw_json=raw_array
            )

        # Extract metrics from result event
        # Prefer modelUsage (aggregate across ALL turns) over usage (final turn only).
        # modelUsage is keyed by model name, e.g.:
        #   "us.anthropic.claude-sonnet-4-5-...": {
        #       "inputTokens": 80, "outputTokens": 2710,
        #       "cacheReadInputTokens": 92737, "cacheCreationInputTokens": 13970,
        #       "costUSD": 0.121, ...
        #   }
        model_usage = result_event.get("modelUsage") or {}
        if model_usage:
            input_tokens = sum(
                int(v.get("inputTokens", 0)) for v in model_usage.values()
            )
            output_tokens = sum(
                int(v.get("outputTokens", 0)) for v in model_usage.values()
            )
            cache_read_input_tokens = sum(
                int(v.get("cacheReadInputTokens", 0)) for v in model_usage.values()
            )
            cache_creation_input_tokens = sum(
                int(v.get("cacheCreationInputTokens", 0)) for v in model_usage.values()
            )
        else:
            usage = result_event.get("usage") or {}
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            cache_read_input_tokens = int(usage.get("cache_read_input_tokens", 0))
            cache_creation_input_tokens = int(usage.get("cache_creation_input_tokens", 0))
        total_cost_usd = float(result_event.get("total_cost_usd", 0.0))
        num_turns = int(result_event.get("num_turns", 0))
        duration_ms = int(result_event.get("duration_ms", 0))
        result_text = result_event.get("result") or ""
        is_error = bool(result_event.get("is_error"))

        # Extract tool calls from assistant message events
        tool_calls = []
        for event in raw_array:
            if event.get("type") == "assistant":
                msg = event.get("message") or {}
                content_list = msg.get("content") or []
                for content_item in content_list:
                    if isinstance(content_item, dict) and content_item.get("type") == "tool_use":
                        tool_calls.append(content_item.get("name", ""))

        raw_json = raw_array
    else:
        # Legacy dict format (backward compatibility)
        usage = raw.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        cache_read_input_tokens = int(usage.get("cache_read_input_tokens", 0))
        cache_creation_input_tokens = int(usage.get("cache_creation_input_tokens", 0))
        total_cost_usd = float(raw.get("total_cost_usd", 0.0))
        num_turns = int(raw.get("num_turns", 0))
        duration_ms = int(raw.get("duration_ms", 0))
        result_text = raw.get("result") or ""
        is_error = bool(raw.get("is_error"))
        tool_calls = [t.get("name", "") for t in (raw.get("tool_uses") or [])]
        raw_json = raw

    # Determine success. Three failure signals from claude:
    #   is_error=true → tool or infrastructure error
    #   terminal_reason != "completed" → e.g., hit max_turns, got cancelled
    #   punt response → no tool calls and text indicates model declined the task
    error: Optional[str] = None
    terminal_reason = (
        result_event.get("terminal_reason") if isinstance(raw, list)
        else raw.get("terminal_reason")
    )
    errors_list = (
        result_event.get("errors", []) if isinstance(raw, list)
        else raw.get("errors", [])
    )
    if is_error:
        error = result_event.get("error") if isinstance(raw, list) else raw.get("error")
        if not error:
            error = "is_error flag set"
    elif terminal_reason and terminal_reason != "completed":
        reason_detail = (errors_list[0] if errors_list else terminal_reason)
        error = f"terminal_reason={terminal_reason}: {reason_detail}"
    # Third failure signal: "punt response" — Claude exited cleanly but didn't
    # actually use any tools and returned a text explaining it can't do the task.
    # A benchmark "success" requires the model to have attempted the work.
    elif not tool_calls and _looks_like_punt(result_text):
        error = "no tool calls (model declined the task)"

    succeeded = (error is None)

    return RunResult(
        path=path,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        total_cost_usd=total_cost_usd,
        num_turns=num_turns,
        duration_ms=duration_ms,
        tool_calls=tool_calls,
        succeeded=succeeded,
        error=error,
        raw_json=raw_json,
    )


def run_once(
    scenario: Scenario,
    path: PathName,
    *,
    model: str,
    max_turns: int,
    timeout_s: int,
    mcp_config_path: Path,
) -> RunResult:
    cmd = build_command(scenario, path, model=model, max_turns=max_turns,
                        mcp_config_path=mcp_config_path)
    _write_audit_entry(scenario, path, cmd)

    # Use Popen + temp files for stdout/stderr instead of subprocess.run's
    # capture_output=True, which on macOS truncates large outputs at pipe
    # buffer boundaries (~192KB). Writing to disk avoids the boundary entirely
    # — claude's JSON responses can be large (MCP metadata dumps easily hit
    # 200KB+) and must be captured in full for parsing.
    import tempfile

    stdout_fh = tempfile.NamedTemporaryFile(
        mode="w+", prefix="claude-stdout-", suffix=".json", delete=False
    )
    stderr_fh = tempfile.NamedTemporaryFile(
        mode="w+", prefix="claude-stderr-", suffix=".log", delete=False
    )
    stdout_path = stdout_fh.name
    stderr_path = stderr_fh.name
    # Close the write handles — Popen inherits the OS file descriptors,
    # so we just need the paths.
    stdout_fh.close()
    stderr_fh.close()

    try:
        with open(stdout_path, "wb") as out_f, open(stderr_path, "wb") as err_f:
            popen = subprocess.Popen(
                cmd,
                stdout=out_f,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                cwd="/tmp",
            )
            try:
                popen.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                popen.kill()
                try:
                    popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return RunResult(
                    path=path, input_tokens=0, output_tokens=0,
                    cache_read_input_tokens=0, cache_creation_input_tokens=0,
                    total_cost_usd=0.0, num_turns=0,
                    duration_ms=timeout_s * 1000, tool_calls=[], succeeded=False,
                    error=f"timeout after {timeout_s}s", raw_json=None,
                )

        # Read captured stdout / stderr from disk (no pipe truncation).
        with open(stdout_path, "r", encoding="utf-8", errors="replace") as f:
            stdout_text = f.read()
        with open(stderr_path, "r", encoding="utf-8", errors="replace") as f:
            stderr_text = f.read()
    finally:
        # Best-effort cleanup. Keep on disk if deletion fails — not critical.
        for p in (stdout_path, stderr_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    # Build a lightweight "proc" shim so the rest of run_once can keep using
    # proc.returncode / proc.stdout / proc.stderr without restructuring.
    class _ProcShim:
        pass
    proc = _ProcShim()
    proc.returncode = popen.returncode
    proc.stdout = stdout_text
    proc.stderr = stderr_text

    # Even a non-zero exit can coexist with valid JSON output (e.g., when
    # claude hits --max-turns, exit=1 but stdout contains a complete result
    # event with is_error=false, terminal_reason="max_turns"). Defer to the
    # JSON content for success/failure.
    if not proc.stdout.strip():
        return RunResult(
            path=path, input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, total_cost_usd=0.0, num_turns=0,
            duration_ms=0, tool_calls=[], succeeded=False,
            error=f"claude exited {proc.returncode} with empty stdout: {proc.stderr[:500]}",
            raw_json=None,
        )

    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        reason = f"bad json at char {e.pos} of {len(proc.stdout)} bytes: {e.msg}"
        if proc.returncode < 0:
            reason += f" (claude was killed by signal {-proc.returncode} — likely timeout or OOM)"
        return RunResult(
            path=path, input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, total_cost_usd=0.0, num_turns=0,
            duration_ms=0, tool_calls=[], succeeded=False,
            error=reason,
            raw_json={"_bad_json_stdout_tail": proc.stdout[-2000:],
                      "_stdout_total_bytes": len(proc.stdout),
                      "_parse_error": str(e),
                      "_returncode": proc.returncode,
                      "_stderr_tail": proc.stderr[-500:]},
        )

    return parse_claude_json(raw, path=path,
                             success_criteria=scenario.success_criteria)
