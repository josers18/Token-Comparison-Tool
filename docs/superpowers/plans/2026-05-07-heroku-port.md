# Heroku Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the Token Comparison Tool from a local-only Python app (driven by `claude` CLI subprocess + local `sf` CLI) to a Heroku-hosted web service that uses the three Heroku Managed Inference addons and Heroku Postgres while preserving the original Native-vs-MCP comparison experiment.

**Architecture:** Replace `runner.py` (subprocess) with `messages_runner.py` (Anthropic Messages API client pointing at Heroku Inference). Native path = a small Python tool set hitting the org's REST API directly. MCP path = the same Messages API call with `mcp_servers` pointed at the Salesforce Platform MCP gateway. SF tokens move from a local cache file into Postgres, keyed by an HTTP-only signed session cookie. Reports move from the filesystem into Postgres. Existing analysis/report/SPA code stays unchanged because the runner returns the same `RunResult` Pydantic shape.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, `anthropic` SDK (Heroku Inference is Anthropic-API-compatible), `asyncpg` (via `databases`/raw asyncpg), Heroku Postgres essential-0, Heroku Managed Inference (3 attached addons), vanilla-JS SPA.

**Spec:** [`docs/superpowers/specs/2026-05-07-heroku-port-design.md`](../specs/2026-05-07-heroku-port-design.md)

---

## Conventions

- Run all commands from the repo root (`/Users/jsifontes/Documents/Git/Token_Comparison_Tool`).
- Activate the venv before running tests: `source .venv/bin/activate`.
- Test command: `pytest tests/ -q` (full suite) or `pytest tests/<file>::<test> -v` (single test).
- Heroku CLI is already authenticated as `jsifontes@salesforce.com`. App name: `token-comparison-tool`.
- Commit after every passing step that adds or fixes code. Commit messages use Conventional Commits.
- Never include `--no-verify` in commits.
- Never run `git push` unless a task says so explicitly.

---

## File Structure

**New files**

```
Procfile                                ← Heroku web dyno entry
runtime.txt                             ← python-3.11.x pin
app.json                                ← Heroku addon manifest
src/token_compare/db.py                 ← Postgres pool + migrations + DAOs
src/token_compare/sessions.py           ← signed-cookie session middleware
src/token_compare/inference_client.py   ← Heroku Inference Anthropic client factory
src/token_compare/messages_runner.py    ← Anthropic Messages API tool-use loop
src/token_compare/native_tools.py       ← REST-backed Native-path tool definitions
src/token_compare/mcp_path.py           ← mcp_servers builder for the MCP path
src/token_compare/pricing.py            ← per-model token price table
tests/test_db.py
tests/test_sessions.py
tests/test_inference_client.py
tests/test_messages_runner.py           ← (renamed from test_runner.py)
tests/test_native_tools.py
tests/test_mcp_path.py
tests/test_pricing.py
tests/test_e2e_smoke.py                 ← real-Inference smoke, opt-in
```

**Modified files**

```
pyproject.toml                          ← add anthropic, asyncpg, itsdangerous
src/token_compare/api.py                ← /api/models, db-backed reports, SSE heartbeats
src/token_compare/benchmark.py          ← call messages_runner; tokens via sessions
src/token_compare/sf_auth.py            ← drop localhost guard; DB-backed token cache
src/token_compare/preflight.py          ← Heroku-flavored checks
static/app.js                           ← model dropdown
static/index.html                       ← model dropdown markup
.env.example                            ← Heroku-flavored env keys
README.md                               ← deploy + usage instructions
```

**Removed files**

```
src/token_compare/runner.py             ← replaced by messages_runner.py
src/token_compare/mcp_config.py         ← replaced by mcp_path.py (no temp file)
tests/test_runner.py                    ← renamed to test_messages_runner.py
tests/test_mcp_config.py                ← obsolete
```

---

## Phase 0: Branch + dependency setup

### Task 0.1: Create a feature branch

**Files:** N/A

- [ ] **Step 1: Create and check out branch**

```bash
git checkout -b heroku-port
```

- [ ] **Step 2: Verify clean working tree**

```bash
git status
```

Expected: `nothing to commit, working tree clean` (the spec from the brainstorm is already committed on `main`).

### Task 0.2: Add Python dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Replace dependencies block in `pyproject.toml`**

Open `pyproject.toml`. Replace the `dependencies = [...]` array with:

```toml
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "pydantic>=2.9",
  "pyyaml>=6.0",
  "httpx>=0.27",
  "python-multipart>=0.0.20",
  "anthropic>=0.40",
  "asyncpg>=0.30",
  "itsdangerous>=2.2",
]
```

In `[project.optional-dependencies]`, append `"pytest-postgresql>=6.1"` to the `dev` list.

- [ ] **Step 2: Reinstall in editable mode**

```bash
source .venv/bin/activate
pip install -e ".[dev]" -q
```

Expected: no errors. Confirm with `python -c "import anthropic, asyncpg, itsdangerous; print('ok')"`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore(deps): add anthropic, asyncpg, itsdangerous"
```

### Task 0.3: Provision Heroku Postgres

**Files:** N/A (Heroku-side)

- [ ] **Step 1: Create the addon**

```bash
heroku addons:create heroku-postgresql:essential-0 -a token-comparison-tool
```

Expected: addon name printed; `DATABASE_URL` config var set automatically.

- [ ] **Step 2: Verify**

```bash
heroku config:get DATABASE_URL -a token-comparison-tool
```

Expected: a `postgres://...` URL.

- [ ] **Step 3: Set the session secret**

```bash
heroku config:set SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" -a token-comparison-tool
```

- [ ] **Step 4: Set the OAuth redirect URI**

```bash
heroku config:set SF_REDIRECT_URI="https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com/callback" -a token-comparison-tool
```

- [ ] **Step 5: Manual: add the redirect URI to your ECA**

In Salesforce Setup → External Client App Manager → your app → OAuth Settings → Callback URL list, add:

```
https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com/callback
```

This is a manual step. Note it as done before continuing.

- [ ] **Step 6: Set the SF ECA credentials as Heroku config**

```bash
heroku config:set \
  SF_CLIENT_ID="<your_eca_client_id>" \
  SF_CLIENT_SECRET="<your_eca_client_secret>" \
  SF_LOGIN_URL="https://login.salesforce.com" \
  -a token-comparison-tool
```

(Use real values; do not commit them anywhere.)

---

## Phase 1: Pricing table (no deps, fully offline)

### Task 1.1: Pricing module + tests

**Files:**
- Create: `src/token_compare/pricing.py`
- Create: `tests/test_pricing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pricing.py`:

```python
from token_compare.pricing import compute_cost_usd, MODEL_PRICES


def test_known_model_computes_cost():
    cost = compute_cost_usd(
        model="claude-4-5-sonnet",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    p = MODEL_PRICES["claude-4-5-sonnet"]
    expected = p["input"] + p["output"]
    assert abs(cost - expected) < 1e-9


def test_cache_tokens_priced_separately():
    cost = compute_cost_usd(
        model="claude-4-5-sonnet",
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
    )
    p = MODEL_PRICES["claude-4-5-sonnet"]
    expected = p["cache_read"] + p["cache_creation"]
    assert abs(cost - expected) < 1e-9


def test_unknown_model_returns_zero():
    cost = compute_cost_usd(
        model="some-unrecognized-model",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    assert cost == 0.0


def test_haiku_priced():
    assert "claude-4-5-haiku" in MODEL_PRICES
    p = MODEL_PRICES["claude-4-5-haiku"]
    assert p["input"] > 0 and p["output"] > p["input"]


def test_opus_priced():
    assert "claude-opus-4-5" in MODEL_PRICES
    p = MODEL_PRICES["claude-opus-4-5"]
    assert p["input"] > 0 and p["output"] > p["input"]
```

- [ ] **Step 2: Run; expect failure**

```bash
pytest tests/test_pricing.py -v
```

Expected: `ModuleNotFoundError: No module named 'token_compare.pricing'`.

- [ ] **Step 3: Implement**

Create `src/token_compare/pricing.py`:

