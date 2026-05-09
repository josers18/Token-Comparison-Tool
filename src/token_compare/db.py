from __future__ import annotations

import asyncio
import json
import os
import secrets
from typing import Any, Optional

import asyncpg


_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()


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
  model        TEXT,
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

-- Pending-login rows for in-flight OAuth round-trips. Persisted
-- (instead of in-process dict) so a dyno restart between
-- /api/sf/login and /callback doesn't drop the PKCE verifier.
CREATE TABLE IF NOT EXISTS pending_logins (
  state       TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL,
  verifier    TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pending_logins_created_idx
  ON pending_logins (created_at);

-- Scenarios in DB so the admin UI can edit them. Seeded from
-- scenarios/*.yaml on first dyno startup; after that the DB is the
-- source of truth. Soft-delete via is_active=false so historical
-- reports referencing a removed scenario can still resolve title etc.
CREATE TABLE IF NOT EXISTS scenarios (
  id                    TEXT PRIMARY KEY,
  title                 TEXT NOT NULL,
  category              TEXT NOT NULL,
  difficulty            TEXT NOT NULL,
  prompt                TEXT NOT NULL,
  expected_operations   JSONB NOT NULL DEFAULT '[]'::jsonb,
  success_criteria_json JSONB NOT NULL DEFAULT '{"must_contain": []}'::jsonb,
  notes                 TEXT NOT NULL DEFAULT '',
  is_active             BOOLEAN NOT NULL DEFAULT TRUE,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS scenarios_is_active_idx ON scenarios (is_active);

-- Tier B: ensure runs.model exists and is NOT NULL on legacy DBs.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS model TEXT;
UPDATE runs r SET model = (SELECT model FROM reports WHERE id = r.report_id)
  WHERE r.model IS NULL;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM runs WHERE model IS NULL) THEN
    ALTER TABLE runs ALTER COLUMN model SET NOT NULL;
  END IF;
END $$;
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
    # Serialize first-time pool creation so concurrent first-callers don't
    # each spin up their own pool and leak the loser. Re-check inside the
    # lock — the first holder may have already created it while we waited.
    async with _pool_lock:
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
    """Store the SF OAuth token JSON under an existing session id.

    Silently no-ops if the session row doesn't exist — the API layer is
    expected to call create_session() first. A vanished session manifests
    downstream as get_sf_token() returning None, which the API surfaces to
    the user as "Salesforce login required".
    """
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


async def list_reports(limit: int = 10, *, finalized_only: bool = False) -> list[dict]:
    """Most-recent reports, newest first. By default includes in-progress
    runs (payload_json IS NULL); pass finalized_only=True for the SPA's
    'load latest finished report' use case."""
    pool = await connect()
    where = "WHERE payload_json IS NOT NULL " if finalized_only else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, started_at, finished_at, model, operator, org_name "
            f"FROM reports {where}ORDER BY started_at DESC LIMIT $1",
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


async def list_finalized_reports_for_history(
    scenario_id: str, model: str
) -> list[dict]:
    """Return finalized reports newest-first with payload_json hydrated, for
    the history walker. The walker filters by scenario_id/model itself —
    we just deliver up to 200 finalized rows (recent are most relevant).
    If history performance becomes a concern, materialize a view of
    (report_id, scenario_id, model, ...) and query that instead."""
    pool = await connect()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, started_at, payload_json FROM reports "
            "WHERE payload_json IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 200"
        )
    out = []
    for r in rows:
        payload = r["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        out.append({"id": r["id"], "started_at": r["started_at"],
                    "payload_json": payload})
    return out


# ---- Runs ----

async def insert_run(
    *, report_id: str, scenario_id: str, path: str,
    run_index: int, model: str, result: dict,
) -> str:
    rid = "run_" + secrets.token_hex(8)
    pool = await connect()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO runs (id, report_id, scenario_id, path, run_index, model, result_json) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            rid, report_id, scenario_id, path, run_index, model, json.dumps(result),
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


# ---- Pending logins ----

async def put_pending_login(*, state: str, session_id: str, verifier: str) -> None:
    pool = await connect()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pending_logins (state, session_id, verifier) VALUES ($1,$2,$3) "
            "ON CONFLICT (state) DO UPDATE SET session_id=EXCLUDED.session_id, "
            "verifier=EXCLUDED.verifier, created_at=now()",
            state, session_id, verifier,
        )


async def pop_pending_login(state: str) -> Optional[dict]:
    """Atomically look up + delete a pending login row by `state`.
    Returns {session_id, verifier} or None if not found / expired."""
    pool = await connect()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM pending_logins WHERE state=$1 "
            "AND created_at > now() - INTERVAL '15 minutes' "
            "RETURNING session_id, verifier",
            state,
        )
    return dict(row) if row else None


async def prune_pending_logins() -> int:
    """Drop pending_login rows older than 1 hour. Cheap to call from
    startup or a heartbeat. Returns the row count deleted."""
    pool = await connect()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM pending_logins WHERE created_at < now() - INTERVAL '1 hour'"
        )
    # asyncpg execute returns 'DELETE N' as a string
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


# ---- Scenarios ----

def _scenario_row_to_dict(row) -> dict:
    """Normalize a scenarios row so JSONB columns come back as Python
    dicts/lists no matter which asyncpg codec path was used."""
    d = dict(row)
    for k in ("expected_operations", "success_criteria_json"):
        v = d.get(k)
        if isinstance(v, str):
            d[k] = json.loads(v)
    return d


async def list_scenarios(*, include_inactive: bool = False) -> list[dict]:
    pool = await connect()
    where = "" if include_inactive else "WHERE is_active = TRUE "
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, category, difficulty, prompt, expected_operations, "
            "success_criteria_json, notes, is_active, created_at, updated_at "
            f"FROM scenarios {where}ORDER BY id"
        )
    return [_scenario_row_to_dict(r) for r in rows]


async def get_scenario(scenario_id: str) -> Optional[dict]:
    pool = await connect()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, title, category, difficulty, prompt, expected_operations, "
            "success_criteria_json, notes, is_active, created_at, updated_at "
            "FROM scenarios WHERE id = $1",
            scenario_id,
        )
    return _scenario_row_to_dict(row) if row else None


async def upsert_scenario(
    *,
    id: str,
    title: str,
    category: str,
    difficulty: str,
    prompt: str,
    expected_operations: list[str] | None = None,
    success_criteria: dict | None = None,
    notes: str = "",
    is_active: bool = True,
) -> None:
    """Insert a new scenario or update an existing one (admin endpoints
    use this for both create and edit). updated_at refreshes on every
    write so the admin UI can show 'last edited' timestamps."""
    pool = await connect()
    expected_operations = expected_operations or []
    success_criteria = success_criteria or {"must_contain": []}
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO scenarios "
            "(id, title, category, difficulty, prompt, expected_operations, "
            " success_criteria_json, notes, is_active) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  title=EXCLUDED.title, category=EXCLUDED.category, "
            "  difficulty=EXCLUDED.difficulty, prompt=EXCLUDED.prompt, "
            "  expected_operations=EXCLUDED.expected_operations, "
            "  success_criteria_json=EXCLUDED.success_criteria_json, "
            "  notes=EXCLUDED.notes, is_active=EXCLUDED.is_active, "
            "  updated_at=now()",
            id, title, category, difficulty, prompt,
            json.dumps(expected_operations),
            json.dumps(success_criteria),
            notes, is_active,
        )


async def set_scenario_active(scenario_id: str, *, is_active: bool) -> bool:
    """Soft-delete (is_active=False) or restore. Returns True if a row
    was actually modified, False if the id didn't exist."""
    pool = await connect()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE scenarios SET is_active=$2, updated_at=now() WHERE id=$1",
            scenario_id, is_active,
        )
    try:
        return int(result.split()[-1]) > 0
    except (ValueError, IndexError):
        return False


async def count_scenarios() -> int:
    pool = await connect()
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM scenarios")
    return int(n or 0)
