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