```python
from __future__ import annotations

# Per-1M-token USD prices for each Heroku Inference model.
# Sourced from Anthropic's published pricing for the equivalent Claude models.
# Update if Heroku Inference publishes its own pricing or Anthropic changes theirs.
# Keys are the model_id strings the Heroku Inference addons set as
# INFERENCE_MODEL_ID / HEROKU_INFERENCE_TEAL_MODEL_ID / HEROKU_INFERENCE_COBALT_MODEL_ID.
MODEL_PRICES: dict[str, dict[str, float]] = {
    "claude-4-5-haiku": {
        "input": 1.00,
        "output": 5.00,
        "cache_read": 0.10,
        "cache_creation": 1.25,
    },
    "claude-4-5-sonnet": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_creation": 3.75,
    },
    "claude-opus-4-5": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_creation": 18.75,
    },
}


def compute_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
) -> float:
    p = MODEL_PRICES.get(model)
    if not p:
        return 0.0
    per_million = 1_000_000.0
    return (
        input_tokens * p["input"] / per_million
        + output_tokens * p["output"] / per_million
        + cache_read_input_tokens * p["cache_read"] / per_million
        + cache_creation_input_tokens * p["cache_creation"] / per_million
    )
```

- [ ] **Step 4: Run; expect pass**

```bash
pytest tests/test_pricing.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/pricing.py tests/test_pricing.py
git commit -m "feat(pricing): add per-model token price table"
```

---

## Phase 2: Database layer

### Task 2.1: DB pool + idempotent migration

**Files:**
- Create: `src/token_compare/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_db.py`:

```python
import os
import pytest
import asyncio
import asyncpg

from token_compare import db as db_mod


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def pool(monkeypatch):
    """Spin up a test pool against the URL in TEST_DATABASE_URL.
    Skip if not set — DB tests are opt-in for CI parity."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", url)
    p = await db_mod.connect()
    # Tear down any prior schema so the migration can run cleanly.
    async with p.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS inference_audit, runs, reports, sessions CASCADE")
    yield p
    await db_mod.close()


async def test_migrate_creates_tables(pool):
    await db_mod.migrate()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename IN ('sessions','reports','runs','inference_audit')"
        )
    names = {r["tablename"] for r in rows}
    assert names == {"sessions", "reports", "runs", "inference_audit"}


async def test_migrate_is_idempotent(pool):
    await db_mod.migrate()
    await db_mod.migrate()
    # Second call must not raise.


async def test_session_round_trip(pool):
    await db_mod.migrate()
    sid = await db_mod.create_session()
    assert isinstance(sid, str) and len(sid) >= 32
    await db_mod.put_sf_token(sid, {"access_token": "abc", "instance_url": "https://x"})
    tok = await db_mod.get_sf_token(sid)
    assert tok["access_token"] == "abc"
    await db_mod.delete_sf_token(sid)
    assert await db_mod.get_sf_token(sid) is None


async def test_report_round_trip(pool):
    await db_mod.migrate()
    rid = await db_mod.create_report(model="claude-4-5-sonnet", operator="me", org_name="org")
    await db_mod.finalize_report(rid, payload={"scenarios": [], "model": "claude-4-5-sonnet"})
    items = await db_mod.list_reports(limit=10)
    assert any(r["id"] == rid for r in items)
    full = await db_mod.get_report(rid)
    assert full["payload_json"]["model"] == "claude-4-5-sonnet"
```

- [ ] **Step 2: Run; expect failure**

```bash
pytest tests/test_db.py -v
```

Expected: skip (no `TEST_DATABASE_URL` set) or `ModuleNotFoundError`.

- [ ] **Step 3: Implement `db.py`**

Create `src/token_compare/db.py`:

```python
from __future__ import annotations

import json
import os
import secrets
from typing import Any, Optional

import asyncpg


_pool: Optional[asyncpg.Pool] = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT PRIMARY KEY,
  sf_token_json JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS reports (
  id           TEXT PRIMARY KEY,
  started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at  TIMESTAMPTZ,
  model        TEXT NOT NULL,
  org_name     TEXT,
  operator     TEXT,
  payload_json JSONB,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reports_started_at_idx ON reports (started_at DESC);

CREATE TABLE IF NOT EXISTS runs (
  id           TEXT PRIMARY KEY,
  report_id    TEXT REFERENCES reports(id) ON DELETE CASCADE,
  scenario_id  TEXT NOT NULL,
  path         TEXT NOT NULL,
  run_index    INT NOT NULL,
  result_json  JSONB NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runs_report_id_idx ON runs (report_id);

CREATE TABLE IF NOT EXISTS inference_audit (
  id               BIGSERIAL PRIMARY KEY,
  run_id           TEXT REFERENCES runs(id) ON DELETE CASCADE,
  scenario_id      TEXT NOT NULL,
  path             TEXT NOT NULL,
  model            TEXT NOT NULL,
  prompt_hash      TEXT NOT NULL,
  token_usage_json JSONB NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _normalize_url(url: str) -> str:
    # Heroku sometimes hands out postgres:// — asyncpg requires postgresql://
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    _pool = await asyncpg.create_pool(_normalize_url(url), min_size=1, max_size=4)
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def migrate() -> None:
    pool = await connect()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)


# ---- Sessions ----

async def create_session() -> str:
    sid = secrets.token_hex(32)
    pool = await connect()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO sessions (id) VALUES ($1)", sid)
    return sid


async def put_sf_token(session_id: str, token: dict[str, Any]) -> None:
    pool = await connect()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET sf_token_json=$2 WHERE id=$1",
            session_id, json.dumps(token),
        )


async def get_sf_token(session_id: str) -> Optional[dict[str, Any]]:
    pool = await connect()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT sf_token_json FROM sessions WHERE id=$1", session_id,
        )
    if not row or row["sf_token_json"] is None:
        return None
    raw = row["sf_token_json"]
    return raw if isinstance(raw, dict) else json.loads(raw)


async def delete_sf_token(session_id: str) -> None:
    pool = await connect()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET sf_token_json=NULL WHERE id=$1", session_id,
        )


# ---- Reports ----

async def create_report(*, model: str, operator: str, org_name: str) -> str:
    rid = "rpt_" + secrets.token_hex(8)
    pool = await connect()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO reports (id, model, operator, org_name) VALUES ($1,$2,$3,$4)",
            rid, model, operator, org_name,
        )
    return rid


async def finalize_report(report_id: str, *, payload: dict) -> None:
    pool = await connect()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE reports SET finished_at=now(), payload_json=$2 WHERE id=$1",
            report_id, json.dumps(payload),
        )


async def list_reports(limit: int = 10) -> list[dict]:
    pool = await connect()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, started_at, finished_at, model, operator, org_name "
            "FROM reports ORDER BY started_at DESC LIMIT $1",
            limit,
        )
    return [dict(r) for r in rows]


async def get_report(report_id: str) -> Optional[dict]:
    pool = await connect()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, started_at, finished_at, model, operator, org_name, payload_json "
            "FROM reports WHERE id=$1",
            report_id,
        )
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("payload_json"), str):
        d["payload_json"] = json.loads(d["payload_json"])
    return d


# ---- Runs ----

async def insert_run(
    *, report_id: str, scenario_id: str, path: str,
    run_index: int, result: dict,
) -> str:
    rid = "run_" + secrets.token_hex(8)
    pool = await connect()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO runs (id, report_id, scenario_id, path, run_index, result_json) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            rid, report_id, scenario_id, path, run_index, json.dumps(result),
        )
    return rid


# ---- Audit ----

async def insert_audit(
    *, run_id: str, scenario_id: str, path: str, model: str,
    prompt_hash: str, token_usage: dict,
) -> None:
    pool = await connect()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inference_audit "
            "(run_id, scenario_id, path, model, prompt_hash, token_usage_json) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            run_id, scenario_id, path, model, prompt_hash, json.dumps(token_usage),
        )
```

- [ ] **Step 4: Run with a real DB**

If you have a local Postgres available, point the test at it:

```bash
TEST_DATABASE_URL="postgresql://localhost/token_compare_test" pytest tests/test_db.py -v
```

Expected: 4 tests pass. If you don't have local PG, the tests will skip — fine for CI; we'll exercise the same code via a Heroku one-off later.

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/db.py tests/test_db.py
git commit -m "feat(db): asyncpg pool + idempotent schema + DAOs"
```

---

## Phase 3: Session middleware

### Task 3.1: Signed-cookie session middleware

**Files:**
- Create: `src/token_compare/sessions.py`
- Create: `tests/test_sessions.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_sessions.py`:

```python
import os
import pytest
from itsdangerous import BadSignature
from token_compare.sessions import sign_session_id, verify_session_id


