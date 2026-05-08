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


def test_runs_table_has_model_column():
    """Test that the SCHEMA constant declares a model column on runs."""
    from token_compare.db import SCHEMA
    # Tolerant against whitespace formatting:
    assert " model " in SCHEMA  # the column must appear somewhere
    # And the migration block must mention adding the column to runs.
    assert "ALTER TABLE runs" in SCHEMA and "ADD COLUMN" in SCHEMA
