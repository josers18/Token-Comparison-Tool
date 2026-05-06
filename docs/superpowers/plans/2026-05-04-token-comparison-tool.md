# Token Comparison Tool Implementation Plan

> **Historical document.** This is the original implementation plan
> generated alongside the design spec. The codebase has shipped this
> plan and continued past it — see the [README](../../../README.md)
> for current state, including features added after the original 14
> tasks (free-format mode, load-saved-reports, JSON sidecar, brand
> mark home affordance, etc.). Kept as a record of the original
> task decomposition.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local, git-distributed FastAPI + vanilla-JS web tool that runs a curated catalog of Salesforce Headless 360 scenarios twice each (Native path vs Salesforce-hosted MCP path), extracts token/cost telemetry from `claude -p --output-format json`, and renders an executive-grade side-by-side comparison with a summary + recommendations — in a single markdown report per run.

**Architecture:** Single `claude -p` invocation helper is the only path to the LLM. Two "modes" (Native and MCP) differ only in the tool-provider flags passed to that helper — keeping the comparison apples-to-apples by construction. Scenarios live as YAML files so adding one is zero-code. FastAPI serves a static single-page app that drives a stepper UI (one page per scenario + final summary) and streams live progress via Server-Sent Events.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, Pydantic, PyYAML, pytest, vanilla HTML/JS, Chart.js (vendored), Claude Code CLI (`claude`), Salesforce CLI (`sf`).

**Frontend security note:** the frontend never uses `innerHTML` with interpolated data. All DOM construction goes through `document.createElement` + `textContent` / attribute setters. This is enforced in Task 12.

---

## Repository Layout (target state)

```
Token_Comparison_Tool/
├── README.md                 # Quickstart + prereqs
├── pyproject.toml            # Python deps + tool config
├── .gitignore                # reports/*.md, reports/*.pdf, .venv, __pycache__
├── run.sh                    # Single-command bootstrap (creates venv, installs, launches)
├── docs/superpowers/
│   ├── specs/2026-05-04-token-comparison-tool-design.md   # existing
│   └── plans/2026-05-04-token-comparison-tool.md          # this file
├── scenarios/
│   ├── s01_soql_top_accounts.yaml
│   ├── s02_unified_profile_lookup.yaml
│   ├── s03_segment_publish_check.yaml
│   ├── s04_agent_session_trace.yaml
│   └── s05_opportunity_pipeline_report.yaml
├── config/
│   └── sf-mcp.json           # MCP config passed to `claude -p --mcp-config`
├── src/token_compare/
│   ├── __init__.py
│   ├── models.py             # Pydantic models
│   ├── preflight.py          # check_environment()
│   ├── scenarios.py          # load_all()
│   ├── runner.py             # run_once()
│   ├── benchmark.py          # run_benchmark() + progress events
│   ├── recommendations.py    # generate()
│   ├── report.py             # write_markdown()
│   └── api.py                # FastAPI app + SSE
├── static/
│   ├── index.html
│   ├── app.js
│   ├── styles.css
│   └── chart.min.js          # vendored Chart.js (pinned version)
├── reports/                  # generated; .gitignored
└── tests/
    ├── conftest.py
    ├── fixtures/
    │   ├── claude_json_success.json
    │   ├── claude_json_failure.json
    │   └── scenario_valid.yaml
    ├── test_models.py
    ├── test_preflight.py
    ├── test_scenarios.py
    ├── test_runner.py
    ├── test_benchmark.py
    ├── test_recommendations.py
    ├── test_report.py
    ├── test_api.py
    └── test_e2e.py
```

Each Python module has one responsibility (spec §8). Tests mirror modules 1:1. The static bundle has no build step.

---

## Task 0: Project Scaffolding

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `README.md`, `run.sh`
- Create: `src/token_compare/__init__.py`, `tests/__init__.py`, `tests/conftest.py`
- Create: `reports/.gitkeep`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "token-compare"
version = "0.1.0"
description = "Token Comparison Tool — Salesforce MCP vs Native LLM benchmark"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "pydantic>=2.9",
  "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "httpx>=0.27",
]

[project.scripts]
token-compare = "token_compare.api:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "-q"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
```

- [ ] **Step 2: Create `.gitignore`**

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
reports/*.md
reports/*.pdf
!reports/.gitkeep
.DS_Store
```

- [ ] **Step 3: Create `reports/.gitkeep`**

```bash
mkdir -p reports && touch reports/.gitkeep
```

- [ ] **Step 4: Create `run.sh` and make it executable**

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -e ".[dev]"
exec python -m token_compare.api
```

Then: `chmod +x run.sh`.

- [ ] **Step 5: Create `README.md`**

```markdown
# Token Comparison Tool

Internal benchmark comparing Salesforce MCP vs native LLM token cost across
a curated catalog of Headless 360 scenarios.

## Prerequisites
- Python 3.11+
- Claude Code installed and logged in (`claude --version`, `claude auth status`)
- Salesforce CLI installed and authenticated to an org (`sf org list`)
- MCP config at `config/sf-mcp.json` pointing to the Salesforce-hosted MCP server

## Quickstart
    ./run.sh
Opens at http://localhost:8000.

## What it does
Runs every scenario in `scenarios/` twice per run (Native vs MCP), 3 runs per path
by default. Produces one markdown report in `reports/` per benchmark run.

## Design spec
See `docs/superpowers/specs/2026-05-04-token-comparison-tool-design.md`.
```

- [ ] **Step 6: Create package init files**

```python
# src/token_compare/__init__.py
__version__ = "0.1.0"
```

```python
# tests/__init__.py
```

```python
# tests/conftest.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore reports/.gitkeep run.sh README.md src/token_compare/__init__.py tests/__init__.py tests/conftest.py
git commit -m "chore: scaffold project structure"
```

---

## Task 1: Data Models

**Files:**
- Create: `src/token_compare/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py
import pytest
from pydantic import ValidationError
from token_compare.models import (
    Scenario, SuccessCriteria, RunResult, PathName,
    ScenarioResult, BenchmarkResult,
)


def test_scenario_loads_from_dict():
    s = Scenario.model_validate({
        "id": "s01",
        "title": "Basic",
        "category": "core-crm",
        "difficulty": "simple",
        "prompt": "List 5 accounts.",
        "expected_operations": ["sf data query"],
        "success_criteria": {"must_contain": ["account"]},
        "notes": "",
    })
    assert s.id == "s01"
    assert s.difficulty == "simple"
    assert s.success_criteria.must_contain == ["account"]


def test_scenario_rejects_bad_difficulty():
    with pytest.raises(ValidationError):
        Scenario.model_validate({
            "id": "s01",
            "title": "Basic",
            "category": "core-crm",
            "difficulty": "trivial",
            "prompt": "x",
            "expected_operations": [],
            "success_criteria": {"must_contain": []},
        })


def test_run_result_defaults():
    r = RunResult(
        path=PathName.NATIVE,
        input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
        total_cost_usd=0.01, num_turns=2, duration_ms=500,
        tool_calls=["sf data query"], succeeded=True, error=None,
    )
    assert r.succeeded is True
    assert r.path == PathName.NATIVE


def test_scenario_result_median_cost():
    runs = [
        RunResult(path=PathName.NATIVE, input_tokens=100, output_tokens=10,
                  cache_read_input_tokens=0, total_cost_usd=0.01, num_turns=1,
                  duration_ms=100, tool_calls=[], succeeded=True, error=None),
        RunResult(path=PathName.NATIVE, input_tokens=200, output_tokens=20,
                  cache_read_input_tokens=0, total_cost_usd=0.02, num_turns=2,
                  duration_ms=200, tool_calls=[], succeeded=True, error=None),
        RunResult(path=PathName.NATIVE, input_tokens=300, output_tokens=30,
                  cache_read_input_tokens=0, total_cost_usd=0.03, num_turns=3,
                  duration_ms=300, tool_calls=[], succeeded=True, error=None),
    ]
    sr = ScenarioResult(scenario_id="s01", native_runs=runs, mcp_runs=runs)
    assert sr.native_median_cost == 0.02
    assert sr.native_median_input_tokens == 200
    assert sr.succeeded_native == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'token_compare.models'`.

- [ ] **Step 3: Write the implementation**

```python
# src/token_compare/models.py
from __future__ import annotations

from enum import Enum
from statistics import median
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PathName(str, Enum):
    NATIVE = "native"
    MCP = "mcp"


class SuccessCriteria(BaseModel):
    must_contain: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    id: str
    title: str
    category: str
    difficulty: Literal["simple", "medium", "complex"]
    prompt: str
    expected_operations: list[str] = Field(default_factory=list)
    success_criteria: SuccessCriteria
    notes: str = ""


class RunResult(BaseModel):
    path: PathName
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    total_cost_usd: float
    num_turns: int
    duration_ms: int
    tool_calls: list[str]
    succeeded: bool
    error: Optional[str] = None
    raw_json: Optional[dict] = None


def _median_int(values: list[int]) -> int:
    return int(median(values)) if values else 0