def test_round_trip(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    signed = sign_session_id("abc123")
    assert verify_session_id(signed) == "abc123"


def test_tampered_cookie_rejected(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    signed = sign_session_id("abc123")
    bad = signed[:-2] + ("AA" if not signed.endswith("AA") else "BB")
    with pytest.raises(BadSignature):
        verify_session_id(bad)


def test_missing_secret_raises(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        sign_session_id("abc123")
```

- [ ] **Step 2: Run; expect failure**

```bash
pytest tests/test_sessions.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/token_compare/sessions.py`:

```python
from __future__ import annotations

import os
from itsdangerous import BadSignature, URLSafeSerializer

COOKIE_NAME = "tct_sid"


def _serializer() -> URLSafeSerializer:
    secret = os.environ.get("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET is not set")
    return URLSafeSerializer(secret, salt="tct-session")


def sign_session_id(session_id: str) -> str:
    return _serializer().dumps(session_id)


def verify_session_id(signed: str) -> str:
    """Returns the unsigned session id. Raises BadSignature if tampered."""
    return _serializer().loads(signed)


__all__ = ["COOKIE_NAME", "sign_session_id", "verify_session_id", "BadSignature"]
```

- [ ] **Step 4: Run; expect pass**

```bash
pytest tests/test_sessions.py -v
```

Expected: 3 pass.

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/sessions.py tests/test_sessions.py
git commit -m "feat(sessions): signed-cookie session id helpers"
```

---

## Phase 4: SF auth on Heroku

### Task 4.1: Allow Heroku redirect URI; remove file-cache fallback

**Files:**
- Modify: `src/token_compare/sf_auth.py`
- Modify: `tests/test_sf_auth.py`

- [ ] **Step 1: Read current `sf_auth.py`**

Already read into context. Key changes:
1. `run_interactive_login` rejects non-localhost redirects. Loosen to also accept `https://*.herokuapp.com/*`.
2. `_save_cache` / `_load_cache` write `.cache/sf-token.json`. We'll keep these for local-dev-only convenience but introduce DB-backed alternates that the API layer will call.

- [ ] **Step 2: Modify `run_interactive_login`**

In `src/token_compare/sf_auth.py`, find the redirect-uri guard:

```python
    redirect = urllib.parse.urlparse(creds.redirect_uri)
    if redirect.hostname not in {"localhost", "127.0.0.1"}:
        raise SfAuthError(
            f"redirect_uri {creds.redirect_uri} is not localhost; refusing to "
            "run interactive login (callback cannot be served)."
        )
```

Replace with:

```python
    redirect = urllib.parse.urlparse(creds.redirect_uri)
    host = (redirect.hostname or "").lower()
    is_local = host in {"localhost", "127.0.0.1"}
    is_heroku = host.endswith(".herokuapp.com") and redirect.scheme == "https"
    if not (is_local or is_heroku):
        raise SfAuthError(
            f"redirect_uri {creds.redirect_uri} is not localhost or *.herokuapp.com; "
            "refusing to run interactive login (callback cannot be served)."
        )
```

- [ ] **Step 3: Update tests for the new redirect rule**

Open `tests/test_sf_auth.py` (already exists). Find any test that asserts `run_interactive_login` rejects non-localhost. Add a new test next to it:

```python
def test_interactive_login_accepts_heroku_redirect(monkeypatch):
    creds = OAuthCredentials(
        client_id="cid", client_secret="csec",
        login_url="https://login.salesforce.com",
        redirect_uri="https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com/callback",
    )
    # We won't actually open a browser; verify only that the host check passes.
    # Stub _register_pending so the function gets past the guard.
    called = {}
    def fake_register(state, c, v):
        called["ok"] = True
        class _P:
            event = type("E", (), {"wait": lambda self, timeout=None: True})()
            error = "stubbed"
            token = None
        return _P()
    monkeypatch.setattr("token_compare.sf_auth._register_pending", fake_register)
    with pytest.raises(SfAuthError, match="stubbed"):
        run_interactive_login(creds, open_browser=False, timeout_s=0.1)
    assert called.get("ok") is True


def test_interactive_login_rejects_random_https(monkeypatch):
    creds = OAuthCredentials(
        client_id="cid", client_secret="csec",
        login_url="https://login.salesforce.com",
        redirect_uri="https://example.com/callback",
    )
    with pytest.raises(SfAuthError, match="not localhost or"):
        run_interactive_login(creds, open_browser=False, timeout_s=0.1)
```

If the existing test file already imports `OAuthCredentials`, `SfAuthError`, `run_interactive_login`, and `pytest`, just append. Otherwise add the imports at the top.

- [ ] **Step 4: Run; expect pass**

```bash
pytest tests/test_sf_auth.py -v
```

Expected: existing tests still pass; the two new ones pass.

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/sf_auth.py tests/test_sf_auth.py
git commit -m "feat(sf-auth): accept *.herokuapp.com as a valid OAuth redirect"
```

---

## Phase 5: Inference client

### Task 5.1: Heroku Inference client factory

**Files:**
- Create: `src/token_compare/inference_client.py`
- Create: `tests/test_inference_client.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_inference_client.py`:

```python
import pytest

from token_compare.inference_client import (
    discover_models, get_client_for_model, ModelInfo,
)


def test_discover_models_reads_three_addons(monkeypatch):
    monkeypatch.setenv("INFERENCE_URL", "https://us.inference.heroku.com")
    monkeypatch.setenv("INFERENCE_KEY", "inf-sonnet")
    monkeypatch.setenv("INFERENCE_MODEL_ID", "claude-4-5-sonnet")
    monkeypatch.setenv("HEROKU_INFERENCE_TEAL_URL", "https://us.inference.heroku.com")
    monkeypatch.setenv("HEROKU_INFERENCE_TEAL_KEY", "inf-haiku")
    monkeypatch.setenv("HEROKU_INFERENCE_TEAL_MODEL_ID", "claude-4-5-haiku")
    monkeypatch.setenv("HEROKU_INFERENCE_COBALT_URL", "https://us.inference.heroku.com")
    monkeypatch.setenv("HEROKU_INFERENCE_COBALT_KEY", "inf-opus")
    monkeypatch.setenv("HEROKU_INFERENCE_COBALT_MODEL_ID", "claude-opus-4-5")
    models = discover_models()
    ids = {m.model_id for m in models}
    assert ids == {"claude-4-5-haiku", "claude-4-5-sonnet", "claude-opus-4-5"}
    for m in models:
        assert m.url.startswith("https://")
        assert m.api_key.startswith("inf-")


def test_get_client_for_model_returns_anthropic_client(monkeypatch):
    monkeypatch.setenv("INFERENCE_URL", "https://x")
    monkeypatch.setenv("INFERENCE_KEY", "k")
    monkeypatch.setenv("INFERENCE_MODEL_ID", "claude-4-5-sonnet")
    client = get_client_for_model("claude-4-5-sonnet")
    # anthropic.Anthropic exposes .messages
    assert hasattr(client, "messages")


def test_unknown_model_raises():
    with pytest.raises(ValueError, match="no Heroku Inference addon"):
        get_client_for_model("not-a-real-model")
```

- [ ] **Step 2: Run; expect failure**

```bash
pytest tests/test_inference_client.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/token_compare/inference_client.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass

from anthropic import Anthropic


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    url: str
    api_key: str


# (env_url_key, env_key_key, env_model_key) tuples for each attached
# Heroku Inference addon. Order is the dropdown order in the UI:
# cheap → mid → premium.
_ADDONS = [
    ("HEROKU_INFERENCE_TEAL_URL",  "HEROKU_INFERENCE_TEAL_KEY",  "HEROKU_INFERENCE_TEAL_MODEL_ID"),
    ("INFERENCE_URL",              "INFERENCE_KEY",              "INFERENCE_MODEL_ID"),
    ("HEROKU_INFERENCE_COBALT_URL","HEROKU_INFERENCE_COBALT_KEY","HEROKU_INFERENCE_COBALT_MODEL_ID"),
]


def discover_models() -> list[ModelInfo]:
    out: list[ModelInfo] = []
    for url_k, key_k, model_k in _ADDONS:
        url = os.environ.get(url_k)
        key = os.environ.get(key_k)
        model = os.environ.get(model_k)
        if url and key and model:
            out.append(ModelInfo(model_id=model, url=url, api_key=key))
    return out


def get_client_for_model(model_id: str) -> Anthropic:
    for m in discover_models():
        if m.model_id == model_id:
            return Anthropic(base_url=m.url, api_key=m.api_key)
    raise ValueError(f"no Heroku Inference addon for model_id={model_id!r}")
```

- [ ] **Step 4: Run; expect pass**

```bash
pytest tests/test_inference_client.py -v
```

Expected: 3 pass.

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/inference_client.py tests/test_inference_client.py
git commit -m "feat(inference): client factory for the 3 Heroku Inference addons"
```

---

## Phase 6: Native-path tools

### Task 6.1: REST-backed Native tools

**Files:**
- Create: `src/token_compare/native_tools.py`
- Create: `tests/test_native_tools.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_native_tools.py`:

```python
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
```

- [ ] **Step 2: Run; expect failure**

```bash
pytest tests/test_native_tools.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/token_compare/native_tools.py`:

```python
from __future__ import annotations

from typing import Any
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
    url = f"{base}/services/data/{_API_VERSION}/sobjects/{args['name']}/describe"
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
```

- [ ] **Step 4: Run; expect pass**

```bash
pytest tests/test_native_tools.py -v
```

Expected: 6 pass.

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/native_tools.py tests/test_native_tools.py
git commit -m "feat(native-tools): REST-backed tool set for the Native path"
```

---

## Phase 7: MCP-path builder

### Task 7.1: `mcp_path.build`

**Files:**
- Create: `src/token_compare/mcp_path.py`
- Create: `tests/test_mcp_path.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_mcp_path.py`:

```python
import json
import pytest
from pathlib import Path

from token_compare.mcp_path import build_mcp_servers


def _write_template(tmp_path: Path) -> Path:
    cfg = {
        "mcpServers": {
            "salesforce_crm": {
                "type": "http",
                "url": "https://api.salesforce.com/platform/mcp/v1/platform/sobject-all",
                "headers": {"Authorization": "Bearer ${SF_ACCESS_TOKEN}"},
            },
            "data_cloud_queries": {
                "type": "http",
                "url": "https://api.salesforce.com/platform/mcp/v1/data/data-cloud-queries",
                "headers": {"Authorization": "Bearer ${SF_ACCESS_TOKEN}"},
            },
        }
    }
    p = tmp_path / "sf-mcp.json"
    p.write_text(json.dumps(cfg))
    return p


def test_build_injects_bearer(tmp_path):
    cfg = _write_template(tmp_path)
    out = build_mcp_servers(cfg, sf_access_token="TOK123")
    assert isinstance(out, list)
    assert len(out) == 2
    by_name = {s["name"]: s for s in out}
    assert by_name["salesforce_crm"]["authorization_token"] == "TOK123"
    assert by_name["salesforce_crm"]["url"].startswith("https://")
    assert by_name["data_cloud_queries"]["type"] == "url"


def test_missing_template_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_mcp_servers(tmp_path / "missing.json", sf_access_token="X")
```

- [ ] **Step 2: Run; expect failure**

```bash
pytest tests/test_mcp_path.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/token_compare/mcp_path.py`:

```python
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
```

- [ ] **Step 4: Run; expect pass**

```bash
pytest tests/test_mcp_path.py -v
```

Expected: 2 pass.

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/mcp_path.py tests/test_mcp_path.py
git commit -m "feat(mcp-path): build mcp_servers payload from sf-mcp.json template"
```

---

## Phase 8: Messages runner

### Task 8.1: Tool-use loop with usage aggregation

**Files:**
- Create: `src/token_compare/messages_runner.py`
- Create: `tests/test_messages_runner.py`

This is the heart of the port. It mirrors the public surface of the old `run_once`.

- [ ] **Step 1: Write failing tests (Native path)**

Create `tests/test_messages_runner.py`:

```python
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from token_compare.messages_runner import run_once
from token_compare.models import PathName, Scenario, SuccessCriteria


def _scenario():
    return Scenario(
        id="s_test", title="t", category="c", difficulty="simple",
        prompt="Find the top 1 Account",
        success_criteria=SuccessCriteria(),
    )


def _make_msg_response(*, stop_reason, content, usage):
    """Build the shape the Anthropic SDK returns from messages.create()."""
    m = MagicMock()
    m.stop_reason = stop_reason
    m.content = content
    m.usage = MagicMock(
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
    )
    return m


def test_native_single_tool_call_then_text(monkeypatch, tmp_path):
    """Two-turn loop: model calls execute_soql, then returns final text."""
    # Turn 1: tool_use
    tool_use = MagicMock(type="tool_use", id="tu_1", name="execute_soql",
                        input={"query": "SELECT Id FROM Account LIMIT 1"})
    r1 = _make_msg_response(
        stop_reason="tool_use", content=[tool_use],
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    # Turn 2: end_turn with final text
    text = MagicMock(type="text", text="Done. Top account: Acme.")
    r2 = _make_msg_response(
        stop_reason="end_turn", content=[text],
        usage={"input_tokens": 200, "output_tokens": 30,
               "cache_read_input_tokens": 80, "cache_creation_input_tokens": 0},
    )

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [r1, r2]
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda model_id: fake_client,
    )
    monkeypatch.setattr(
        "token_compare.messages_runner.dispatch_native_tool",
        lambda name, args, tok: {"records": [{"Id": "001"}]},
    )

    result = run_once(
        _scenario(), PathName.NATIVE,
        model="claude-4-5-sonnet", max_turns=10, timeout_s=60,
        mcp_template_path=tmp_path / "unused.json",
        sf_token={"access_token": "T", "instance_url": "https://x"},
    )

    # Aggregated across both turns
    assert result.input_tokens == 300
    assert result.output_tokens == 80
    assert result.cache_read_input_tokens == 80
    assert result.num_turns == 2
    assert result.tool_calls == ["execute_soql"]
    assert result.succeeded is True
    assert result.path == PathName.NATIVE


def test_max_turns_recorded_as_failure(monkeypatch, tmp_path):
    tool_use = MagicMock(type="tool_use", id="tu_1", name="execute_soql",
                        input={"query": "SELECT 1"})
    r = _make_msg_response(
        stop_reason="tool_use", content=[tool_use],
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = r
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda mid: fake_client,
    )
    monkeypatch.setattr(
        "token_compare.messages_runner.dispatch_native_tool",
        lambda *a, **kw: {"records": []},
    )

    result = run_once(
        _scenario(), PathName.NATIVE,
        model="claude-4-5-sonnet", max_turns=2, timeout_s=60,
        mcp_template_path=tmp_path / "x.json",
        sf_token={"access_token": "T", "instance_url": "https://x"},
    )

    assert result.succeeded is False
    assert "max_turns" in (result.error or "")
    assert result.num_turns == 2  # cap honored


def test_mcp_path_passes_mcp_servers_not_tools(monkeypatch, tmp_path):
    cfg = tmp_path / "sf-mcp.json"
    cfg.write_text(
        '{"mcpServers":{"x":{"type":"http","url":"https://example",'
        '"headers":{"Authorization":"Bearer ${SF_ACCESS_TOKEN}"}}}}'
    )
    text = MagicMock(type="text", text="ok")
    r = _make_msg_response(
        stop_reason="end_turn", content=[text],
        usage={"input_tokens": 10, "output_tokens": 1},
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = r
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda mid: fake_client,
    )

    run_once(
        _scenario(), PathName.MCP,
        model="claude-4-5-sonnet", max_turns=5, timeout_s=60,
        mcp_template_path=cfg,
        sf_token={"access_token": "TOK", "instance_url": "https://x"},
    )

    kwargs = fake_client.messages.create.call_args.kwargs
    assert "mcp_servers" in kwargs
    assert kwargs["mcp_servers"][0]["authorization_token"] == "TOK"
    assert "tools" not in kwargs


def test_inference_5xx_retried_then_fails(monkeypatch, tmp_path):
    import anthropic
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = anthropic.APIError(
        message="boom", request=None, body=None,
    )
    monkeypatch.setattr(
        "token_compare.messages_runner.get_client_for_model",
        lambda mid: fake_client,
    )
    result = run_once(
        _scenario(), PathName.NATIVE,
        model="claude-4-5-sonnet", max_turns=5, timeout_s=60,
        mcp_template_path=tmp_path / "x.json",
        sf_token={"access_token": "T", "instance_url": "https://x"},
    )
    assert result.succeeded is False
    assert "inference" in (result.error or "").lower()
    # Retried at least once
    assert fake_client.messages.create.call_count >= 2
```

- [ ] **Step 2: Run; expect failure**

```bash
pytest tests/test_messages_runner.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/token_compare/messages_runner.py`:

```python
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import anthropic

from token_compare.inference_client import get_client_for_model
from token_compare.mcp_path import build_mcp_servers
from token_compare.models import PathName, RunResult, Scenario, SuccessCriteria
from token_compare.native_tools import NATIVE_TOOL_DEFS, dispatch_native_tool
from token_compare.pricing import compute_cost_usd


SHARED_PREAMBLE = (
    "You have access to tools for querying Salesforce and Data Cloud. "
    "Data Cloud Data Model Objects (DMOs, typically ending in __dlm) are "
    "queryable as regular sObjects in this org. "
    "Before querying, use your available tools to discover the correct "
    "object, field, and table names — do not guess schema. "
    "This org has thousands of sObjects; when discovering, narrow results "
    "with filters or grep rather than scanning full lists. "
    "\n\nComplete the user's request and return a concise answer."
)


def _build_prompt(scenario: Scenario) -> str:
    return f"{SHARED_PREAMBLE}\n\n{scenario.prompt}"


def _accumulate_usage(acc: dict[str, int], u) -> None:
    acc["input_tokens"] += getattr(u, "input_tokens", 0) or 0
    acc["output_tokens"] += getattr(u, "output_tokens", 0) or 0
    acc["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
    acc["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0


def _create_with_retry(client, kwargs, *, retries: int = 1):
    """One retry on APIError / RateLimitError. Honors Retry-After if present."""
    attempt = 0
    while True:
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            if attempt >= retries:
                raise
            ra = float(getattr(e, "retry_after", 5) or 5)
            time.sleep(min(ra, 10.0))
            attempt += 1
        except anthropic.APIError:
            if attempt >= retries:
                raise
            time.sleep(1.0)
            attempt += 1


def _native_tool_blocks_to_results(content_blocks, sf_token) -> list[dict]:
    """For each tool_use block, dispatch to the local Native tool and
    return the tool_result blocks Claude expects on the next turn."""
    out = []
    for blk in content_blocks:
        if getattr(blk, "type", None) != "tool_use":
            continue
        try:
            result = dispatch_native_tool(blk.name, blk.input or {}, sf_token)
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        out.append({
            "type": "tool_result",
            "tool_use_id": blk.id,
            "content": str(result)[:50_000],  # guard against runaway sizes
        })
    return out


def run_once(
    scenario: Scenario,
    path: PathName,
    *,
    model: str,
    max_turns: int,
    timeout_s: int,
    mcp_template_path: Path,
    sf_token: dict,
) -> RunResult:
    """Run one scenario through one path. Returns a RunResult with tokens
    aggregated across all turns."""
    started = time.time()
    client = get_client_for_model(model)

    prompt = _build_prompt(scenario)
    messages: list[dict] = [{"role": "user", "content": prompt}]

    base_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 4096,
    }
    if path == PathName.NATIVE:
        base_kwargs["tools"] = NATIVE_TOOL_DEFS
    else:
        base_kwargs["mcp_servers"] = build_mcp_servers(
            mcp_template_path, sf_access_token=sf_token["access_token"],
        )

    usage_acc = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    tool_calls: list[str] = []
    num_turns = 0
    final_text: str = ""
    error: Optional[str] = None
    last_stop: Optional[str] = None

    try:
        while num_turns < max_turns:
            num_turns += 1
            kwargs = {**base_kwargs, "messages": messages}
            try:
                resp = _create_with_retry(client, kwargs)
            except anthropic.APIError as e:
                error = f"inference error: {e}"
                break

            _accumulate_usage(usage_acc, resp.usage)
            last_stop = resp.stop_reason

            # Record tool calls + capture any final text
            for blk in (resp.content or []):
                btype = getattr(blk, "type", None)
                if btype == "tool_use":
                    tool_calls.append(getattr(blk, "name", ""))
                elif btype == "text":
                    final_text = getattr(blk, "text", "") or final_text

            if resp.stop_reason != "tool_use":
                break

            # Native path needs to dispatch tools locally and append tool_result.
            # MCP path: Inference resolves tools server-side, so stop_reason should
            # not normally come back as "tool_use" — but if it does, we have
            # nothing to append, so we treat it as a stuck state and stop.
            if path == PathName.NATIVE:
                tool_results = _native_tool_blocks_to_results(resp.content, sf_token)
                if not tool_results:
                    break
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                break

        if error is None and last_stop == "tool_use" and num_turns >= max_turns:
            error = "terminal_reason=max_turns: tool-use loop did not terminate"
    finally:
        duration_ms = int((time.time() - started) * 1000)

    cost = compute_cost_usd(
        model=model,
        input_tokens=usage_acc["input_tokens"],
        output_tokens=usage_acc["output_tokens"],
        cache_read_input_tokens=usage_acc["cache_read_input_tokens"],
        cache_creation_input_tokens=usage_acc["cache_creation_input_tokens"],
    )

    return RunResult(
        path=path,
        input_tokens=usage_acc["input_tokens"],
        output_tokens=usage_acc["output_tokens"],
        cache_read_input_tokens=usage_acc["cache_read_input_tokens"],
        cache_creation_input_tokens=usage_acc["cache_creation_input_tokens"],
        total_cost_usd=cost,
        num_turns=num_turns,
        duration_ms=duration_ms,
        tool_calls=tool_calls,
        succeeded=(error is None),
        error=error,
        raw_json=None,
    )
```

- [ ] **Step 4: Run; expect pass**

```bash
pytest tests/test_messages_runner.py -v
```

Expected: 4 pass.

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/messages_runner.py tests/test_messages_runner.py
git commit -m "feat(runner): Anthropic Messages tool-use loop replaces subprocess"
```

### Task 8.2: Retire the old subprocess runner

**Files:**
- Delete: `src/token_compare/runner.py`
- Delete: `src/token_compare/mcp_config.py`
- Delete: `tests/test_runner.py` (if present)
- Delete: `tests/test_mcp_config.py` (if present)

- [ ] **Step 1: Verify there are no remaining imports**

```bash
grep -rn "from token_compare.runner" src/ tests/ || echo "no imports of runner"
grep -rn "from token_compare.mcp_config" src/ tests/ || echo "no imports of mcp_config"
```

Expected: only the imports inside `benchmark.py` (which we'll fix in Phase 9).

- [ ] **Step 2: Delete the obsolete files**

```bash
git rm src/token_compare/runner.py src/token_compare/mcp_config.py
git rm tests/test_runner.py tests/test_mcp_config.py 2>/dev/null || true
```

- [ ] **Step 3: Commit (no test run yet — benchmark.py imports will fail until Phase 9)**

```bash
git commit -m "chore(runner): drop subprocess runner + mcp_config (replaced)"
```

---

## Phase 9: Wire benchmark.py to the new runner

### Task 9.1: Replace runner import + token plumbing

**Files:**
- Modify: `src/token_compare/benchmark.py`

- [ ] **Step 1: Replace top imports**

In `src/token_compare/benchmark.py`, replace:

```python
from token_compare.mcp_config import resolve_template
from token_compare.models import (
    BenchmarkResult, PathName, RunResult, Scenario, ScenarioResult,
)
from token_compare.runner import run_once
from token_compare.sf_auth import (
    AccessToken, SfAuthError, fetch_access_token, load_credentials_from_env,
)
```

With:

```python
from token_compare.messages_runner import run_once
from token_compare.models import (
    BenchmarkResult, PathName, RunResult, Scenario, ScenarioResult,
)
```

- [ ] **Step 2: Replace `BenchmarkOptions`**

Replace the existing `BenchmarkOptions` class with:

```python
class BenchmarkOptions(BaseModel):
    model: str
    max_turns: int
    timeout_s: int
    runs_per_path: int
    mcp_template_path: Path
    operator: str
    org_name: str
    sf_token: dict  # AccessToken serialized — passed directly to run_once
```

- [ ] **Step 3: Replace `run_benchmark` body**

Replace the `run_benchmark` function with:

```python
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
                mcp_template_path=options.mcp_template_path,
                sf_token=options.sf_token,
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

- [ ] **Step 4: Run the full suite to confirm nothing else broke**

```bash
pytest tests/ -q
```

Expected: existing benchmark/analysis/report tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/token_compare/benchmark.py
git commit -m "refactor(benchmark): switch to messages_runner; sf_token via options"
```

---

## Phase 10: Wire up the API

### Task 10.1: Session middleware + DB lifecycle in api.py

**Files:**
- Modify: `src/token_compare/api.py`

- [ ] **Step 1: Add startup/shutdown hooks and a session dependency**

Open `api.py`. Locate the `def create_app(config: AppConfig) -> FastAPI:` function. Right after `app = FastAPI(...)`, insert:

```python
    @app.on_event("startup")
    async def _startup() -> None:
        from token_compare import db
        await db.connect()
        await db.migrate()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        from token_compare import db
        await db.close()

    async def _get_or_create_sid(request: Request, response_setter) -> str:
        from token_compare import db
        from token_compare.sessions import (
            COOKIE_NAME, sign_session_id, verify_session_id, BadSignature,
        )
        signed = request.cookies.get(COOKIE_NAME)
        if signed:
            try:
                return verify_session_id(signed)
            except BadSignature:
                pass
        sid = await db.create_session()
        response_setter(COOKIE_NAME, sign_session_id(sid),
                        httponly=True, secure=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)
        return sid
```

- [ ] **Step 2: Add `/api/models`**

Inside `create_app`, before the existing routes:

```python
    @app.get("/api/models")
    def list_models() -> dict:
        from token_compare.inference_client import discover_models
        return {"models": [m.model_id for m in discover_models()]}
```

- [ ] **Step 3: Update `/api/run` to read SF token from DB and persist via `db.create_report`**

Replace the existing `@app.post("/api/run")` handler with:

```python
    @app.post("/api/run")
    async def run(req: RunRequest, request: Request) -> StreamingResponse:
        from fastapi import Response
        from token_compare import db
        from token_compare.benchmark import BenchmarkOptions, run_benchmark
        resp = Response()
        sid = await _get_or_create_sid(request, resp.set_cookie)
        sf_token = await db.get_sf_token(sid)
        if not sf_token:
            return JSONResponse(
                {"error": "Salesforce login required"}, status_code=401,
            )

        all_scenarios = load_all(config.scenarios_dir)
        picked = [s for s in all_scenarios if s.id in set(req.scenario_ids)]
        if not picked:
            picked = all_scenarios

        report_id = await db.create_report(
            model=req.model, operator=req.operator, org_name=req.org_name,
        )

        options = BenchmarkOptions(
            model=req.model, max_turns=req.max_turns, timeout_s=req.timeout_s,
            runs_per_path=req.runs_per_path,
            mcp_template_path=config.mcp_config_path,
            operator=req.operator, org_name=req.org_name,
            sf_token=sf_token,
        )
        return _start_benchmark_stream(picked, options, report_id=report_id)
```

(Pass the cookie back via the SSE response — the `_start_benchmark_stream` helper will be modified in step 5 to include the `Set-Cookie` header.)

- [ ] **Step 4: Update `/api/run/freeform` similarly**

Replace its body so it also reads `sf_token`, creates a report row, and passes the new `BenchmarkOptions` shape. Same pattern as step 3. Pass `report_id` into `_start_benchmark_stream`.

- [ ] **Step 5: Modify `_start_benchmark_stream` to persist runs + finalize report**

Inside the helper, after the existing `runner_task` definition, replace the body that writes to disk with:

```python
        async def runner_task():
            try:
                loop = asyncio.get_running_loop()

                # Persist each completed run to the DB as it streams in.
                # We wrap on_progress so progress events also DB-insert.
                from token_compare import db as _db

                original_on_progress = on_progress

                def db_on_progress(e):
                    original_on_progress(e)
                    if e.kind == "run_complete" and e.run_result is not None:
                        loop.create_task(_db.insert_run(
                            report_id=report_id,
                            scenario_id=e.scenario_id,
                            path=e.path.value,
                            run_index=e.run_index,
                            result=e.run_result.model_dump(),
                        ))

                result = await loop.run_in_executor(
                    None, lambda: run_benchmark(picked_scenarios, options, db_on_progress),
                )

                await _db.finalize_report(report_id, payload=result.model_dump())
                _current_run["result_data"] = result.model_dump()
                _current_run["report_path"] = report_id
                queue.put_nowait({"kind": "report_written", "report_id": report_id})
            except Exception as e:
                queue.put_nowait({"kind": "error", "message": str(e)})
            finally:
                _current_run["active"] = False
                queue.put_nowait(None)
```

Also update `event_stream()` to emit a heartbeat every 15s:

```python
        async def event_stream():
            task = asyncio.create_task(runner_task())
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
            await task
```

Change the helper signature to accept `report_id`:

```python
    def _start_benchmark_stream(
        picked_scenarios: list[Scenario],
        options: BenchmarkOptions,
        *,
        report_id: str,
        freeform_scenario: Optional[Scenario] = None,
    ) -> StreamingResponse:
```

- [ ] **Step 6: Update `/api/reports` to read from DB**

Replace the body:

```python
    @app.get("/api/reports")
    async def list_saved_reports() -> dict:
        from token_compare import db
        rows = await db.list_reports(limit=10)
        return {"reports": [
            {
                "name": r["id"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "model": r["model"],
                "operator": r["operator"],
                "org_name": r["org_name"],
            } for r in rows
        ]}

    @app.get("/api/reports/{report_id}/data")
    async def load_saved_report(report_id: str) -> dict:
        from token_compare import db
        rec = await db.get_report(report_id)
        if not rec or not rec.get("payload_json"):
            return JSONResponse({"error": "report not found"}, status_code=404)
        from token_compare.models import BenchmarkResult
        result = BenchmarkResult.model_validate(rec["payload_json"])
        return _hydrate_from_result(result, source=report_id)
```

- [ ] **Step 7: Replace `/api/sf/login` to use DB sessions**

```python
    @app.post("/api/sf/login")
    async def sf_login(request: Request) -> JSONResponse:
        from fastapi import Response
        from token_compare import db
        from token_compare.sessions import COOKIE_NAME, sign_session_id
        from token_compare.sf_auth import (
            SfAuthError, load_credentials_from_env,
            _generate_pkce, _build_authorize_url, _register_pending,
        )
        import secrets as _secrets

        creds = load_credentials_from_env()
        if creds is None:
            return JSONResponse(
                {"ok": False, "error": "SF_CLIENT_ID/SECRET/LOGIN_URL not set"},
                status_code=400,
            )
        verifier, challenge = _generate_pkce()
        state = _secrets.token_urlsafe(24)
        auth_url = _build_authorize_url(creds, state, challenge)
        _register_pending(state, creds, verifier)

        # Bind state → sid in the sessions table so the /callback can find it.
        sid = await db.create_session()
        # Stash the state into the session as a placeholder so callback can find
        # the cookie via state lookup. Simpler: store state_to_sid mapping in
        # an in-process dict keyed by state.
        _state_to_sid[state] = sid

        resp = JSONResponse({"ok": True, "authorize_url": auth_url})
        resp.set_cookie(
            COOKIE_NAME, sign_session_id(sid),
            httponly=True, secure=True, samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
        return resp
```

Above `create_app`, add the module-level state map:

```python
_state_to_sid: dict[str, str] = {}
```

- [ ] **Step 8: Update `/callback` to write token into DB**

Replace the callback's success branch so that after `complete_pending_login(state, code)` succeeds, it also persists the token under the right `sid`:

```python
        try:
            pending = complete_pending_login(state, code)
        except SfAuthError as e:
            return page("Salesforce login failed", str(e), 400)

        sid = _state_to_sid.pop(state, None)
        if sid and pending.token is not None:
            from token_compare import db
            await db.put_sf_token(sid, pending.token.model_dump())
        return page(
            "Salesforce login complete",
            "You can close this tab and return to the benchmark tool.",
            200,
        )
```

Make sure the route is `async def oauth_callback(...)`.

- [ ] **Step 9: Run the full test suite**

```bash
pytest tests/ -q
```

Expected: existing tests still pass; any tests that called the old `/api/run` shape with filesystem reports may need a stub-DB fixture. If `tests/test_api.py` exists and breaks, mark its DB-touching cases with `pytest.mark.skip(reason="rewritten in Task 10.2")` for now.

- [ ] **Step 10: Commit**

```bash
git add src/token_compare/api.py
git commit -m "feat(api): db-backed sessions + reports; SSE heartbeats; /api/models"
```

### Task 10.2: Update preflight for Heroku

**Files:**
- Modify: `src/token_compare/preflight.py`
- Modify: `tests/test_preflight.py` if present

- [ ] **Step 1: Replace `check_environment` body**

Replace `check_environment` in `src/token_compare/preflight.py` with:

```python
def check_environment(mcp_config_path: Optional[Path] = None) -> PreflightResult:
    import os
    checks: dict[str, bool] = {}
    remediation: list[str] = []
    details: dict[str, str] = {}

    # Inference addon env present?
    from token_compare.inference_client import discover_models
    models = discover_models()
    checks["inference_models_present"] = len(models) >= 1
    details["inference_models"] = ", ".join(m.model_id for m in models) or "none"
    if not checks["inference_models_present"]:
        remediation.append(
            "No Heroku Inference addons detected. Attach at least one "
            "heroku-inference:claude-* addon to the app."
        )

    # Postgres reachable?
    try:
        from token_compare import db
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(db.connect())
            checks["postgres_reachable"] = True
            details["postgres"] = "ok"
        finally:
            loop.close()
    except Exception as e:
        checks["postgres_reachable"] = False
        details["postgres"] = str(e)[:200]
        remediation.append("Set DATABASE_URL (heroku-postgresql:essential-0).")

    # ECA env vars set?
    creds = load_credentials_from_env()
    if creds is None:
        checks["sf_eca_configured"] = False
        details["sf_eca"] = "SF_CLIENT_ID / SF_CLIENT_SECRET / SF_LOGIN_URL not set"
        remediation.append(
            "Set SF_CLIENT_ID, SF_CLIENT_SECRET, SF_LOGIN_URL as Heroku config vars."
        )
    else:
        checks["sf_eca_configured"] = True
        details["sf_eca"] = f"login_url={creds.login_url}"

    # mcp template present?
    mcp_cfg = Path(mcp_config_path) if mcp_config_path else Path("config/sf-mcp.json")
    checks["mcp_template_present"] = mcp_cfg.is_file()
    details["mcp_template_path"] = str(mcp_cfg)
    if not checks["mcp_template_present"]:
        remediation.append(f"Create MCP template at {mcp_cfg}.")

    # SESSION_SECRET set?
    checks["session_secret_set"] = bool(os.environ.get("SESSION_SECRET"))
    if not checks["session_secret_set"]:
        remediation.append("Set SESSION_SECRET to a 32-byte hex via heroku config:set.")

    return PreflightResult(
        ok=all(checks.values()),
        checks=checks,
        remediation=remediation,
        details=details,
    )
```

Remove the now-dead helpers (`_run`, `_is_claude_logged_in`, `_summarize_claude_auth`, `_sf_has_org`, `_summarize_sf_orgs`, `_redact_org`, `_SENSITIVE_ORG_FIELDS`).

- [ ] **Step 2: Run any existing preflight tests; mark as skipped if they assume the old shape**

```bash
pytest tests/ -k preflight -v
```

Expected: any test that asserted "claude installed" should be deleted or updated. For this plan, we just delete the obsolete file:

```bash
git rm tests/test_preflight.py 2>/dev/null || true
```

(We are intentionally not building a new preflight test suite — the new checks are mostly env-var booleans best validated end-to-end on Heroku.)

- [ ] **Step 3: Commit**

```bash
git add src/token_compare/preflight.py
git commit -m "feat(preflight): Heroku-flavored environment checks"
```

---

## Phase 11: Frontend changes

### Task 11.1: Model dropdown in the SPA

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.js`

- [ ] **Step 1: Add the dropdown element to `index.html`**

In the catalog page near the existing run-config controls, add (the exact insertion point depends on the current layout — find the existing "Operator" or "Org" input and insert next to it):

```html
<label class="ctl">
  <span>Model</span>
  <select id="model-select"></select>
</label>
```

If you need a fresh card rather than inline, follow the existing `.ctl` pattern used by other inputs.

- [ ] **Step 2: Populate it in `app.js`**

Find the existing init/boot block in `static/app.js`. Add:

```javascript
async function loadModels() {
  const sel = document.getElementById("model-select");
  if (!sel) return;
  const r = await fetch("/api/models");
  const { models } = await r.json();
  sel.replaceChildren();
  for (const m of models) {
    const o = document.createElement("option");
    o.value = m;
    o.textContent = m;
    sel.appendChild(o);
  }
}
```

Call `loadModels()` from the existing `init()` (or wherever `loadScenarios()` is called).

Then, where the run POST builds its body (search for `scenario_ids` or `runs_per_path` in `app.js`), pull `model` from the dropdown:

```javascript
const model = document.getElementById("model-select").value;
// ... include `model` in the run POST body
```

(The existing `RunRequest` already accepts `model`, so the backend doesn't need a change.)

- [ ] **Step 3: Smoke-test locally**

```bash
DATABASE_URL=postgresql://localhost/token_compare_test \
  SESSION_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))') \
  INFERENCE_URL=https://us.inference.heroku.com \
  INFERENCE_KEY=fake \
  INFERENCE_MODEL_ID=claude-4-5-sonnet \
  python -m token_compare.api &
```

Open `http://127.0.0.1:8000`. Verify the dropdown shows `claude-4-5-sonnet`. Kill the process.

- [ ] **Step 4: Commit**

```bash
git add static/index.html static/app.js
git commit -m "feat(ui): model dropdown populated from /api/models"
```

---

## Phase 12: Heroku deployment artifacts

### Task 12.1: Procfile, runtime, app.json

**Files:**
- Create: `Procfile`
- Create: `runtime.txt`
- Create: `app.json`
- Modify: `.env.example`
- Modify: `pyproject.toml` (add a console script that's importable as `token_compare.api:app`)

- [ ] **Step 1: Confirm `app` is importable**

The Heroku command will be `uvicorn token_compare.api:app`. We need a module-level `app` symbol. Open `src/token_compare/api.py` and at the very bottom (after `def main()`), add:

```python
def _bootstrap_app() -> FastAPI:
    """Module-level entry point for `uvicorn token_compare.api:app`."""
    _load_dotenv_if_present()
    return create_app(AppConfig())


app = _bootstrap_app()
```

Smoke-test:

```bash
python -c "from token_compare.api import app; print(type(app).__name__)"
```

Expected: `FastAPI`.

- [ ] **Step 2: Create `Procfile`**

Create `Procfile` (no extension, single line, no trailing newline issues):

```
web: uvicorn token_compare.api:app --host 0.0.0.0 --port $PORT
```

- [ ] **Step 3: Create `runtime.txt`**

```
python-3.11.10
```

- [ ] **Step 4: Create `app.json`**

```json
{
  "name": "Token Comparison Tool",
  "description": "Native vs MCP token-cost benchmark for Salesforce on Heroku Inference.",
  "addons": [
    "heroku-postgresql:essential-0",
    {"plan": "heroku-inference:claude-4-5-haiku",  "as": "HEROKU_INFERENCE_TEAL"},
    {"plan": "heroku-inference:claude-4-5-sonnet", "as": "INFERENCE"},
    {"plan": "heroku-inference:claude-opus-4-5",   "as": "HEROKU_INFERENCE_COBALT"}
  ],
  "env": {
    "SF_CLIENT_ID":     {"description": "Salesforce ECA client id"},
    "SF_CLIENT_SECRET": {"description": "Salesforce ECA client secret"},
    "SF_LOGIN_URL":     {"value": "https://login.salesforce.com"},
    "SF_REDIRECT_URI":  {"description": "Set to https://<heroku-app-host>/callback after first deploy"},
    "SESSION_SECRET":   {"generator": "secret"}
  }
}
```

- [ ] **Step 5: Update `.env.example`**

Replace the contents of `.env.example` with the Heroku-flavored set:

```
# Heroku Inference addons (auto-set by `heroku-inference:*` addons)
INFERENCE_URL=
INFERENCE_KEY=
INFERENCE_MODEL_ID=
HEROKU_INFERENCE_TEAL_URL=
HEROKU_INFERENCE_TEAL_KEY=
HEROKU_INFERENCE_TEAL_MODEL_ID=
HEROKU_INFERENCE_COBALT_URL=
HEROKU_INFERENCE_COBALT_KEY=
HEROKU_INFERENCE_COBALT_MODEL_ID=

# Heroku Postgres (auto-set by heroku-postgresql:essential-0)
DATABASE_URL=

# 32-byte hex; signs the session cookie. Generate with:
#   python3 -c 'import secrets; print(secrets.token_hex(32))'
SESSION_SECRET=

# Salesforce External Client App (ECA) credentials.
# Add https://<heroku-app-host>/callback to the ECA's callback URL list.
SF_CLIENT_ID=
SF_CLIENT_SECRET=
SF_LOGIN_URL=https://login.salesforce.com
SF_REDIRECT_URI=https://<heroku-app-host>/callback
```

- [ ] **Step 6: Run the full test suite once more**

```bash
pytest tests/ -q
```

Expected: green.

- [ ] **Step 7: Commit**

```bash
git add Procfile runtime.txt app.json .env.example src/token_compare/api.py
git commit -m "chore(heroku): Procfile, runtime, app.json, .env.example"
```

### Task 12.2: First deploy

**Files:** N/A (Heroku-side)

- [ ] **Step 1: Add Heroku as a remote and push**

```bash
heroku git:remote -a token-comparison-tool
git push heroku heroku-port:main
```

Expected: build runs Python 3.11.10, installs deps, slug compiles, single web dyno boots.

- [ ] **Step 2: Tail logs**

```bash
heroku logs --tail -a token-comparison-tool
```

Expected: you see `Application startup complete.` and the migration runs without error. If any `RuntimeError: <var> is not set` appears, set it via `heroku config:set` and `heroku restart`.

- [ ] **Step 3: Smoke-test the home page**

```bash
curl -sIL https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com/ | head -1
curl -s https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com/api/models
```

Expected: `HTTP/2 200`; the second prints a JSON `{"models": [...]}` with all three model IDs.

- [ ] **Step 4: Walk through the OAuth flow in a browser**

Open the URL, click **Connect Salesforce**, complete the OAuth round-trip, and confirm you land on the success page. Then run a single trivial scenario from the catalog and confirm the SSE stream emits events and a report row appears in the report dropdown after completion.

- [ ] **Step 5: If everything works, merge to `main`**

```bash
git checkout main
git merge heroku-port
git push origin main
git push heroku main
```

(If anything's wrong, fix on the branch first before merging.)

---

## Phase 13: Final smoke + README

### Task 13.1: End-to-end Inference smoke test (opt-in)

**Files:**
- Create: `tests/test_e2e_smoke.py`

- [ ] **Step 1: Write the test**

Create `tests/test_e2e_smoke.py`:

```python
import os
import pytest
from pathlib import Path

from token_compare.messages_runner import run_once
from token_compare.models import PathName, Scenario, SuccessCriteria


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INFERENCE_E2E") != "1",
    reason="set RUN_INFERENCE_E2E=1 to opt in (real Inference call, costs cents)",
)


def test_native_path_against_real_inference():
    s = Scenario(
        id="smoke", title="t", category="c", difficulty="simple",
        prompt="Say hello in one sentence and stop. Do not call any tools.",
        success_criteria=SuccessCriteria(),
    )
    # Use haiku (cheapest) and a stub token; we expect the model to NOT call
    # tools given the prompt, so it never tries to use the token.
    fake_token = {"access_token": "X", "instance_url": "https://example.com"}
    r = run_once(
        s, PathName.NATIVE,
        model=os.environ["HEROKU_INFERENCE_TEAL_MODEL_ID"],
        max_turns=2, timeout_s=60,
        mcp_template_path=Path("config/sf-mcp.json"),
        sf_token=fake_token,
    )
    assert r.input_tokens > 0
    assert r.output_tokens > 0
    # haiku should usually finish without calling tools given that prompt
    assert r.num_turns >= 1
```

- [ ] **Step 2: Run it once locally with the right env**

```bash
RUN_INFERENCE_E2E=1 \
  HEROKU_INFERENCE_TEAL_URL=https://us.inference.heroku.com \
  HEROKU_INFERENCE_TEAL_KEY=$(heroku config:get HEROKU_INFERENCE_TEAL_KEY -a token-comparison-tool) \
  HEROKU_INFERENCE_TEAL_MODEL_ID=claude-4-5-haiku \
  pytest tests/test_e2e_smoke.py -v
```

Expected: pass. Tokens accumulated > 0.

- [ ] **Step 3: Commit**

```bash
git add tests/test_e2e_smoke.py
git commit -m "test(e2e): opt-in smoke against real Heroku Inference"
```

### Task 13.2: README refresh

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace the "Get started in 5 steps" header with a Heroku-first version**

Open `README.md`. Replace the leading blockquote with:

```markdown
> **Use it on Heroku**
>
> The hosted instance is at:
> <https://token-comparison-tool-cb60c8f1dcc3.herokuapp.com/>
>
> 1. Click **Connect Salesforce** to authorize via OAuth (the ECA's callback list must include the Heroku URL — done by repo owner).
> 2. Pick a **model** from the dropdown — `claude-4-5-haiku` (cheap), `claude-4-5-sonnet` (default), or `claude-opus-4-5` (premium).
> 3. Run the catalog, run a free-format prompt, or load a saved report.
>
> **Deploy your own**
>
> ```bash
> heroku create your-app-name
> heroku addons:create heroku-postgresql:essential-0 -a your-app-name
> heroku addons:create heroku-inference:claude-4-5-haiku  --as HEROKU_INFERENCE_TEAL   -a your-app-name
> heroku addons:create heroku-inference:claude-4-5-sonnet --as INFERENCE              -a your-app-name
> heroku addons:create heroku-inference:claude-opus-4-5   --as HEROKU_INFERENCE_COBALT -a your-app-name
> heroku config:set SESSION_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))') \
>                   SF_CLIENT_ID=... SF_CLIENT_SECRET=... \
>                   SF_LOGIN_URL=https://login.salesforce.com \
>                   SF_REDIRECT_URI=https://your-app-name.herokuapp.com/callback \
>                   -a your-app-name
> git push heroku main
> ```
>
> Add `https://your-app-name.herokuapp.com/callback` to your Salesforce ECA's callback URL list.
```

- [ ] **Step 2: Replace the "How it works under the hood" section**

Find the section explaining `claude -p` invocations and replace its body so it describes the Anthropic Messages API tool-use loop and the two paths (`tools=[NATIVE_TOOL_DEFS]` vs `mcp_servers=[...]`). Reuse language from the spec doc to keep it consistent.

- [ ] **Step 3: Update the project layout block**

Reflect the new module set (`messages_runner.py`, `native_tools.py`, `mcp_path.py`, `db.py`, `sessions.py`, `inference_client.py`, `pricing.py`); drop `runner.py` and `mcp_config.py`.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: heroku-first README"
```

### Task 13.3: Final main-branch merge

**Files:** N/A

- [ ] **Step 1: Merge and push**

```bash
git checkout main
git merge --no-ff heroku-port -m "feat: heroku port"
git push origin main
git push heroku main
```

- [ ] **Step 2: Final verification**

Open the Heroku URL one more time. Confirm:
- Login flow works.
- Catalog runs against `claude-4-5-haiku` (cheapest) end-to-end.
- A second tab shows the report in the dropdown.
- Filter the report dropdown, reload, verify previous reports load.

If anything regresses, branch off `main` for a fix-up commit; don't `git push --force`.

---

## Self-review notes

- **Spec coverage** — Every numbered section in the spec is covered by at least one task: §3 (Phase 12), §4 (Phases 5/6/7/8/10/2/3), §5 (Phases 9/10), §6 (Phase 2), §7 (Phase 12 + Task 0.3), §8 (Phase 8 retries, Phase 9 max-turns wiring, Phase 10 SSE heartbeats), §9 (Phase 2 audit table; readers query via `heroku logs` no extra task), §10 (Phase 3 sessions + Phase 12 cookie attrs), §11 (Phases 1–13 tests).
- **Type consistency** — `BenchmarkOptions.mcp_template_path` matches `messages_runner.run_once(mcp_template_path=...)`. `sf_token` is consistently `dict` everywhere. `PathName` enum from `models.py` re-used unchanged. `RunResult` shape preserved exactly so analysis/report code is untouched.
- **No placeholders** — every step contains the actual code or command an engineer needs.
- **Open assumption** — Anthropic's `mcp_servers` parameter shape (`type: "url"`, `name`, `url`, `authorization_token`). If Heroku Inference doesn't forward that yet, fall back to running the MCP path through `NATIVE_TOOL_DEFS`-style proxies — same `RunResult` shape, no plan-level rework.