def _median_float(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


class ScenarioResult(BaseModel):
    scenario_id: str
    native_runs: list[RunResult]
    mcp_runs: list[RunResult]

    @property
    def native_successful(self) -> list[RunResult]:
        return [r for r in self.native_runs if r.succeeded]

    @property
    def mcp_successful(self) -> list[RunResult]:
        return [r for r in self.mcp_runs if r.succeeded]

    @property
    def succeeded_native(self) -> int:
        return len(self.native_successful)

    @property
    def succeeded_mcp(self) -> int:
        return len(self.mcp_successful)

    @property
    def native_median_cost(self) -> float:
        return _median_float([r.total_cost_usd for r in self.native_successful])

    @property
    def mcp_median_cost(self) -> float:
        return _median_float([r.total_cost_usd for r in self.mcp_successful])

    @property
    def native_median_input_tokens(self) -> int:
        return _median_int([r.input_tokens for r in self.native_successful])

    @property
    def mcp_median_input_tokens(self) -> int:
        return _median_int([r.input_tokens for r in self.mcp_successful])

    @property
    def native_median_output_tokens(self) -> int:
        return _median_int([r.output_tokens for r in self.native_successful])

    @property
    def mcp_median_output_tokens(self) -> int:
        return _median_int([r.output_tokens for r in self.mcp_successful])

    @property
    def native_median_turns(self) -> int:
        return _median_int([r.num_turns for r in self.native_successful])

    @property
    def mcp_median_turns(self) -> int:
        return _median_int([r.num_turns for r in self.mcp_successful])

    @property
    def cheaper_multiplier(self) -> Optional[float]:
        """MCP median / Native median. None if either side has no successes."""
        if not self.native_successful or not self.mcp_successful:
            return None
        if self.native_median_cost <= 0:
            return None
        return self.mcp_median_cost / self.native_median_cost


class BenchmarkResult(BaseModel):
    started_at: str
    finished_at: str
    operator: str
    model: str
    org_name: str
    tool_commit: str
    runs_per_path: int
    scenarios: list[ScenarioResult]

    @property
    def total_native_cost(self) -> float:
        return sum(r.total_cost_usd for s in self.scenarios for r in s.native_successful)

    @property
    def total_mcp_cost(self) -> float:
        return sum(r.total_cost_usd for s in self.scenarios for r in s.mcp_successful)

    @property
    def total_native_input_tokens(self) -> int:
        return sum(r.input_tokens for s in self.scenarios for r in s.native_successful)

    @property
    def total_mcp_input_tokens(self) -> int:
        return sum(r.input_tokens for s in self.scenarios for r in s.mcp_successful)

    @property
    def average_multiplier(self) -> Optional[float]:
        mults = [s.cheaper_multiplier for s in self.scenarios if s.cheaper_multiplier is not None]
        return sum(mults) / len(mults) if mults else None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_models.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/models.py tests/test_models.py
git commit -m "feat: Pydantic data models for scenarios, runs, and benchmark results"
```

---

## Task 2: Scenario YAML Loader

**Files:**
- Create: `src/token_compare/scenarios.py`
- Create: `tests/fixtures/scenario_valid.yaml`
- Test: `tests/test_scenarios.py`

- [ ] **Step 1: Create the fixture scenario file**

```yaml
# tests/fixtures/scenario_valid.yaml
id: s99_fixture
title: "Fixture scenario"
category: core-crm
difficulty: simple
prompt: |
  Return the count of accounts.
expected_operations:
  - sf data query
success_criteria:
  must_contain: ["count"]
notes: "fixture"
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_scenarios.py
from pathlib import Path
import pytest

from token_compare.scenarios import load_all, load_file


FIXTURES = Path(__file__).parent / "fixtures"


def test_load_file_parses_fixture():
    s = load_file(FIXTURES / "scenario_valid.yaml")
    assert s.id == "s99_fixture"
    assert s.difficulty == "simple"
    assert s.success_criteria.must_contain == ["count"]


def test_load_all_returns_sorted_by_id(tmp_path):
    (tmp_path / "b.yaml").write_text(
        "id: s02\ntitle: b\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    (tmp_path / "a.yaml").write_text(
        "id: s01\ntitle: a\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    got = load_all(tmp_path)
    assert [s.id for s in got] == ["s01", "s02"]


def test_load_all_rejects_duplicate_ids(tmp_path):
    for name in ("x.yaml", "y.yaml"):
        (tmp_path / name).write_text(
            "id: dup\ntitle: t\ncategory: c\ndifficulty: simple\n"
            "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
        )
    with pytest.raises(ValueError, match="duplicate"):
        load_all(tmp_path)
```

- [ ] **Step 3: Run tests to verify failure**

Run: `pytest tests/test_scenarios.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Write the implementation**

```python
# src/token_compare/scenarios.py
from __future__ import annotations

from pathlib import Path

import yaml

from token_compare.models import Scenario


def load_file(path: Path) -> Scenario:
    with open(path, "r") as fh:
        data = yaml.safe_load(fh)
    return Scenario.model_validate(data)


def load_all(directory: Path) -> list[Scenario]:
    scenarios = [load_file(p) for p in sorted(Path(directory).glob("*.yaml"))]
    seen: set[str] = set()
    for s in scenarios:
        if s.id in seen:
            raise ValueError(f"duplicate scenario id: {s.id}")
        seen.add(s.id)
    return scenarios
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_scenarios.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/token_compare/scenarios.py tests/test_scenarios.py tests/fixtures/scenario_valid.yaml
git commit -m "feat: load and validate scenario YAML files"
```

---

## Task 3: Scenario Catalog Files

**Files:**
- Create: `scenarios/s01_soql_top_accounts.yaml`
- Create: `scenarios/s02_unified_profile_lookup.yaml`
- Create: `scenarios/s03_segment_publish_check.yaml`
- Create: `scenarios/s04_agent_session_trace.yaml`
- Create: `scenarios/s05_opportunity_pipeline_report.yaml`
- Modify: `tests/test_scenarios.py` (append catalog sanity test)

- [ ] **Step 1: Write `scenarios/s01_soql_top_accounts.yaml`**

```yaml
id: s01_soql_top_accounts
title: "Top 5 accounts by annual revenue"
category: core-crm
difficulty: simple
prompt: |
  List the top 5 Salesforce Accounts by AnnualRevenue descending.
  Return each account's Name and AnnualRevenue as a simple list.
expected_operations:
  - sf data query / query_data_cloud
success_criteria:
  must_contain: ["Name", "AnnualRevenue"]
notes: |
  Baseline per-turn overhead for a trivial SOQL read.
```

- [ ] **Step 2: Write `scenarios/s02_unified_profile_lookup.yaml`**

```yaml
id: s02_unified_profile_lookup
title: "Unified customer profile lookup"
category: data-360
difficulty: simple
prompt: |
  Find the unified profile for the Data Cloud individual with email
  "jane.doe@example.com". Return lifetime value, most recent purchase
  date, and segment memberships.
expected_operations:
  - query_data_cloud
  - get_unified_profile
success_criteria:
  must_contain: ["lifetime value", "segment"]
notes: |
  Tests the schema-overhead hypothesis on a short Data Cloud task.
```

- [ ] **Step 3: Write `scenarios/s03_segment_publish_check.yaml`**

```yaml
id: s03_segment_publish_check
title: "Segment publish status check"
category: data-360
difficulty: medium
prompt: |
  List the available Data Cloud segments, pick the one named
  "High Value Customers" (or nearest match), and report its current
  publish status and member count.
expected_operations:
  - list_segments
  - describe segment / get_calculated_insights
success_criteria:
  must_contain: ["publish", "member"]
notes: |
  Multi-tool chain — exercises tool selection and chaining costs.
```

- [ ] **Step 4: Write `scenarios/s04_agent_session_trace.yaml`**

```yaml
id: s04_agent_session_trace
title: "Agentforce session trace summary"
category: agentforce
difficulty: medium
prompt: |
  Query the Agentforce Session Trace data (STDM) for any session from
  the last 24 hours. Summarize which topics fired and in what order.
expected_operations:
  - query Data Cloud STDM DLO
success_criteria:
  must_contain: ["topic"]
notes: |
  Exercises a less-trodden MCP area where schemas may be heavier.
```

- [ ] **Step 5: Write `scenarios/s05_opportunity_pipeline_report.yaml`**

```yaml
id: s05_opportunity_pipeline_report
title: "Enterprise West pipeline report"
category: mixed
difficulty: complex
prompt: |
  For accounts that are members of the "Enterprise West" segment in
  Data Cloud, list their open Salesforce Opportunities worth more than
  $100,000. Group the output by Opportunity StageName and include the
  Owner Name for each Opportunity.
expected_operations:
  - query_data_cloud (segment members)
  - sf data query Opportunity
success_criteria:
  must_contain: ["Stage", "Owner"]
notes: |
  Multi-source join. MCP's richer schemas may reduce turn count here.
```

- [ ] **Step 6: Append catalog-sanity test to `tests/test_scenarios.py`**

Append this function (keep existing tests):

```python
def test_real_catalog_has_five_scenarios():
    repo_root = Path(__file__).parent.parent
    scenarios = load_all(repo_root / "scenarios")
    assert len(scenarios) == 5
    ids = {s.id for s in scenarios}
    assert ids == {
        "s01_soql_top_accounts",
        "s02_unified_profile_lookup",
        "s03_segment_publish_check",
        "s04_agent_session_trace",
        "s05_opportunity_pipeline_report",
    }
```

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_scenarios.py -v`
Expected: PASS (4 tests).

- [ ] **Step 8: Commit**

```bash
git add scenarios/ tests/test_scenarios.py
git commit -m "feat: five-scenario benchmark catalog spanning CRM, Data 360, Agentforce"
```

---

## Task 4: Preflight Environment Check

**Files:**
- Create: `src/token_compare/preflight.py`
- Test: `tests/test_preflight.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_preflight.py
from pathlib import Path
from unittest.mock import patch, MagicMock

from token_compare.preflight import check_environment, PreflightResult


def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_all_green(tmp_path):
    mcp_cfg = tmp_path / "sf-mcp.json"
    mcp_cfg.write_text("{}")

    with patch("token_compare.preflight.subprocess.run") as run:
        run.side_effect = [
            _fake_proc(0, "claude 1.2.3\n"),
            _fake_proc(0, "Logged in as user@x.com\n"),
            _fake_proc(0, '[{"alias":"me"}]'),
        ]
        result = check_environment(mcp_config_path=mcp_cfg)

    assert isinstance(result, PreflightResult)
    assert result.ok is True
    assert result.checks == {
        "claude_installed": True, "claude_logged_in": True,
        "sf_authenticated": True, "mcp_config_present": True,
    }


def test_missing_mcp_config(tmp_path):
    with patch("token_compare.preflight.subprocess.run") as run:
        run.side_effect = [
            _fake_proc(0, "claude 1.2.3\n"),
            _fake_proc(0, "Logged in as user@x.com\n"),
            _fake_proc(0, '[{"alias":"me"}]'),
        ]
        result = check_environment(mcp_config_path=tmp_path / "nope.json")

    assert result.ok is False
    assert result.checks["mcp_config_present"] is False
    assert any("mcp" in r.lower() for r in result.remediation)


def test_claude_not_installed(tmp_path):
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    with patch("token_compare.preflight.subprocess.run") as run:
        run.side_effect = FileNotFoundError("claude: not found")
        result = check_environment(mcp_config_path=mcp_cfg)

    assert result.ok is False
    assert result.checks["claude_installed"] is False
    assert any("claude" in r.lower() for r in result.remediation)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_preflight.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/token_compare/preflight.py
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


class PreflightResult(BaseModel):
    ok: bool
    checks: dict[str, bool]
    remediation: list[str]
    details: dict[str, str]


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return p.returncode, p.stdout, p.stderr


def check_environment(mcp_config_path: Optional[Path] = None) -> PreflightResult:
    checks: dict[str, bool] = {}
    remediation: list[str] = []
    details: dict[str, str] = {}

    # claude installed
    try:
        rc, out, _ = _run(["claude", "--version"])
        checks["claude_installed"] = rc == 0
        details["claude_version"] = out.strip()
        if rc != 0:
            remediation.append("Install Claude Code (https://claude.ai/code).")
    except FileNotFoundError:
        checks["claude_installed"] = False
        remediation.append("Install Claude Code: `claude` was not found on PATH.")

    # claude logged in
    if checks.get("claude_installed"):
        try:
            rc, out, _ = _run(["claude", "auth", "status"])
            logged_in = rc == 0 and "logged in" in out.lower()
            checks["claude_logged_in"] = logged_in
            details["claude_account"] = out.strip()
            if not logged_in:
                remediation.append("Run `claude login` to authenticate Claude Code.")
        except FileNotFoundError:
            checks["claude_logged_in"] = False
    else:
        checks["claude_logged_in"] = False

    # sf authenticated
    try:
        rc, out, _ = _run(["sf", "org", "list", "--json"])
        checks["sf_authenticated"] = rc == 0 and out.strip() not in ("", "[]")
        details["sf_orgs"] = out.strip()[:200]
        if not checks["sf_authenticated"]:
            remediation.append("Authenticate Salesforce CLI: `sf org login web`.")
    except FileNotFoundError:
        checks["sf_authenticated"] = False
        remediation.append("Install Salesforce CLI (`sf`): https://developer.salesforce.com/tools/salesforcecli")

    # mcp config present
    mcp_cfg = Path(mcp_config_path) if mcp_config_path else Path("config/sf-mcp.json")
    checks["mcp_config_present"] = mcp_cfg.is_file()
    details["mcp_config_path"] = str(mcp_cfg)
    if not checks["mcp_config_present"]:
        remediation.append(
            f"Create MCP config at {mcp_cfg} pointing to the Salesforce-hosted MCP server."
        )

    return PreflightResult(
        ok=all(checks.values()),
        checks=checks,
        remediation=remediation,
        details=details,
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_preflight.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/preflight.py tests/test_preflight.py
git commit -m "feat: preflight check for claude, sf CLI, and MCP config"
```

---

## Task 5: Claude Code Runner

**Files:**
- Create: `src/token_compare/runner.py`
- Create: `tests/fixtures/claude_json_success.json`
- Create: `tests/fixtures/claude_json_failure.json`
- Test: `tests/test_runner.py`

- [ ] **Step 1: Create `tests/fixtures/claude_json_success.json`**

```json
{
  "result": "Here are the top 5 accounts by AnnualRevenue: ...",
  "usage": {
    "input_tokens": 1204,
    "output_tokens": 118,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  },
  "total_cost_usd": 0.021,
  "duration_ms": 8432,
  "num_turns": 2,
  "tool_uses": [
    {"name": "Bash", "input": {"command": "sf data query ..."}},
    {"name": "Bash", "input": {"command": "sf data query Contact ..."}}
  ],
  "is_error": false
}
```

- [ ] **Step 2: Create `tests/fixtures/claude_json_failure.json`**

```json
{
  "result": "I was unable to complete the request.",
  "usage": {
    "input_tokens": 500,
    "output_tokens": 40,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  },
  "total_cost_usd": 0.005,
  "duration_ms": 2000,
  "num_turns": 1,
  "tool_uses": [],
  "is_error": true,
  "error": "Tool execution failed"
}
```

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_runner.py
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from token_compare.models import PathName, Scenario, SuccessCriteria
from token_compare.runner import run_once, build_command, parse_claude_json


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def scenario() -> Scenario:
    return Scenario(
        id="s99", title="t", category="c", difficulty="simple",
        prompt="List 5 accounts.", expected_operations=[],
        success_criteria=SuccessCriteria(must_contain=["AnnualRevenue"]),
        notes="",
    )


def test_build_command_native(scenario):
    cmd = build_command(scenario, PathName.NATIVE, model="claude-opus-4-7",
                        max_turns=15, mcp_config_path=Path("config/sf-mcp.json"))
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--output-format" in cmd and "json" in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "Bash" in allowed
    assert "--mcp-config" not in cmd


def test_build_command_mcp(scenario):
    cmd = build_command(scenario, PathName.MCP, model="claude-opus-4-7",
                        max_turns=15, mcp_config_path=Path("config/sf-mcp.json"))
    assert "--mcp-config" in cmd
    assert "config/sf-mcp.json" in " ".join(cmd)
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "Bash" not in allowed


def test_parse_claude_json_success():
    raw = json.loads((FIXTURES / "claude_json_success.json").read_text())
    parsed = parse_claude_json(
        raw, path=PathName.NATIVE,
        success_criteria=SuccessCriteria(must_contain=["AnnualRevenue"]),
    )
    assert parsed.input_tokens == 1204
    assert parsed.output_tokens == 118
    assert parsed.total_cost_usd == 0.021
    assert parsed.num_turns == 2
    assert parsed.tool_calls == ["Bash", "Bash"]
    assert parsed.succeeded is True


def test_parse_claude_json_fails_success_criteria():
    raw = json.loads((FIXTURES / "claude_json_success.json").read_text())
    parsed = parse_claude_json(
        raw, path=PathName.NATIVE,
        success_criteria=SuccessCriteria(must_contain=["NotInResult"]),
    )
    assert parsed.succeeded is False
    assert "must_contain" in (parsed.error or "").lower()


def test_parse_claude_json_is_error_flag():
    raw = json.loads((FIXTURES / "claude_json_failure.json").read_text())
    parsed = parse_claude_json(raw, path=PathName.MCP,
                               success_criteria=SuccessCriteria(must_contain=["x"]))
    assert parsed.succeeded is False
    assert "Tool execution failed" in (parsed.error or "")


def test_run_once_invokes_claude(scenario, tmp_path):
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    fixture_json = (FIXTURES / "claude_json_success.json").read_text()

    with patch("token_compare.runner.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout=fixture_json, stderr="")
        r = run_once(scenario, PathName.NATIVE, model="claude-opus-4-7",
                     max_turns=15, timeout_s=90, mcp_config_path=mcp_cfg)

    assert r.path == PathName.NATIVE
    assert r.input_tokens == 1204
    assert r.succeeded is True


def test_run_once_handles_timeout(scenario, tmp_path):
    import subprocess as sp
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    with patch("token_compare.runner.subprocess.run") as run:
        run.side_effect = sp.TimeoutExpired(cmd="claude", timeout=90)
        r = run_once(scenario, PathName.MCP, model="claude-opus-4-7",
                     max_turns=15, timeout_s=90, mcp_config_path=mcp_cfg)

    assert r.succeeded is False
    assert "timeout" in (r.error or "").lower()
```

- [ ] **Step 4: Run tests to verify failure**

Run: `pytest tests/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 5: Write the implementation**

```python
# src/token_compare/runner.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from token_compare.models import PathName, RunResult, Scenario, SuccessCriteria


NATIVE_PREAMBLE = (
    "You have access to Bash. Use the `sf` Salesforce CLI for all "
    "Salesforce operations (e.g., `sf data query`, `sf data record get`). "
    "Complete the user's request and return a concise answer."
)

MCP_PREAMBLE = (
    "You have access to Salesforce MCP tools. Use them to complete the "
    "user's request and return a concise answer."
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
    preamble = NATIVE_PREAMBLE if path == PathName.NATIVE else MCP_PREAMBLE
    prompt = f"{preamble}\n\n{scenario.prompt}"
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--max-turns", str(max_turns),
        "--output-format", "json",
    ]
    if path == PathName.NATIVE:
        cmd += ["--allowedTools", "Bash"]
    else:
        cmd += ["--mcp-config", str(mcp_config_path)]
        cmd += ["--allowedTools", MCP_ALLOWED_TOOLS]
    return cmd


def parse_claude_json(
    raw: dict,
    *,
    path: PathName,
    success_criteria: SuccessCriteria,
) -> RunResult:
    usage = raw.get("usage") or {}
    result_text = raw.get("result") or ""
    is_error = bool(raw.get("is_error"))
    tool_calls = [t.get("name", "") for t in (raw.get("tool_uses") or [])]

    error: Optional[str] = None
    if is_error:
        error = raw.get("error") or "is_error flag set"

    succeeded = not is_error
    if succeeded and success_criteria.must_contain:
        missing = [tok for tok in success_criteria.must_contain
                   if tok.lower() not in result_text.lower()]
        if missing:
            succeeded = False
            error = f"must_contain failed: missing {missing}"

    return RunResult(
        path=path,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
        total_cost_usd=float(raw.get("total_cost_usd", 0.0)),
        num_turns=int(raw.get("num_turns", 0)),
        duration_ms=int(raw.get("duration_ms", 0)),
        tool_calls=tool_calls,
        succeeded=succeeded,
        error=error,
        raw_json=raw,
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
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return RunResult(
            path=path, input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
            total_cost_usd=0.0, num_turns=0, duration_ms=timeout_s * 1000,
            tool_calls=[], succeeded=False, error=f"timeout after {timeout_s}s",
            raw_json=None,
        )

    if proc.returncode != 0 or not proc.stdout.strip():
        return RunResult(
            path=path, input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
            total_cost_usd=0.0, num_turns=0, duration_ms=0,
            tool_calls=[], succeeded=False,
            error=f"claude exited {proc.returncode}: {proc.stderr[:500]}",
            raw_json=None,
        )

    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return RunResult(
            path=path, input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
            total_cost_usd=0.0, num_turns=0, duration_ms=0,
            tool_calls=[], succeeded=False, error=f"bad json: {e}", raw_json=None,
        )

    return parse_claude_json(raw, path=path,
                             success_criteria=scenario.success_criteria)
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_runner.py -v`
Expected: PASS (7 tests).

- [ ] **Step 7: Commit**

```bash
git add src/token_compare/runner.py tests/test_runner.py tests/fixtures/claude_json_success.json tests/fixtures/claude_json_failure.json
git commit -m "feat: claude -p runner with JSON parsing and success-criteria checking"
```

---

## Task 6: Benchmark Orchestrator

**Files:**
- Create: `src/token_compare/benchmark.py`
- Test: `tests/test_benchmark.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_benchmark.py
from pathlib import Path
from unittest.mock import patch

from token_compare.benchmark import run_benchmark, BenchmarkOptions, ProgressEvent
from token_compare.models import PathName, RunResult, Scenario, SuccessCriteria


def _mk_scenario(sid: str) -> Scenario:
    return Scenario(id=sid, title=sid, category="c", difficulty="simple",
                    prompt="x", expected_operations=[],
                    success_criteria=SuccessCriteria(must_contain=[]), notes="")


def _fake_run(cost: float) -> RunResult:
    return RunResult(path=PathName.NATIVE, input_tokens=100, output_tokens=10,
                     cache_read_input_tokens=0, total_cost_usd=cost, num_turns=1,
                     duration_ms=100, tool_calls=[], succeeded=True, error=None)


def test_run_benchmark_shape(tmp_path):
    scenarios = [_mk_scenario("s01"), _mk_scenario("s02")]
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")

    def fake_run_once(scenario, path, **kwargs):
        r = _fake_run(0.01 if path == PathName.NATIVE else 0.03)
        r.path = path
        return r

    opts = BenchmarkOptions(model="m", max_turns=5, timeout_s=5, runs_per_path=2,
                            mcp_config_path=mcp_cfg, operator="me", org_name="org")

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc123"):
        result = run_benchmark(scenarios, opts)

    assert len(result.scenarios) == 2
    for s in result.scenarios:
        assert len(s.native_runs) == 2
        assert len(s.mcp_runs) == 2
    assert result.runs_per_path == 2
    assert result.operator == "me"
    assert result.tool_commit == "abc123"


def test_run_benchmark_emits_progress(tmp_path):
    scenarios = [_mk_scenario("s01")]
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    events: list[ProgressEvent] = []

    def fake_run_once(scenario, path, **kwargs):
        return _fake_run(0.01)

    opts = BenchmarkOptions(model="m", max_turns=5, timeout_s=5, runs_per_path=1,
                            mcp_config_path=mcp_cfg, operator="me", org_name="org")

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        run_benchmark(scenarios, opts, on_progress=events.append)

    kinds = [e.kind for e in events]
    assert "benchmark_start" in kinds
    assert "scenario_start" in kinds
    assert "run_complete" in kinds
    assert "benchmark_complete" in kinds


def test_run_benchmark_randomizes_path_order(tmp_path):
    scenarios = [_mk_scenario("s01")]
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    observed_order: list[PathName] = []

    def fake_run_once(scenario, path, **kwargs):
        observed_order.append(path)
        return _fake_run(0.01)

    opts = BenchmarkOptions(model="m", max_turns=5, timeout_s=5, runs_per_path=3,
                            mcp_config_path=mcp_cfg, operator="me", org_name="org")

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"), \
         patch("token_compare.benchmark.random.random", return_value=0.1):
        run_benchmark(scenarios, opts)

    assert observed_order == [PathName.MCP, PathName.NATIVE] * 3
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_benchmark.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/token_compare/benchmark.py
from __future__ import annotations

import random
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional

from pydantic import BaseModel

from token_compare.models import (
    BenchmarkResult, PathName, RunResult, Scenario, ScenarioResult,
)
from token_compare.runner import run_once


class BenchmarkOptions(BaseModel):
    model: str
    max_turns: int
    timeout_s: int
    runs_per_path: int
    mcp_config_path: Path
    operator: str
    org_name: str


EventKind = Literal[
    "benchmark_start", "scenario_start", "run_start",
    "run_complete", "scenario_complete", "benchmark_complete",
]


@dataclass
class ProgressEvent:
    kind: EventKind
    scenario_id: Optional[str] = None
    path: Optional[PathName] = None
    run_index: Optional[int] = None
    total_runs: Optional[int] = None
    run_result: Optional[RunResult] = None


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_benchmark(
    scenarios: list[Scenario],
    options: BenchmarkOptions,
    on_progress: Optional[Callable[[ProgressEvent], None]] = None,
) -> BenchmarkResult:
    emit = on_progress or (lambda e: None)
    started_at = _now_iso()
    emit(ProgressEvent(kind="benchmark_start"))

    results: list[ScenarioResult] = []
    total_runs_per_scenario = options.runs_per_path * 2

    for scenario in scenarios:
        emit(ProgressEvent(kind="scenario_start", scenario_id=scenario.id,
                           total_runs=total_runs_per_scenario))

        first_is_mcp = random.random() < 0.5
        order: list[PathName] = []
        for _ in range(options.runs_per_path):
            if first_is_mcp:
                order.extend([PathName.MCP, PathName.NATIVE])
            else:
                order.extend([PathName.NATIVE, PathName.MCP])

        native_runs: list[RunResult] = []
        mcp_runs: list[RunResult] = []

        for i, path in enumerate(order, start=1):
            emit(ProgressEvent(kind="run_start", scenario_id=scenario.id,
                               path=path, run_index=i,
                               total_runs=total_runs_per_scenario))
            r = run_once(
                scenario, path,
                model=options.model,
                max_turns=options.max_turns,
                timeout_s=options.timeout_s,
                mcp_config_path=options.mcp_config_path,
            )
            (native_runs if path == PathName.NATIVE else mcp_runs).append(r)
            emit(ProgressEvent(kind="run_complete", scenario_id=scenario.id,
                               path=path, run_index=i,
                               total_runs=total_runs_per_scenario, run_result=r))

        sr = ScenarioResult(scenario_id=scenario.id,
                            native_runs=native_runs, mcp_runs=mcp_runs)
        results.append(sr)
        emit(ProgressEvent(kind="scenario_complete", scenario_id=scenario.id))

    finished_at = _now_iso()
    emit(ProgressEvent(kind="benchmark_complete"))

    return BenchmarkResult(
        started_at=started_at,
        finished_at=finished_at,
        operator=options.operator,
        model=options.model,
        org_name=options.org_name,
        tool_commit=_git_sha(),
        runs_per_path=options.runs_per_path,
        scenarios=results,
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_benchmark.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/benchmark.py tests/test_benchmark.py
git commit -m "feat: benchmark orchestrator with progress events and path randomization"
```

---

## Task 7: Recommendations Generator

**Files:**
- Create: `src/token_compare/recommendations.py`
- Test: `tests/test_recommendations.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_recommendations.py
from token_compare.models import (
    BenchmarkResult, PathName, RunResult, ScenarioResult,
)
from token_compare.recommendations import generate


def _run(path: PathName, cost: float, inp: int = 500) -> RunResult:
    return RunResult(path=path, input_tokens=inp, output_tokens=50,
                     cache_read_input_tokens=0, total_cost_usd=cost,
                     num_turns=2, duration_ms=100, tool_calls=[],
                     succeeded=True, error=None)


def _scenario_result(sid: str, native_cost: float, mcp_cost: float) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=sid,
        native_runs=[_run(PathName.NATIVE, native_cost) for _ in range(3)],
        mcp_runs=[_run(PathName.MCP, mcp_cost) for _ in range(3)],
    )


def _benchmark(scenarios):
    return BenchmarkResult(
        started_at="2026-05-04T14:00:00+00:00",
        finished_at="2026-05-04T14:15:00+00:00",
        operator="me", model="m", org_name="org",
        tool_commit="abc", runs_per_path=3, scenarios=scenarios,
    )


def test_generate_mentions_overall_multiplier():
    b = _benchmark([
        _scenario_result("s01", 0.01, 0.04),
        _scenario_result("s02", 0.02, 0.06),
    ])
    lines = generate(b)
    joined = " ".join(lines).lower()
    assert "cheaper" in joined or "more expensive" in joined
    assert "%" in joined or "×" in joined


def test_generate_when_mcp_wins_some_scenarios():
    b = _benchmark([
        _scenario_result("s01", 0.01, 0.04),
        _scenario_result("s02", 0.05, 0.02),
    ])
    lines = generate(b)
    assert any("mcp" in line.lower() for line in lines)


def test_generate_handles_empty_benchmark():
    b = _benchmark([])
    lines = generate(b)
    assert lines == [] or all(isinstance(line, str) for line in lines)


def test_generate_with_difficulty_map():
    b = _benchmark([
        _scenario_result("s01", 0.01, 0.06),
        _scenario_result("s02", 0.05, 0.06),
    ])
    lines = generate(b, scenarios_by_id={"s01": "simple", "s02": "complex"})
    joined = " ".join(lines).lower()
    assert "simple" in joined or "complex" in joined or "schema" in joined
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_recommendations.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/token_compare/recommendations.py
from __future__ import annotations

from token_compare.models import BenchmarkResult


def generate(
    result: BenchmarkResult,
    scenarios_by_id: dict[str, str] | None = None,
) -> list[str]:
    """
    scenarios_by_id: optional map of scenario_id -> difficulty
                     ("simple" | "medium" | "complex"). When omitted,
                     difficulty-specific lines are skipped.
    """
    if not result.scenarios:
        return []

    lines: list[str] = []
    mult = result.average_multiplier
    if mult is not None:
        if mult > 1.05:
            pct = int(round((1 - 1 / mult) * 100))
            lines.append(
                f"Across {len(result.scenarios)} scenarios, native integrations "
                f"cost ~{pct}% less per task than the Salesforce-hosted MCP "
                f"equivalent (average {mult:.1f}× cheaper)."
            )
        elif mult < 0.95:
            pct = int(round((1 / mult - 1) * 100))
            lines.append(
                f"Across {len(result.scenarios)} scenarios, MCP averaged "
                f"~{pct}% cheaper than the native equivalent."
            )
        else:
            lines.append(
                "Across these scenarios, native and MCP paths were "
                "effectively tied on token cost."
            )

    if scenarios_by_id:
        simple = [s for s in result.scenarios
                  if scenarios_by_id.get(s.scenario_id) == "simple"]
        complex_ = [s for s in result.scenarios
                    if scenarios_by_id.get(s.scenario_id) == "complex"]

        simple_mults = [s.cheaper_multiplier for s in simple if s.cheaper_multiplier]
        if simple_mults and sum(simple_mults) / len(simple_mults) > 1.5:
            lines.append(
                "The gap is widest on simple queries, where MCP tool-schema "
                "overhead dominates the prompt."
            )

        complex_mults = [s.cheaper_multiplier for s in complex_ if s.cheaper_multiplier]
        if complex_mults and min(complex_mults) < 1.2:
            lines.append(
                "MCP closes the gap on complex, multi-step scenarios — richer "
                "tool schemas can reduce the number of turns required."
            )

    mcp_wins = [s for s in result.scenarios
                if s.cheaper_multiplier is not None and s.cheaper_multiplier < 1.0]
    if mcp_wins:
        ids = ", ".join(s.scenario_id for s in mcp_wins)
        lines.append(f"MCP was cheaper on: {ids}. Worth investigating why.")

    lines.append(
        "Recommendation: prefer native for read-heavy, well-scoped workflows; "
        "reconsider MCP where schema richness demonstrably reduces turn count "
        "enough to offset input-token overhead."
    )
    return lines
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_recommendations.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/recommendations.py tests/test_recommendations.py
git commit -m "feat: template-driven recommendations from measured deltas"
```

---

## Task 8: Markdown Report Writer

**Files:**
- Create: `src/token_compare/report.py`
- Test: `tests/test_report.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_report.py
from pathlib import Path

from token_compare.models import (
    BenchmarkResult, PathName, RunResult, Scenario, ScenarioResult,
    SuccessCriteria,
)
from token_compare.report import write_markdown, default_report_path


def _run(path: PathName, cost: float) -> RunResult:
    return RunResult(path=path, input_tokens=1000, output_tokens=100,
                     cache_read_input_tokens=0, total_cost_usd=cost,
                     num_turns=2, duration_ms=1000,
                     tool_calls=["Bash"] if path == PathName.NATIVE else ["mcp__x"],
                     succeeded=True, error=None,
                     raw_json={"usage": {"input_tokens": 1000}})


def _benchmark() -> BenchmarkResult:
    return BenchmarkResult(
        started_at="2026-05-04T14:00:00+00:00",
        finished_at="2026-05-04T14:15:00+00:00",
        operator="user@example.com",
        model="claude-opus-4-7",
        org_name="MyOrg",
        tool_commit="abc1234",
        runs_per_path=3,
        scenarios=[
            ScenarioResult(
                scenario_id="s01_soql_top_accounts",
                native_runs=[_run(PathName.NATIVE, 0.01) for _ in range(3)],
                mcp_runs=[_run(PathName.MCP, 0.04) for _ in range(3)],
            ),
        ],
    )


def test_write_markdown_creates_file(tmp_path):
    b = _benchmark()
    scenarios = [Scenario(id="s01_soql_top_accounts", title="Top accounts",
                          category="core-crm", difficulty="simple",
                          prompt="List accounts.", expected_operations=[],
                          success_criteria=SuccessCriteria(must_contain=[]),
                          notes="")]
    out = tmp_path / "report.md"
    write_markdown(b, out, scenarios=scenarios)
    text = out.read_text()

    assert "# Token Comparison Benchmark" in text
    assert "## Executive Summary" in text
    assert "## Per-Scenario Comparisons" in text
    assert "## Methodology" in text
    assert "## Appendix — Raw Data" in text
    assert "claude-opus-4-7" in text
    assert "abc1234" in text
    assert "s01_soql_top_accounts" in text
    assert "Top accounts" in text
    assert "Native" in text and "MCP" in text


def test_default_report_path_format(tmp_path):
    p = default_report_path(tmp_path, started_at="2026-05-04T14:32:00+00:00")
    assert p.parent == tmp_path
    assert p.name.startswith("benchmark-2026-05-04-1432")
    assert p.suffix == ".md"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/token_compare/report.py
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from token_compare.models import BenchmarkResult, Scenario, ScenarioResult
from token_compare.recommendations import generate as generate_recs


def default_report_path(directory: Path, started_at: str) -> Path:
    dt = datetime.fromisoformat(started_at)
    stamp = dt.strftime("%Y-%m-%d-%H%M")
    return Path(directory) / f"benchmark-{stamp}.md"


def write_markdown(
    result: BenchmarkResult,
    path: Path,
    *,
    scenarios: list[Scenario] | None = None,
) -> None:
    scenarios = scenarios or []
    by_id = {s.id: s for s in scenarios}
    difficulty_by_id = {s.id: s.difficulty for s in scenarios}

    out: list[str] = []
    out.append("# Token Comparison Benchmark")
    out.append("")
    out.append(f"**Date:** {result.started_at} → {result.finished_at}  ")
    out.append(f"**Operator:** {result.operator}  ")
    out.append(f"**Model:** {result.model}  ·  **Runs per path:** {result.runs_per_path}  ")
    out.append(f"**Salesforce org:** {result.org_name}  ·  **Tool commit:** {result.tool_commit}")
    out.append("")
    out.append("---")
    out.append("")

    # Executive Summary
    out.append("## Executive Summary")
    out.append("")
    mult = result.average_multiplier
    if mult is not None:
        if mult > 1:
            out.append(
                f"Across **{len(result.scenarios)} scenarios**, native was "
                f"**{mult:.1f}× cheaper on average** than the Salesforce-hosted MCP equivalent."
            )
        else:
            out.append(
                f"Across **{len(result.scenarios)} scenarios**, MCP was "
                f"**{(1/mult):.1f}× cheaper on average** than the native equivalent."
            )
        out.append("")
    out.append("|                   | Native   | MCP      |")
    out.append("|-------------------|----------|----------|")
    out.append(f"| Total cost        | ${result.total_native_cost:.2f}    | ${result.total_mcp_cost:.2f}    |")
    out.append(f"| Total input tok   | {result.total_native_input_tokens:,}    | {result.total_mcp_input_tokens:,}    |")
    succ_native = sum(s.succeeded_native for s in result.scenarios)
    succ_mcp = sum(s.succeeded_mcp for s in result.scenarios)
    total = result.runs_per_path * len(result.scenarios)
    out.append(f"| Success rate      | {succ_native}/{total}     | {succ_mcp}/{total}     |")
    out.append("")

    # Recommendations
    out.append("### Recommendations")
    out.append("")
    for line in generate_recs(result, scenarios_by_id=difficulty_by_id):
        out.append(f"- {line}")
    out.append("")
    out.append("---")
    out.append("")

    # Per-Scenario
    out.append("## Per-Scenario Comparisons")
    out.append("")
    for sr in result.scenarios:
        sc = by_id.get(sr.scenario_id)
        title = sc.title if sc else sr.scenario_id
        cat_diff = f"{sc.category} · {sc.difficulty}" if sc else ""
        out.append(f"### {sr.scenario_id} — {title}  ({cat_diff})")
        out.append("")
        if sc:
            out.append(f"**Prompt:** {sc.prompt.strip()}")
            out.append("")
        out.append("|              | Native (median) | MCP (median) |")
        out.append("|--------------|-----------------|---------------|")
        out.append(f"| Input tok    | {sr.native_median_input_tokens:,}           | {sr.mcp_median_input_tokens:,}          |")
        out.append(f"| Output tok   | {sr.native_median_output_tokens:,}           | {sr.mcp_median_output_tokens:,}          |")
        out.append(f"| Cost         | ${sr.native_median_cost:.3f}         | ${sr.mcp_median_cost:.3f}        |")
        out.append(f"| Turns        | {sr.native_median_turns}              | {sr.mcp_median_turns}             |")
        out.append(f"| Succeeded    | {sr.succeeded_native}/{len(sr.native_runs)}             | {sr.succeeded_mcp}/{len(sr.mcp_runs)}            |")
        out.append("")
        out.append(f"**Tool calls — Native:** {_summarize_tool_calls(sr.native_runs)}  ")
        out.append(f"**Tool calls — MCP:** {_summarize_tool_calls(sr.mcp_runs)}")
        out.append("")
        out.append(_outcome_sentence(sr))
        out.append("")

    out.append("---")
    out.append("")

    # Methodology
    out.append("## Methodology")
    out.append("")
    out.append("- **Measurement source:** `claude -p --output-format json`; every number is extracted directly from the per-run `usage` block.")
    out.append("- **Held constant:** same prompt, model, org, machine, `--max-turns` cap. Path order randomized per scenario.")
    out.append("- **One axis of variance:** tool provider only (native `sf` CLI vs Salesforce-hosted MCP server).")
    out.append("- **Stats:** medians reported in tables; full per-run data in Appendix.")
    out.append("- **Out of scope:** Salesforce API consumption, semantic accuracy beyond `must_contain`, MCP server startup time.")
    out.append("")
    out.append("---")
    out.append("")

    # Appendix
    out.append("## Appendix — Raw Data")
    out.append("")
    for sr in result.scenarios:
        out.append(f"### {sr.scenario_id}")
        out.append("")
        for i, r in enumerate(sr.native_runs, start=1):
            out.append(f"<details><summary>Native run {i} — ${r.total_cost_usd:.3f}, {r.num_turns} turns</summary>")
            out.append("")
            out.append("```json")
            out.append(json.dumps(r.raw_json or {}, indent=2))
            out.append("```")
            out.append("</details>")
            out.append("")
        for i, r in enumerate(sr.mcp_runs, start=1):
            out.append(f"<details><summary>MCP run {i} — ${r.total_cost_usd:.3f}, {r.num_turns} turns</summary>")
            out.append("")
            out.append("```json")
            out.append(json.dumps(r.raw_json or {}, indent=2))
            out.append("```")
            out.append("</details>")
            out.append("")

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(out))


def _summarize_tool_calls(runs) -> str:
    if not runs:
        return "(none)"
    for r in runs:
        if r.succeeded and r.tool_calls:
            return ", ".join(r.tool_calls)
    return "(no tool calls)"


def _outcome_sentence(sr: ScenarioResult) -> str:
    m = sr.cheaper_multiplier
    if m is None:
        return "**Outcome:** inconclusive — one or both paths had no successful runs."
    if m > 1.05:
        return f"**Outcome:** Native {m:.1f}× cheaper on this scenario."
    if m < 0.95:
        return f"**Outcome:** MCP {(1/m):.1f}× cheaper on this scenario."
    return "**Outcome:** effectively tied on token cost."
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_report.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/report.py tests/test_report.py
git commit -m "feat: markdown report writer with summary, per-scenario, methodology, appendix"
```

---

## Task 9: FastAPI Backend + SSE

**Files:**
- Create: `src/token_compare/api.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api.py
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from token_compare.api import create_app, AppConfig


@pytest.fixture
def client(tmp_path):
    scen_dir = tmp_path / "scenarios"; scen_dir.mkdir()
    (scen_dir / "sA.yaml").write_text(
        "id: sA\ntitle: A\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    mcp_cfg = tmp_path / "sf-mcp.json"; mcp_cfg.write_text("{}")
    reports = tmp_path / "reports"; reports.mkdir()

    cfg = AppConfig(
        scenarios_dir=scen_dir, mcp_config_path=mcp_cfg,
        reports_dir=reports, static_dir=None,
    )
    return TestClient(create_app(cfg))


def test_get_scenarios(client):
    r = client.get("/api/scenarios")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == "sA"


def test_get_preflight(client):
    from token_compare.preflight import PreflightResult
    with patch("token_compare.api.check_environment") as m:
        m.return_value = PreflightResult(
            ok=True,
            checks={"claude_installed": True, "claude_logged_in": True,
                    "sf_authenticated": True, "mcp_config_present": True},
            remediation=[], details={},
        )
        r = client.get("/api/preflight")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_post_run_streams_events(client):
    from token_compare.models import PathName, RunResult

    def fake_run_once(scenario, path, **kwargs):
        return RunResult(path=path, input_tokens=100, output_tokens=10,
                         cache_read_input_tokens=0, total_cost_usd=0.01,
                         num_turns=1, duration_ms=10, tool_calls=[],
                         succeeded=True, error=None, raw_json={})

    with patch("token_compare.benchmark.run_once", side_effect=fake_run_once), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream(
            "POST", "/api/run",
            json={"scenario_ids": ["sA"], "runs_per_path": 1,
                  "model": "claude-opus-4-7", "operator": "me", "org_name": "org"},
        ) as resp:
            chunks = "".join(list(resp.iter_text()))

    assert "benchmark_start" in chunks
    assert "run_complete" in chunks
    assert "benchmark_complete" in chunks
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_api.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/token_compare/api.py
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from token_compare.benchmark import BenchmarkOptions, ProgressEvent, run_benchmark
from token_compare.preflight import check_environment
from token_compare.report import default_report_path, write_markdown
from token_compare.scenarios import load_all


class AppConfig(BaseModel):
    scenarios_dir: Path = Path("scenarios")
    mcp_config_path: Path = Path("config/sf-mcp.json")
    reports_dir: Path = Path("reports")
    static_dir: Optional[Path] = Path("static")
    reports_retain: int = 10


class RunRequest(BaseModel):
    scenario_ids: list[str]
    runs_per_path: int = 3
    model: str = "claude-opus-4-7"
    operator: str
    org_name: str
    max_turns: int = 15
    timeout_s: int = 90


def _event_to_dict(e: ProgressEvent) -> dict:
    d: dict = {"kind": e.kind}
    if e.scenario_id: d["scenario_id"] = e.scenario_id
    if e.path: d["path"] = e.path.value
    if e.run_index is not None: d["run_index"] = e.run_index
    if e.total_runs is not None: d["total_runs"] = e.total_runs
    if e.run_result is not None:
        d["run_result"] = e.run_result.model_dump(exclude={"raw_json"})
    return d


def _prune_reports(reports_dir: Path, retain: int) -> None:
    files = sorted(reports_dir.glob("benchmark-*.md"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[retain:]:
        old.unlink(missing_ok=True)


def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI(title="Token Comparison Tool")

    @app.get("/api/preflight")
    def preflight() -> dict:
        return check_environment(mcp_config_path=config.mcp_config_path).model_dump()

    @app.get("/api/scenarios")
    def list_scenarios() -> list[dict]:
        return [s.model_dump() for s in load_all(config.scenarios_dir)]

    @app.post("/api/run")
    async def run(req: RunRequest) -> StreamingResponse:
        all_scenarios = load_all(config.scenarios_dir)
        picked = [s for s in all_scenarios if s.id in set(req.scenario_ids)]
        if not picked:
            picked = all_scenarios

        options = BenchmarkOptions(
            model=req.model, max_turns=req.max_turns, timeout_s=req.timeout_s,
            runs_per_path=req.runs_per_path,
            mcp_config_path=config.mcp_config_path,
            operator=req.operator, org_name=req.org_name,
        )
        queue: asyncio.Queue = asyncio.Queue()

        def on_progress(e: ProgressEvent) -> None:
            queue.put_nowait(_event_to_dict(e))

        async def runner_task():
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, lambda: run_benchmark(picked, options, on_progress),
            )
            config.reports_dir.mkdir(parents=True, exist_ok=True)
            out = default_report_path(config.reports_dir, result.started_at)
            write_markdown(result, out, scenarios=picked)
            _prune_reports(config.reports_dir, config.reports_retain)
            queue.put_nowait({"kind": "report_written", "path": str(out)})
            queue.put_nowait(None)

        async def event_stream():
            task = asyncio.create_task(runner_task())
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
            await task

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/api/reports/latest")
    def latest_report(request: Request):
        files = sorted(config.reports_dir.glob("benchmark-*.md"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return JSONResponse({"path": None, "content": None})
        latest = files[0]
        return FileResponse(latest, media_type="text/markdown", filename=latest.name)

    if config.static_dir and config.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(config.static_dir), html=True), name="static")

    return app


def main() -> None:
    cfg = AppConfig()
    app = create_app(cfg)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_api.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/api.py tests/test_api.py
git commit -m "feat: FastAPI backend with SSE progress stream and report persistence"
```

---

## Task 10: MCP Config Placeholder

**Files:**
- Create: `config/sf-mcp.json`
- Create: `config/README.md`

- [ ] **Step 1: Create `config/sf-mcp.json`**

```json
{
  "mcpServers": {
    "salesforce": {
      "command": "npx",
      "args": ["-y", "@salesforce/mcp-server"],
      "env": {
        "SF_USERNAME": "REPLACE_WITH_SF_USERNAME",
        "SF_ORG_ALIAS": "REPLACE_WITH_SF_ORG_ALIAS"
      }
    }
  }
}
```

- [ ] **Step 2: Create `config/README.md`**

```markdown
# MCP config

`sf-mcp.json` is passed to `claude -p --mcp-config` on Path B runs.

The example file in this directory is a placeholder. Replace the placeholder
values with your Salesforce-hosted MCP server configuration. The exact shape
is determined by the MCP server distribution you use; the `mcpServers`
top-level key is required by Claude Code.

Do not commit real credentials.
```

- [ ] **Step 3: Commit**

```bash
git add config/sf-mcp.json config/README.md
git commit -m "chore: MCP config placeholder with README"
```

---

## Task 11: Frontend — Static Shell & Styles

**Files:**
- Create: `static/index.html`, `static/styles.css`
- Download: `static/chart.min.js`

- [ ] **Step 1: Download Chart.js (pinned version)**

```bash
curl -sSL https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js -o static/chart.min.js
```

Verify the file size is > 100KB (not an error page).

- [ ] **Step 2: Create `static/index.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Token Comparison Tool — Salesforce MCP vs Native LLM</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="app-header">
    <div class="brand">
      <span class="dot"></span>
      Token Comparison <span class="sub">— Salesforce MCP vs Native LLM</span>
    </div>
    <div class="status" id="preflight-status">checking…</div>
  </header>

  <main class="container">
    <nav class="stepper" id="stepper" aria-label="Benchmark steps"></nav>

    <section id="setup-view" class="card">
      <h2>Benchmark catalog</h2>
      <p class="muted" id="catalog-summary">loading scenarios…</p>
      <ul class="scenario-list" id="scenario-list"></ul>
      <div class="controls">
        <label>Runs per path
          <select id="runs-per-path">
            <option>1</option>
            <option selected>3</option>
            <option>5</option>
          </select>
        </label>
        <label>Model
          <select id="model">
            <option selected>claude-opus-4-7</option>
            <option>claude-sonnet-4-6</option>
            <option>claude-haiku-4-5-20251001</option>
          </select>
        </label>
        <button class="primary" id="run-btn" disabled>Run Full Benchmark →</button>
      </div>
      <div id="preflight-remediation" class="remediation" hidden></div>
    </section>

    <section id="progress-view" class="card" hidden>
      <h2>Running benchmark</h2>
      <div class="progress-bar"><div class="fill" id="progress-fill"></div></div>
      <p class="muted" id="progress-text">starting…</p>
    </section>

    <section id="scenario-view" class="card" hidden>
      <div class="scenario-head">
        <h2 id="sv-title"></h2>
        <p class="muted" id="sv-meta"></p>
        <blockquote id="sv-prompt"></blockquote>
      </div>
      <div class="dual">
        <div class="panel native">
          <h3>Native (Path A)</h3>
          <div class="panel-status" id="sv-native-status">—</div>
          <div class="hero" id="sv-native-input">—</div>
          <div class="hero-label">input tokens</div>
          <div class="panel-meta">
            <div>Turns <span id="sv-native-turns">—</span></div>
            <div>Cost <span id="sv-native-cost">—</span></div>
          </div>
          <ul class="tools" id="sv-native-tools"></ul>
        </div>
        <div class="panel mcp">
          <h3>MCP (Path B)</h3>
          <div class="panel-status" id="sv-mcp-status">—</div>
          <div class="hero" id="sv-mcp-input">—</div>
          <div class="hero-label">input tokens</div>
          <div class="panel-meta">
            <div>Turns <span id="sv-mcp-turns">—</span></div>
            <div>Cost <span id="sv-mcp-cost">—</span></div>
          </div>
          <ul class="tools" id="sv-mcp-tools"></ul>
        </div>
      </div>
      <div class="results-card">
        <div class="headline" id="sv-headline">—</div>
        <canvas id="sv-chart" height="140"></canvas>
      </div>
    </section>

    <section id="summary-view" class="card" hidden>
      <h2>Overall Summary</h2>
      <div class="hero-summary" id="summary-headline">—</div>
      <table class="summary-table">
        <thead>
          <tr><th></th><th>Native</th><th>MCP</th></tr>
        </thead>
        <tbody id="summary-tbody"></tbody>
      </table>
      <h3>Per-scenario overview</h3>
      <ul class="summary-bars" id="summary-bars"></ul>
      <h3>Recommendations</h3>
      <ul id="summary-recs"></ul>
      <div class="actions">
        <a id="download-report" class="primary" href="#" download>⬇ Download report</a>
        <button id="run-again" class="secondary">↻ Run again</button>
      </div>
    </section>
  </main>

  <script src="chart.min.js"></script>
  <script src="app.js"></script>
</body>
</html>
```

- [ ] **Step 3: Create `static/styles.css`**

```css
:root {
  --bg: #F3F3F3;
  --card: #FFFFFF;
  --text: #181818;
  --muted: #747474;
  --border: #C9C9C9;
  --native: #0176D3;
  --mcp: #5867E8;
  --ok: #2E844A;
  --warn: #FE9339;
  --err: #BA0517;
  --shadow: 0 2px 4px rgba(0,0,0,0.04);
  --radius: 6px;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Salesforce Sans", Inter, system-ui, sans-serif;
  font-size: 14px;
  background: var(--bg);
  color: var(--text);
}

.app-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 16px 24px;
  background: var(--card);
  border-bottom: 1px solid var(--border);
}
.brand { font-weight: 600; }
.brand .dot {
  display: inline-block; width: 8px; height: 8px; background: var(--native);
  border-radius: 50%; margin-right: 8px;
}
.brand .sub { color: var(--muted); font-weight: 400; }
.status { color: var(--muted); }
.status.ok { color: var(--ok); }
.status.err { color: var(--err); }

.container { max-width: 1100px; margin: 0 auto; padding: 24px; }

.stepper {
  display: flex; gap: 8px; align-items: center;
  margin-bottom: 16px; flex-wrap: wrap;
}
.stepper .step {
  padding: 6px 12px; border: 1px solid var(--border); border-radius: 999px;
  background: var(--card); cursor: pointer; font-size: 12px;
}
.stepper .step.done { border-color: var(--ok); color: var(--ok); }
.stepper .step.active { border-color: var(--native); color: var(--native); }
.stepper .step.error { border-color: var(--err); color: var(--err); }

.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 24px;
  margin-bottom: 16px;
}
.muted { color: var(--muted); }
.primary {
  background: var(--native); color: white; border: 0;
  padding: 10px 18px; border-radius: var(--radius); cursor: pointer;
  font-weight: 600; text-decoration: none; display: inline-block;
}
.primary:disabled { background: var(--border); cursor: not-allowed; }
.secondary {
  background: var(--card); color: var(--text);
  border: 1px solid var(--border);
  padding: 10px 18px; border-radius: var(--radius); cursor: pointer;
}

.scenario-list { list-style: none; padding: 0; margin: 16px 0; }
.scenario-list li {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 0; border-bottom: 1px solid var(--border);
}
.scenario-list li:last-child { border-bottom: 0; }
.scenario-list .meta {
  margin-left: auto; color: var(--muted); font-size: 12px;
}
.controls {
  display: flex; gap: 16px; align-items: flex-end; margin-top: 24px;
}
.controls label { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); }
.controls select {
  font-size: 14px; padding: 6px 8px; border: 1px solid var(--border);
  border-radius: var(--radius); background: var(--card);
}
.controls .primary { margin-left: auto; }

.remediation {
  background: #FFF4E5; border: 1px solid var(--warn);
  color: #5C3A00; padding: 12px; margin-top: 16px; border-radius: var(--radius);
}

.progress-bar {
  height: 8px; background: var(--bg); border-radius: 999px;
  overflow: hidden; margin: 16px 0;
}
.progress-bar .fill {
  height: 100%; background: var(--native); width: 0%;
  transition: width 200ms ease;
}

.scenario-head h2 { margin-bottom: 4px; }
.scenario-head blockquote {
  margin: 16px 0; padding: 12px 16px;
  background: var(--bg); border-left: 3px solid var(--border);
  font-style: italic;
}

.dual {
  display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 24px;
}
.panel {
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 20px; background: var(--card);
}
.panel.native h3 { color: var(--native); margin: 0 0 8px 0; }
.panel.mcp h3 { color: var(--mcp); margin: 0 0 8px 0; }
.panel-status { color: var(--muted); font-size: 12px; margin-bottom: 16px; }
.panel-status.running::before { content: "● "; color: var(--native); }
.panel-status.done::before { content: "✓ "; color: var(--ok); }
.panel-status.error::before { content: "⚠ "; color: var(--err); }
.hero { font-size: 42px; font-weight: 700; letter-spacing: -1px; }
.hero-label { color: var(--muted); margin-bottom: 16px; }
.panel-meta {
  display: flex; gap: 24px; padding: 12px 0;
  border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  margin-bottom: 12px;
}
.panel-meta > div { color: var(--muted); font-size: 12px; }
.panel-meta span { color: var(--text); font-weight: 600; margin-left: 4px; }
.tools { list-style: none; padding: 0; margin: 0; font-family: ui-monospace, monospace; font-size: 12px; }
.tools li { padding: 4px 0; }
.tools li::before { content: "▸ "; color: var(--muted); }

.results-card {
  margin-top: 24px; padding: 24px; background: var(--bg);
  border-radius: var(--radius); text-align: center;
}
.headline { font-size: 28px; font-weight: 600; margin-bottom: 16px; }

.hero-summary {
  font-size: 32px; font-weight: 700; text-align: center; padding: 24px;
  background: var(--bg); border-radius: var(--radius); margin-bottom: 24px;
}
.summary-table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
.summary-table td, .summary-table th {
  padding: 10px 12px; border-bottom: 1px solid var(--border); text-align: right;
}
.summary-table th:first-child, .summary-table td:first-child { text-align: left; }

.summary-bars { list-style: none; padding: 0; }
.summary-bars li {
  display: grid; grid-template-columns: 220px 80px 1fr;
  gap: 12px; align-items: center; padding: 8px 0;
  border-bottom: 1px solid var(--border);
}
.summary-bars .bar-cell { background: var(--bg); border-radius: 999px; overflow: hidden; height: 8px; }
.summary-bars .bar { height: 100%; background: var(--native); }
.actions {
  display: flex; gap: 12px; justify-content: flex-end; margin-top: 24px;
}

@media (max-width: 820px) {
  .dual { grid-template-columns: 1fr; }
}
```

- [ ] **Step 4: Commit**

```bash
git add static/index.html static/styles.css static/chart.min.js
git commit -m "feat: static frontend shell — HTML, SLDS-inspired CSS, vendored Chart.js"
```

---

## Task 12: Frontend — App Logic (`app.js`)

**Files:**
- Create: `static/app.js`

**Security rule (enforced):** this file MUST NOT use `innerHTML` with interpolated data. All DOM writes go through `document.createElement` + `textContent` / `setAttribute`. Static clearing via `el.replaceChildren()` is fine (no HTML parsing). A later step greps for `innerHTML` and fails the task if any remain.

- [ ] **Step 1: Create `static/app.js`**

```javascript
// static/app.js — vanilla JS SPA. No innerHTML.

const state = {
  preflight: null,
  scenarios: [],
  runsPerPath: 3,
  model: "claude-opus-4-7",
  scenarioResults: {},   // { [sid]: { native: RunResult[], mcp: RunResult[] } }
  charts: {},
  reportPath: null,
  active: "setup",
};

const $ = (id) => document.getElementById(id);

function el(tag, opts = {}, ...children) {
  const node = document.createElement(tag);
  if (opts.className) node.className = opts.className;
  if (opts.text != null) node.textContent = opts.text;
  if (opts.attrs) {
    for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  }
  if (opts.onClick) node.addEventListener("click", opts.onClick);
  for (const c of children) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

async function init() {
  await loadPreflight();
  await loadScenarios();
  renderSetup();
  $("run-btn").addEventListener("click", startRun);
  $("run-again").addEventListener("click", () => location.reload());
}

async function loadPreflight() {
  const res = await fetch("/api/preflight");
  state.preflight = await res.json();
  const banner = $("preflight-status");
  if (state.preflight.ok) {
    banner.textContent = "● ready";
    banner.className = "status ok";
  } else {
    banner.textContent = "● preflight failed";
    banner.className = "status err";
  }
}

async function loadScenarios() {
  const res = await fetch("/api/scenarios");
  state.scenarios = await res.json();
  $("catalog-summary").textContent =
    `${state.scenarios.length} scenarios · 3 runs per path · ~${state.scenarios.length * 3} min total`;

  const list = $("scenario-list");
  list.replaceChildren();
  for (const s of state.scenarios) {
    const checkbox = el("input", {
      attrs: { type: "checkbox", "data-sid": s.id, checked: "checked" },
    });
    const title = el("div", {}, el("strong", { text: s.id }), " — ", s.title);
    const meta = el("div", { className: "meta", text: `${s.category} · ${s.difficulty}` });
    list.appendChild(el("li", {}, checkbox, title, meta));
  }

  if (state.preflight?.ok) $("run-btn").disabled = false;
  else showRemediation();
  buildStepper();
}

function showRemediation() {
  const box = $("preflight-remediation");
  box.hidden = false;
  box.replaceChildren();
  box.appendChild(el("strong", { text: "Preflight issues:" }));
  const ul = el("ul");
  for (const r of state.preflight.remediation) {
    ul.appendChild(el("li", { text: r }));
  }
  box.appendChild(ul);
}

function buildStepper() {
  const nav = $("stepper");
  nav.replaceChildren();
  for (const s of state.scenarios) {
    nav.appendChild(el("div", {
      className: "step",
      text: s.id.split("_")[0],
      attrs: { "data-sid": s.id },
      onClick: () => showScenario(s.id),
    }));
  }
  nav.appendChild(el("div", {
    className: "step",
    text: "Summary",
    attrs: { "data-sid": "summary" },
    onClick: () => showSummary(),
  }));
}

function setStepStatus(sid, cls) {
  const node = document.querySelector(`.step[data-sid="${CSS.escape(sid)}"]`);
  if (!node) return;
  node.classList.remove("done", "active", "error");
  if (cls) node.classList.add(cls);
}

async function startRun() {
  const checked = Array.from(document.querySelectorAll("#scenario-list input:checked"))
    .map((i) => i.dataset.sid);
  if (checked.length === 0) return;
  state.runsPerPath = parseInt($("runs-per-path").value, 10);
  state.model = $("model").value;
  for (const s of state.scenarios) {
    state.scenarioResults[s.id] = { native: [], mcp: [] };
  }

  $("setup-view").hidden = true;
  $("progress-view").hidden = false;

  const body = {
    scenario_ids: checked,
    runs_per_path: state.runsPerPath,
    model: state.model,
    operator: "local user",
    org_name: "(local org)",
  };

  const res = await fetch("/api/run", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    $("progress-text").textContent = "error starting run";
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  const totalRuns = checked.length * state.runsPerPath * 2;
  let doneRuns = 0;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n\n");
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const ev = JSON.parse(line.slice(6));
      handleEvent(ev, () => {
        doneRuns += 1;
        const pct = Math.round((doneRuns / totalRuns) * 100);
        $("progress-fill").style.width = pct + "%";
      });
    }
  }
}

function handleEvent(ev, onRunComplete) {
  switch (ev.kind) {
    case "benchmark_start":
      $("progress-text").textContent = "starting…";
      break;
    case "scenario_start":
      setStepStatus(ev.scenario_id, "active");
      $("progress-text").textContent = `Scenario ${ev.scenario_id} · starting`;
      break;
    case "run_start":
      $("progress-text").textContent =
        `Scenario ${ev.scenario_id} · ${ev.path} run ${ev.run_index}/${ev.total_runs}`;
      break;
    case "run_complete": {
      const bucket = state.scenarioResults[ev.scenario_id];
      bucket[ev.path].push(ev.run_result);
      onRunComplete();
      if (state.active === ev.scenario_id) renderScenario(ev.scenario_id);
      break;
    }
    case "scenario_complete":
      setStepStatus(ev.scenario_id, "done");
      break;
    case "report_written":
      state.reportPath = ev.path;
      break;
    case "benchmark_complete":
      $("progress-view").hidden = true;
      showSummary();
      break;
  }
}

function medianOf(list, key) {
  const ok = list.filter((r) => r.succeeded).map((r) => r[key]).sort((a, b) => a - b);
  if (!ok.length) return 0;
  return ok[Math.floor(ok.length / 2)];
}

function showScenario(sid) {
  state.active = sid;
  $("setup-view").hidden = true;
  $("summary-view").hidden = true;
  $("scenario-view").hidden = false;
  renderScenario(sid);
}

function renderScenario(sid) {
  const scenario = state.scenarios.find((s) => s.id === sid);
  const bucket = state.scenarioResults[sid] || { native: [], mcp: [] };
  $("sv-title").textContent = `${scenario.id} — ${scenario.title}`;
  $("sv-meta").textContent =
    `${scenario.category} · ${scenario.difficulty} · ${state.runsPerPath} runs per path`;
  $("sv-prompt").textContent = scenario.prompt;

  fillPanel("native", bucket.native);
  fillPanel("mcp", bucket.mcp);

  const nativeMed = medianOf(bucket.native, "total_cost_usd");
  const mcpMed = medianOf(bucket.mcp, "total_cost_usd");
  let headline = "—";
  if (nativeMed && mcpMed) {
    const mult = mcpMed / nativeMed;
    if (mult > 1.05) headline = `Native was ${mult.toFixed(1)}× cheaper on this scenario`;
    else if (mult < 0.95) headline = `MCP was ${(1 / mult).toFixed(1)}× cheaper on this scenario`;
    else headline = "Effectively tied on token cost";
  }
  $("sv-headline").textContent = headline;

  renderChart(sid, bucket);
}

function fillPanel(pathName, runs) {
  const pre = `sv-${pathName}`;
  const med = {
    input: medianOf(runs, "input_tokens"),
    cost: medianOf(runs, "total_cost_usd"),
    turns: medianOf(runs, "num_turns"),
  };
  const done = runs.length;
  const ok = runs.filter((r) => r.succeeded).length;
  const status = $(`${pre}-status`);
  if (done === 0) {
    status.className = "panel-status";
    status.textContent = "—";
  } else if (done < state.runsPerPath) {
    status.className = "panel-status running";
    status.textContent = `Running ${done}/${state.runsPerPath}`;
  } else {
    status.className = ok === done ? "panel-status done" : "panel-status error";
    status.textContent = `${ok}/${done} runs succeeded`;
  }
  $(`${pre}-input`).textContent = med.input.toLocaleString();
  $(`${pre}-turns`).textContent = med.turns || "—";
  $(`${pre}-cost`).textContent = med.cost ? `$${med.cost.toFixed(3)}` : "—";

  const tools = $(`${pre}-tools`);
  tools.replaceChildren();
  const firstOk = runs.find((r) => r.succeeded && r.tool_calls?.length);
  for (const t of firstOk?.tool_calls || []) {
    tools.appendChild(el("li", { text: t }));
  }
}

function renderChart(sid, bucket) {
  const ctx = document.getElementById("sv-chart");
  if (state.charts[sid]) state.charts[sid].destroy();
  state.charts[sid] = new Chart(ctx, {
    type: "bar",
    data: {
      labels: ["input tokens", "output tokens", "cost ($×1000)", "turns"],
      datasets: [
        {
          label: "Native",
          backgroundColor: "#0176D3",
          data: [
            medianOf(bucket.native, "input_tokens"),
            medianOf(bucket.native, "output_tokens"),
            medianOf(bucket.native, "total_cost_usd") * 1000,
            medianOf(bucket.native, "num_turns"),
          ],
        },
        {
          label: "MCP",
          backgroundColor: "#5867E8",
          data: [
            medianOf(bucket.mcp, "input_tokens"),
            medianOf(bucket.mcp, "output_tokens"),
            medianOf(bucket.mcp, "total_cost_usd") * 1000,
            medianOf(bucket.mcp, "num_turns"),
          ],
        },
      ],
    },
    options: {
      indexAxis: "y",
      animation: { duration: 200 },
      plugins: { legend: { position: "bottom" } },
      scales: { x: { grid: { display: false } }, y: { grid: { display: false } } },
    },
  });
}

function showSummary() {
  state.active = "summary";
  $("setup-view").hidden = true;
  $("scenario-view").hidden = true;
  $("summary-view").hidden = false;

  let totalN = 0, totalM = 0;
  const mults = [];
  for (const sid in state.scenarioResults) {
    const b = state.scenarioResults[sid];
    const n = medianOf(b.native, "total_cost_usd");
    const m = medianOf(b.mcp, "total_cost_usd");
    totalN += n; totalM += m;
    if (n && m) mults.push(m / n);
  }
  const avg = mults.length ? mults.reduce((a, b) => a + b, 0) / mults.length : null;

  $("summary-headline").textContent = avg
    ? (avg > 1
        ? `Across ${mults.length} scenarios, Native was ${avg.toFixed(1)}× cheaper on average`
        : `Across ${mults.length} scenarios, MCP was ${(1 / avg).toFixed(1)}× cheaper on average`)
    : "—";

  const tbody = $("summary-tbody");
  tbody.replaceChildren();
  const addRow = (label, v1, v2) => {
    tbody.appendChild(el("tr", {},
      el("td", { text: label }),
      el("td", { text: v1 }),
      el("td", { text: v2 }),
    ));
  };
  addRow("Total cost", `$${totalN.toFixed(2)}`, `$${totalM.toFixed(2)}`);

  const bars = $("summary-bars");
  bars.replaceChildren();
  for (const sid in state.scenarioResults) {
    const b = state.scenarioResults[sid];
    const n = medianOf(b.native, "total_cost_usd");
    const m = medianOf(b.mcp, "total_cost_usd");
    const mult = (n && m) ? m / n : 0;
    const barCell = el("div", { className: "bar-cell" },
      el("div", { className: "bar", attrs: { style: `width:${Math.min(mult * 30, 100)}%` } }));
    bars.appendChild(el("li", {},
      el("span", { text: sid }),
      el("span", { text: mult ? `${mult.toFixed(1)}×` : "—" }),
      barCell,
    ));
  }

  const recs = $("summary-recs");
  recs.replaceChildren();
  const lines = [];
  if (avg && avg > 1.05) {
    const pct = Math.round((1 - 1 / avg) * 100);
    lines.push(`Native integrations cost ~${pct}% less per task than MCP on this mix.`);
  } else if (avg && avg < 0.95) {
    const pct = Math.round((1 / avg - 1) * 100);
    lines.push(`MCP averaged ~${pct}% cheaper than native on this mix.`);
  }
  lines.push("Recommendation: prefer native for read-heavy, well-scoped workflows; reconsider MCP where schema richness demonstrably reduces turn count.");
  for (const l of lines) recs.appendChild(el("li", { text: l }));

  const dl = $("download-report");
  if (state.reportPath) {
    dl.href = "/api/reports/latest";
    dl.setAttribute("download", state.reportPath.split("/").pop());
  }
}

function renderSetup() {
  $("setup-view").hidden = false;
  $("scenario-view").hidden = true;
  $("summary-view").hidden = true;
  $("progress-view").hidden = true;
}

document.addEventListener("DOMContentLoaded", init);
```

- [ ] **Step 2: Verify no `innerHTML` usage**

```bash
grep -n "innerHTML" static/app.js
```
Expected: no matches. If any match, replace with `replaceChildren()` + `createElement` / `textContent`.

- [ ] **Step 3: Manual sanity check**

```bash
./run.sh
```
Open `http://localhost:8000`. Verify:
- Header shows `● ready` if preflight passes.
- Scenario list shows 5 scenarios.
- Stepper shows s01…s05 + Summary.
- Clicking "Run Full Benchmark" transitions to the progress view.

Do not claim this task complete if the page fails to render or throws JS errors in DevTools.

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat: frontend SPA — stepper, per-scenario dual panels, summary (safe DOM)"
```

---

## Task 13: End-to-End Smoke Test (mocked Claude)

**Files:**
- Create: `tests/test_e2e.py`

- [ ] **Step 1: Write the tests**

```python
# tests/test_e2e.py
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from token_compare.api import AppConfig, create_app
from token_compare.models import PathName, RunResult


@pytest.fixture
def e2e_client(tmp_path):
    scen_dir = tmp_path / "scenarios"; scen_dir.mkdir()
    (scen_dir / "sA.yaml").write_text(
        "id: sA\ntitle: A\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    (tmp_path / "sf-mcp.json").write_text("{}")
    reports = tmp_path / "reports"; reports.mkdir()
    cfg = AppConfig(
        scenarios_dir=scen_dir, mcp_config_path=tmp_path / "sf-mcp.json",
        reports_dir=reports, static_dir=None,
    )
    return TestClient(create_app(cfg)), reports


def _mk_run(path):
    return RunResult(path=path, input_tokens=500, output_tokens=50,
                     cache_read_input_tokens=0, total_cost_usd=0.02,
                     num_turns=1, duration_ms=100, tool_calls=["x"],
                     succeeded=True, error=None, raw_json={})


def test_full_run_writes_one_report(e2e_client):
    client, reports = e2e_client

    with patch("token_compare.benchmark.run_once", side_effect=lambda s, p, **k: _mk_run(p)), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream(
            "POST", "/api/run",
            json={"scenario_ids": ["sA"], "runs_per_path": 2,
                  "model": "claude-opus-4-7", "operator": "me", "org_name": "o"},
        ) as resp:
            list(resp.iter_text())

    md_files = list(reports.glob("benchmark-*.md"))
    assert len(md_files) == 1
    content = md_files[0].read_text()
    assert "# Token Comparison Benchmark" in content
    assert "sA" in content
    assert "## Appendix — Raw Data" in content


def test_reports_retention_prunes_old(e2e_client):
    client, reports = e2e_client
    for i in range(12):
        (reports / f"benchmark-2026-05-0{i%9+1}-0{i%9+1}{i%9+1}00.md").write_text("old")
    assert len(list(reports.glob("benchmark-*.md"))) == 12

    with patch("token_compare.benchmark.run_once", side_effect=lambda s, p, **k: _mk_run(p)), \
         patch("token_compare.benchmark._git_sha", return_value="abc"):
        with client.stream(
            "POST", "/api/run",
            json={"scenario_ids": ["sA"], "runs_per_path": 1,
                  "model": "m", "operator": "me", "org_name": "o"},
        ) as resp:
            list(resp.iter_text())

    assert len(list(reports.glob("benchmark-*.md"))) <= 10
```

- [ ] **Step 2: Run end-to-end tests**

Run: `pytest tests/test_e2e.py -v`
Expected: PASS (2 tests).

- [ ] **Step 3: Run the full test suite**

Run: `pytest -v`
Expected: all tests pass across every module.

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test: end-to-end smoke tests for run + report persistence + retention"
```

---

## Task 14: Live Manual Validation & README Finalization

**Files:**
- Modify: `README.md` (append troubleshooting + demo script)

- [ ] **Step 1: Run the full benchmark against a real Salesforce org**

```bash
./run.sh
```
Visit `http://localhost:8000`, click "Run Full Benchmark". Wait for it to finish.

Verify in order:
- Preflight banner shows `● ready`.
- Stepper progresses through all 5 scenarios; each turns green when done.
- Each per-scenario page renders dual panels with non-zero numbers.
- Summary page shows an overall multiplier + at least one recommendation.
- `reports/` contains exactly one new `benchmark-YYYY-MM-DD-HHmm.md`.

If anything fails, **fix the bug**, re-run, and only then proceed. Do not mark this task complete while the live run fails.

- [ ] **Step 2: Append Troubleshooting section to `README.md`**

```markdown

## Troubleshooting

**"preflight failed"** — the top banner lists specific remediation. Common causes:
- `claude` not installed or not on PATH → install from https://claude.ai/code.
- `claude auth status` reports not logged in → run `claude login`.
- `sf org list --json` returns empty → run `sf org login web` and pick your org.
- `config/sf-mcp.json` missing → copy/edit the example in `config/README.md`.

**A path's panel turns yellow/red mid-run** — open the raw appendix section in
the generated markdown report; the last entry contains the full stderr and any
tool-call errors. Distinguish "the path was slow" from "the path was broken"
before comparing costs.

**Reports folder is empty after a run** — the run may have errored before
writing. Check the terminal where `./run.sh` is running for the traceback.

**"Run again" reloads the page** — the tool keeps state per page load by design.
```

- [ ] **Step 3: Append Demo script to `README.md`**

```markdown

## Demo script (≈5 minutes)

1. Open `http://localhost:8000`. Point out the top-right status: `● ready`.
2. Show the scenario catalog. Explain: same prompt, two integration paths.
3. Click **Run Full Benchmark**. Narrate the progress bar.
4. During the run, click into any completed scenario's step. Walk through the
   dual panel: identical prompt, very different input-token counts.
5. After completion, land on the **Summary** page. Read the headline.
6. Click **Download report** to show the single markdown artifact that travels.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: troubleshooting and demo-script sections"
```

- [ ] **Step 5: Tag the first working build**

```bash
git tag -a v0.1.0 -m "v0.1.0 — first working end-to-end token comparison benchmark"
```

---

## Self-Review (after writing this plan)

**1. Spec coverage**

| Spec section | Task(s) that implement it |
|--------------|---------------------------|
| §1 Purpose / non-goals | Read-only scenarios (Task 3); no API keys (Tasks 4, 5) |
| §2 Distribution & execution | Task 0 (`run.sh`, README), Task 4 (preflight) |
| §3 Architecture | Tasks 0, 5, 6, 9 |
| §4 Scenario catalog | Tasks 2, 3 |
| §5 Measurement methodology | Task 1 (models), Task 5 (parse `usage`/cost), Task 6 (path randomization), Task 8 (methodology section in report) |
| §6 UI (light mode, stepper, dual panel, summary) | Tasks 11, 12 |
| §7 Report output (single markdown, retention, privacy) | Tasks 8, 9 |
| §8 Units / components | 1:1 task breakdown |
| §9 Open questions | PDF export deferred — explicit follow-up (not blocking MVP) |
| §10 Success criteria | Task 14 (live validation, README quickstart) |

PDF export (§7) and record-data redaction logic are acknowledged follow-ups. The markdown report covers the single-artifact requirement; PDF is additive.

**2. Placeholder scan:** no "TBD", "TODO", or "implement later". Every code step contains executable code; every command step contains the exact command.

**3. Type consistency:** `PathName`, `Scenario`, `SuccessCriteria`, `RunResult`, `ScenarioResult`, `BenchmarkResult`, `BenchmarkOptions`, `ProgressEvent`, `AppConfig`, `RunRequest`, `PreflightResult` are each defined in exactly one task and referenced by matching name in later tasks and tests. Function names (`run_once`, `run_benchmark`, `write_markdown`, `load_all`, `load_file`, `check_environment`, `generate`, `build_command`, `parse_claude_json`, `create_app`) stay consistent throughout.

**4. Scope check:** single subsystem — one local benchmark tool with a bounded UI. Fits one plan cleanly; no decomposition needed.

**5. Security:** the hook flagged `innerHTML` use in the frontend draft. Task 12 explicitly bans it and includes a grep check as an acceptance step.
